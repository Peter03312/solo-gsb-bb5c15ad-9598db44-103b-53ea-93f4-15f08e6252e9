"""机构公开脱敏记录发布 HTTP API。

设计要点
========
* 维护方请求带 ``X-Service-Key`` 头，与配置的服务密钥做恒定时间比较。
* 修订与发布均要求携带基准版本号（乐观并发控制），
  在单个事务内“锁行 + 校验版本 + 写入”，过期则整体拒绝（不产生部分写入）。
* 发布时在同一事务内把当前原文与敏感区间固化为不可变快照；
  原文后续修订只追加新版本，旧快照与其脱敏正文永不改变。
* 读取令牌仅以 SHA-256 哈希落库；读取在事务内判定撤销/到期状态。
  撤销“完成后开始的读取”以及到期后的读取都不会返回正文。
* 所有端点为 async，数据库走 psycopg3 异步连接池，请求可真正并发；
  冲突交由行锁与版本比较在数据库内裁决。
* 公开端点与错误响应均不回显原文或区间内容。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

from .redaction import InvalidRange, merge_ranges, normalize_ranges, redact


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


# ---- 配置（启动期读取，缺失立即失败）---------------------------------------
SERVICE_KEY = _env("SERVICE_KEY")
DATABASE_URL = _env(
    "DATABASE_URL",
    default="postgresql://redact:redact@db:5432/redaction",
)
MAX_CONTENT_CODEPOINTS = int(os.environ.get("MAX_CONTENT_CODEPOINTS", "1000000"))
MAX_RANGES = int(os.environ.get("MAX_RANGES", "10000"))
# 令牌自创建起允许的最大有效期（秒），防止维护方设置无限长 TTL
MAX_TOKEN_TTL_SECONDS = int(os.environ.get("MAX_TOKEN_TTL_SECONDS", "31536000"))

pool: Optional[AsyncConnectionPool] = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS records (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    current_version integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS record_versions (
    record_id  uuid NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    version    integer NOT NULL,
    content    text NOT NULL,
    ranges     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, version)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id            uuid NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    version              integer NOT NULL,
    redacted_content     text NOT NULL,
    merged_ranges        jsonb NOT NULL,
    status               text NOT NULL DEFAULT 'published'
                               CHECK (status = 'published'),
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (record_id, version)
);

CREATE TABLE IF NOT EXISTS tokens (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id      uuid NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    token_hash       bytea NOT NULL UNIQUE,
    status           text NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'revoked')),
    expires_at       timestamptz,          -- NULL 表示不过期
    created_at       timestamptz NOT NULL DEFAULT now(),
    revoked_at       timestamptz
);
"""


class ApiError(Exception):
    """业务错误：携带 HTTP 状态码、机器可读 code 与可选不敏感附加上下文。"""

    def __init__(self, status_code: int, code: str, detail: str = "", **extra: Any):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- 输入解析与校验（手工处理，保证错误信息不回显请求体）--------------------
async def _json_body(request: Request, max_bytes: int = 20 * 1024 * 1024) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError(415, "unsupported_media_type", "expected application/json")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise ApiError(413, "payload_too_large", "request body too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # 不回显任何提交内容
        raise ApiError(400, "invalid_json", "request body must be valid JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "request body must be a JSON object")
    return data


def _require_service_key(request: Request) -> None:
    supplied = request.headers.get("x-service-key")
    if supplied is None or not hmac.compare_digest(supplied, SERVICE_KEY):
        raise ApiError(401, "unauthorized", "missing or invalid service key")


def _field(data: dict, name: str, expected_type: type) -> Any:
    value = data.get(name)
    # bool 是 int 子类：期望非 bool 类型时显式拒绝布尔值
    type_ok = isinstance(value, expected_type) and (
        expected_type is bool or not isinstance(value, bool)
    )
    if not type_ok:
        raise ApiError(400, "invalid_request", f"field '{name}' is missing or has the wrong type")
    return value


def _optional_int(
    data: dict,
    name: str,
    *,
    minimum: int,
    maximum: Optional[int] = None,
) -> Optional[int]:
    if name not in data or data[name] is None:
        return None
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_request", f"field '{name}' must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ApiError(400, "invalid_request", f"field '{name}' is out of range")
    return value


def _parse_content_and_ranges(data: dict) -> tuple[str, list]:
    content = _field(data, "content", str)
    if len(content) > MAX_CONTENT_CODEPOINTS:
        raise ApiError(413, "payload_too_large", "content too large")
    if "ranges" not in data or not isinstance(data["ranges"], list):
        raise ApiError(400, "invalid_request", "field 'ranges' is missing or has the wrong type")
    if len(data["ranges"]) > MAX_RANGES:
        raise ApiError(413, "payload_too_large", "too many ranges")
    try:
        normalized = normalize_ranges(data["ranges"], len(content))
    except InvalidRange:
        # 不告诉调用方是哪一个区间或具体越界值，公开错误保持笼统
        raise ApiError(422, "invalid_range", "one or more ranges are out of bounds")
    return content, normalized


def _record_id(data: dict) -> str:
    value = _field(data, "record_id", str)
    if len(value) != 36:
        raise ApiError(404, "record_not_found", "record not found")
    return value


def _base_version(data: dict) -> int:
    value = _optional_int(data, "base_version", minimum=0)
    if value is None:
        raise ApiError(400, "invalid_request", "field 'base_version' is required")
    return value


async def _lock_record(cur, record_id: str) -> None:
    """锁定记录行；非法 UUID / 不存在统一为 404（不泄露存在性差异）。"""
    try:
        await cur.execute("SELECT id FROM records WHERE id = %s FOR UPDATE", (record_id,))
    except psycopg.errors.InvalidTextRepresentation:
        raise ApiError(404, "record_not_found", "record not found")
    if await cur.fetchone() is None:
        raise ApiError(404, "record_not_found", "record not found")


# ---- 应用生命周期 -----------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    # compose 下数据库可能还在启动，做有限次重试
    last_error: Optional[Exception] = None
    for _ in range(30):
        try:
            pool = AsyncConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={"autocommit": False},
                open=False,
            )
            await pool.open()
            break
        except Exception as exc:  # noqa: BLE001 - 启动期重试
            last_error = exc
            await asyncio.sleep(1)
    else:  # pragma: no cover - 仅在数据库不可用时触发
        raise RuntimeError(f"cannot connect to database: {last_error}")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
        await conn.commit()

    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="公开脱敏记录 API",
    version="1.0.0",
    description="维护方提交原文与敏感区间，发布不可变脱敏快照并签发读取令牌。",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"error": exc.code}
    if exc.detail:
        body["message"] = exc.detail
    body.update(exc.extra)
    if exc.status_code == 401:
        return JSONResponse(
            body, status_code=401, headers={"WWW-Authenticate": "X-Service-Key"}
        )
    return JSONResponse(body, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 兜底：绝不把堆栈/SQL 错误细节（可能含原文）回给调用方
    return JSONResponse(
        {"error": "internal_error", "message": "internal server error"},
        status_code=500,
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """就绪探针：连接池可用且能查询数据库才返回 200。"""
    assert pool is not None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
    except Exception:  # noqa: BLE001 - 探针只报告状态，不回传细节
        return JSONResponse({"status": "degraded"}, status_code=503)
    return JSONResponse({"status": "ok"})


# ---- 维护方：创建 / 修订 ----------------------------------------------------
@app.post("/v1/records")
async def create_record(request: Request) -> JSONResponse:
    """创建记录并写入第 0 版原文与敏感区间。无需基准版本。"""
    _require_service_key(request)
    data = await _json_body(request)
    content, normalized = _parse_content_and_ranges(data)

    assert pool is not None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO records DEFAULT VALUES RETURNING id")
            (record_id,) = await cur.fetchone()
            await cur.execute(
                """
                INSERT INTO record_versions (record_id, version, content, ranges)
                VALUES (%s, 0, %s, %s::jsonb)
                """,
                (record_id, content, json.dumps(normalized)),
            )
        await conn.commit()

    return JSONResponse({"record_id": str(record_id), "version": 0}, status_code=201)


@app.put("/v1/records/versions")
async def revise_record(request: Request) -> JSONResponse:
    """提交新原文与新区间。必须携带 ``base_version``；过期则 409。"""
    _require_service_key(request)
    data = await _json_body(request)
    record_id = _record_id(data)
    base_version = _base_version(data)
    content, normalized = _parse_content_and_ranges(data)

    assert pool is not None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await _lock_record(cur, record_id)
            await cur.execute(
                "SELECT current_version FROM records WHERE id = %s",
                (record_id,),
            )
            (current_version,) = await cur.fetchone()
            if base_version != current_version:
                # 不写入任何内容，返回当前版本帮助维护方重试
                raise ApiError(
                    409,
                    "version_conflict",
                    "base_version does not match the current version",
                    current_version=current_version,
                )

            new_version = current_version + 1
            await cur.execute(
                """
                INSERT INTO record_versions (record_id, version, content, ranges)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (record_id, new_version, content, json.dumps(normalized)),
            )
            await cur.execute(
                "UPDATE records SET current_version = %s WHERE id = %s",
                (new_version, record_id),
            )
        await conn.commit()

    return JSONResponse({"record_id": record_id, "version": new_version})


# ---- 维护方：发布快照 -------------------------------------------------------
@app.post("/v1/records/publish")
async def publish_record(request: Request) -> JSONResponse:
    """以基准版本固化不可变脱敏快照，并在同一事务内签发读取令牌。

    事务内：锁行 -> 校验基准版本 -> 读取该版原文/区间 -> 合并区间并脱敏 ->
    写快照（按 record_id+version 幂等）-> 写令牌，最后一次提交。
    因此并发/故障下不会出现“有快照无令牌”之类的部分发布。

    发布不改变原文，也不修改版本号；已发布过的版本再次发布会返回同一不可变
    快照（200），并另签一枚新令牌，可用 ``expires_in_seconds`` 设定有效期。
    """
    _require_service_key(request)
    data = await _json_body(request)
    record_id = _record_id(data)
    base_version = _base_version(data)
    ttl = _optional_int(
        data,
        "expires_in_seconds",
        minimum=1,
        maximum=MAX_TOKEN_TTL_SECONDS,
    )

    token_text = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_text.encode("utf-8")).digest()
    expires_at = _now() + timedelta(seconds=ttl) if ttl is not None else None

    assert pool is not None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await _lock_record(cur, record_id)
            await cur.execute(
                "SELECT current_version FROM records WHERE id = %s",
                (record_id,),
            )
            (current_version,) = await cur.fetchone()
            if base_version != current_version:
                raise ApiError(
                    409,
                    "version_conflict",
                    "base_version does not match the current version",
                    current_version=current_version,
                )

            await cur.execute(
                """
                SELECT content, ranges
                  FROM record_versions
                 WHERE record_id = %s AND version = %s
                """,
                (record_id, base_version),
            )
            row = await cur.fetchone()
            if row is None:  # 理论不可达：current_version 必有对应版本
                raise ApiError(404, "version_not_found", "base version content not found")
            content, stored_ranges = row

            # stored_ranges 来自 jsonb，psycopg 解码为 Python list
            merged = merge_ranges(stored_ranges)
            redacted = redact(content, merged)

            # 所有发布事务都持有 records 行锁而串行，此处可安全判断是否首次发布
            await cur.execute(
                """
                SELECT id, status FROM snapshots
                 WHERE record_id = %s AND version = %s
                """,
                (record_id, base_version),
            )
            existing = await cur.fetchone()
            if existing is None:
                await cur.execute(
                    """
                    INSERT INTO snapshots (record_id, version, redacted_content, merged_ranges)
                    VALUES (%s, %s, %s, %s::jsonb)
                    RETURNING id, status
                    """,
                    (record_id, base_version, redacted, json.dumps(merged)),
                )
                snapshot_id, status = await cur.fetchone()
                inserted = True
            else:
                # 同版本已发布：快照不可变，沿用既有快照与当时生成的脱敏正文
                snapshot_id, status = existing
                inserted = False

            # 快照与令牌在同一事务落库，杜绝部分发布
            await cur.execute(
                """
                INSERT INTO tokens (snapshot_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (snapshot_id, token_hash, expires_at),
            )
            (token_id,) = await cur.fetchone()
        await conn.commit()

    body = {
        "record_id": record_id,
        "version": base_version,
        "current_version": current_version,
        "snapshot_id": str(snapshot_id),
        "snapshot_status": status,
        "redacted_content": redacted,
        "token_id": str(token_id),
        "access_token": token_text,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    return JSONResponse(body, status_code=201 if inserted else 200)


# ---- 维护方：令牌签发 / 撤销 ------------------------------------------------
@app.post("/v1/snapshots/tokens")
async def issue_token(request: Request) -> JSONResponse:
    """为已发布快照签发随机读取令牌，可指定有效期（秒）。"""
    _require_service_key(request)
    data = await _json_body(request)
    snapshot_id = _field(data, "snapshot_id", str)
    ttl = _optional_int(
        data,
        "expires_in_seconds",
        minimum=1,
        maximum=MAX_TOKEN_TTL_SECONDS,
    )
    if len(snapshot_id) != 36:
        raise ApiError(404, "snapshot_not_found", "snapshot not found")

    token_text = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_text.encode("utf-8")).digest()
    expires_at = _now() + timedelta(seconds=ttl) if ttl is not None else None

    assert pool is not None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO tokens (snapshot_id, token_hash, expires_at)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (snapshot_id, token_hash, expires_at),
                )
            except psycopg.errors.ForeignKeyViolation:
                raise ApiError(404, "snapshot_not_found", "snapshot not found")
            except psycopg.errors.InvalidTextRepresentation:
                raise ApiError(404, "snapshot_not_found", "snapshot not found")
            (token_id,) = await cur.fetchone()
        await conn.commit()

    return JSONResponse(
        {
            "token_id": str(token_id),
            "snapshot_id": snapshot_id,
            "access_token": token_text,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
        status_code=201,
    )


@app.post("/v1/tokens/{token_id}/revoke")
async def revoke_token(token_id: str, request: Request) -> JSONResponse:
    """撤销令牌：提交事务（revoked_at 落库）后开始的读取一律拿不到正文。"""
    _require_service_key(request)
    if len(token_id) != 36:
        raise ApiError(404, "token_not_found", "token not found")

    assert pool is not None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    UPDATE tokens
                       SET status = 'revoked', revoked_at = now()
                     WHERE id = %s
                 RETURNING status
                    """,
                    (token_id,),
                )
            except psycopg.errors.InvalidTextRepresentation:
                raise ApiError(404, "token_not_found", "token not found")
            if await cur.fetchone() is None:
                raise ApiError(404, "token_not_found", "token not found")
        await conn.commit()

    return JSONResponse({"token_id": token_id, "status": "revoked"})


# ---- 公开读取 ---------------------------------------------------------------
def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise ApiError(401, "unauthorized", "missing bearer token")
    return value.strip()


@app.get("/v1/public/records")
async def public_read(request: Request) -> JSONResponse:
    """凭令牌读取脱敏正文。

    判定与读取在同一事务（READ COMMITTED 的语句级快照）中完成：
    撤销事务提交后开始的本事务只能看到 revoked 状态；到期则以数据库时间判定。
    无效、撤销、到期情形均不返回正文，错误响应只包含笼统状态，不含快照内容。
    """
    token_text = _bearer(request)
    token_hash = hashlib.sha256(token_text.encode("utf-8")).digest()

    assert pool is not None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT t.status, t.expires_at, t.revoked_at,
                       s.id, s.record_id, s.version, s.status, s.redacted_content
                  FROM tokens t
                  JOIN snapshots s ON s.id = t.snapshot_id
                 WHERE t.token_hash = %s
                """,
                (token_hash,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ApiError(404, "invalid_token", "token not found or no longer valid")
            (
                status,
                expires_at,
                revoked_at,
                snapshot_id,
                record_id,
                version,
                snapshot_status,
                redacted,
            ) = row

            # 到期由数据库当前时间判定，避免客户端时钟影响
            await cur.execute("SELECT now()")
            (db_now,) = await cur.fetchone()
            if status != "active" or revoked_at is not None:
                raise ApiError(410, "token_revoked", "token is no longer valid")
            if expires_at is not None and db_now >= expires_at:
                raise ApiError(410, "token_expired", "token is no longer valid")

            return JSONResponse(
                {
                    "record_id": str(record_id),
                    "version": version,
                    "snapshot_id": str(snapshot_id),
                    "snapshot_status": snapshot_status,
                    "redacted_content": redacted,
                }
            )
