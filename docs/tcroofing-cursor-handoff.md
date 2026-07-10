# TC Roofing Blog Automation — Cursor session handoff

> **Updated 2026-06-16:** The Peachtree pipeline now uses `write_tournament`, Cloudflare Worker approval, citation/fact-audit validation, and per-role model fallbacks. Use the generic duplication guide below and substitute **TC Roofing** wherever it says `[COMPANY]`.

**Full checklist:** [`docs/company-duplication-handoff.md`](company-duplication-handoff.md)

---

## TC-specific values (fill in when duplicating)

| Item | TC Roofing value |
|------|------------------|
| GitHub repo | `M-W-Johnson-Holdco/TCRoofing-Blog-Automation` (adjust) |
| Worker name | `tcroofing-slack-events` |
| Slack channel | `#tcroofing-blog-approval` |
| `workers/slack-events/wrangler.toml` | `GITHUB_REPOSITORY` = TC repo |
| `config/psai.json` | TC PSAI tenant + author |

Copy the body of `company-duplication-handoff.md` into your Cursor chat, then tell the agent: **rebrand to TC Roofing** and use the table above.

---

## Legacy note

Earlier versions of this file referenced `write_serverless` and optional `write_multi` wiring in `weekly.yml`. That is **done** in Peachtree — CI already runs `write_tournament.py --with-search`.
