# Slack webhook (cloud approval)

Use a **hosted Slack Events API webhook** so ✅ approve, 🌐 publish, and thread feedback work **without** keeping your Mac running `approve_listen listen`.

## How it fits together

```text
Monday 8 AM — GitHub Actions (weekly.yml)
        ↓
search → evaluate → write → post draft to Slack
        ↓
archive draft → generated/runs/<run_id>/ → git commit + push
        ↓
Cloud webhook (always on) receives Slack reactions/messages
        ↓
git pull → approve / publish → git push (generated/ + used_sources.json)
```

The webhook **replaces** Socket Mode locally. You do not run both for the same Slack app unless you know what you are doing (duplicate handling).

## Prerequisites

1. **Weekly pipeline working** — see [github-actions.md](./github-actions.md).
2. **Slack app** — same “Blog Automation” app that posts drafts (`SLACK_APPROVAL_BOT_TOKEN`).
3. **GitHub PAT or fine-grained token** with `contents: write` and `actions: write` (for `:repeat:` restart).
4. **Host** — Render, Railway, Fly.io, or any container platform with a public HTTPS URL.

## Step 1 — Slack app: Events API

1. Open [api.slack.com/apps](https://api.slack.com/apps) → your Blog Automation app.
2. **Socket Mode** — you can leave enabled for local dev, but production approval should use the webhook URL below.
3. **Event Subscriptions** → Enable Events.
4. **Request URL**: `https://YOUR-HOST/slack/events`  
   Slack sends a `url_verification` challenge; the app responds automatically once deployed.
5. Subscribe to **bot events**:
   - `reaction_added`
   - `reaction_removed`
   - `message.channels` (or `message.groups` if private channel)
   - `app_mention` (manual `@PT Blog Bot pipeline` in channel)
6. **OAuth & Permissions** — ensure the bot has scopes used by approval (e.g. `reactions:read`, `chat:write`, `app_mentions:read`, `files:read`, channels history as needed).
7. **Basic Information** → copy **Signing Secret** → `SLACK_SIGNING_SECRET`.

Reinstall the app to the workspace if you added scopes.

## Step 2 — GitHub token for the webhook

Create a **fine-grained PAT** (or classic PAT) for the repo:

| Scope | Why |
|-------|-----|
| **Contents: Read and write** | Pull `generated/` after CI; push approval state |
| **Actions: Write** | `:repeat:` triggers `weekly.yml` |

Set on the host (not necessarily Actions secrets):

- `GITHUB_TOKEN` — the PAT
- `GITHUB_REPOSITORY` — e.g. `your-org/Peachtree-Roofing-Blog-Automation`
- `GITHUB_REF_NAME` — branch to pull/push (default `main`)

## Step 3 — Environment variables on the host

| Variable | Required | Notes |
|----------|----------|--------|
| `SLACK_SIGNING_SECRET` | Yes | Verifies requests from Slack |
| `SLACK_APPROVAL_BOT_TOKEN` | Yes | `xoxb-…` |
| `SLACK_APPROVAL_CHANNEL` | Yes | Channel ID (optional if only in validation JSON) |
| `PSAI_API_KEY` | For 🌐 publish | Same as Actions |
| `TOGETHER_API_KEY` | For thread rewrites | Cloud sets `auto_rewrite=False` by default; feedback still works if you enable rewrite |
| `GITHUB_TOKEN` | Yes (cloud) | Repo sync |
| `GITHUB_REPOSITORY` | Yes (cloud) | `owner/repo` |
| `GITHUB_REF_NAME` | No | Default `main` |
| `PORT` | No | Default `8080` |

Do **not** need `SLACK_APPROVAL_TOKEN` (Socket Mode) on the host.

PSAI non-secrets stay in `config/psai.json` in the repo (baked into the image at build time; `git pull` refreshes).

## Step 4 — Deploy (Render example)

1. **New → Web Service** → connect this GitHub repo.
2. **Root directory**: repo root.
3. **Dockerfile path**: `webhook/Dockerfile` (or use native Python: install `requirements.txt`, start command below).
4. **Health check path**: `/health`
5. Add env vars from Step 3.
6. Deploy → copy the public URL → paste into Slack **Request URL** (Step 1).

**Start command** (non-Docker):

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m blog_automation.slack_webhook.app
```

**Local Docker test**:

```bash
cd webhook
docker compose up --build
# Slack Request URL: https://YOUR-NGROK-URL/slack/events  (use ngrok for local)
```

## Step 5 — Enable CI archive (already in weekly.yml)

After each Slack post, Actions runs:

1. `scripts/archive_ci_draft.py` — copies the draft into `generated/runs/<run_id>/` and updates `generated/slack_index.json`.
2. Commits and pushes `generated/` so the webhook can `git pull` and find drafts by Slack message timestamp.

Run **Weekly Blog Pipeline** once after merging this change to seed the first archived run.

## Step 6 — Turn off the Mac listener

When the webhook is live and Request URL verifies:

```bash
# Stop local Socket Mode (no longer needed for approval)
# conda run -n blog-automation python -m blog_automation.pipeline.approve_listen listen
```

Use Slack reactions as before:

| Reaction / command | Action |
|----------|--------|
| ✅ | Approve → moves draft to `generated/approved/`, updates `used_sources.json`, pushes to GitHub |
| 🌐 | Publish to PSAI (when platform ready) |
| ❌ / thread reply | Request feedback |
| 🔁 `:repeat:` | Triggers **Weekly Blog Pipeline** in GitHub Actions |
| `@PT Blog Bot pipeline` (top-level in approval channel) | Same as 🔁 — queues **Weekly Blog Pipeline** |

## Troubleshooting

| Issue | Check |
|-------|--------|
| Slack “URL didn’t respond” | Host up? `/health` returns `{"status":"ok"}`? |
| 401 on events | `SLACK_SIGNING_SECRET` matches app |
| ✅ does nothing | Webhook logs; CI committed `generated/`? Webhook `git pull` succeeding? |
| Duplicate handling | Disable Socket Mode listener on Mac while webhook is active |
| Push fails from webhook | PAT has `contents: write`; `GITHUB_REPOSITORY` correct |

## Files

| Path | Role |
|------|------|
| `src/blog_automation/slack_webhook/app.py` | FastAPI `/slack/events`, `/health` |
| `src/blog_automation/slack_webhook/events.py` | Reaction/message routing |
| `src/blog_automation/slack_webhook/github_sync.py` | Pull/push `generated/` and `used_sources.json` |
| `src/blog_automation/generated_store.py` | Archive + Slack message index |
| `scripts/archive_ci_draft.py` | CI archive step |
| `webhook/Dockerfile` | Container image |

See also [github-actions.md](./github-actions.md) for secrets and weekly schedule.
