# Redacted Records API

供机构公开发布脱敏记录的 HTTP 服务。维护人员凭服务密钥提交原文与敏感区间，发布时按 Unicode 码点区间合并遮蔽，生成**不可变快照**与**随机读取令牌**；公众仅凭有效令牌读取脱敏正文。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档：`http://localhost:8000/docs`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与 API 两个容器，自动完成依赖安装与数据库初始化（启动时幂等建表，并重试等待数据库就绪）。数据持久化在命名卷 `pgdata` 中。

停止并清空数据：`docker compose down -v`

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SERVICE_KEY` | 维护人员服务密钥，**必填**，未配置服务拒绝启动 | 无（Compose 中为演示值 `dev-service-key-change-me`，生产务必修改） |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://redact:redact@localhost:5432/redact` |
| `DB_CONNECT_RETRIES` | 启动时等待数据库的重试次数 | `30` |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | 重试间隔（秒） | `1.0` |

维护接口通过请求头 `X-Service-Key` 鉴权；缺失或不符一律 `401` 整次拒绝。

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/records` | 服务密钥 | 创建记录（版本 1），提交原文与敏感区间 |
| `PUT` | `/records/{record_id}` | 服务密钥 | 修订：携带 `base_version`，过期返回 `409` 与当前版本 |
| `POST` | `/records/{record_id}/publish` | 服务密钥 | 发布指定版本：生成不可变快照 + 随机读取令牌 |
| `POST` | `/tokens/{token}/revoke` | 服务密钥 | 撤销读取令牌 |
| `GET` | `/records/{record_id}` | 服务密钥 | 查看当前版本与快照列表 |
| `GET` | `/public/snapshots/{snapshot_id}?token=...` | 无（凭令牌） | 公开读取脱敏正文 |
| `GET` | `/healthz` | 无 | 健康检查 |

### 区间语义

- 左闭右开 `[start, end)`，位置按 **Unicode 码点**计（与 Python 字符串下标一致，BMP 之外的字符算 1 个码点）。
- 越界、为空或倒置的区间 → `422`，**整次拒绝**，不落库任何内容。
- 发布时合并**相交或相邻**的区间，每段替换为 `[已遮蔽]`，其余原文保留。

### 调用示例

```bash
# 1. 创建记录（版本 1）
curl -sS -X POST http://localhost:8000/records \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
        "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
      }'
# => {"record_id":"<RID>","version":1}

# 2. 修订（base_version 过期时返回 409 与当前版本）
curl -sS -X PUT http://localhost:8000/records/<RID> \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"base_version": 1, "text": "……新原文……", "intervals": [{"start": 0, "end": 3}]}'
# => {"record_id":"<RID>","version":2}
# 过期时 => 409 {"detail":{"message":"base_version is stale","current_version":2}}

# 3. 发布版本 2（可另加 "expires_in_seconds": 3600 或 "expires_at": "2026-10-01T00:00:00Z"）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"version": 2}'
# => {"snapshot_id":"<SID>","version":2,"status":"published","token":"<TOKEN>","expires_at":null}

# 4. 公开读取（无需密钥，只返回脱敏正文）
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"患者[已遮蔽]，……"}

# 5. 撤销令牌（撤销完成后，新开始的读取一律 403）
curl -sS -X POST http://localhost:8000/tokens/<TOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"
```

完整端到端演示（含 401/409/422、快照不可变、撤销与到期）：

```bash
./examples/demo.sh        # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖默认配置
```

## 设计要点

- **不可变快照**：发布时把脱敏结果写入 `snapshots`（按 `(record_id, version)` 唯一），公开读取只访问快照；后续修订写入新版本，旧快照不变。
- **并发安全**：
  - 修订用条件更新 `UPDATE ... WHERE current_version = :base_version`，原子比较并递增，并发修订只有一个成功，其余收到 `409` 与当前版本；
  - 发布用 `INSERT ... ON CONFLICT DO NOTHING` 保证同一版本只有一份快照，快照与令牌同事务提交，不存在部分发布；
  - 撤销是单条原子 `UPDATE`，提交后新开始的读取即被拒绝。
- **不泄露原文**：公开接口只返回快照中的脱敏正文；令牌无效/撤销/到期统一返回模糊的 `403 snapshot unavailable`；错误信息不含原文内容。
- **令牌**：`secrets.token_urlsafe(32)` 随机生成；支持 `expires_in_seconds` 或 `expires_at`（二选一）设置到期时间，到期或撤销后读取一律拒绝。

## 本地开发

```bash
pip install -r requirements.txt
export SERVICE_KEY=dev-service-key-change-me
export DATABASE_URL=postgresql+psycopg2://redact:redact@localhost:5432/redact
uvicorn app.main:app --reload --port 8000

# 脱敏核心逻辑单元测试（仅依赖标准库）
python3 -m unittest tests.test_redaction -v
```

## 目录结构

```
app/
  main.py       # 路由与事务逻辑
  models.py     # records / record_versions / snapshots / read_tokens
  redaction.py  # 区间校验、合并、替换（纯函数）
  schemas.py    # 请求/响应模型
  security.py   # 服务密钥鉴权
  config.py     # 环境变量配置
  db.py         # 引擎与会话
examples/demo.sh  # 端到端调用示例
tests/            # 脱敏逻辑单元测试
Dockerfile / docker-compose.yml
```
