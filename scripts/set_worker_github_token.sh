#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/workers/slack-events"

echo "Updating Cloudflare Worker secret: GITHUB_TOKEN"
echo "Worker: peachtree-slack-events"
echo ""
echo "Paste your GitHub PAT when prompted (input is hidden)."
echo "Fine-grained PAT: Actions Read and write + Contents Read on PT-Blog-Automation."
echo "Classic PAT: repo scope is enough to dispatch workflows."
echo ""

npx wrangler secret put GITHUB_TOKEN

echo ""
echo "Done. No redeploy needed — the Worker will use the new token on the next dispatch."
