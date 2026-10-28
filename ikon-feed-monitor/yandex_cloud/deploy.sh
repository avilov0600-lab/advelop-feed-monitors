#!/usr/bin/env bash
# Update only the existing IKON replay function. No feeds, timers or MASTERS writes.
set -euo pipefail

: "${YC_BIN:=yc}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$(mktemp -d /tmp/ikon-telegram-replay.XXXXXX)"
PACKAGE_PATH="${BUILD_DIR}/handler.zip"
trap 'rm -f "${PACKAGE_PATH}"; rmdir "${BUILD_DIR}"' EXIT

cd "${PROJECT_DIR}"
zip -q "${PACKAGE_PATH}" monitor.py config.py yandex_cloud/__init__.py yandex_cloud/index.py

"${YC_BIN}" serverless function version create \
  --function-id d4e7q4tsbnf8q795rj2h \
  --runtime python312 \
  --entrypoint yandex_cloud.index.handler \
  --memory 256m \
  --execution-timeout 15s \
  --source-path "${PACKAGE_PATH}" \
  --service-account-id ajelc58hk55ne80kdolg \
  --secret environment-variable=TELEGRAM_BOT_TOKEN,id=e6q7f76acilc6e8c17em,version-id=e6q7qammsmdcmpep12im,key=telegram_bot_token \
  --mount type=object-storage,mount-point=state,bucket=ikon-telegram-state-b1ga13crehsadbl3uf50,prefix=latest,mode=rw \
  --concurrency 1 \
  --min-log-level error

"${YC_BIN}" serverless function set-scaling-policy \
  --id d4e7q4tsbnf8q795rj2h --tag '$latest' \
  --zone-instances-limit 1 --zone-requests-limit 1 --provisioned-instances-count 0
