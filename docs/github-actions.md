# GitHub Actions automation

This repo uses GitHub Actions for **scheduled draft generation** and **manual website publishing**. Slack **approval reactions** can run on your Mac (Socket Mode) or on a **hosted webhook** — see [slack-webhook-setup.md](./slack-webhook-setup.md).

## Workflows

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| **Weekly Blog Pipeline** (`weekly.yml`) | Mon + Wed 8 AM ET (cron) or manual | Mon: search → evaluate → write → Slack. Writing template auto-rotates by ISO week (`geo` → `scenario` → `explainer`). Wed retry if no draft (widened search); Slack on Mon failure and final alert if Wed also fails |
| **Slack Approval Handler** (`slack_approve.yml`) | Dispatched by Cloudflare Worker | Processes Slack ✅/🌐/feedback via Python; commits `generated/` |
| **Publish to Website** (`publish.yml`) | Manual only | `POST /v1/blogs` to PSAI for an approved draft |
| **Approval Webhook** (`approve.yml`) | Manual only | Legacy alias of `publish.yml` with `decision: approve \| revise` |

## Architecture

```text
Monday 8 AM (GitHub Actions)
        ↓
search → evaluate → write
        ↓
Post draft + PDF to Slack (#blog-approvals)
        ↓
If Mon fails → Slack notice + Wed 8 AM retry (--all-queries)
        ↓
If Wed retry also fails → one Slack “no draft this week” message
        ↓
React ✅ approve · reply publish (when PSAI platform is ready)
        ↓
Optional: Actions → Publish to Website (manual backup)
```

**Option A — Local (Socket Mode)**  
GitHub Actions **cannot** run the Slack Socket Mode listener — it is a long-lived process. After the weekly job posts to Slack, start the listener on your Mac:

```bash
conda run -n blog-automation python -m blog_automation.pipeline.approve_listen listen
```

**Option B — Cloud webhook (no Mac)**  
Deploy the **Cloudflare Worker** + `slack_approve.yml` (recommended, free at low volume): **[cloudflare-workers-setup.md](./cloudflare-workers-setup.md)**.  
Or use the **Render FastAPI webhook**: **[slack-webhook-setup.md](./slack-webhook-setup.md)**.

Without either listener, the weekly job still posts drafts to Slack, but reactions will not approve or publish.

## Required repository secrets

Add at **Settings → Secrets and variables → Actions → New repository secret**.

### Weekly pipeline (required)

| Secret | Used for |
|--------|----------|
| `TAVILY_API_KEY` | News search |
| `TOGETHER_API_KEY` | Optional (Together rollback only) |
| `SLACK_APPROVAL_BOT_TOKEN` | Post draft to Slack (`xoxb-…`) |
| `SLACK_APPROVAL_CHANNEL` | Channel ID — or move to a config file later |

Author bylines use code defaults (`Jonathan Gil`, etc.) unless you set optional `AUTHOR_NAME` / `AUTHOR_CREDENTIALS` secrets.

### PSAI publish (optional)

| Secret | Used for |
|--------|----------|
| `PSAI_API_KEY` | Globe reaction + publish workflows |

`api_url`, `author`, `default_status`, and other PSAI settings are in **`config/psai.json`** (not secrets).

### Optional

| Secret | Used for |
|--------|----------|
| `TOGETHER_WRITING_MODEL` | Override default Qwen model |

### Slack listener (local only — not Actions secrets)

| Variable | Where |
|----------|--------|
| `SLACK_APPROVAL_TOKEN` | `.env` only (`xapp-…` Socket Mode) |

Do **not** put the Socket Mode app token in GitHub unless you later run a hosted listener.

### Slack webhook (cloud — host env vars, not Actions secrets)

| Variable | Used for |
|----------|----------|
| `SLACK_SIGNING_SECRET` | Verify Slack Events API requests |
| `GITHUB_TOKEN` | Pull/push `generated/` and `used_sources.json` |
| `GITHUB_REPOSITORY` | e.g. `owner/repo` |

Setup: [slack-webhook-setup.md](./slack-webhook-setup.md).

## Pushing workflow files

OAuth apps (including some IDE git integrations) block pushes that change `.github/workflows/` without the **`workflow`** scope. Push from Terminal:

```bash
gh auth login   # grant workflow scope
git push -u origin your-branch
```

Or use a Personal Access Token with **workflow** checked.

## After a local Slack approval

Approving in Slack updates `output/sources/used_sources.json` on your machine. **Commit and push** that file so the next weekly run skips those story URLs:

```bash
git add output/sources/used_sources.json
git commit -m "Record used sources from approved blog"
git push
```

## Manual test before enabling the schedule

1. **Actions → Weekly Blog Pipeline → Run workflow** (leave “Post to Slack” checked).
2. Confirm a new message in `#blog-approvals`.
3. Locally: `approve_listen listen` → approve with ✅.
4. When PSAI platform shows drafts: **Actions → Publish to Website** with the validation JSON path, or react 🌐 in Slack.

## Disable the Monday schedule

Edit `weekly.yml` and remove or comment the `schedule:` block, or disable the workflow under **Actions → Weekly Blog Pipeline → … → Disable workflow**.
