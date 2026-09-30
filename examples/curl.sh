#!/usr/bin/env bash
# 完整调用示例：创建 -> 修订（乐观版本）-> 发布快照+令牌 -> 公开读取 -> 撤销
# 依赖：bash、curl。运行：BASE_URL=http://localhost:8080 SERVICE_KEY=xxxx bash examples/curl.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
SERVICE_KEY="${SERVICE_KEY:-change-me-please-use-a-long-random-secret}"
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

echo "== 1. 创建记录（v0），原文含姓名与身份证号，区间按 Unicode 码点 [start,end) =="
CREATE=$(curl -sS -X POST "${BASE_URL}/v1/records" "${AUTH[@]}" \
  -d '{"content":"张三的身份证号是110101199003077890，请保密。","ranges":[[0,2],[6,24]]}')
echo "${CREATE}"
RID=$(python3 -c 'import sys,json;print(json.load(sys.stdin)["record_id"])' <<<"${CREATE}")

echo "== 2. 修订原文（必须带 base_version=0）=="
curl -sS -X PUT "${BASE_URL}/v1/records/versions" "${AUTH[@]}" \
  -d "{\"record_id\":\"${RID}\",\"base_version\":0,\"content\":\"李四的身份证号是320102198507154421，请保密。\",\"ranges\":[[0,2],[6,24]]}"
echo

echo "== 3. 过期基准版本会被拒绝（409，并返回 current_version）=="
curl -sS -o /dev/null -w "status=%{http_code}\n" -X PUT "${BASE_URL}/v1/records/versions" "${AUTH[@]}" \
  -d "{\"record_id\":\"${RID}\",\"base_version\":0,\"content\":\"陈旧提交\",\"ranges\":[]}"

echo "== 4. 发布当前版本（v1），同一事务内生成不可变快照与读取令牌；令牌 3600 秒后到期 =="
PUBLISH=$(curl -sS -X POST "${BASE_URL}/v1/records/publish" "${AUTH[@]}" \
  -d "{\"record_id\":\"${RID}\",\"base_version\":1,\"expires_in_seconds\":3600}")
echo "${PUBLISH}"
SNAP_ID=$(python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshot_id"])' <<<"${PUBLISH}")
TOKEN=$(python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])' <<<"${PUBLISH}")
TOKEN_ID=$(python3 -c 'import sys,json;print(json.load(sys.stdin)["token_id"])' <<<"${PUBLISH}")

echo "== 5. 公开读取（无需服务密钥，只认 Bearer 令牌）=="
curl -sS "${BASE_URL}/v1/public/records" -H "Authorization: Bearer ${TOKEN}"
echo

echo "== 6. 发布后继续修订原文（v2），旧快照不受影响 =="
curl -sS -X PUT "${BASE_URL}/v1/records/versions" "${AUTH[@]}" \
  -d "{\"record_id\":\"${RID}\",\"base_version\":1,\"content\":\"全新版本的原文内容。\",\"ranges\":[[0,4]]}"
echo
curl -sS "${BASE_URL}/v1/public/records" -H "Authorization: Bearer ${TOKEN}"
echo

echo "== 7. 撤销令牌后读取被拒绝（410，不返回正文）=="
curl -sS -X POST "${BASE_URL}/v1/tokens/${TOKEN_ID}/revoke" "${AUTH[@]}"
echo
curl -sS -o /dev/null -w "status=%{http_code}\n" "${BASE_URL}/v1/public/records" \
  -H "Authorization: Bearer ${TOKEN}"
