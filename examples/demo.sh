#!/usr/bin/env bash
# 端到端调用示例：创建 -> 并发保护(409) -> 修订 -> 发布 -> 公开读取 -> 撤销 -> 过期
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 无服务密钥 -> 401 整次拒绝"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE_URL}/records" \
  -H "Content-Type: application/json" -d '{"text":"x","intervals":[]}'

step "2. 创建记录（版本 1）"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
echo "${CREATE}" | jq .
RECORD_ID=$(echo "${CREATE}" | jq -r .record_id)

step "3. 越界区间 -> 422 整次拒绝"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" \
  -d '{"base_version": 1, "text": "短文本", "intervals": [{"start": 0, "end": 99}]}' | jq .

step "4. 过期基准版本 -> 409 并返回当前版本"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" \
  -d '{"base_version": 99, "text": "x", "intervals": []}' | jq .

step "5. 正常修订（版本 2，区间相交/相邻将在发布时合并）"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [
    {"start": 2, "end": 4}, {"start": 4, "end": 5},
    {"start": 10, "end": 28}, {"start": 20, "end": 30},
    {"start": 34, "end": 45}
  ]
}' | jq .

step "6. 发布版本 2 -> 不可变快照 + 随机读取令牌"
PUB=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" \
  -d '{"version": 2}')
echo "${PUB}" | jq .
SNAPSHOT_ID=$(echo "${PUB}" | jq -r .snapshot_id)
TOKEN=$(echo "${PUB}" | jq -r .token)

step "7. 公开读取（合并后的脱敏正文，不含原文）"
curl -sS "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}" | jq .

step "8. 再次修订到版本 3，旧快照不受影响"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" \
  -d '{"base_version": 2, "text": "完全不同的新原文，敏感词ABC。", "intervals": [{"start": 9, "end": 15}]}' | jq .
curl -sS "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}" | jq .

step "9. 维护侧查看记录状态"
curl -sS "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" | jq .

step "10. 撤销令牌后读取 -> 403"
curl -sS -X POST "${BASE_URL}/tokens/${TOKEN}/revoke" "${AUTH[@]}" | jq .
curl -sS "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}" | jq .

step "11. 带 2 秒到期时间的令牌，到期后读取 -> 403"
PUB2=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" \
  -d '{"version": 2, "expires_in_seconds": 2}')
TOKEN2=$(echo "${PUB2}" | jq -r .token)
echo "（等待 3 秒让令牌过期…）"
sleep 3
curl -sS "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN2}" | jq .

printf '\n\033[1m演示完成。\033[0m\n'
