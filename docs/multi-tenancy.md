# Multi-tenancy

One codebase drives multiple roofing-company blogs. This replaces the old
"duplicate the repo per company" workflow (see `company-duplication-handoff.md`,
now historical).

## How company selection works

- Pick a company with `--company peachtree|tc` on `pipeline.py` /
  `write_tournament.py`, or set `COMPANY=<slug>` in the environment.
- Both entry points read `--company` **before** importing any
  `blog_automation` module and set `COMPANY`, because the active profile binds
  at import time (`blog_automation.company.get_profile()`).
- Default when unset: `peachtree`.
- **One process = one company.** The profile is cached per process; don't try
  to switch companies mid-run. CI runs a separate job per company; the shared
  Cloudflare Worker routes by `/slack/events/<slug>`.

## What is shared vs. per-company

| Shared (one copy) | Per-company |
|---|---|
| All pipeline logic in `src/blog_automation/` | `src/blog_automation/companies/<slug>.py` profile |
| `prompts/*.txt` (templates with `{{placeholders}}`) | `PROMPT_VARS` in each profile fills the templates |
| Tests | `config/psai.<slug>.json` (PSAI tenant) |
| Workflows / Worker code | `feedback/<slug>/style_notes.txt` |
| — | `output/<slug>/`, `generated/<slug>/` (runtime, isolated) |
| — | GitHub Environment secrets (`peachtree`, `tc`) |
| — | Cloudflare Worker path `/slack/events/<slug>` + signing secret |

Credentials stay isolated per company: separate Slack apps, separate Tavily
accounts, separate PSAI tenants. Shared keys (Anthropic, Together) live at the
GitHub repo level.

## Adding a new company

1. `cp src/blog_automation/companies/peachtree.py src/blog_automation/companies/<slug>.py`
   and edit every constant (identity, `SEARCH_*` geo data, `METRO_LOCATIONS`,
   `OUTLET_NAME_BY_DOMAIN_MARKER`, `EVALUATE_FALLBACK_ANGLES`, `PROMPT_VARS`,
   `CTA_SENTENCE`, `BYLINE_SERVICE_SENTENCE`, tags/keywords, etc.).
2. Add `<slug>` to `VALID_COMPANIES` in `company.py`.
3. `cp config/psai.peachtree.json config/psai.<slug>.json` and set author +
   site_brand.
4. `mkdir feedback/<slug>` and add a `style_notes.txt`.
5. `mkdir -p generated/<slug>/{runs,approved}`, seed `slack_index.json`
   (`{"messages": {}}`) and `weekly_pipeline.json` (`{}`).
6. Add the slug to the matrix in `.github/workflows/weekly.yml`, the `company`
   choice inputs in `slack_approve.yml` / `publish.yml`, and add the slug to
   `VALID_COMPANIES` plus `SLACK_BOT_USER_ID_<SLUG>` in
   `workers/slack-events/` (Worker code + `wrangler.toml`).
7. Create the GitHub Environment `<slug>` with its secrets; a new Slack app;
   set `SLACK_SIGNING_SECRET_<SLUG>` on the shared Worker; point the Slack
   Request URL at `/slack/events/<slug>`.
8. Verify: `COMPANY=<slug> PYTHONPATH=src python -m unittest discover -s tests`.

## Verifying prompt templates

Rendering a template with a profile's `PROMPT_VARS` must leave no unfilled
`{{key}}` placeholders. The `peachtree` render is byte-for-byte identical to the
pre-merge Peachtree prompts (regression baseline).
