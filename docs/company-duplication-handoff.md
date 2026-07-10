> **HISTORICAL — superseded.** This describes the old "copy the repo per company" approach. The repo is now multi-tenant; see [multi-tenancy.md](multi-tenancy.md). Kept for reference only.

# Blog Automation — company duplication handoff

Copy everything below the line into a new Cursor chat after opening the **new company’s repo** (duplicated from Peachtree-Roofing-Blog-Automation).

---

## Context

This repo was copied from **Peachtree-Roofing-Blog-Automation**. Same core pipeline:

```text
search (+ evaluate inline) → write_tournament → Slack approval → archive → PSAI publish
```

**Inside `write_tournament`:**

```text
Tavily search → incremental evaluate → write_multi
  → geo / scenario / explainer tournament (or single template when source mix dictates)
  → structural validate → citation/percent checks → LLM fact-audit
  → 235B scorer picks winner
  → output/drafts/ + output/multi_run/<timestamp>/
```

**Source-mode routing** (automatic — do not fight this):

| Kept sources | Template(s) written |
|---|---|
| 2+ local/geo hits | geo + scenario + explainer tournament |
| 1 local + national | `local_anchor` only |
| National/trade only | `industry_insight` only |

**Reuse across companies** (same accounts/keys OK):

- Tavily API key
- Together API key
- Cloudflare account (deploy a **new** Worker per brand)

**Must be separate per brand:**

- GitHub repo + Actions secrets
- Slack app + approval channel + signing secret
- PSAI tenant (`PSAI_API_KEY`, `config/psai.json`)
- Cloudflare Worker name + `GITHUB_REPOSITORY`
- `generated/`, `output/`, `output/sources/used_sources.json`

**Do not run search or pipeline unless I explicitly ask** — Tavily credits cost real money (~100 credits on a full search week; Slack 🔁 repeat triggers another full run).

---

## Goals for this session

1. Rebrand Peachtree → **[COMPANY]** (ask me: legal name, website, PSAI author email, service area).
2. Update brand/geo files (prompts, `write_common.py`, search, `config/psai.json`).
3. Configure **new** Slack app, GitHub secrets, Cloudflare Worker for **this repo only**.
4. Clear Peachtree artifacts from `generated/` and `output/`.
5. Produce a concrete diff plan (grep first) before editing.
6. Test only when I say go.

---

## Files to change (priority)

### A. PSAI publish

| File | Change |
|------|--------|
| `config/psai.json` | `author`, `site_brand`, `default_status` for new PSAI tenant |
| `src/blog_automation/post.py` | `DEFAULT_SITE_BRAND`, tags/keywords if market-specific |
| `.env.template` | Document PSAI vars (no secrets in repo) |

### B. Writing + evaluation prompts

| File | Change |
|------|--------|
| `prompts/blog_geo.txt` | Company name, CTA, service area, voice |
| `prompts/blog_scenario.txt` | Same |
| `prompts/blog_explainer.txt` | Same |
| `prompts/blog_local_anchor.txt` | Same (used when 1 local + national sources) |
| `prompts/blog_industry_insight.txt` | Same (used when national/trade only) |
| `prompts/evaluate.txt` | Evaluator persona + territory for local sources |
| `prompts/evaluate_national_trade.txt` | National-trade evaluator; hard-reject rule for other-state legislation |
| `prompts/fact_audit.txt` | Usually no brand change — accuracy rules are generic; update if company name appears |

Citation rules in all blog prompts: **exactly 3–6** linked cites; when 2+ sources kept, **at least one cite per outlet/domain**.

### C. Validation + generation defaults

| File | What to edit |
|------|----------------|
| `src/blog_automation/write_common.py` | `CTA_SENTENCE`, `DEFAULT_AUTHOR_NAME`, `DEFAULT_AUTHOR_CREDENTIALS`, `METRO_LOCATIONS`, `COMPETITOR_VENDOR_BRANDS`, author byline validation string |
| `src/blog_automation/fact_audit.py` | Only if hardcoding brand strings (currently uses shared `format_sources_block`) |

Automated draft checks (no prompt change needed — know they exist):

- Structural GEO checks (H1, Quick Answer, 8 FAQs, 6+ locations, CTA count, etc.)
- Citation URLs must **exactly match** kept source URLs
- Every `N%` / `N percent` must appear in kept source text
- LLM fact-audit (235B) after structural validation passes

### D. Search + evaluate (geo)

| File | What to edit |
|------|----------------|
| `src/blog_automation/pipeline/search.py` | `STRATEGY_CLUSTERS`, queries, `LOCAL_TERMS`, domain filters, `OUT_OF_MARKET_STATE_TERMS` if market differs |
| `src/blog_automation/pipeline/evaluate.py` | Fallback angle strings if they mention Peachtree/Atlanta |

If the new company serves the **same Metro Atlanta market**, search queries can mostly stay; still replace Peachtree references everywhere.

### E. Cloudflare Worker + GitHub identity

| File | Change |
|------|--------|
| `workers/slack-events/` | Add slug to `VALID_COMPANIES`, `SLACK_BOT_USER_ID_<SLUG>`, and `SLACK_SIGNING_SECRET_<SLUG>` |
| `workers/slack-events/package.json` | `name` field |
| `docs/cloudflare-workers-setup.md`, `docs/slack-webhook-setup.md`, `README.md` | Repo/brand examples |

Python package name `blog_automation` can stay for v1 — renaming is optional cosmetic work.

### F. Reset runtime / CI artifacts (do not share with Peachtree)

| Path | Action |
|------|--------|
| `generated/runs/*` | Delete Peachtree CI archives |
| `generated/slack_index.json` | Reset — must not point at Peachtree Slack threads |
| `generated/weekly_pipeline.json` | Reset |
| `output/` | Do not copy from Peachtree; start fresh |
| `.env` | Create from `.env.template`; **never** copy Peachtree `.env` |

---

## Production pipeline (already wired — verify after rebrand)

**CI (`weekly.yml`)** — current Peachtree production path:

1. `python write_tournament.py --with-search --clear-drafts` (+ optional `--all-queries`, `--include-used-sources`)
2. `python pipeline.py --stage approve_post --no-archive` → Slack PDF + `run.txt` log attachment
3. `python scripts/archive_ci_draft.py` → commit `generated/` for Worker webhook
4. Slack reactions handled by **Cloudflare Worker** → `slack_approve.yml` (not local Socket Mode)

**Local menu (`pipeline.py`)** mirrors CI:

- Search uses `--all-queries --domain-stages` by default
- `--all` runs full tournament + search
- `approve_post` posts to Slack; approval via Worker in production

**Logging:**

- Full CLI transcript: `output/multi_run/<timestamp>/run.log`
- Canonical copy for Slack: `output/drafts/pipeline_run.log` (uploaded to Slack as `run.txt` for preview)
- PDF is **draft content only** (log is a separate Slack attachment)

---

## Together models (defaults + env overrides)

Defined in `src/blog_automation/together_models.py`. Each role retries down its chain on `model_not_available`.

| Role | Default | Env override | Fallback chain |
|------|---------|--------------|----------------|
| Writing | Qwen3.5 397B | `TOGETHER_WRITING_MODEL` | 397B → 235B → GPT-OSS 120B → Llama 3.3 70B |
| Evaluation | Qwen2.5 7B | `TOGETHER_EVALUATION_MODEL` | 7B → 235B → GPT-OSS 120B → Llama 3.3 70B |
| Scorer | Qwen3 235B tput | `TOGETHER_SCORER_MODEL` | 235B → 397B → GPT-OSS 120B → Llama 3.3 70B |
| Fact-audit | Qwen3 235B tput | `TOGETHER_FACT_AUDIT_MODEL` | same as scorer |

`write_multi` does **not** multiply Tavily — one search, up to three writes + scorer + fact-audit calls per template attempt.

**Evaluate:** `KEEP_THRESHOLD = 7.0` (sources below this are rejected unless minimum-kept fallback applies).

---

## GitHub Actions secrets (new on this repo)

| Secret | Notes |
|--------|--------|
| `TAVILY_API_KEY` | Can reuse same key |
| `TOGETHER_API_KEY` | Can reuse same key |
| `TOGETHER_WRITING_MODEL` | Optional override |
| `TOGETHER_EVALUATION_MODEL` | Optional override |
| `TOGETHER_SCORER_MODEL` | Optional override |
| `TOGETHER_FACT_AUDIT_MODEL` | Optional override |
| `SLACK_APPROVAL_BOT_TOKEN` | **New** Slack app bot token |
| `SLACK_APPROVAL_CHANNEL` | **New** channel ID |
| `PSAI_API_KEY` | **New company** PSAI tenant |
| `AUTHOR_NAME` / `AUTHOR_CREDENTIALS` | Optional overrides |

**Cloudflare Worker secrets** (`wrangler secret put`):

- `SLACK_SIGNING_SECRET` — from **new** Slack app
- `GITHUB_TOKEN` — PAT with `contents:write` + `actions:write` on **this repo**

---

## Slack setup (new app — not Peachtree bot)

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App**.
2. Create `#[company]-blog-approval`, invite the new bot.
3. **Event Subscriptions** → Request URL = `https://blog-automation-slack-events.<subdomain>.workers.dev/slack/events/[company]`
4. Bot events: `reaction_added`, `reaction_removed`, `message.channels`
5. Scopes: `chat:write`, `reactions:read`, `reactions:write`, `files:write`, channel history as needed.

Reactions (via Worker → `slack_approve.yml`):

- ✅ approve → PSAI publish
- 🔁 repeat → re-dispatches `weekly.yml` (~100 Tavily credits)
- 💬 thread reply → editorial feedback + optional rewrite

---

## Cloudflare Worker deploy

```bash
cd workers/slack-events
npm install
# Edit wrangler.toml first (name + GITHUB_REPOSITORY)
npx wrangler secret put SLACK_SIGNING_SECRET_<SLUG>
npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```

---

## Regression tests (run after rebrand edits)

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

| Test file | Covers |
|-----------|--------|
| `tests/test_search_relevance.py` | Arson off-topic, Fox national-domain false positives |
| `tests/test_draft_validation.py` | Citation URL match, percentage grounding |
| `tests/test_together_models.py` | Per-role fallback chains, fact-audit JSON parse |

---

## Suggested task order for the agent

1. **Ask me first:**
   - Exact brand name + website
   - Service area (same Metro Atlanta or different?)
   - PSAI author email + API key ready?
   - Slack channel name preference?
2. Grep for `Peachtree`, `peachtree`, `Metro Atlanta`, `Peachtree Roofing` — produce a concrete diff plan **before** editing.
3. Update `config/psai.json`, prompts, `write_common.py`, Worker `wrangler.toml`, README.
4. Clear `generated/` and `output/` Peachtree artifacts.
5. Hand me a checklist of secrets + Slack + Worker steps to complete manually.
6. Run tests / pipeline **only when I say go**.

---

## What I need from you (human) before going live

- [ ] New company PSAI API key + author email
- [ ] New Slack app + channel ID
- [ ] GitHub secrets set on new repo
- [ ] Cloudflare Worker deployed + Event URL verified in Slack
- [ ] Fresh `.env` from `.env.template`
- [ ] Confirm service area / location allowlist if not Metro Atlanta

---

## Quick reference commands (after setup)

```bash
# Full local run (expensive — ask first)
python write_tournament.py --with-search --clear-drafts --all-queries

# Stage by stage
python pipeline.py --stage search
python pipeline.py --stage write_tournament --with-search --clear-drafts
python pipeline.py --stage approve_post

# Manual CI
gh workflow run weekly.yml --field send_to_slack=true
```
