# Claude API migration log (Peachtree / pt-blog-automation)

Dedicated log for the Together → Claude inference migration. Use this alongside `CHANGELOG.md` when you need a **migration-only audit trail** — especially when repeating the same work in **TC-Blog-Automation**.

**Handoff source:** `docs/claude-api-migration-handoff.md` (planned 2026-06-16).

**Project rule:** After each migration phase or meaningful Claude-related change, add a dated entry at the top (below this section). Mirror the same pattern in TC when you port the migration.

## Entry template

```md
## YYYY-MM-DD - Short change title

Phase:
- (e.g. Phase 2 — llm_client)

Changed:
-

Why:
-

Files touched:
-

Tested:
-

TC port notes:
- What to copy / rename / rebrand for TC-Blog-Automation

Rollback:
- How to revert (e.g. `LLM_PROVIDER=together`)

Notes / next step:
-
```

## Migration checklist (Peachtree)

- [x] Phase 0 — Grep / diff plan
- [x] Phase 1 — Dependencies, `.env.template`, GitHub Actions secrets docs
- [x] Phase 2 — `claude_models.py`, `llm_client.py`, Claude pricing
- [x] Phase 3 — Wire evaluate, write, fact-audit, scorer, Slack rewrites
- [x] Phase 4 — Single-template local mode (ISO-week rotation)
- [x] Phase 5 — Unit tests
- [x] Phase 6 — README + CHANGELOG
- [ ] Dry run from `kept_sources.json` (user-approved)
- [ ] Full pipeline run (user-approved)
- [ ] CI `weekly.yml` with `ANTHROPIC_API_KEY`
- [ ] TC-Blog-Automation port

## 2026-06-17 - Raise write max_tokens + Slack rewrite Claude path

Phase:
- Hotfix after truncated Opus draft; clarify Slack rewrite provider

Changed:
- Claude write `max_tokens` default 6000 (`CLAUDE_WRITING_MAX_TOKENS` env).
- Slack `regenerate_from_feedback` resolves Claude model; logs `provider=` / `model=`.
- `normalize_writing_model_for_provider` prevents Qwen id reuse on Anthropic.

Why:
- FAQ/byline truncation at 3400 out tokens.
- User asked whether Slack revise uses Claude — yes via `write_serverless`, now explicit in logs.

Files touched:
- `write_common.py`, `llm_models.py`, `approve_listen.py`, `write_serverless.py`, `.env.template`, `tests/test_write_max_tokens.py`

TC port notes:
- Copy `resolve_write_max_tokens`, `normalize_writing_model_for_provider`, and approve_listen rewrite changes.

## 2026-06-17 - Per-stage API cost logging

Phase:
- Observability improvement after Claude migration testing

Changed:
- Added pipeline log summary block: Search (Tavily), Evaluate, Write, Fact-audit, Scorer, Total.

Why:
- User requested per-part API credit/cost reporting in logs.

Files touched:
- `pipeline_costs.py`, `search.py`, `evaluate.py`, `write_multi.py`, `write_common.py`, `tests/test_pipeline_costs.py`

TC port notes:
- Copy `pipeline_costs.py` summary helpers and the same print call sites.

## 2026-06-17 - Fix Opus temperature 400 (post local test)

Phase:
- Hotfix after first local `pipeline.py` write_tournament run

Changed:
- `llm_client.py`: skip `temperature` on Opus 4.7+; Sonnet evaluate/fact-audit unchanged.

Why:
- Local run failed: `` `temperature` is deprecated for this model `` on `claude-opus-4-8`.

Files touched:
- `src/blog_automation/llm_client.py`, `tests/test_llm_client.py`, `CHANGELOG.md`

Tested:
- `PYTHONPATH=src python -m unittest tests.test_llm_client -v`

TC port notes:
- Same one-line logic in TC `llm_client.py` when porting.

Notes / next step:
- Re-run write-only test via `pipeline.py`.

## 2026-06-17 - Phases 3–6 complete (wire call sites + tests)

Phase:
- Phase 3 (call sites), Phase 4 (single local template), Phase 5 (tests), Phase 6 (docs)

Changed:
- `evaluate.py`: `evaluate_source_with_llm()` via `llm_client`; incremental evaluate unchanged otherwise.
- `write_common.py`: `generate_draft()` provider switch; validation retries = 2.
- `fact_audit.py`, `draft_scorer.py`: Claude/Together via `chat_completion()`.
- `writing_prompts.py`: local mode picks one ISO-week-rotated template (`geo` / `scenario` / `explainer`).
- `write_multi.py`, `write_serverless.py`, `write_tournament.py`: provider-aware model defaults.
- `tests/test_llm_client.py`, `tests/test_claude_models.py`; README LLM section updated.

Why:
- Complete Peachtree Claude migration per `docs/claude-api-migration-handoff.md`.

Files touched:
- (see Phase 1–2 entry plus) `evaluate.py`, `fact_audit.py`, `draft_scorer.py`, `writing_prompts.py`
- `write_multi.py`, `write_serverless.py`, `write_tournament.py`, `search.py`
- `tests/test_llm_client.py`, `tests/test_claude_models.py`, `README.md`, `CHANGELOG.md`

Tested:
- `PYTHONPATH=src python -m unittest discover -s tests -v`

TC port notes:
- Port files in dependency order: `claude_models.py` → `llm_client.py` → `llm_models.py` → call-site edits.
- Keep this log; add a `## TC-Blog-Automation` section when port starts.
- TC package name differs — replace `blog_automation` imports only.

Rollback:
- `LLM_PROVIDER=together` + `TOGETHER_API_KEY`; no code revert required.

Notes / next step:
- [ ] User adds `ANTHROPIC_API_KEY` locally + GitHub
- [ ] Dry run from existing `kept_sources.json` (user says go)
- [ ] Full pipeline run (user says go)
- [ ] Port to TC-Blog-Automation

## 2026-06-17 - Start Claude migration (Phases 0–2 foundation)

Phase:
- Phase 0 (grep plan), Phase 1 (deps/config), Phase 2 (new modules)

Changed:
- Added `docs/claude-migration-log.md` (this file) for migration-only tracking.
- Added `anthropic` dependency; `LLM_PROVIDER` and Claude model env vars in `.env.template`.
- Added `src/blog_automation/claude_models.py`, `llm_models.py`, `llm_client.py`.
- Extended token pricing in `write_common.py` for Claude Sonnet 4.6 and Opus 4.8.
- GitHub Actions workflows accept `ANTHROPIC_API_KEY` and optional `LLM_PROVIDER`.

Why:
- Replace Together multi-model stack with Claude API while keeping Tavily, validation, Slack, and PSAI unchanged.
- Reusable migration log for porting to TC-Blog-Automation.

Files touched:
- `docs/claude-migration-log.md`
- `pyproject.toml`, `requirements.txt`, `.env.template`
- `.github/workflows/weekly.yml`, `.github/workflows/slack_approve.yml`
- `src/blog_automation/claude_models.py`, `llm_models.py`, `llm_client.py`
- `src/blog_automation/write_common.py` (pricing + `generate_draft`)
- `CHANGELOG.md`

Tested:
- (pending — unit tests in Phase 5)

TC port notes:
- Copy `claude_models.py`, `llm_models.py`, `llm_client.py` verbatim; rename package `blog_automation` → TC package name.
- Reuse this log file structure; keep Peachtree-specific entries, add TC section at bottom.
- Same env vars and workflow secret names; update repo secrets on TC GitHub project.

Rollback:
- Set `LLM_PROVIDER=together` and ensure `TOGETHER_API_KEY` is set; Together code paths remain behind provider branch.

Notes / next step:
- Phase 3: wire `evaluate.py`, `fact_audit.py`, `draft_scorer.py`, `generate_validated_draft`, `write_serverless`, `write_multi`, `approve_listen`.
- Phase 4: local mode → single ISO-week-rotated template (`geo` / `scenario` / `explainer`).
- User must add `ANTHROPIC_API_KEY` locally and in GitHub Actions before live runs.
