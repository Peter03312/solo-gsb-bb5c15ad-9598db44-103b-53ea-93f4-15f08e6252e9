import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from .models import ReadToken, Record, RecordVersion, Snapshot
from .redaction import IntervalOutOfBoundsError, redact_text, validate_intervals
from .schemas import (
    Interval,
    PublicSnapshotResponse,
    PublishRequest,
    PublishResponse,
    RecordCreateRequest,
    RecordResponse,
    RecordReviseRequest,
    RecordStatusResponse,
    RevokeResponse,
    SnapshotInfo,
)
from .security import require_service_key

SNAPSHOT_STATUS = "published"


def _init_db_with_retry() -> None:
    """启动时建表（幂等），并等待数据库就绪。"""
    last_exc: Exception | None = None
    for _ in range(settings.db_connect_retries):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            last_exc = exc
            time.sleep(settings.db_connect_retry_delay_seconds)
    raise RuntimeError("database is not reachable") from last_exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db_with_retry()
    yield


app = FastAPI(
    title="Redacted Records API",
    version="1.0.0",
    summary="机构公开脱敏记录服务",
    lifespan=lifespan,
)


def _validated_intervals(text: str, intervals: list[Interval]) -> list[tuple[int, int]]:
    """越界区间直接 422，且发生在任何写库之前，保证整次拒绝。"""
    try:
        return validate_intervals(len(text), [(iv.start, iv.end) for iv in intervals])
    except IntervalOutOfBoundsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post(
    "/records",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def create_record(payload: RecordCreateRequest, db: Session = Depends(get_db)):
    """创建记录（版本 1）：提交原文与敏感区间。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    record = Record(current_version=1)
    db.add(record)
    db.flush()
    db.add(
        RecordVersion(
            record_id=record.id,
            version=1,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record.id, version=1)


@app.put(
    "/records/{record_id}",
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def revise_record(record_id: UUID, payload: RecordReviseRequest, db: Session = Depends(get_db)):
    """修订记录：必须携带当前版本号作为基准，过期则 409 并返回当前版本。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    # 条件更新是原子的：并发修订时只有一个能基于同一基准版本成功，
    # 其余因 current_version 已变化而失败，不会产生分叉版本。
    new_version = db.execute(
        update(Record)
        .where(Record.id == record_id, Record.current_version == payload.base_version)
        .values(current_version=payload.base_version + 1)
        .returning(Record.current_version)
    ).scalar_one_or_none()
    if new_version is None:
        db.rollback()
        record = db.get(Record, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "base_version is stale",
                "current_version": record.current_version,
            },
        )
    db.add(
        RecordVersion(
            record_id=record_id,
            version=new_version,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record_id, version=new_version)


def _compute_expires_at(payload: PublishRequest) -> datetime | None:
    if payload.expires_at is not None:
        expires_at = payload.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expires_at must be in the future",
            )
        return expires_at
    if payload.expires_in_seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=payload.expires_in_seconds)
    return None


@app.post(
    "/records/{record_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=PublishResponse,
    dependencies=[Depends(require_service_key)],
)
def publish_record(record_id: UUID, payload: PublishRequest, db: Session = Depends(get_db)):
    """发布指定版本：合并区间生成不可变快照，并签发随机读取令牌。"""
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    expires_at = _compute_expires_at(payload)
    redacted = redact_text(
        version.original_text, [(iv["start"], iv["end"]) for iv in version.intervals]
    )
    # 快照按 (record_id, version) 唯一：并发发布时唯一约束保证只落库一份，
    # 且快照与令牌在同一事务提交，不会出现部分发布。
    db.execute(
        pg_insert(Snapshot)
        .values(record_id=record_id, version=payload.version, redacted_text=redacted)
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    snapshot = db.execute(
        select(Snapshot).where(
            Snapshot.record_id == record_id, Snapshot.version == payload.version
        )
    ).scalar_one()
    token = ReadToken(
        token=secrets.token_urlsafe(32), snapshot_id=snapshot.id, expires_at=expires_at
    )
    db.add(token)
    db.commit()
    return PublishResponse(
        snapshot_id=snapshot.id,
        record_id=record_id,
        version=payload.version,
        status=SNAPSHOT_STATUS,
        token=token.token,
        expires_at=expires_at,
    )


@app.post(
    "/tokens/{token}/revoke",
    response_model=RevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_token(token: str, db: Session = Depends(get_db)):
    """撤销令牌：单条原子 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadToken)
        .where(ReadToken.token == token, ReadToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return RevokeResponse(token=token, revoked=True, already_revoked=False)
    if db.get(ReadToken, token) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    return RevokeResponse(token=token, revoked=True, already_revoked=True)


@app.get(
    "/records/{record_id}",
    response_model=RecordStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_record(record_id: UUID, db: Session = Depends(get_db)):
    """维护侧查看记录当前版本与已发布快照列表。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.record_id == record_id).order_by(Snapshot.version)
        )
        .scalars()
        .all()
    )
    return RecordStatusResponse(
        record_id=record.id,
        current_version=record.current_version,
        snapshots=[
            SnapshotInfo(
                snapshot_id=s.id,
                version=s.version,
                status=SNAPSHOT_STATUS,
                published_at=s.published_at,
            )
            for s in snapshots
        ],
    )


@app.get("/public/snapshots/{snapshot_id}", response_model=PublicSnapshotResponse)
def read_snapshot(
    snapshot_id: UUID,
    token: str = Query(..., description="发布时签发的读取令牌"),
    db: Session = Depends(get_db),
):
    """公开读取：凭有效令牌返回脱敏正文；任何失败都返回统一的模糊错误。"""
    snapshot = db.get(Snapshot, snapshot_id)
    read_token = db.get(ReadToken, token)
    now = datetime.now(timezone.utc)
    if (
        snapshot is None
        or read_token is None
        or read_token.snapshot_id != snapshot.id
        or read_token.revoked_at is not None
        or (read_token.expires_at is not None and read_token.expires_at <= now)
    ):
        # 不区分“不存在 / 已撤销 / 已过期”，避免泄露任何信息
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    return PublicSnapshotResponse(
        record_id=snapshot.record_id,
        version=snapshot.version,
        status=SNAPSHOT_STATUS,
        redacted_text=snapshot.redacted_text,
    )
