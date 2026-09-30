"""真实 PostgreSQL 上的端到端测试（本地/CI 使用，不属于容器镜像）。

依赖：pip install httpx pgserver
运行：python tests/e2e_api.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

os.environ.setdefault("SERVICE_KEY", "secret-key-123")
os.environ.setdefault("MAX_TOKEN_TTL_SECONDS", "31536000")

import pgserver  # noqa: E402


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="pge2e-")
    server = pgserver.get_server(tmp, cleanup_mode="delete")
    # pgserver 默认 superuser 库；建一个本测试用数据库
    import psycopg  # noqa: E402

    admin_uri = server.get_uri()
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        try:
            conn.execute("CREATE DATABASE e2e_redaction")
        except psycopg.errors.DuplicateDatabase:
            pass
    uri = server.get_uri("e2e_redaction")
    os.environ["DATABASE_URL"] = uri
    print("postgres uri:", uri.replace(uri.split("@")[0].split("://")[-1], "***"))

    import httpx  # noqa: E402
    from app.main import app  # noqa: E402

    asyncio.run(_run(app, httpx))


async def _run(app, httpx) -> None:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS" if cond else "FAIL"), "-", name + (f" :: {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            auth = {"X-Service-Key": "secret-key-123"}
            bad_auth = {"X-Service-Key": "wrong"}

            # health
            r = await c.get("/healthz")
            check("healthz", r.status_code == 200)

            # 无密钥拒绝
            r = await c.post("/v1/records", json={"content": "x", "ranges": []})
            check("no key -> 401", r.status_code == 401, str(r.status_code))
            r = await c.post("/v1/records", json={"content": "x", "ranges": []}, headers=bad_auth)
            check("bad key -> 401", r.status_code == 401, str(r.status_code))

            # 创建记录
            content0 = "张三的身份证号是110101199003077890，请保密。"
            ranges0 = [[0, 1], [6, 24]]  # 遮蔽姓名 + 18 位身份证号
            r = await c.post("/v1/records", json={"content": content0, "ranges": ranges0}, headers=auth)
            check("create 201", r.status_code == 201, r.text)
            rid = r.json()["record_id"]
            check("create version 0", r.json()["version"] == 0)

            # 越界区间整次拒绝（版本不应前进）
            r = await c.put(
                "/v1/records/versions",
                json={
                    "record_id": rid,
                    "base_version": 0,
                    "content": content0,
                    "ranges": [[0, 999]],
                },
                headers=auth,
            )
            check("out of bounds -> 422", r.status_code == 422, r.text)
            check("error does not leak content", content0 not in r.text)

            # 非数组/形状错误
            r = await c.put(
                "/v1/records/versions",
                json={"record_id": rid, "base_version": 0, "content": content0, "ranges": "x"},
                headers=auth,
            )
            check("bad ranges type -> 400", r.status_code == 400, r.text)

            # 缺基准版本
            r = await c.put(
                "/v1/records/versions",
                json={"record_id": rid, "content": content0, "ranges": []},
                headers=auth,
            )
            check("missing base_version -> 400", r.status_code == 400, r.text)

            # 正常修订到 v1
            content1 = "李四的身份证号是320102198507154421，旧内容无效。"
            r = await c.put(
                "/v1/records/versions",
                json={"record_id": rid, "base_version": 0, "content": content1, "ranges": [[0, 1], [6, 24]]},
                headers=auth,
            )
            check("revise -> 200 v1", r.status_code == 200 and r.json()["version"] == 1, r.text)

            # 过期基准版本 -> 409 且返回当前版本
            r = await c.put(
                "/v1/records/versions",
                json={"record_id": rid, "base_version": 0, "content": content1, "ranges": []},
                headers=auth,
            )
            check("stale revise -> 409", r.status_code == 409, r.text)
            check("409 carries current_version=1", r.json().get("current_version") == 1, r.text)

            # 发布 v1：合并相交/相邻区间 + 占位
            # 合并测试：相交 [0,2)+[1,3) => [0,3)；相邻 [4,5)+[5,6) => [4,6)
            content_pub = "ABCDEFG"
            r = await c.put(
                "/v1/records/versions",
                json={
                    "record_id": rid,
                    "base_version": 1,
                    "content": content_pub,
                    "ranges": [[0, 2], [1, 3], [4, 5], [5, 6]],
                },
                headers=auth,
            )
            check("revise -> v2", r.status_code == 200 and r.json()["version"] == 2, r.text)
            r = await c.post(
                "/v1/records/publish",
                json={"record_id": rid, "base_version": 2},
                headers=auth,
            )
            check("publish 201", r.status_code == 201, r.text)
            pub = r.json()
            snap_id = pub["snapshot_id"]
            expected = "[已遮蔽]D[已遮蔽]G"
            check("merged redaction", pub["redacted_content"] == expected, f"{pub['redacted_content']!r}")
            check("snapshot status published", pub["snapshot_status"] == "published")
            check("publish issues token", bool(pub.get("access_token")) and pub.get("expires_at") is None, str(pub))
            token = pub["access_token"]
            token_id = pub["token_id"]

            # 发布是幂等的：同版本重复发布返回同一快照（200），但另签一枚新令牌
            r = await c.post(
                "/v1/records/publish",
                json={"record_id": rid, "base_version": 2},
                headers=auth,
            )
            pub2 = r.json()
            check(
                "republish same snapshot/new token",
                r.status_code == 200
                and pub2["snapshot_id"] == snap_id
                and pub2["access_token"] != token,
                r.text,
            )
            # 第二枚令牌同样可读
            r = await c.get("/v1/public/records", headers={"Authorization": f"Bearer {pub2['access_token']}"})
            check("republish token readable", r.status_code == 200 and r.json()["redacted_content"] == expected, r.text)

            # 发布带 TTL：返回到期时间
            r = await c.post(
                "/v1/records/publish",
                json={"record_id": rid, "base_version": 2, "expires_in_seconds": 60},
                headers=auth,
            )
            check("publish with ttl", r.status_code == 200 and bool(r.json()["expires_at"]), r.text)

            # 发布过期版本 -> 409
            r = await c.post(
                "/v1/records/publish",
                json={"record_id": rid, "base_version": 1},
                headers=auth,
            )
            check("publish stale -> 409", r.status_code == 409, r.text)

            # 发布后再修订原文（v3），旧快照不变
            r = await c.put(
                "/v1/records/versions",
                json={"record_id": rid, "base_version": 2, "content": "完全不同的新原文XXXX", "ranges": [[8, 12]]},
                headers=auth,
            )
            check("revise after publish -> v3", r.status_code == 200 and r.json()["version"] == 3, r.text)

            # 签发令牌
            r = await c.post(
                "/v1/snapshots/tokens",
                json={"snapshot_id": snap_id},
                headers=auth,
            )
            check("issue token 201", r.status_code == 201, r.text)
            token = r.json()["access_token"]
            token_id = r.json()["token_id"]
            check("token has no expiry", r.json()["expires_at"] is None)

            # 无 Bearer
            r = await c.get("/v1/public/records")
            check("read no token -> 401", r.status_code == 401, r.text)
            # 错误令牌
            r = await c.get("/v1/public/records", headers={"Authorization": "Bearer nope"})
            check("read bad token -> 404", r.status_code == 404, r.text)
            # 正确令牌读到的是旧快照正文
            r = await c.get("/v1/public/records", headers={"Authorization": f"Bearer {token}"})
            check("read 200", r.status_code == 200, r.text)
            body = r.json()
            check("snapshot immutable after revise", body["redacted_content"] == expected, str(body))
            check("read version pinned 2", body["version"] == 2 and body["snapshot_status"] == "published")
            check("read does not expose raw", "完全不同" not in r.text and content_pub not in r.text)

            # 撤销后不可读
            r = await c.post(f"/v1/tokens/{token_id}/revoke", headers=auth)
            check("revoke 200", r.status_code == 200 and r.json()["status"] == "revoked", r.text)
            r = await c.get("/v1/public/records", headers={"Authorization": f"Bearer {token}"})
            check("read after revoke -> 410", r.status_code == 410, r.text)
            check("revoke error has no body text", "redacted_content" not in r.text)
            # 重复撤销幂等
            r = await c.post(f"/v1/tokens/{token_id}/revoke", headers=auth)
            check("double revoke idempotent", r.status_code == 200, r.text)

            # 过期令牌不可读（用极短 TTL + 等待）
            r = await c.post(
                "/v1/snapshots/tokens",
                json={"snapshot_id": snap_id, "expires_in_seconds": 1},
                headers=auth,
            )
            check("issue ttl token", r.status_code == 201 and r.json()["expires_at"], r.text)
            ttoken = r.json()["access_token"]
            r = await c.get("/v1/public/records", headers={"Authorization": f"Bearer {ttoken}"})
            check("ttl token valid before expiry", r.status_code == 200, r.text)
            time.sleep(1.2)
            r = await c.get("/v1/public/records", headers={"Authorization": f"Bearer {ttoken}"})
            check("ttl token expired -> 410", r.status_code == 410, r.text)

            # TTL 非法
            r = await c.post(
                "/v1/snapshots/tokens",
                json={"snapshot_id": snap_id, "expires_in_seconds": 0},
                headers=auth,
            )
            check("ttl 0 -> 400", r.status_code == 400, r.text)

            # 不存在资源
            r = await c.post(
                "/v1/records/publish",
                json={"record_id": "00000000-0000-0000-0000-000000000000", "base_version": 0},
                headers=auth,
            )
            check("missing record -> 404", r.status_code == 404, r.text)
            r = await c.post(
                "/v1/snapshots/tokens",
                json={"snapshot_id": "00000000-0000-0000-0000-000000000000"},
                headers=auth,
            )
            check("token for missing snapshot -> 404", r.status_code == 404, r.text)

            # ---- 并发修订：N 个同基准请求只有一个成功 ----
            r = await c.post("/v1/records", json={"content": "并发测试原文0000", "ranges": []}, headers=auth)
            rid2 = r.json()["record_id"]

            async def bump(i: int):
                return await c.put(
                    "/v1/records/versions",
                    json={
                        "record_id": rid2,
                        "base_version": 0,
                        "content": f"并发修订第{i}版AAAAAAAA",
                        "ranges": [[6, 14]],
                    },
                    headers=auth,
                )

            results = await asyncio.gather(*(bump(i) for i in range(8)))
            codes = sorted(r.status_code for r in results)
            oks = [r for r in results if r.status_code == 200]
            check("concurrent revise exactly one 200", len(oks) == 1, f"codes={codes}")
            check("rest are 409", codes.count(409) == 7, f"codes={codes}")
            winner = oks[0].json()["version"]
            check("winner version is 1", winner == 1, str(winner))

            # ---- 并发发布：同版本只产生一个快照（幂等返回同一个 id）----
            async def pub_call():
                return await c.post(
                    "/v1/records/publish",
                    json={"record_id": rid2, "base_version": 1},
                    headers=auth,
                )

            presults = await asyncio.gather(*(pub_call() for _ in range(6)))
            snap_ids = {r.json().get("snapshot_id") for r in presults if r.status_code in (200, 201)}
            check("concurrent publish single snapshot", len(snap_ids) == 1, str(snap_ids))
            check("concurrent publish all success", all(r.status_code in (200, 201) for r in presults),
                  str([r.status_code for r in presults]))

            # 并发撤销 + 读取竞争：撤销完成后开启的读取必须 410
            r = await c.post(
                "/v1/snapshots/tokens",
                json={"snapshot_id": next(iter(snap_ids))},
                headers=auth,
            )
            t2 = r.json()["access_token"]
            t2id = r.json()["token_id"]
            await c.post(f"/v1/tokens/{t2id}/revoke", headers=auth)
            # 给一点时间确保撤销提交可见后再读
            await asyncio.sleep(0.05)
            r = await c.get("/v1/public/records", headers={"Authorization": f"Bearer {t2}"})
            check("post-revoke read denied under concurrency", r.status_code == 410, r.text)

            # 公开错误不含原文：尝试各种畸形输入
            for payload in (
                {"content": "机密机密机密", "ranges": [[0, 1.5]]},
                {"content": "机密机密机密", "ranges": [[True, 2]]},
            ):
                r = await c.post("/v1/records", json=payload, headers=auth)
                check(f"reject malformed {payload['ranges']}", r.status_code in (400, 422), r.text)
                check("no leak", "机密" not in r.text)

            # content-type 错误
            r = await c.post("/v1/records", content=b"{}", headers={**auth, "content-type": "text/plain"})
            check("wrong content-type -> 415", r.status_code == 415, r.text)

        if failures:
            print(f"\n{len(failures)} FAILURES: {failures}")
            raise SystemExit(1)
        print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
