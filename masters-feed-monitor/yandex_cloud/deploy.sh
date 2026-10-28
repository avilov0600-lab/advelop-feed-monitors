#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/deploy.env" ]; then
  # Contains resource IDs and the Telegram chat ID, never the bot token.
  source "${SCRIPT_DIR}/deploy.env"
fi

# Required, non-secret deployment settings.
: "${YC_FOLDER_ID:?Set YC_FOLDER_ID}"
: "${YC_STATE_BUCKET:?Set a globally unique YC_STATE_BUCKET name}"
: "${YC_TELEGRAM_SECRET_ID:?Set the Lockbox secret ID}"
: "${YC_TELEGRAM_SECRET_VERSION_ID:?Set the Lockbox secret version ID}"
: "${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID}"
: "${TELEGRAM_WEBHOOK_URL:?Set the public Telegram relay URL}"

FUNCTION_NAME="${FUNCTION_NAME:-masters-feed-monitor}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-masters-feed-monitor}"
MONITOR_TRIGGER_NAME="${MONITOR_TRIGGER_NAME:-masters-feed-monitor-2h}"
DIGEST_TRIGGER_NAME="${DIGEST_TRIGGER_NAME:-masters-feed-monitor-daily}"
# Keep existing trigger names so an update does not leave old timers behind.
# Alternative only: Moscow 09:00, 13:00, 15:30, 18:30.
MONITOR_CRON="${MONITOR_CRON:-30 12,15 ? * * *}"
DIGEST_CRON="${DIGEST_CRON:-0 6,10 ? * * *}"

PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -n "${YC_BIN:-}" ]; then
  YC_COMMAND="${YC_BIN}"
elif command -v yc >/dev/null 2>&1; then
  YC_COMMAND="$(command -v yc)"
elif [ -x "${PROJECT_DIR}/.tools/yandex-cloud/bin/yc" ]; then
  YC_COMMAND="${PROJECT_DIR}/.tools/yandex-cloud/bin/yc"
else
  echo "Yandex Cloud CLI (yc) is not installed." >&2
  exit 1
fi

BUILD_DIR="$(mktemp -d /tmp/masters-feed-monitor.XXXXXX)"
PACKAGE_PATH="${BUILD_DIR}/masters-feed-monitor.zip"
trap 'rm -rf "${BUILD_DIR}"' EXIT

cp "${PROJECT_DIR}/monitor.py" "${BUILD_DIR}/monitor.py"
cp "${PROJECT_DIR}/yandex_cloud/index.py" "${BUILD_DIR}/index.py"
(
  cd "${BUILD_DIR}"
  zip -q "${PACKAGE_PATH}" monitor.py index.py
)

"${YC_COMMAND}" config set folder-id "${YC_FOLDER_ID}"

if ! SERVICE_ACCOUNT_JSON="$("${YC_COMMAND}" iam service-account get --name "${SERVICE_ACCOUNT_NAME}" --format json 2>/dev/null)"; then
  SERVICE_ACCOUNT_JSON="$("${YC_COMMAND}" iam service-account create --name "${SERVICE_ACCOUNT_NAME}" --format json)"
fi
SERVICE_ACCOUNT_ID="$(printf '%s' "${SERVICE_ACCOUNT_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

if ! "${YC_COMMAND}" storage bucket get --name "${YC_STATE_BUCKET}" >/dev/null 2>&1; then
  "${YC_COMMAND}" storage bucket create --name "${YC_STATE_BUCKET}"
fi

"${YC_COMMAND}" resource-manager folder add-access-binding \
  --id "${YC_FOLDER_ID}" \
  --role storage.uploader \
  --subject "serviceAccount:${SERVICE_ACCOUNT_ID}"
"${YC_COMMAND}" lockbox secret add-access-binding "${YC_TELEGRAM_SECRET_ID}" \
  --role lockbox.payloadViewer \
  --subject "serviceAccount:${SERVICE_ACCOUNT_ID}"

if ! FUNCTION_JSON="$("${YC_COMMAND}" serverless function get --name "${FUNCTION_NAME}" --format json 2>/dev/null)"; then
  FUNCTION_JSON="$("${YC_COMMAND}" serverless function create --name "${FUNCTION_NAME}" --format json)"
fi
FUNCTION_ID="$(printf '%s' "${FUNCTION_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

"${YC_COMMAND}" serverless function add-access-binding "${FUNCTION_ID}" \
  --role functions.functionInvoker \
  --subject "serviceAccount:${SERVICE_ACCOUNT_ID}"
"${YC_COMMAND}" serverless function allow-unauthenticated-invoke "${FUNCTION_ID}"

"${YC_COMMAND}" serverless function version create \
  --function-id "${FUNCTION_ID}" \
  --runtime python312 \
  --entrypoint index.handler \
  --memory 512m \
  --execution-timeout 180s \
  --source-path "${PACKAGE_PATH}" \
  --service-account-id "${SERVICE_ACCOUNT_ID}" \
  --environment "TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID},MONITOR_NOTIFY_AFTER=2" \
  --secret "environment-variable=TELEGRAM_BOT_TOKEN,id=${YC_TELEGRAM_SECRET_ID},version-id=${YC_TELEGRAM_SECRET_VERSION_ID},key=telegram_bot_token" \
  --mount "type=object-storage,mount-point=state,bucket=${YC_STATE_BUCKET},prefix=masters-feed-monitor,mode=rw"

upsert_timer() {
  local trigger_name="$1"
  local cron_expression="$2"
  local payload="$3"
  local trigger_json
  local trigger_id
  if trigger_json="$("${YC_COMMAND}" serverless trigger get \
    --name "${trigger_name}" --format json 2>/dev/null)"; then
    trigger_id="$(printf '%s' "${trigger_json}" | \
      python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
    "${YC_COMMAND}" serverless trigger update timer --id "${trigger_id}" \
      --new-cron-expression "${cron_expression}" \
      --new-payload "${payload}" \
      --new-invoke-function-id "${FUNCTION_ID}" \
      --new-invoke-function-service-account-id "${SERVICE_ACCOUNT_ID}"
  else
    "${YC_COMMAND}" serverless trigger create timer \
      --name "${trigger_name}" \
      --cron-expression "${cron_expression}" \
      --payload "${payload}" \
      --invoke-function-id "${FUNCTION_ID}" \
      --invoke-function-service-account-id "${SERVICE_ACCOUNT_ID}"
  fi
}

upsert_timer "${MONITOR_TRIGGER_NAME}" "${MONITOR_CRON}" '{"mode":"digest"}'
upsert_timer "${DIGEST_TRIGGER_NAME}" "${DIGEST_CRON}" '{"mode":"digest"}'

WEBHOOK_URL="${TELEGRAM_WEBHOOK_URL}"
"${YC_COMMAND}" serverless function invoke \
  --id "${FUNCTION_ID}" \
  --data "{\"mode\":\"configure_webhook\",\"url\":\"${WEBHOOK_URL}\"}" \
  >/dev/null

echo "Deployment complete. Function ID: ${FUNCTION_ID}"
echo "Digest schedules (UTC): ${MONITOR_CRON}; ${DIGEST_CRON}."
echo "Telegram webhook: ${WEBHOOK_URL}"
