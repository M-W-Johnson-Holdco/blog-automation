# Claude API migration — Cursor handoff (Peachtree / pt-blog-automation)

Copy this entire file into a Cursor chat opened on **`pt-blog-automation`** (Peachtree Roofing repo). Tell the agent: **implement the Claude migration per this handoff**.

> **Source:** Planned in TC-Blog-Automation session (2026-06-16). Same pipeline architecture; Peachtree keeps `blog_automation`, Metro Atlanta geo, and Peachtree branding.

---

## Goal

Replace the **Together multi-model stack** with **Claude API** for inference, while keeping Tavily search, Python validation, Slack approval, and PSAI publish unchanged.

**Target LLM layout (cost aside — quality/simplicity first):**

| Role | Today (Together) | After (Claude) |
|------|------------------|----------------|
| Evaluate sources | Qwen 7B | **Claude Sonnet 4.6** (`claude-sonnet-4-6`) |
| Write draft | Qwen 397B (+ tournament geo/scenario) | **Claude Opus 4.8** (`claude-opus-4-8`) — **single template** |
| Fact-audit | Qwen 235B | **Claude Sonnet 4.6** |
| Scorer (tournament) | Qwen 235B | **Remove** when single-template write is enabled |
| Slack rewrites | Together write model | **Claude Opus 4.8** |

**Do not replace Tavily with Claude.** Claude has no live URL index; citation rules require exact kept-source URLs.

---

## What to keep (unchanged)

- `search.py` + Tavily (`TAVILY_API_KEY`)
- Incremental evaluate during search (swap model provider only)
- `write_common.py` structural validation (H1, FAQs, CTA, locations, cites)
- Citation URL must match `kept_sources.json` exactly
- Percentage grounding checks
- Source-mode routing (`classify_source_mode` / `resolve_template_ids`)
- `pipeline.py`, `write_tournament.py`, Slack Worker, `weekly.yml`, PSAI `post.py`
- `used_sources.json` dedup

---

## What to simplify

1. **Drop tournament on local weeks** — today `local` mode runs `geo` + `scenario` in parallel + 235B scorer. Change to **one template per run**:
   - `local` (2+ metro sources) → **`geo` only** (or rotate `geo` / `scenario` / `explainer` by ISO week — pick one approach and document it)
   - `mixed` → `local_anchor` (unchanged)
   - `national` → `industry_insight` (unchanged)

   In `writing_prompts.py`, change e.g.:
   ```python
   SOURCE_MODE_TEMPLATE_IDS = {
       "local": ("geo",),  # was ("geo", "scenario")
       "mixed": ("local_anchor",),
       "national": ("industry_insight",),
   }
   ```

2. **Lower validation retries** — try `DEFAULT_VALIDATION_MAX_ATTEMPTS = 2` (was 3) after Claude is wired; tune from real runs.

3. **Remove Together-only code paths** (or gate behind `LLM_PROVIDER=together` for rollback):
   - `together_chat_completion_kwargs` / thinking-block stripping (Qwen-specific)
   - `model_not_available` fallback chains in hot path (optional keep for Together rollback)
   - 235B scorer calls when `len(template_ids) == 1` already skips — verify tournament fully off

4. **Keep fact-audit** initially — do not remove until 2–3 Peachtree drafts pass without audit failures.

---

## Implementation plan (agent task order)

### Phase 0 — Grep and diff plan (do before editing)

```bash
rg -l "together|Together|get_together_client|call_together_chat|generate_with_together" src/
rg "blog_automation" pyproject.toml .github/workflows/
```

Confirm package name is still `blog_automation` in Peachtree repo.

### Phase 1 — Dependencies and config

**`pyproject.toml`**
- Add: `anthropic>=0.40.0` (or current stable)
- Keep `together` optional for rollback, or remove after migration verified

**`.env.template`** — add:
```bash
LLM_PROVIDER="anthropic"          # anthropic | together (rollback)

ANTHROPIC_API_KEY=""

# Per-role Claude models (defaults in claude_models.py if unset)
CLAUDE_EVALUATION_MODEL="claude-sonnet-4-6"
CLAUDE_WRITING_MODEL="claude-opus-4-8"
CLAUDE_FACT_AUDIT_MODEL="claude-sonnet-4-6"
CLAUDE_SCORER_MODEL="claude-sonnet-4-6"   # only if tournament kept
```

**GitHub Actions** (`weekly.yml`, `slack_approve.yml`):
- Add `ANTHROPIC_API_KEY` secret
- Add optional `LLM_PROVIDER=anthropic`
- Keep `TOGETHER_API_KEY` during transition or remove after cutover

### Phase 2 — New modules

**`src/blog_automation/claude_models.py`**
- Mirror `together_models.py` shape: defaults, env resolution, model IDs
- No fallback chains required for v1 (Claude availability is stable)

**`src/blog_automation/llm_client.py`** (recommended abstraction)

Single entry point used by evaluate, write, fact-audit, scorer:

```python
def get_llm_provider() -> str:  # "anthropic" | "together" from LLM_PROVIDER

def chat_completion(
    *,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    response_format: str | None = None,  # "json" | None
    log_prefix: str = "[llm]",
) -> tuple[str, dict]:  # content, generation_metadata (usage, model_used, elapsed)
```

**Anthropic branch:**
- Client: `anthropic.Anthropic(api_key=...)`
- Map `messages` → Anthropic format (extract system message to `system=`)
- Response text: `response.content[0].text`
- Usage: `response.usage.input_tokens`, `output_tokens`
- JSON mode: use structured outputs / prompt “return JSON only” (same as today)

**Together branch:** delegate to existing `call_together_chat_with_fallback` / `generate_with_together` for rollback.

**`src/blog_automation/llm_pricing.py`** (or extend `write_common.py`)
- Add Claude $/MTok table for Slack cost lines:
  - Sonnet 4.6: $3 input / $15 output per 1M
  - Opus 4.8: $5 input / $25 output per 1M
- Update `estimate_token_cost_usd()` to price Claude models

### Phase 3 — Wire each call site

| File | Change |
|------|--------|
| `write_common.py` | `generate_with_together` → `generate_draft` calling `llm_client`; keep validation loop |
| `evaluate.py` | `evaluate_source_with_together` → `evaluate_source_with_llm` |
| `fact_audit.py` | use `llm_client` with JSON response |
| `draft_scorer.py` | use `llm_client` OR skip when single-template (preferred) |
| `pipeline/write_multi.py` | resolve writing model from `CLAUDE_WRITING_MODEL`; single-template local mode |
| `pipeline/write_serverless.py` | same write path as write_multi |
| `pipeline/approve_listen.py` | Slack rewrites use Claude writing model |
| `write_tournament.py` | log Claude writing model; pass `--model` from env default |

**Writing system prompt** (today in `generate_with_together`):
```text
You are a senior home-services content strategist.
Return only the complete Markdown blog draft starting with one H1 title line.
Do not include planning notes, analysis, or a thinking process.
```
Keep this for Claude — no Qwen thinking-strip logic needed.

### Phase 4 — Single-template local mode

Edit `src/blog_automation/writing_prompts.py`:
- `SOURCE_MODE_TEMPLATE_IDS["local"]` → `("geo",)` only **OR** ISO-week rotation among `geo`/`scenario`/`explainer` (document choice in CHANGELOG)

Optional: add `--rotate-local-template` flag if you want weekly variety without tournament.

### Phase 5 — Tests

**`tests/test_llm_client.py`** (new)
- Mock Anthropic client; verify message mapping and usage extraction

**`tests/test_claude_models.py`**
- Default model resolution from env

**Keep** `tests/test_draft_validation.py`, `tests/test_search_relevance.py` — unchanged

**Update** `tests/test_together_models.py` — skip if `LLM_PROVIDER=anthropic` or keep for Together rollback

Run:
```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

### Phase 6 — Docs and changelog

- Update `README.md` model section
- Update `.env.template`
- `CHANGELOG.md` entry: Claude migration, single-template local, provider flag

---

## Peachtree-specific (do not change unless asked)

- Brand: **Peachtree Roofing & Exteriors**
- Market: **Metro Atlanta** in `search.py`, prompts, `METRO_LOCATIONS`
- Package: `blog_automation` (no rename required)
- `config/psai.json` — Peachtree PSAI tenant

---

## CI secrets checklist (Peachtree repo)

| Secret | Required |
|--------|----------|
| `ANTHROPIC_API_KEY` | Yes (new) |
| `TAVILY_API_KEY` | Yes (unchanged) |
| `SLACK_*`, `PSAI_API_KEY` | Yes (unchanged) |
| `TOGETHER_API_KEY` | Optional (rollback only) |
| `LLM_PROVIDER` | Optional repo variable → `anthropic` |

Cloudflare Worker secrets unchanged.

---

## Testing protocol (agent: do not run full search unless user says go)

**Cheap dry run** (no Tavily credits):
```bash
# Requires existing output/sources/kept_sources.json from a prior run
PYTHONPATH=src LLM_PROVIDER=anthropic python -m blog_automation.pipeline.write_multi \
  --model claude-opus-4-8 --clear-drafts
```

**Compare** validation JSON: `llm_fact_audit_passed`, attempt count, `estimated_cost_usd`

**Full run** (expensive — user must approve):
```bash
PYTHONPATH=src python write_tournament.py --with-search --clear-drafts
```

---

## Rollback

Set `LLM_PROVIDER=together` and restore `TOGETHER_API_KEY`. Keep Together code behind provider branch until Peachtree production is stable on Claude for 2+ weeks.

---

## Explicit non-goals

- Do **not** replace Tavily with Claude web search
- Do **not** remove Python structural validation or citation/% checks
- Do **not** remove Slack approval or Worker
- Do **not** run full pipeline in agent session unless user says **go**
- Do **not** rebrand Peachtree → TC

---

## Success criteria

- [ ] One draft from `kept_sources.json` completes on Claude without Together key
- [ ] Evaluate during search uses Sonnet
- [ ] Local weeks use **one** template (no scorer)
- [ ] Validation + fact-audit still run
- [ ] Slack approval shows model + cost lines
- [ ] `weekly.yml` works with `ANTHROPIC_API_KEY`
- [ ] Unit tests pass
- [ ] Rollback to Together still works via `LLM_PROVIDER=together`

---

## Quick reference — file touch list

**New**
- `src/blog_automation/llm_client.py`
- `src/blog_automation/claude_models.py`
- `tests/test_llm_client.py` (optional but recommended)

**Modify**
- `src/blog_automation/write_common.py`
- `src/blog_automation/pipeline/evaluate.py`
- `src/blog_automation/fact_audit.py`
- `src/blog_automation/draft_scorer.py` (or no-op when single template)
- `src/blog_automation/writing_prompts.py` (single local template)
- `src/blog_automation/pipeline/write_multi.py`
- `src/blog_automation/pipeline/approve_listen.py`
- `pyproject.toml`, `.env.template`
- `.github/workflows/weekly.yml`, `slack_approve.yml`
- `README.md`, `CHANGELOG.md`

**Unchanged**
- `src/blog_automation/pipeline/search.py` (Tavily)
- `workers/slack-events/`
- `prompts/*.txt` (Peachtree brand/geo)
- `config/psai.json`
