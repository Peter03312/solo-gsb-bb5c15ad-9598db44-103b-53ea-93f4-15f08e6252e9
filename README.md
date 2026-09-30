# 公开脱敏记录发布 API

供机构**维护人员**提交记录原文与敏感区间、发布**不可变脱敏快照**，并向公众开放
**凭令牌只读**的 HTTP API。

- 框架：Python 3.11 + FastAPI + Uvicorn
- 数据库：PostgreSQL 16（psycopg3 异步连接池）
- 区间单位：**Unicode 码点**，左闭右开 `[start, end)`
- 脱敏规则：发布时把**相交或相邻**的区间合并，每段统一替换为 `[已遮蔽]`，其余原文逐码点保留
- 并发：修订 / 发布 / 撤销全部在单事务内“锁行 + 校验 + 写入”，拒绝部分结果

---

## 一键启动

需要 Docker（含 Compose v2）。

```bash
# 1) 配置服务密钥（也可直接用 docker compose 的默认值做本地试用）
cp .env.example .env
# 编辑 .env，把 SERVICE_KEY 改成足够长的随机串，例如：
#   openssl rand -base64 48

# 2) 构建并启动（首次会拉取镜像、安装 Python 依赖；数据库表由应用自动幂等创建）
docker compose up --build -d

# 3) 健康检查
curl http://localhost:8080/healthz
# {"status":"ok"}
```

- API 服务地址：`http://localhost:8080`（容器内监听 `0.0.0.0:8080`）
- API 文档（Swagger UI）：`http://localhost:8080/docs`
- PostgreSQL：容器内服务名 `db`，端口 `5432`（默认**未**映射到宿主机）
- 停止：`docker compose down`（加 `-v` 同时删除数据卷）

数据库初始化无需手工执行 SQL：应用启动时以 `CREATE TABLE IF NOT EXISTS` 幂等建表，
并带有限次连接重试以等待数据库就绪。

---

## 配置（环境变量）

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SERVICE_KEY` | 是 | （compose 内置试用值） | 维护方服务密钥，通过 `X-Service-Key` 请求头提交 |
| `DATABASE_URL` | 是 | `postgresql://redact:redact@db:5432/redaction` | PostgreSQL 连接串 |
| `MAX_CONTENT_CODEPOINTS` | 否 | `1000000` | 单次提交原文的最大码点数 |
| `MAX_RANGES` | 否 | `10000` | 单次提交区间最大条数 |
| `MAX_TOKEN_TTL_SECONDS` | 否 | `31536000`（一年） | 令牌可设置的最长有效期 |

生产部署务必覆盖 `SERVICE_KEY` 与 compose 中数据库账号密码。

---

## 模型与版本语义

- 记录创建后当前版本为 **0**；每次成功修订 `current_version += 1`，旧版本原文全部保留。
- 修订与发布都必须携带 `base_version`。仅当它等于服务端当前版本时才接受；
  否则整次请求以 `409 version_conflict` 拒绝，响应里给出 `current_version` 供调用方重试。
- **发布快照不可变**：快照固化“该版本原文 + 合并区间 + 脱敏正文”。之后原文再怎么修订，
  旧快照与其令牌读到的内容都不会改变。同一 `(record, version)` 只有一个快照；
  重复发布会返回同一快照并另签一枚新令牌（首次 `201`，重复 `200`）。
- 越界区间（`start < 0`、`end > 码点长度`、`end < start`、非整数端点等）一律整次拒绝，
  不写入任何版本。
- 令牌只以 **SHA-256 哈希**落库；可设到期时间。撤销提交完成后开始的读取、
  以及到期后的读取都拿不到正文。

错误响应统一形如 `{"error": "<code>", "message": "..."}`，**不回显原文、区间或令牌**。

---

## 接口

维护方接口需请求头 `X-Service-Key: <SERVICE_KEY>`；公开读取需
`Authorization: Bearer <access_token>`。

| 方法 | 路径 | 鉴权 | 作用 |
| --- | --- | --- | --- |
| `POST` | `/v1/records` | 服务密钥 | 创建记录（写入 v0） |
| `PUT` | `/v1/records/versions` | 服务密钥 | 按基准版本修订原文与区间 |
| `POST` | `/v1/records/publish` | 服务密钥 | 发布当前版本快照并签发令牌 |
| `POST` | `/v1/snapshots/tokens` | 服务密钥 | 为既有快照额外签发令牌 |
| `POST` | `/v1/tokens/{token_id}/revoke` | 服务密钥 | 撤销令牌（幂等） |
| `GET` | `/v1/public/records` | Bearer 令牌 | 公开读取脱敏正文 |
| `GET` | `/healthz` | 无 | 就绪探针 |

### 1. 创建记录

```bash
curl -X POST http://localhost:8080/v1/records \
  -H "X-Service-Key: $SERVICE_KEY" -H "Content-Type: application/json" \
  -d '{
        "content": "张三的身份证号是110101199003077890，请保密。",
        "ranges": [[0,2],[6,24]]
      }'
```

```json
{ "record_id": "8c1a…", "version": 0 }
```

`ranges` 中区间可为任意顺序，服务端会排序；空区间 `[n,n)` 会被忽略。

### 2. 修订（乐观并发）

```bash
curl -X PUT http://localhost:8080/v1/records/versions \
  -H "X-Service-Key: $SERVICE_KEY" -H "Content-Type: application/json" \
  -d '{
        "record_id": "8c1a…",
        "base_version": 0,
        "content": "李四的身份证号是320102198507154421，请保密。",
        "ranges": [[0,2],[6,24]]
      }'
```

基准版本过期时：

```json
HTTP 409
{ "error": "version_conflict",
  "message": "base_version does not match the current version",
  "current_version": 1 }
```

### 3. 发布快照并获取令牌

```bash
curl -X POST http://localhost:8080/v1/records/publish \
  -H "X-Service-Key: $SERVICE_KEY" -H "Content-Type: application/json" \
  -d '{"record_id":"8c1a…","base_version":1,"expires_in_seconds":3600}'
```

```json
{
  "record_id": "8c1a…",
  "version": 1,
  "current_version": 1,
  "snapshot_id": "4df6…",
  "snapshot_status": "published",
  "redacted_content": "李四[已遮蔽]320102198507154421，请保密。" ,
  "token_id": "e2be…",
  "access_token": "7Xq…（只在响应中出现一次，请妥善保存）",
  "expires_at": "2026-09-30T12:00:00+00:00"
}
```

- 省略 `expires_in_seconds` 时令牌不过期（仍可被撤销）。
- 合并效果示例：区间 `[[0,2],[1,3],[4,5],[5,6]]` 作用于 `"ABCDEFG"`
  （相交的前两段合并为 `[0,3)`，相邻的后两段合并为 `[4,6)`）：
  `[已遮蔽]D[已遮蔽]G`。

### 4. 公开读取

```bash
curl http://localhost:8080/v1/public/records \
  -H "Authorization: Bearer 7Xq…"
```

```json
{
  "record_id": "8c1a…",
  "version": 1,
  "snapshot_id": "4df6…",
  "snapshot_status": "published",
  "redacted_content": "李四[已遮蔽]…"
}
```

令牌错误 / 撤销 / 到期分别返回 `404 invalid_token`、`410 token_revoked`、
`410 token_expired`，响应体不含正文。

### 5. 撤销

```bash
curl -X POST http://localhost:8080/v1/tokens/e2be…/revoke \
  -H "X-Service-Key: $SERVICE_KEY"
# {"token_id":"e2be…","status":"revoked"}
```

完整脚本见 [`examples/curl.sh`](examples/curl.sh)：

```bash
SERVICE_KEY=你的密钥 bash examples/curl.sh
```

---

## 状态码

| 状态码 | code | 场景 |
| --- | --- | --- |
| 200 / 201 | — | 成功 / 资源首次创建 |
| 400 | `invalid_request` / `invalid_json` | 请求结构错误、缺字段、类型错误 |
| 401 | `unauthorized` | 无服务密钥 / 服务密钥错误 / 无 Bearer 令牌 |
| 404 | `record_not_found` / `snapshot_not_found` / `token_not_found` / `invalid_token` | 资源不存在或令牌无效 |
| 409 | `version_conflict` | 基准版本与当前版本不符（附 `current_version`） |
| 410 | `token_revoked` / `token_expired` | 令牌已撤销或已到期 |
| 413 | `payload_too_large` | 原文或区间数量超配置上限 |
| 415 | `unsupported_media_type` | Content-Type 非 application/json |
| 422 | `invalid_range` | 区间越界或端点非法（整次拒绝，不落库） |
| 500 / 503 | `internal_error` / 健康探针 | 服务内部错误（不回传细节） |

---

## 安全与一致性说明

- **密钥校验**使用恒定时间比较（`hmac.compare_digest`）；公开接口不接受服务密钥，
  维护接口不暴露在匿名路径上。
- **原文保护**：原文仅存于维护侧表；公开读取只返回快照中预先计算好的脱敏正文。
  所有错误处理器兜底返回笼统信息，不回显请求体、SQL 或堆栈。
- **令牌存储**：数据库只保存 SHA-256 哈希；撤销后 `status=revoked` 且记录 `revoked_at`。
- **事务原子性**：
  - 修订：`SELECT … FOR UPDATE` 锁记录 → 比对版本 → 插版本 → 推进版本号，一次提交；
  - 发布：锁记录 → 比对版本 → 写快照 → 写令牌，一次提交，失败整体回滚，无部分发布；
  - 撤销：单条 `UPDATE` 提交后生效。在 READ COMMITTED 下，撤销提交之后开启的读取
    只会看到撤销后的状态。
- **到期判定**以数据库 `now()` 为准，不信任客户端时钟。
- **不可变性**：快照表对 `(record_id, version)` 加唯一约束，快照行不提供更新接口。

---

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install httpx                       # 运行端到端测试还需要 httpx

# 纯脱敏逻辑单测（不需要数据库）
python -m unittest discover -s tests -p 'test_*.py' -v

# 端到端测试：自动拉起一个用户态 PostgreSQL（pip install pgserver），
# 覆盖鉴权、版本冲突、区间合并、快照不可变、令牌到期/撤销、并发修订与发布等
python tests/e2e_api.py
```

## 项目结构

```
app/
  main.py          # FastAPI 应用、路由、事务与鉴权
  redaction.py     # 区间校验 / 合并 / 脱敏纯函数
tests/
  test_redaction.py  # 纯函数单元测试
  e2e_api.py         # 真实 PostgreSQL 端到端测试
examples/curl.sh     # 全流程调用示例
Dockerfile
docker-compose.yml
requirements.txt
```
