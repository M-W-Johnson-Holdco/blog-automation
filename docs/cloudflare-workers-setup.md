# Cloudflare Workers + GitHub Actions (recommended)

Thin **Cloudflare Worker** receives Slack events, runs the weekly schedule, and dispatches **GitHub Actions** (`weekly.yml` / `slack_approve.yml`). No Mac, no Render bill at low volume.

## Architecture

```text
Scheduled weekly run:
  Cloudflare Worker cron
    → checks for 8:00 AM America/New_York on Monday/Wednesday
    → dispatches weekly.yml
    → draft → Slack → archive generated/ → git push

Slack reviewer reactions / thread replies:
  Cloudflare Worker (/slack/events/<company>)
    → verify signature → HTTP 200 immediately
    → dispatch slack_approve.yml

  slack_approve.yml:
    → git pull
    → scripts/process_slack_event.py (approve_listen handlers)
    → git commit/push generated/ + used_sources.json

Top-level @Bot pipeline:
  Cloudflare Worker (/slack/events/<company>)
    → verify signature → HTTP 200 immediately
    → dispatch weekly.yml for that company

Reviewer 🔁 on a draft intro:
  Cloudflare Worker (/slack/events/<company>)
    → verify signature → HTTP 200 immediately
    → dispatch weekly.yml for that company
```

The Worker does **not** run approve logic — only GitHub Actions does. The scheduled Monday/Wednesday trigger dispatches `weekly.yml` with `company=both`. Manual top-level `@Bot pipeline` and reviewer-added 🔁 dispatch `weekly.yml` for the path's company. GitHub's native `schedule:` block is intentionally disabled so Cloudflare is the source of truth for timing.

`slack_approve.yml` uses a lightweight dependency install for simple approval events. It installs PDF/Cairo packages only for free-form thread feedback, because that path can auto-rewrite a draft and generate a new PDF.

---

## What you need to set up

| # | Where | What |
|---|--------|------|
| 1 | **Cloudflare** | Free account + Worker deploy |
| 2 | **Slack app** | Events API Request URL → Worker URL |
| 3 | **GitHub repo secrets** | Already used by weekly pipeline + `slack_approve.yml` |
| 4 | **GitHub PAT** | For the Worker only (dispatch workflows) |
| 5 | **Push code** | `slack_approve.yml` and `workers/` must be on `main` |

You do **not** need Render, Docker, or `approve_listen listen` on your Mac for this path.

---

## Step 1 — GitHub repository secrets

**Settings → Secrets and variables → Actions** — ensure these exist:

| Secret | Used by |
|--------|---------|
| `TAVILY_API_KEY` | Weekly pipeline |
| `TOGETHER_API_KEY` | Optional Together rollback only |
| `SLACK_APPROVAL_BOT_TOKEN` | Weekly + slack_approve |
| `SLACK_APPROVAL_CHANNEL` | Weekly + slack_approve |
| `PSAI_API_KEY` | 🌐 publish (optional) |
| `TOGETHER_WRITING_MODEL` | Optional override |

`GITHUB_TOKEN` is provided automatically inside Actions jobs (no extra secret for commits).

---

## Step 2 — GitHub PAT for the Worker

Create a **fine-grained PAT** (or classic) with access to this repo:

- **Contents: Read**
- **Actions: Write** (dispatch `slack_approve.yml` and `weekly.yml`)

Save it — you will set it as `GITHUB_TOKEN` on Cloudflare (not in repo secrets).

---

## Step 3 — Deploy the Cloudflare Worker

### 3a. Cloudflare account

1. Sign up at [dash.cloudflare.com](https://dash.cloudflare.com) (free).
2. No paid Workers plan required for ~8 blogs/month.

### 3b. Install Wrangler (one time)

```bash
cd workers/slack-events
npm install
```

### 3c. Configure repo name

Edit `workers/slack-events/wrangler.toml`:

```toml
name = "blog-automation-slack-events"

[triggers]
crons = ["0 * * * 2,4"]

[vars]
GITHUB_REPOSITORY = "M-W-Johnson-Holdco/blog-automation"
GITHUB_REF_NAME = "main"
SLACK_BOT_USER_ID_PEACHTREE = "U..."
SLACK_BOT_USER_ID_TC = "U..."
```

Cloudflare cron is UTC, so the Worker runs hourly on Tuesday/Thursday (cron days `2,4`) and checks `America/New_York` local time in JavaScript. It dispatches `weekly.yml` with `company=both` only when the scheduled time is exactly **8:00 AM ET**, including daylight saving time changes.

Per-company Slack apps post to path-scoped endpoints:

- `/slack/events/peachtree`
- `/slack/events/tc`

The Worker forwards only events that need approval-state work to `slack_approve.yml`:

- reviewer `reaction_added` / `reaction_removed`
- thread reply `message` events (`thread_ts` exists and differs from `ts`)

Top-level `app_mention` events are handled separately: exact `@Bot pipeline` dispatches `weekly.yml` for that path's company. Reviewer-added 🔁 reactions also dispatch `weekly.yml` directly. Threaded `app_mention` events, top-level non-mention channel messages, bot messages, and message edits/deletes are ignored before they create GitHub Actions runs. Per-company `SLACK_BOT_USER_ID_<SLUG>` values skip bot-added prompt reactions at the Worker.

### 3d. Set Worker secrets

```bash
npx wrangler login
npx wrangler secret put SLACK_SIGNING_SECRET_PEACHTREE  # Peachtree Slack app → Basic Information
npx wrangler secret put SLACK_SIGNING_SECRET_TC         # TC Slack app → Basic Information
npx wrangler secret put GITHUB_TOKEN                   # PAT from Step 2
```

To rotate `GITHUB_TOKEN` after regenerating a PAT, run from the repo root (Wrangler prompts for the value; input is hidden):

```bash
bash scripts/set_worker_github_token.sh
```

Or from `workers/slack-events`:

```bash
npm run secret:github-token
```

No redeploy is required after updating a secret — the live Worker picks it up on the next dispatch.

### 3e. Deploy

```bash
npm run deploy
```

Wrangler prints a URL like `https://blog-automation-slack-events.your-subdomain.workers.dev`.

Health check: `https://YOUR-WORKER-URL/health`

---

## Step 4 — Slack app (Events API)

1. [api.slack.com/apps](https://api.slack.com/apps) → Blog Automation app.
2. **Event Subscriptions** → Enable.
3. **Request URL:** `https://YOUR-WORKER-URL/slack/events/peachtree` (or `/slack/events/tc`)  
   Slack sends a challenge; the Worker responds automatically.
4. **Subscribe to bot events:**
   - `reaction_added`
   - `reaction_removed`
   - `message.channels` (or `message.groups` for private channels)
   - `app_mention` (manual `@PT Blog Bot pipeline` in channel)
5. Copy **Signing Secret** if not already set on the Worker.
6. Reinstall app to workspace if you added scopes (`app_mentions:read`).

**Socket Mode:** optional for local dev only. For production, rely on the Worker — do not run `approve_listen listen` at the same time.

---

## Step 5 — Verify end-to-end

1. Push this repo (including `.github/workflows/slack_approve.yml`).
2. **Actions → Weekly Blog Pipeline → Run workflow** (post to Slack on).
3. Confirm `generated/runs/<run_id>/` was committed.
4. React ✅ on the draft in Slack.
5. **Actions → Slack Approval Handler** should start within ~30s.
6. Confirm commit moving draft to `generated/approved/` and updating `used_sources.json`.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Slack URL verification fails | Deploy Worker first; open `/health`; retry Request URL |
| No Actions run after ✅ | Worker logs in Cloudflare dashboard; check `GITHUB_TOKEN` + `GITHUB_REPOSITORY` |
| `@PT Blog Bot pipeline` starts Slack Approval Handler | Redeploy Worker; current Worker should dispatch `weekly.yml` directly |
| 🔁 starts Slack Approval Handler | Redeploy Worker; current Worker should dispatch `weekly.yml` directly |
| Bot-added ✅/❌/🔁 prompt reactions create runs | Set `SLACK_BOT_USER_ID` (or `SLACK_APPROVAL_BOT_USER_ID`) on the Worker |
| Dispatch 404 | `slack_approve.yml` not on `main` yet, or wrong repo in `wrangler.toml` |
| Duplicate handling | Stop local `approve_listen listen` |
| Rewrite timeout | `slack_approve.yml` allows 120 minutes; check `TOGETHER_API_KEY` |

---

## Files

| Path | Role |
|------|------|
| `workers/slack-events/src/index.js` | Slack verify + GitHub dispatch |
| `workers/slack-events/wrangler.toml` | Worker config |
| `.github/workflows/slack_approve.yml` | Runs Python approval |
| `scripts/process_slack_event.py` | CLI entry for Actions |
| `src/blog_automation/slack_actions/processor.py` | Shared event routing |

## Alternative: Render webhook

See [slack-webhook-setup.md](./slack-webhook-setup.md) if you prefer a always-on Docker container instead of Workers + Actions.
