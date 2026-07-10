"""
Broad local-market roofing search — keyword match + state geography gate, 7-day recency.
(Market data — metro/state terms, source domains, clusters — comes from the active
company profile; see blog_automation/companies/.)

Keeps sources with metro / state headline geography and roof/storm/insurance
signals in the headline or article lead (not sidebar job ads or trending links).
Rejects out-of-state headlines, crime/health stories, and national pages without in-state focus.

Run:
    python -m blog_automation.pipeline.search

Output:
    output/sources/search_results.json

Required environment:
    TAVILY_API_KEY
"""

from __future__ import annotations

import blog_automation._pycache_prefix  # noqa: F401

from blog_automation.paths import PROJECT_ROOT

import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from blog_automation.pipeline.evaluate import (
    DEFAULT_KEPT_PATH,
    IncrementalEvaluator,
    KEEP_THRESHOLD,
    MIN_EVALUATED_KEPT_TO_PROCEED,
    TARGET_EVALUATED_KEPT,
)
from blog_automation.used_sources import normalize_source_url, used_source_urls
from blog_automation.pipeline_costs import (
    estimate_tavily_cost_usd,
    record_evaluate_cost,
    record_search_cost,
    reset_pipeline_costs,
    mark_tavily_search_ran,
    tavily_usd_per_credit,
)
from blog_automation.company import get_profile

# Company profile: all brand/geo/market-specific search data lives in
# blog_automation/companies/<slug>.py. Bound once at import time — one
# process serves one company (selected via the COMPANY env var).
_PROFILE = get_profile()


DEFAULT_MAX_AGE_DAYS = 14

# Urgency-framed clusters get a tighter recency window so stale storm events
# don't produce "act within 24-48 hours" content weeks after the fact.
CLUSTER_MAX_AGE_DAYS: dict[str, int] = {
    "storm_damage": 7,
}
DEFAULT_MAX_RESULTS_PER_QUERY = 8
DEFAULT_TARGET_RESULTS = 15
DEFAULT_QUERIES_PER_CLUSTER = 3
DEFAULT_MAX_TAVILY_CREDITS = 100
# Domain-stage runs span 8 stages instead of 2 — raise the cap so the query
# space isn't trimmed to a handful of searches.
DOMAIN_STAGES_MAX_TAVILY_CREDITS = 200
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "output" / "sources" / "search_results.json"
TAVILY_ADVANCED_SEARCH_CREDITS = 2

PRIORITY_SOURCES = _PROFILE.SEARCH_PRIORITY_SOURCES

SECONDARY_SOURCES = _PROFILE.SEARCH_SECONDARY_SOURCES

OFFICIAL_SOURCES = _PROFILE.SEARCH_OFFICIAL_SOURCES

ROOFING_TRADE_SOURCES = _PROFILE.SEARCH_ROOFING_TRADE_SOURCES

# National roofing/insurance trade publications used in the quiet-week fallback.
# These never carry the local metro in their headlines; geographic gates are skipped
# for them. Results route to the industry_insight template via classify_source_mode.
NATIONAL_TRADE_SOURCES = [
    "roofingcontractor.com",
    "nrca.net",
    "claimsjournal.com",
    "insurancejournal.com",
    "propertycasualty360.com",
    "roughnotes.com",
    "verisk.com",
    "rooferscoffeeshop.com",
    "constructiondive.com",
    "jlconline.com",
    "rcknews.com",
    "contractormag.com",
    "nahb.org",
    "iii.org",
]

# Queries run against NATIONAL_TRADE_SOURCES when local stages return 0 keeps.
NATIONAL_TRADE_QUERIES: list[str] = [
    "hail damage residential roof insurance claims 2025",
    "severe weather roof replacement cost increase United States",
    "homeowners insurance roof claim denial rate 2025",
    "actual cash value replacement cost roof insurance gap",
    "residential roof replacement cost per square foot 2025",
    "roofing material cost inflation labor shortage 2025",
    "hail storm property damage claims NRCA data",
    "insurance company roof settlement trends 2025",
    "aging housing stock roof replacement statistics",
    "residential roofing industry trends storm season 2025",
]

# Credit budget for the national trade fallback stage (one stage × ~15 queries × 2 credits).
NATIONAL_FALLBACK_MAX_CREDITS = 30
# National trade sources need one extra keep vs local runs because the industry_insight
# template draws on more data points to frame a credible metro-specific post.
NATIONAL_TRADE_TARGET_KEPT = 3

EXCLUDED_DOMAINS = [
    "espn.com",
    "sports.yahoo.com",
    "cbssports.com",
    "si.com",
    "theathletic.com",
    "247sports.com",
    "on3.com",
    "bleacherreport.com",
    "mlb.com",
    "nba.com",
    "nfl.com",
    "pgatour.com",
    "golf.com",
    "foxsports.com",
    "nbcsports.com",
    "usatoday.com/sports",
    "people.com",
    "tmz.com",
    "eonline.com",
    "rollingstone.com",
]

STRATEGY_CLUSTERS = _PROFILE.SEARCH_STRATEGY_CLUSTERS

LOCAL_TERMS = _PROFILE.SEARCH_LOCAL_TERMS

STATE_LOCAL_TERMS = _PROFILE.SEARCH_STATE_LOCAL_TERMS

# Counties/cities/Metro labels — stronger than bare regional labels alone.
METRO_LOCAL_TERMS = [term for term in LOCAL_TERMS if term not in set(STATE_LOCAL_TERMS)]

TOPIC_TERMS = _PROFILE.SEARCH_TOPIC_TERMS

# Roof/storm/structure signals — standalone "insurance" or "claim" are not enough.
PRIMARY_CORE_ROOFING_TERMS = _PROFILE.SEARCH_PRIMARY_CORE_ROOFING_TERMS

INSURANCE_CORE_TERMS = [
    "homeowners insurance",
    "roof insurance",
    "insurance claim",
    "insurance commissioner",
    "rcv",
    "acv",
    "deductible",
    "xactimate",
    "public adjuster",
    "recoverable depreciation",
    "supplemental claim",
]

STORM_HEADLINE_TERMS = [
    "severe thunderstorm",
    "thunderstorm warning",
    "tornado warning",
    "tornado watch",
    "severe weather",
    "hail storm",
    "flash flood",
    "high wind",
    # Broader weather terms local TV headlines commonly use — these alone don't
    # confirm a roofing story but are acceptable from metro news domains where the
    # geography gate already filters for local relevance.
    "storm",
    "flooding",
    "flood warning",
    "flood watch",
    "severe storm",
    "wind damage",
    "storm damage",
    "weather damage",
    "heavy rain",
    "hail",
    "tornado",
]

# Back-compat alias used in match metadata.
CORE_ROOFING_SIGNAL_TERMS = PRIMARY_CORE_ROOFING_TERMS + INSURANCE_CORE_TERMS

OUT_OF_MARKET_STATE_TERMS = _PROFILE.SEARCH_OUT_OF_MARKET_STATE_TERMS

OUT_OF_MARKET_URL_PATHS = [
    "/news/west/",
    "/news/midwest/",
    "/news/northeast/",
    "/news/northcentral/",
    "/news/international/",
    "/news/europe/",
    "/news/central/",
]

OFF_TOPIC_SIGNAL_TERMS = [
    "blood pressure",
    "senior center",
    "nursing home",
    "hospital",
    "health monitor",
    "medicaid",
    "medicare",
    "physician",
    "clinic",
    "maternity",
    "pediatric",
    "shooting",
    "murder",
    "homicide",
    "murder-suicide",
    "arrest",
    "suspect sought",
    "marta train",
    "killed family",
    "self-defense",
    "dog attack",
    "stabbing",
    "robbery",
    "carjacking",
    "arson",
    "reward for information",
    "string of arsons",
    "set fire",
    "deliberately set",
    "suspicious fire",
]

METRO_NEWS_DOMAIN_EXCLUSIONS = _PROFILE.SEARCH_METRO_NEWS_DOMAIN_EXCLUSIONS

STATE_SCOPED_DOMAIN_MARKERS = _PROFILE.SEARCH_STATE_SCOPED_DOMAIN_MARKERS

CLUSTER_TERMS = _PROFILE.SEARCH_CLUSTER_TERMS

SEARCH_PLAN = [
    {
        "query": query,
        "strategy_cluster": cluster_name,
        "pillar_topic": cluster["pillar_topic"],
        "trigger_window_hours": cluster["trigger_window_hours"],
    }
    for cluster_name, cluster in STRATEGY_CLUSTERS.items()
    for query in cluster["queries"]
]

SEARCH_STAGES = [
    {
        "name": "priority_7_day_news",
        "days": 7,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": PRIORITY_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
    {
        "name": "priority_21_day_news",
        "days": 21,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": PRIORITY_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
    {
        "name": "priority_30_day_news",
        "days": 30,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": PRIORITY_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
    {
        "name": "secondary_14_day_news",
        "days": 14,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": SECONDARY_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
    {
        "name": "secondary_30_day_news",
        "days": 30,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": SECONDARY_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
    {
        "name": _PROFILE.SEARCH_TRADE_STAGE_NAME,
        "days": 21,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": ROOFING_TRADE_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
    {
        "name": "broad_21_day_news",
        "days": 21,
        "topic": "news",
        "search_depth": "advanced",
        "include_domains": None,
        "exclude_domains": EXCLUDED_DOMAINS,
        "max_results_per_query": 8,
    },
    {
        "name": "official_30_day_general",
        "days": 30,
        "topic": "general",
        "search_depth": "advanced",
        "include_domains": OFFICIAL_SOURCES,
        "exclude_domains": None,
        "max_results_per_query": 8,
    },
]

# Content-first mode: open web + official sources only (skip local-TV / trade domain locks).
BROAD_FOCUS_STAGE_NAMES = frozenset({"broad_21_day_news", "official_30_day_general"})


def resolve_search_stages(*, use_domain_stages: bool = False) -> list[dict]:
    """Return Tavily stage configs. Default skips PRIORITY/SECONDARY/trade domain locks."""
    if use_domain_stages:
        return list(SEARCH_STAGES)
    return [stage for stage in SEARCH_STAGES if stage["name"] in BROAD_FOCUS_STAGE_NAMES]


LOG_PREFIX = "search"


def _load_client():
    try:
        from tavily import TavilyClient
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: tavily-python. Install with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "TAVILY_API_KEY is not set. Copy .env.template to .env and add your Tavily key."
        )
    return TavilyClient(api_key=api_key)


def _source_domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _domain_matches_list(domain: str, sources: list[str]) -> bool:
    return any(domain == source or domain.endswith(f".{source}") for source in sources)


def _is_priority_source(url: str) -> bool:
    return _domain_matches_list(_source_domain(url), PRIORITY_SOURCES)


def _is_secondary_source(url: str) -> bool:
    domain = _source_domain(url)
    return _domain_matches_list(domain, SECONDARY_SOURCES) and not _domain_matches_list(
        domain, PRIORITY_SOURCES
    )


def _is_official_source(url: str) -> bool:
    return _domain_matches_list(_source_domain(url), OFFICIAL_SOURCES)


def estimate_tavily_credits(query_count: int, stage_count: int) -> int:
    return query_count * stage_count * TAVILY_ADVANCED_SEARCH_CREDITS


def cap_search_plan_for_credits(
    plan: list[dict],
    *,
    max_credits: int,
    stage_count: int,
) -> list[dict]:
    """Limit active queries so a full multi-stage run stays within the credit budget."""
    if max_credits <= 0 or not plan or stage_count <= 0:
        return plan

    max_api_calls = max_credits // TAVILY_ADVANCED_SEARCH_CREDITS
    max_queries = max(1, max_api_calls // stage_count)
    if len(plan) <= max_queries:
        return plan
    return plan[:max_queries]


def build_active_search_plan(
    *,
    queries_per_cluster: int = DEFAULT_QUERIES_PER_CLUSTER,
    use_all_queries: bool = False,
    rotation_week: int | None = None,
    preferred_cluster: str | None = None,
) -> list[dict]:
    if use_all_queries or queries_per_cluster <= 0:
        plan = list(SEARCH_PLAN)
    else:
        week_number = rotation_week or datetime.now(timezone.utc).isocalendar().week
        plan = []
        for cluster_name, cluster in STRATEGY_CLUSTERS.items():
            queries = cluster["queries"]
            if not queries:
                continue
            count = min(queries_per_cluster, len(queries))
            start_index = (week_number - 1) % len(queries)
            for offset in range(count):
                query = queries[(start_index + offset) % len(queries)]
                plan.append(
                    {
                        "query": query,
                        "strategy_cluster": cluster_name,
                        "pillar_topic": cluster["pillar_topic"],
                        "trigger_window_hours": cluster["trigger_window_hours"],
                    }
                )

    if preferred_cluster and preferred_cluster in STRATEGY_CLUSTERS:
        preferred = [item for item in plan if item["strategy_cluster"] == preferred_cluster]
        other = [item for item in plan if item["strategy_cluster"] != preferred_cluster]
        if preferred:
            plan = preferred + other
    return plan


def _term_matches(text: str, term: str) -> bool:
    normalized = term.lower()
    if " " in normalized:
        return normalized in text
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text))


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if _term_matches(text, term)]


def _parse_published_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _effective_published_date(result: dict) -> datetime | None:
    parsed = _parse_published_date(result.get("published_date", ""))
    if parsed:
        return parsed

    combined = f"{result.get('title', '')} {result.get('url', '')}".lower()

    compact = re.search(r"(?:/|_|-)(20\d{2})(\d{2})(\d{2})(?:[_/.-]|$)", combined)
    if compact:
        try:
            return datetime(
                int(compact.group(1)),
                int(compact.group(2)),
                int(compact.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    dashed = re.search(r"(?:/|_|-)(20\d{2})-(\d{2})-(\d{2})(?:[_/.-]|$)", combined)
    if dashed:
        try:
            return datetime(
                int(dashed.group(1)),
                int(dashed.group(2)),
                int(dashed.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    years = [int(year) for year in re.findall(r"\b(20\d{2})\b", combined)]
    if years:
        year = max(years)
        if year < datetime.now(timezone.utc).year:
            return datetime(year, 12, 31, tzinfo=timezone.utc)

    return None


def _content_age_days(result: dict) -> int | None:
    parsed = _effective_published_date(result)
    if not parsed:
        return None
    return (datetime.now(timezone.utc) - parsed).days


def _is_within_recency_window(result: dict, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> bool:
    age_days = _content_age_days(result)
    if age_days is None:
        if result.get("search_stage") in {"broad_14_day_news", "official_30_day_general"}:
            return False
        return True
    return age_days <= max_age_days


def _headline_text(result: dict) -> str:
    return " ".join([result.get("title", ""), result.get("url", "")]).lower()


def _lead_body_text(result: dict, *, limit: int = 700) -> str:
    """Article lead only — avoids WSB 'Most Read' sidebars that pollute the full snippet."""
    content = result.get("content", "").strip()
    if not content:
        return ""

    lowered = content.lower()
    sidebar_markers = (
        "## most read",
        "trending stories:",
        "sign up:",
        "download:",
    )
    cut_at = len(content)
    for marker in sidebar_markers:
        index = lowered.find(marker)
        if index != -1:
            cut_at = min(cut_at, index)
    return content[:cut_at][:limit].lower()


def _is_state_scoped_domain(url: str) -> bool:
    lower = url.lower()
    domain = _source_domain(url).lower()
    return any(marker in domain or marker in lower for marker in STATE_SCOPED_DOMAIN_MARKERS)


def _is_metro_news_domain(url: str) -> bool:
    domain = _source_domain(url)
    if _domain_matches_list(domain, list(METRO_NEWS_DOMAIN_EXCLUSIONS)):
        return False
    return _domain_matches_list(domain, PRIORITY_SOURCES + SECONDARY_SOURCES)


def _has_roofing_headline_signal(headline: str) -> bool:
    return bool(
        _matched_terms(headline, PRIMARY_CORE_ROOFING_TERMS)
        or _matched_terms(headline, INSURANCE_CORE_TERMS)
        or _matched_terms(headline, STORM_HEADLINE_TERMS)
    )


def _has_roofing_content_signal(text: str) -> bool:
    return bool(
        _matched_terms(text, PRIMARY_CORE_ROOFING_TERMS)
        or _matched_terms(text, INSURANCE_CORE_TERMS)
    )


def _article_evidence(result: dict) -> dict[str, str]:
    title_url = _headline_text(result)
    early_content = " ".join(
        [
            result.get("title", ""),
            result.get("url", ""),
            result.get("content", "")[:1400],
        ]
    ).lower()
    return {"title_url": title_url, "early_content": early_content}


def _normalize_result(item: dict, search_item: dict, stage: dict) -> dict:
    url = item.get("url", "").strip()
    return {
        "title": item.get("title", "").strip(),
        "url": url,
        "domain": _source_domain(url),
        "content": item.get("content", "").strip(),
        "published_date": item.get("published_date") or item.get("publishedDate") or "",
        "tavily_score": item.get("score", 0.0),
        "priority_source": _is_priority_source(url),
        "secondary_source": _is_secondary_source(url),
        "official_source": _is_official_source(url),
        "query": search_item["query"],
        "strategy_cluster": search_item["strategy_cluster"],
        "pillar_topic": search_item["pillar_topic"],
        "trigger_window_hours": search_item["trigger_window_hours"],
        "search_stage": stage["name"],
        "search_days": stage["days"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _collect_matched_terms(result: dict) -> dict[str, object]:
    evidence = _article_evidence(result)
    text = f"{evidence['title_url']} {evidence['early_content']}"
    cluster = result.get("strategy_cluster", "")
    cluster_terms = CLUSTER_TERMS.get(cluster, TOPIC_TERMS)

    local_terms = sorted(set(_matched_terms(text, LOCAL_TERMS)))
    topic_terms = sorted(set(_matched_terms(text, TOPIC_TERMS)))
    cluster_matched = sorted(set(_matched_terms(text, cluster_terms)))

    return {
        "local_terms": local_terms,
        "topic_terms": topic_terms,
        "cluster_terms": cluster_matched,
        "scoring_mode": "roofing_keyword_match_only",
    }


def _relevance_failure_reason(
    result: dict,
    matched: dict[str, object],
    max_age_days: int,
    *,
    is_national_trade: bool = False,
) -> str | None:
    cluster = result.get("strategy_cluster", "")
    effective_max_age = min(max_age_days, CLUSTER_MAX_AGE_DAYS.get(cluster, max_age_days))
    if not _is_within_recency_window(result, effective_max_age):
        return "outside_recency_window"

    headline = _headline_text(result)
    lead = _lead_body_text(result)
    headline_and_lead = f"{headline} {lead}".strip()

    if _matched_terms(headline, OFF_TOPIC_SIGNAL_TERMS):
        return "off_topic_headline"

    # National trade sources (industry publications, NRCA, Verisk, etc.) never
    # carry the local metro in their headlines. Skip all geographic gates and only
    # verify the content is roofing-relevant.
    if is_national_trade:
        if not _has_roofing_content_signal(headline_and_lead):
            return "missing_core_roofing_topic"
        return None

    url = result.get("url", "")
    state_scoped = _is_state_scoped_domain(url) or _is_official_source(url)

    lower_url = url.lower()
    if any(path in lower_url for path in OUT_OF_MARKET_URL_PATHS):
        return "out_of_market_url_path"

    out_of_market_states = _matched_terms(headline, OUT_OF_MARKET_STATE_TERMS)
    metro_in_headline = _matched_terms(headline, METRO_LOCAL_TERMS)
    state_in_headline = _matched_terms(headline, STATE_LOCAL_TERMS)
    if out_of_market_states and not metro_in_headline and not (state_scoped and state_in_headline):
        return "out_of_market_state_headline"

    local_headline_lead = _matched_terms(headline_and_lead, LOCAL_TERMS)
    if not local_headline_lead:
        return "missing_headline_local_terms"

    if not metro_in_headline and not state_scoped:
        regional_in_headline_lead = _matched_terms(headline_and_lead, STATE_LOCAL_TERMS)
        state_only_local = set(local_headline_lead) <= set(STATE_LOCAL_TERMS)
        statewide_story = bool(regional_in_headline_lead) and (
            _has_roofing_headline_signal(headline)
            or _has_roofing_content_signal(headline_and_lead)
            or bool(_matched_terms(headline_and_lead, INSURANCE_CORE_TERMS))
        )
        if state_only_local and not statewide_story:
            return "state_only_without_metro_signal"

    if _is_metro_news_domain(url) and not _has_roofing_headline_signal(headline):
        return "missing_roofing_headline_for_metro_news"

    # National/non-local domains must anchor to the local metro in the headline, or
    # to a state-level term + a roofing signal in the headline — snippet text alone
    # is not enough (sidebars and tangential mentions produce too many false positives).
    is_local_domain = _is_metro_news_domain(url) or _is_official_source(url) or _is_state_scoped_domain(url)
    if not is_local_domain:
        state_in_headline = bool(_matched_terms(headline, STATE_LOCAL_TERMS))
        if not metro_in_headline and not (state_in_headline and _has_roofing_headline_signal(headline)):
            return "national_domain_missing_local_headline"

    if not _has_roofing_content_signal(headline_and_lead):
        return "missing_core_roofing_topic"

    return None


def _match_score(matched: dict[str, object]) -> float:
    return float(len(matched["local_terms"]) + len(matched["topic_terms"]) + len(matched["cluster_terms"]))


def _ingest_tavily_items(
    raw_items: list[dict],
    *,
    search_item: dict,
    stage: dict,
    results_by_url: dict[str, dict],
    blocked_urls: set[str],
    max_age_days: int,
    is_national_trade: bool = False,
) -> tuple[int, dict[str, int], list[dict]]:
    skipped_used = 0
    pipeline_rejects: dict[str, int] = {}
    newly_added: list[dict] = []

    for item in raw_items:
        url = str(item.get("url", "")).strip()
        if not url:
            continue

        if blocked_urls and normalize_source_url(url) in blocked_urls:
            skipped_used += 1
            continue

        result = _normalize_result(item, search_item, stage)
        matched = _collect_matched_terms(result)
        result["matched_terms"] = matched
        result["search_quality_score"] = _match_score(matched)
        result["content_age_days"] = _content_age_days(result)
        result["scoring_mode"] = matched["scoring_mode"]

        failure = _relevance_failure_reason(result, matched, max_age_days, is_national_trade=is_national_trade)
        if failure:
            pipeline_rejects[failure] = pipeline_rejects.get(failure, 0) + 1
            continue

        existing = results_by_url.get(url)
        if not existing or result["search_quality_score"] > existing["search_quality_score"]:
            was_new = url not in results_by_url
            results_by_url[url] = result
            if was_new:
                newly_added.append(result)

    return skipped_used, pipeline_rejects, newly_added


def _sort_results(results: list[dict]) -> list[dict]:
    return sorted(
        results,
        key=lambda result: (
            result.get("content_age_days") is not None,
            -(result.get("content_age_days") or 9999),
            result["search_quality_score"],
            result["priority_source"],
            result.get("secondary_source", False),
            result["tavily_score"],
        ),
        reverse=True,
    )


def search_roofing_news(
    max_results_per_query: int = DEFAULT_MAX_RESULTS_PER_QUERY,
    target_results: int | None = DEFAULT_TARGET_RESULTS,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_tavily_credits: int = DEFAULT_MAX_TAVILY_CREDITS,
    skip_used_sources: bool = True,
    queries_per_cluster: int = DEFAULT_QUERIES_PER_CLUSTER,
    use_all_queries: bool = False,
    rotation_week: int | None = None,
    preferred_cluster: str | None = None,
    incremental_evaluate: bool = False,
    min_evaluated_kept: int = MIN_EVALUATED_KEPT_TO_PROCEED,
    target_evaluated_kept: int = TARGET_EVALUATED_KEPT,
    evaluate_model: str | None = None,
    use_domain_stages: bool = False,
    confirm_national_fallback: bool = False,
) -> list[dict]:
    load_dotenv(PROJECT_ROOT / ".env")
    reset_pipeline_costs()
    # Domain-stage runs cover 8 stages; auto-raise the credit cap so the query
    # plan isn't trimmed to a handful of searches unless the caller overrode it.
    if use_domain_stages and max_tavily_credits == DEFAULT_MAX_TAVILY_CREDITS:
        max_tavily_credits = DOMAIN_STAGES_MAX_TAVILY_CREDITS
    client = _load_client()
    evaluator: IncrementalEvaluator | None = None
    if target_evaluated_kept < min_evaluated_kept:
        raise ValueError(
            f"target_evaluated_kept ({target_evaluated_kept}) must be >= "
            f"min_evaluated_kept ({min_evaluated_kept})"
        )

    if incremental_evaluate:
        evaluator = IncrementalEvaluator(model=evaluate_model)
        print(
            f"[{LOG_PREFIX}] Incremental evaluate: proceed with >= {min_evaluated_kept} kept, "
            f"stop early at {target_evaluated_kept} (score >= {KEEP_THRESHOLD}); "
            f"otherwise search until credit cap"
        )
    goal_reached = False

    blocked_urls: set[str] = set()
    if skip_used_sources:
        blocked_urls = used_source_urls()
        if blocked_urls:
            print(f"[{LOG_PREFIX}] Skipping {len(blocked_urls)} previously used source URL(s)")

    # ── Source strategy (interactive mode only) ───────────────────────────────
    # In CI mode (confirm_national_fallback=False) we always run local stages
    # first and fall back to national trade automatically if nothing is found.
    # In interactive mode the operator chooses upfront so quiet weeks skip the
    # 8 local stages entirely and save ~144 Tavily credits.
    run_local_stages = True
    run_national_trade = False  # set True by user choice or by CI auto-fallback below

    if confirm_national_fallback:
        # Write the interactive menu to stderr so it stays visible in the
        # terminal even when the pipeline runner has stdout captured for logging.
        import sys as _sys
        _sys.stderr.write(
            f"\n[{LOG_PREFIX}] Select source strategy for this run:\n"
            f"  1. Local + national  (search {_PROFILE.METRO_AREA} news, then industry publications)\n"
            f"  2. National only     (skip local stages — use when no local news expected)\n"
            f"\n[search] Choice (1/2, default 1): "
        )
        _sys.stderr.flush()
        try:
            strategy_choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            strategy_choice = "1"
        if strategy_choice == "2":
            run_local_stages = False
            run_national_trade = True
            print(f"[{LOG_PREFIX}] National only — skipping local stages.")
        else:
            run_local_stages = True
            run_national_trade = True
            print(f"[{LOG_PREFIX}] Local + national — will search both.")

    stage_plan = resolve_search_stages(use_domain_stages=use_domain_stages)
    stage_count = len(stage_plan)
    stage_mode = (
        "all stages (domain-restricted + broad + official)"
        if use_domain_stages
        else "broad + official only (content-first, no local-TV domain lock)"
    )

    active_plan = build_active_search_plan(
        queries_per_cluster=queries_per_cluster,
        use_all_queries=use_all_queries,
        rotation_week=rotation_week,
        preferred_cluster=preferred_cluster,
    )
    planned_query_count = len(active_plan)
    active_plan = cap_search_plan_for_credits(
        active_plan,
        max_credits=max_tavily_credits,
        stage_count=stage_count,
    )
    query_count = len(active_plan)
    planned_credits = estimate_tavily_credits(planned_query_count, stage_count)
    max_credits = min(planned_credits, max_tavily_credits)
    week_number = rotation_week or datetime.now(timezone.utc).isocalendar().week
    target_note = target_results if target_results is not None else "all"
    trim_note = ""
    if query_count < planned_query_count:
        trim_note = f", trimmed {planned_query_count}→{query_count} queries for {max_tavily_credits}-credit cap"

    results_by_url: dict[str, dict] = {}
    stages = [
        {
            **stage,
            "max_results_per_query": stage.get("max_results_per_query", max_results_per_query),
            "recency_days": min(stage["days"], max_age_days),
        }
        for stage in stage_plan
    ]
    queries_run = 0
    skipped_used_sources = 0
    pipeline_rejects: dict[str, int] = {}
    credit_limit_reached = False

    if run_local_stages:
        print(
            f"[{LOG_PREFIX}] Plan: {query_count} active queries "
            f"({queries_per_cluster}/cluster, ISO week {week_number}) "
            f"from {len(SEARCH_PLAN)} total across {stage_count} stages "
            f"({stage_mode}; target {target_note}, recency cap {max_age_days}d, "
            f"max ~{max_credits} credits{trim_note})"
        )
    else:
        print(
            f"[{LOG_PREFIX}] Skipping local stages — national trade only "
            f"(~{NATIONAL_FALLBACK_MAX_CREDITS} credits, {len(NATIONAL_TRADE_QUERIES)} queries)"
        )

    for stage_index, stage in enumerate(stages) if run_local_stages else []:
        if credit_limit_reached or goal_reached:
            break
        print(f"[{LOG_PREFIX}] Running stage: {stage['name']}")
        is_last_stage = stage_index == len(stages) - 1

        for search_item in active_plan:
            if goal_reached:
                break
            credits_after_call = (queries_run + 1) * TAVILY_ADVANCED_SEARCH_CREDITS
            if credits_after_call > max_tavily_credits:
                print(
                    f"[{LOG_PREFIX}] Tavily credit cap ({max_tavily_credits}) reached — "
                    "stopping search"
                )
                credit_limit_reached = True
                break

            query = search_item["query"]
            try:
                response = client.search(
                    query=query,
                    search_depth=stage["search_depth"],
                    topic=stage["topic"],
                    days=stage["days"],
                    max_results=stage["max_results_per_query"],
                    include_domains=stage["include_domains"],
                    exclude_domains=stage["exclude_domains"],
                    include_raw_content=False,
                )
                queries_run += 1
            except Exception as exc:
                print(f"[{LOG_PREFIX}] Query failed for {query!r}: {exc}")
                continue

            skipped, stage_rejects, newly_added = _ingest_tavily_items(
                response.get("results", []),
                search_item=search_item,
                stage=stage,
                results_by_url=results_by_url,
                blocked_urls=blocked_urls,
                max_age_days=stage["recency_days"],
            )
            skipped_used_sources += skipped
            for reason, count in stage_rejects.items():
                pipeline_rejects[reason] = pipeline_rejects.get(reason, 0) + count

            if evaluator:
                for candidate in newly_added:
                    evaluator.evaluate_search_result(candidate)
                    if len(evaluator.kept) >= target_evaluated_kept:
                        goal_reached = True
                        print(
                            f"[{LOG_PREFIX}] Target reached: {len(evaluator.kept)} evaluated source(s) "
                            f"kept at score >= {KEEP_THRESHOLD} — stopping Tavily search "
                            f"(~{queries_run * TAVILY_ADVANCED_SEARCH_CREDITS} credits used so far)"
                        )
                        break

            if (
                not incremental_evaluate
                and target_results is not None
                and is_last_stage
                and len(results_by_url) >= target_results
            ):
                print(
                    f"[{LOG_PREFIX}] Target of {target_results} reached mid-stage — "
                    f"skipping remaining queries in {stage['name']}"
                )
                break

        search_kept = len(results_by_url)
        if evaluator:
            print(
                f"[{LOG_PREFIX}] Stage {stage['name']}: "
                f"{search_kept} search keep(s), {len(evaluator.kept)} evaluated keep(s)"
            )
        else:
            print(f"[{LOG_PREFIX}] Stage {stage['name']} total kept so far: {search_kept}")

    # ── National trade stage ──────────────────────────────────────────────────
    # Runs when: (a) user chose local+national or national-only (interactive),
    # or (b) CI mode and local stages kept 0 results (automatic fallback).
    # Geographic headline gates are disabled for these sources — industry
    # publications never mention the local metro but carry cost/claims data that the
    # industry_insight template frames for local homeowners.
    local_search_kept = len(results_by_url)
    evaluator_kept_count = len(evaluator.kept) if evaluator else 0

    # CI auto-fallback: trigger national trade when local stages found nothing.
    if not confirm_national_fallback and local_search_kept == 0 and evaluator_kept_count == 0:
        run_national_trade = True
        print(f"\n[{LOG_PREFIX}] No local results found — running national trade stage automatically.")

    if run_national_trade:
        if evaluator:
            evaluator.use_national_trade_prompt()
        print(
            f"[{LOG_PREFIX}] Running national trade stage "
            f"(~{NATIONAL_FALLBACK_MAX_CREDITS} credits, {len(NATIONAL_TRADE_QUERIES)} queries)"
        )
        fallback_cluster = _PROFILE.INSURANCE_CLUSTER_KEY
        fallback_cluster_data = STRATEGY_CLUSTERS[fallback_cluster]
        fallback_stage = {
            "name": "national_trade_7_day",
            "days": 7,
            "topic": "news",
            "search_depth": "advanced",
            "include_domains": NATIONAL_TRADE_SOURCES,
            "exclude_domains": None,
            "max_results_per_query": 8,
            "recency_days": 7,
        }
        fallback_queries_run = 0
        for query_str in NATIONAL_TRADE_QUERIES:
            credits_after = (fallback_queries_run + 1) * TAVILY_ADVANCED_SEARCH_CREDITS
            if credits_after > NATIONAL_FALLBACK_MAX_CREDITS:
                break
            fallback_item = {
                "query": query_str,
                "strategy_cluster": fallback_cluster,
                "pillar_topic": fallback_cluster_data["pillar_topic"],
                "trigger_window_hours": fallback_cluster_data["trigger_window_hours"],
            }
            try:
                response = client.search(
                    query=query_str,
                    search_depth=fallback_stage["search_depth"],
                    topic=fallback_stage["topic"],
                    days=fallback_stage["days"],
                    max_results=fallback_stage["max_results_per_query"],
                    include_domains=fallback_stage["include_domains"],
                    exclude_domains=fallback_stage["exclude_domains"],
                    include_raw_content=False,
                )
                fallback_queries_run += 1
                queries_run += 1
            except Exception as exc:
                print(f"[{LOG_PREFIX}] [national_trade_fallback] Query failed for {query_str!r}: {exc}")
                continue

            skipped, stage_rejects, newly_added = _ingest_tavily_items(
                response.get("results", []),
                search_item=fallback_item,
                stage=fallback_stage,
                results_by_url=results_by_url,
                blocked_urls=blocked_urls,
                max_age_days=7,
                is_national_trade=True,
            )
            skipped_used_sources += skipped
            for reason, count in stage_rejects.items():
                pipeline_rejects[reason] = pipeline_rejects.get(reason, 0) + count

            if evaluator:
                for candidate in newly_added:
                    evaluator.evaluate_search_result(candidate)
                    if len(evaluator.kept) >= NATIONAL_TRADE_TARGET_KEPT:
                        print(
                            f"[{LOG_PREFIX}] Target reached: {len(evaluator.kept)} source(s) kept "
                            f"— stopping national trade stage "
                            f"(~{fallback_queries_run * TAVILY_ADVANCED_SEARCH_CREDITS} credits used)"
                        )
                        break
                else:
                    continue
                break  # break outer query loop too

        fallback_credits = fallback_queries_run * TAVILY_ADVANCED_SEARCH_CREDITS
        print(
            f"[{LOG_PREFIX}] Stage national_trade_7_day: "
            f"{len(results_by_url)} search keep(s) "
            f"({fallback_queries_run} queries, ~{fallback_credits} credits)"
        )
        if evaluator:
            evaluator.save_outputs()
            evaluator.print_summary()
            record_evaluate_cost(evaluator.build_run_report())
    elif evaluator:
        evaluator.save_outputs()
        evaluator.print_summary()
        record_evaluate_cost(evaluator.build_run_report())

    results = _sort_results(list(results_by_url.values()))
    if target_results is not None:
        results = results[:target_results]

    credits_used = queries_run * TAVILY_ADVANCED_SEARCH_CREDITS
    record_search_cost(queries_run=queries_run, credits_used=credits_used)
    mark_tavily_search_ran(credits_used=credits_used, queries_run=queries_run)
    skipped_note = f", skipped {skipped_used_sources} previously used" if skipped_used_sources else ""
    reject_note = ""
    if pipeline_rejects:
        reject_summary = ", ".join(
            f"{reason}={count}" for reason, count in sorted(pipeline_rejects.items())
        )
        reject_note = f"; rejects ({reject_summary})"

    domains = sorted({result["domain"] for result in results})
    print(
        f"[{LOG_PREFIX}] Found {len(results)} unique results "
        f"from {query_count} active queries "
        f"({queries_run} API calls, ~{credits_used} Tavily credits{skipped_note}{reject_note})"
    )
    search_cost_usd = estimate_tavily_cost_usd(credits_used)
    print(
        f"[{LOG_PREFIX}] Est. Tavily API cost: ${search_cost_usd:.4f} USD "
        f"({credits_used} credits @ ${tavily_usd_per_credit():.4f}/credit)"
    )
    print(f"[{LOG_PREFIX}] Domains ({len(domains)}): {', '.join(domains)}")

    return results


def _json_safe(value: object) -> object:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def save_results(results: list[dict], path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(_json_safe(results), f, indent=2)
    print(f"[{LOG_PREFIX}] Results saved to {output_path}")
    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=f"{_PROFILE.METRO_AREA} / {_PROFILE.STATE} roofing search with multi-stage Tavily lookback."
    )
    parser.add_argument(
        "--target-results",
        type=int,
        default=DEFAULT_TARGET_RESULTS,
        help=f"Max results to keep; use 0 for no cap (default: {DEFAULT_TARGET_RESULTS}).",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"Reject sources older than this many days (default: {DEFAULT_MAX_AGE_DAYS}).",
    )
    parser.add_argument(
        "--max-credits",
        type=int,
        default=DEFAULT_MAX_TAVILY_CREDITS,
        help=f"Stop after this many Tavily credits (default: {DEFAULT_MAX_TAVILY_CREDITS}).",
    )
    parser.add_argument("--max-results-per-query", type=int, default=DEFAULT_MAX_RESULTS_PER_QUERY)
    parser.add_argument("--include-used-sources", action="store_true")
    parser.add_argument("--queries-per-cluster", type=int, default=DEFAULT_QUERIES_PER_CLUSTER)
    parser.add_argument("--all-queries", action="store_true")
    parser.add_argument(
        "--domain-stages",
        action="store_true",
        help=(
            "Run PRIORITY/SECONDARY/trade domain-locked stages before broad search "
            "(default: broad + official only; content filters still apply)."
        ),
    )
    parser.add_argument("--rotation-week", type=int)
    parser.add_argument(
        "--preferred-cluster",
        default=os.getenv("PIPELINE_PREFERRED_CLUSTER"),
        help=f"Run this strategy cluster's queries first (e.g. {_PROFILE.INSURANCE_CLUSTER_KEY}).",
    )
    parser.add_argument(
        "--confirm-national-fallback",
        action="store_true",
        help=(
            "If local/metro stages find nothing, prompt before running the national trade "
            "fallback stage. Pass this flag for interactive/local runs; omit it in CI so "
            "the fallback runs automatically."
        ),
    )
    parser.add_argument(
        "--incremental-evaluate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Evaluate each search keep immediately; stop Tavily after enough high-score sources (default: on).",
    )
    parser.add_argument(
        "--min-evaluated-kept",
        type=int,
        default=MIN_EVALUATED_KEPT_TO_PROCEED,
        help=(
            "With --incremental-evaluate: minimum kept sources required to proceed "
            f"(default: {MIN_EVALUATED_KEPT_TO_PROCEED})."
        ),
    )
    parser.add_argument(
        "--target-evaluated-kept",
        type=int,
        default=TARGET_EVALUATED_KEPT,
        help=(
            "With --incremental-evaluate: stop Tavily early after this many kept sources "
            f"(default: {TARGET_EVALUATED_KEPT}; search continues until credit cap if below target)."
        ),
    )
    parser.add_argument(
        "--evaluate-model",
        default=os.getenv("CLAUDE_EVALUATION_MODEL") or os.getenv("TOGETHER_EVALUATION_MODEL"),
        help="LLM model for incremental evaluate (provider default if unset).",
    )
    args = parser.parse_args()

    if args.target_evaluated_kept < args.min_evaluated_kept:
        parser.error(
            f"--target-evaluated-kept ({args.target_evaluated_kept}) must be >= "
            f"--min-evaluated-kept ({args.min_evaluated_kept})"
        )

    target = None if args.target_results == 0 else args.target_results

    print("=" * 60)
    print(f"{_PROFILE.COMPANY_SHORT} Blog Pipeline - search.py")
    print(
        f"Target {target or 'all'} results | keep within {args.max_age_days}d | "
        f"{_PROFILE.METRO_AREA} / {_PROFILE.STATE} headline + roofing/storm signals | "
        f"{'domain-locked stages' if args.domain_stages else 'broad + official (content-first)'}"
    )
    print("=" * 60)

    try:
        results = search_roofing_news(
            max_results_per_query=args.max_results_per_query,
            target_results=target,
            max_age_days=args.max_age_days,
            max_tavily_credits=args.max_credits,
            skip_used_sources=not args.include_used_sources,
            queries_per_cluster=args.queries_per_cluster,
            use_all_queries=args.all_queries,
            rotation_week=args.rotation_week,
            preferred_cluster=args.preferred_cluster,
            incremental_evaluate=args.incremental_evaluate,
            min_evaluated_kept=args.min_evaluated_kept,
            target_evaluated_kept=args.target_evaluated_kept,
            evaluate_model=args.evaluate_model,
            use_domain_stages=args.domain_stages,
            confirm_national_fallback=args.confirm_national_fallback,
        )
    except (EnvironmentError, RuntimeError) as exc:
        print(f"\n[{LOG_PREFIX}] Setup needed: {exc}")
        raise SystemExit(1) from exc

    save_results(results)
    if args.incremental_evaluate:
        kept_count = 0
        if DEFAULT_KEPT_PATH.exists():
            with DEFAULT_KEPT_PATH.open(encoding="utf-8") as handle:
                kept_payload = json.load(handle)
            if isinstance(kept_payload, list):
                kept_count = len(kept_payload)
        if kept_count < args.min_evaluated_kept:
            print(
                f"\n[{LOG_PREFIX}] Incremental search finished with {kept_count} kept source(s) "
                f"(need {args.min_evaluated_kept} at score >= {KEEP_THRESHOLD}). "
                "Try again later, --all-queries, --include-used-sources, or --domain-stages."
            )
            raise SystemExit(1)
        print(
            f"\n[{LOG_PREFIX}] Done — {kept_count} evaluated keep(s) ready for write "
            f"({len(results)} search candidate(s) logged)"
        )
    elif not results:
        print(
            "\n[!] No results kept. Try --include-used-sources, --all-queries, "
            "or --domain-stages for local-TV-first search."
        )
    else:
        print(f"\n[{LOG_PREFIX}] Done — {len(results)} results saved")
