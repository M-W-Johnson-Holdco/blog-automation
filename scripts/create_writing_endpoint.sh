#!/usr/bin/env bash
# One-time helper to create a Qwen 72B dedicated writing endpoint with auto-shutdown.
# After creation, copy the endpoint ID and deployed model name into .env.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODEL="${TOGETHER_DEDICATED_BASE_MODEL:-Qwen/Qwen2.5-72B-Instruct-Turbo}"
DISPLAY_NAME="${TOGETHER_DEDICATED_DISPLAY_NAME:-Peachtree blog writing}"
INACTIVE_TIMEOUT="${TOGETHER_ENDPOINT_INACTIVE_TIMEOUT:-20}"

if ! command -v tg >/dev/null 2>&1; then
  echo "[create_writing_endpoint] Together CLI (tg) not found." >&2
  exit 1
fi

echo "[create_writing_endpoint] Listing hardware for $MODEL"
tg endpoints hardware --model "$MODEL"

read -r -p "Enter hardware ID from the list above: " HARDWARE
if [[ -z "$HARDWARE" ]]; then
  echo "Hardware ID is required." >&2
  exit 1
fi

echo "[create_writing_endpoint] Creating endpoint (auto-stops after ${INACTIVE_TIMEOUT}m idle)"
tg endpoints create \
  --model "$MODEL" \
  --hardware "$HARDWARE" \
  --display-name "$DISPLAY_NAME" \
  --inactive-timeout "$INACTIVE_TIMEOUT" \
  --no-auto-start \
  --wait

echo
echo "Add these values to .env:"
echo "  TOGETHER_DEDICATED_ENDPOINT_ID=<endpoint ID from output above>"
echo "  TOGETHER_WRITING_MODEL=<deployed model name from output above>"
echo
echo "Then run: PYTHONPATH=src python -m blog_automation.pipeline.write_serverless --dedicated-endpoint"
