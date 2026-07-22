#!/bin/zsh
set -euo pipefail

APP_URL="${APP_URL:-http://127.0.0.1:4001}"
MOCK_URL="${MOCK_URL:-http://127.0.0.1:4100}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-LocalTest123}"

cookie_file="$(mktemp /private/tmp/ccmax-e2e-cookie.XXXXXX)"
trap 'rm -f "$cookie_file"' EXIT

assert_jq() {
  local json="$1"
  local expression="$2"
  local message="$3"
  if ! print -r -- "$json" | jq -e "$expression" >/dev/null; then
    print -u2 -- "FAIL: $message"
    print -u2 -- "$json"
    exit 1
  fi
}

curl -fsS "$MOCK_URL/healthz" >/dev/null
curl -fsS "$APP_URL/api/health" >/dev/null

login_response="$(curl -fsS -c "$cookie_file" -X POST "$APP_URL/api/admin/auth/login" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg username "$ADMIN_USER" --arg password "$ADMIN_PASSWORD" '{username:$username,password:$password}')")"
assert_jq "$login_response" '.data.username == "admin"' 'admin login failed'

order_no="E2E-MOCK-$(date +%Y%m%d-%H%M%S)"
generate_response="$(curl -fsS -b "$cookie_file" -X POST "$APP_URL/api/admin/chatgpt-cdks/generate" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg orderNo "$order_no" '{sku:"pro",quantity:1,format:"uuid",orderNo:$orderNo,remark:"mock end-to-end test"}')")"
assert_jq "$generate_response" '.data.count == 1 and (.data.items[0].code | length == 36)' 'CDK generation failed'
cdk="$(print -r -- "$generate_response" | jq -r '.data.items[0].code')"

check_response="$(curl -fsS -X POST "$APP_URL/api/chatgpt/redeem/check" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg code "$cdk" '{code:$code}')")"
assert_jq "$check_response" '.data.available == true and .data.sku == "pro"' 'CDK validation failed'

email="e2e-mock@example.com"
claims="$(jq -nc --arg email "$email" '{"https://api.openai.com/profile":{email:$email}}')"
claims_b64="$(print -rn -- "$claims" | base64 | tr -d '=\n' | tr '/+' '_-')"
access_token="e30.${claims_b64}.mock-signature"
session="$(jq -nc --arg token "$access_token" '{accessToken:$token,userId:"e2e-user",authProvider:"openai"}')"

submit_response="$(curl -fsS -X POST "$APP_URL/api/chatgpt/redeem/submit" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg code "$cdk" --arg session "$session" '{code:$code,channel:"official",session:$session}')")"
assert_jq "$submit_response" '.data.taskId | startswith("ctk_")' 'submit did not return a local Hash task ID'
if print -r -- "$submit_response" | rg -q 'rdm_mock_|127\.0\.0\.1:4100'; then
  print -u2 -- 'FAIL: submit response exposed upstream implementation details'
  exit 1
fi
task_hash="$(print -r -- "$submit_response" | jq -r '.data.taskId')"

statuses=()
for poll in 1 2 3; do
  poll_response="$(curl -fsS -X POST "$APP_URL/api/chatgpt/redeem/task" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg code "$cdk" --arg taskId "$task_hash" '{code:$code,taskId:$taskId}')")"
  if print -r -- "$poll_response" | rg -q 'rdm_mock_|127\.0\.0\.1:4100'; then
    print -u2 -- 'FAIL: polling response exposed upstream implementation details'
    exit 1
  fi
  statuses+=("$(print -r -- "$poll_response" | jq -r '.data.status')")
done
if [[ "${(j:,:)statuses}" != "pending,processing,success" ]]; then
  print -u2 -- "FAIL: unexpected status sequence ${(j:,:)statuses}"
  exit 1
fi

task_list="$(curl -fsS -b "$cookie_file" --get "$APP_URL/api/admin/chatgpt-tasks" --data-urlencode "q=$task_hash")"
assert_jq "$task_list" ".data.total == 1 and .data.items[0].hashId == \"$task_hash\" and .data.items[0].userEmail == \"$email\" and .data.items[0].status == \"success\"" 'local task record is incomplete'
local_task_id="$(print -r -- "$task_list" | jq -r '.data.items[0].id')"

task_detail="$(curl -fsS -b "$cookie_file" "$APP_URL/api/admin/chatgpt-tasks/$local_task_id")"
assert_jq "$task_detail" '.data.task.remoteTaskId | startswith("rdm_mock_")' 'remote task ID was not retained for admin diagnostics'
assert_jq "$task_detail" '.data.session | fromjson | .authProvider == "openai"' 'full Session was not retained locally'

cdk_list="$(curl -fsS -b "$cookie_file" --get "$APP_URL/api/admin/chatgpt-cdks" --data-urlencode "q=$cdk")"
assert_jq "$cdk_list" ".data.total == 1 and .data.items[0].status == \"used\" and .data.items[0].taskId == $local_task_id" 'CDK was not linked to the local task'

failed_generate="$(curl -fsS -b "$cookie_file" -X POST "$APP_URL/api/admin/chatgpt-cdks/generate" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg orderNo "${order_no}-FAILED" '{sku:"plus",quantity:1,format:"uuid",orderNo:$orderNo,remark:"mock failed terminal test"}')")"
assert_jq "$failed_generate" '.data.count == 1' 'failed-path CDK generation failed'
failed_cdk="$(print -r -- "$failed_generate" | jq -r '.data.items[0].code')"
failed_session="$(jq -nc --arg token "$access_token" '{accessToken:$token,userId:"e2e-user",authProvider:"openai",mockResult:"failed"}')"
failed_submit="$(curl -fsS -X POST "$APP_URL/api/chatgpt/redeem/submit" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg code "$failed_cdk" --arg session "$failed_session" '{code:$code,channel:"official",session:$session}')")"
assert_jq "$failed_submit" '.data.taskId | startswith("ctk_")' 'failed-path submit did not return a local Hash task ID'
failed_task_hash="$(print -r -- "$failed_submit" | jq -r '.data.taskId')"
for poll in 1 2 3; do
  failed_poll="$(curl -fsS -X POST "$APP_URL/api/chatgpt/redeem/task" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg code "$failed_cdk" --arg taskId "$failed_task_hash" '{code:$code,taskId:$taskId}')")"
done
assert_jq "$failed_poll" '.data.status == "failed" and .data.error.code == "MOCK_UPGRADE_FAILED"' 'failed-path task did not reach failed terminal state'
if print -r -- "$failed_submit$failed_poll" | rg -q 'rdm_mock_|127\.0\.0\.1:4100'; then
  print -u2 -- 'FAIL: failed-path response exposed upstream implementation details'
  exit 1
fi
failed_task_list="$(curl -fsS -b "$cookie_file" --get "$APP_URL/api/admin/chatgpt-tasks" --data-urlencode "q=$failed_task_hash")"
assert_jq "$failed_task_list" ".data.total == 1 and .data.items[0].hashId == \"$failed_task_hash\" and .data.items[0].status == \"failed\"" 'failed local task record is incomplete'
failed_local_task_id="$(print -r -- "$failed_task_list" | jq -r '.data.items[0].id')"
failed_cdk_list="$(curl -fsS -b "$cookie_file" --get "$APP_URL/api/admin/chatgpt-cdks" --data-urlencode "q=$failed_cdk")"
assert_jq "$failed_cdk_list" ".data.total == 1 and .data.items[0].status == \"used\" and .data.items[0].taskId == $failed_local_task_id" 'failed-path CDK was not linked to the local task'

jq -n \
  --arg cdk "$cdk" \
  --arg taskId "$task_hash" \
  --arg email "$email" \
  --arg statuses "${(j: -> :)statuses}" \
  --arg failedTaskId "$failed_task_hash" \
  --argjson localTaskId "$local_task_id" \
  --argjson failedLocalTaskId "$failed_local_task_id" \
  '{success:true,cdk:$cdk,publicTaskId:$taskId,localTaskId:$localTaskId,userEmail:$email,statuses:$statuses,finalStatus:"success",failedPath:{publicTaskId:$failedTaskId,localTaskId:$failedLocalTaskId,finalStatus:"failed"}}'
