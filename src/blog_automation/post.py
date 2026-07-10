"""Predictive Sales AI (PSAI) blog posting client.

Posts approved Markdown drafts to the company website via POST /v1/blogs.
API docs: https://developers.predictivesalesai.com/swagger/index.html
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from blog_automation.draft_approval import (
    get_approval_block,
    load_validation_report,
    resolve_draft_path_from_report,
    save_validation_report,
)
from blog_automation.draft_pdf import markdown_body_to_html, normalize_text_for_pdf
from blog_automation.paths import PROJECT_ROOT
from blog_automation.write_common import (
    draft_validation_json_path,
    extract_opening_paragraph,
    first_heading,
)
from blog_automation.company import get_company_slug, get_profile

_PROFILE = get_profile()

STRATEGY_CATEGORY_LABELS: dict[str, str] = {
    "storm_damage": "Storm Damage",
    _PROFILE.INSURANCE_CLUSTER_KEY: "Insurance",
    "roof_safety": "Roof Safety",
    "county_guides": "County Guides",
    "local_roofing": "Roofing",
}

DEFAULT_SITE_BRAND = _PROFILE.COMPANY_NAME
PSAI_CONFIG_PATH = PROJECT_ROOT / "config" / f"psai.{get_company_slug()}.json"
VALID_STATUSES = frozenset({"published", "draft", "submitted"})
META_DESCRIPTION_MAX = 160
SOCIAL_DESCRIPTION_MAX = 150
# Used by approve_listen.py to identify publish-to-website Slack reactions.
PSAI_PUBLISH_REACTIONS = frozenset({"globe_with_meridians"})
# Slack thread commands after ✅ approval → PSAI status.
PSAI_POST_COMMAND_STATUSES: dict[str, str] = {
    "publish": "published",
    "draft": "draft",
}
RESPONSE_URL_KEYS = ("url", "blogUrl", "blog_url", "friendly_url", "public_url")
DEFAULT_REQUEST_TIMEOUT = 30.0

logger = logging.getLogger(__name__)


class PsaiError(RuntimeError):
    """PSAI API request failed."""


@dataclass(frozen=True)
class PsaiConfig:
    api_key: str
    api_url: str
    author: str
    default_status: str = "draft"
    notify_subscribers: bool = False
    auto_publish: bool = False
    display_author_details: bool = True
    site_brand: str = DEFAULT_SITE_BRAND


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_psai_settings_file() -> dict[str, Any]:
    """Load non-secret PSAI settings from config/psai.<company>.json (committed to the repo)."""
    if not PSAI_CONFIG_PATH.is_file():
        return {}
    with PSAI_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {PSAI_CONFIG_PATH}")
    return data


def _file_str(settings: dict[str, Any], key: str, default: str = "") -> str:
    value = settings.get(key)
    if value is None:
        return default
    return str(value).strip()


def _file_bool(settings: dict[str, Any], key: str, default: bool) -> bool:
    if key not in settings:
        return default
    value = settings[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _env_or_file_str(env_name: str, settings: dict[str, Any], file_key: str, default: str = "") -> str:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return env_value
    file_value = _file_str(settings, file_key)
    return file_value or default


def load_psai_config() -> PsaiConfig | None:
    """Return PSAI settings when posting is configured, else None."""
    api_key = os.getenv("PSAI_API_KEY", "").strip()
    if not api_key:
        return None

    settings = load_psai_settings_file()
    api_url = _env_or_file_str("PSAI_API_URL", settings, "api_url").rstrip("/")
    author = _env_or_file_str("PSAI_AUTHOR", settings, "author")
    if not api_url:
        raise EnvironmentError(
            f"PSAI_API_KEY is set but api_url is missing. Set PSAI_API_URL or add api_url to {PSAI_CONFIG_PATH}."
        )
    if not author:
        raise EnvironmentError(
            f"PSAI_API_KEY is set but author is missing. Set PSAI_AUTHOR or add author to {PSAI_CONFIG_PATH}."
        )

    default_status = _env_or_file_str("PSAI_DEFAULT_STATUS", settings, "default_status", "draft").lower()
    if default_status not in VALID_STATUSES:
        raise ValueError(
            f"PSAI default_status must be one of {sorted(VALID_STATUSES)}; got {default_status!r}."
        )

    site_brand = _env_or_file_str("PSAI_SITE_BRAND", settings, "site_brand", DEFAULT_SITE_BRAND)

    notify_subscribers = (
        _env_bool("PSAI_NOTIFY_SUBSCRIBERS")
        if os.getenv("PSAI_NOTIFY_SUBSCRIBERS") is not None
        else _file_bool(settings, "notify_subscribers", False)
    )
    auto_publish = (
        _env_bool("PSAI_AUTO_PUBLISH")
        if os.getenv("PSAI_AUTO_PUBLISH") is not None
        else _file_bool(settings, "auto_publish", False)
    )
    display_author_details = (
        _env_bool("PSAI_DISPLAY_AUTHOR_DETAILS", True)
        if os.getenv("PSAI_DISPLAY_AUTHOR_DETAILS") is not None
        else _file_bool(settings, "display_author_details", True)
    )

    return PsaiConfig(
        api_key=api_key,
        api_url=api_url,
        author=author,
        default_status=default_status,
        notify_subscribers=notify_subscribers,
        auto_publish=auto_publish,
        display_author_details=display_author_details,
        site_brand=site_brand or DEFAULT_SITE_BRAND,
    )


def psai_configured() -> bool:
    try:
        return load_psai_config() is not None
    except (EnvironmentError, ValueError):
        return False


def blogs_endpoint(config: PsaiConfig) -> str:
    return f"{config.api_url}/v1/blogs"


def blog_endpoint(config: PsaiConfig, blog_id: str) -> str:
    return f"{blogs_endpoint(config)}/{blog_id}"


def _get_blog_id(response: dict[str, Any]) -> str | None:
    value = response.get("blogId") or response.get("blog_id")
    if value is None:
        return None
    return str(value)


def _get_request_id(body: dict[str, Any]) -> str | None:
    value = body.get("requestId") or body.get("request_id")
    if value is None:
        return None
    return str(value)


def publish_result_url(response: dict[str, Any]) -> str | None:
    for key in RESPONSE_URL_KEYS:
        value = response.get(key)
        if value:
            return str(value)
    logger.debug(
        "No URL key found in PSAI response; keys=%s",
        sorted(response.keys()),
    )
    return None


def _truncate(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _infer_strategy_cluster(report: dict[str, Any] | None) -> str:
    if not report:
        return "local_roofing"

    selection = report.get("source_selection")
    if isinstance(selection, dict):
        cluster = str(selection.get("strategy_cluster") or "").strip()
        if cluster:
            return cluster

    sources = report.get("sources_used") or []
    for item in sources:
        if isinstance(item, dict):
            cluster = str(item.get("strategy_cluster") or "").strip()
            if cluster:
                return cluster
    return "local_roofing"


def _categories_and_tags(report: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    cluster = _infer_strategy_cluster(report)
    category = STRATEGY_CATEGORY_LABELS.get(cluster, "Roofing")
    categories = ["Roofing", category]

    tags = list(_PROFILE.DEFAULT_TAGS)
    if report:
        locations = report.get("locations_found")
        if isinstance(locations, list):
            for location in locations[:4]:
                name = str(location).strip()
                if name and name not in tags:
                    tags.append(name)

    deduped_categories: list[str] = []
    for name in categories:
        if name and name not in deduped_categories:
            deduped_categories.append(name)

    deduped_tags: list[str] = []
    for name in tags:
        if name and name not in deduped_tags and "," not in name:
            deduped_tags.append(name)

    return deduped_categories, deduped_tags


def _meta_keywords(title: str, report: dict[str, Any] | None) -> list[str]:
    keywords = list(_PROFILE.DEFAULT_KEYWORDS)
    cluster = _infer_strategy_cluster(report)
    label = STRATEGY_CATEGORY_LABELS.get(cluster)
    if label:
        keywords.append(label.lower())
    title_words = [word for word in re.findall(r"[A-Za-z0-9']+", title) if len(word) > 3][:4]
    for word in title_words:
        lowered = word.lower()
        if lowered not in {item.lower() for item in keywords}:
            keywords.append(lowered)
    return keywords[:8]


def build_blog_payload(
    markdown: str,
    report: dict[str, Any] | None,
    config: PsaiConfig,
    *,
    status: str | None = None,
    notify_subscribers: bool | None = None,
) -> dict[str, Any]:
    title = first_heading(markdown) or "Blog post"
    opening = extract_opening_paragraph(markdown)
    meta_description = _truncate(opening, META_DESCRIPTION_MAX)
    social_description = _truncate(opening, SOCIAL_DESCRIPTION_MAX)
    categories, tags = _categories_and_tags(report)
    page_title = f"{title} | {config.site_brand}"

    resolved_status = (status or config.default_status).strip().lower()
    if resolved_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {resolved_status!r}; expected one of {sorted(VALID_STATUSES)}.")

    payload: dict[str, Any] = {
        "title": title,
        "status": resolved_status,
        "author": config.author,
        "display_author_details": config.display_author_details,
        "content": markdown_body_to_html(normalize_text_for_pdf(markdown)),
        "categories": categories,
        "tags": tags,
        "meta": {
            "page_title": page_title,
            "meta_description": meta_description,
            "meta_keywords": _meta_keywords(title, report),
        },
        "is_private": False,
        "notify_subscribers": config.notify_subscribers if notify_subscribers is None else notify_subscribers,
        "social": {
            "title": title,
            "description": social_description,
        },
    }
    return payload


def _parse_api_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"

    if isinstance(body, dict):
        details = body.get("details")
        if isinstance(details, list) and details:
            rendered = []
            for item in details:
                if isinstance(item, dict):
                    field = item.get("field") or item.get("path") or "field"
                    message = item.get("message") or item.get("detail") or str(item)
                    rendered.append(f"{field}: {message}")
                else:
                    rendered.append(str(item))
            if rendered:
                return "; ".join(rendered)
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if value:
                return str(value)
    return str(body)


def create_blog_post(
    payload: dict[str, Any],
    config: PsaiConfig,
    *,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    endpoint = blogs_endpoint(config)
    title = payload.get("title", "")
    status = payload.get("status", "")
    author = payload.get("author", "")

    logger.info("Posting to %s", endpoint)
    logger.info("Title: %s", title)
    logger.info("Status: %s", status)
    logger.info("Author: %s", author)

    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    if response.status_code == 201:
        body = response.json()
        if not isinstance(body, dict):
            raise PsaiError("PSAI returned 201 but the response body was not a JSON object.")
        blog_id = _get_blog_id(body)
        logger.info("Created successfully — blogId=%s", blog_id or "n/a")
        if not publish_result_url(body):
            logger.debug("201 response keys: %s", sorted(body.keys()))
        return body

    message = _parse_api_error(response)
    logger.error("PSAI request failed with HTTP %s: %s", response.status_code, message)
    if response.status_code == 401:
        raise PsaiError(f"PSAI unauthorized (check PSAI_API_KEY and blogs:write scope): {message}")
    if response.status_code == 403:
        raise PsaiError(f"PSAI forbidden (API key lacks blogs:write scope): {message}")
    if response.status_code == 400:
        raise PsaiError(f"PSAI validation failed: {message}")
    if response.status_code >= 500:
        request_id = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                parsed_request_id = _get_request_id(body)
                if parsed_request_id:
                    request_id = f" (requestId={parsed_request_id})"
        except ValueError:
            pass
        raise PsaiError(f"PSAI server error{request_id}: {message}")

    raise PsaiError(f"PSAI request failed with HTTP {response.status_code}: {message}")


def delete_blog_post(
    blog_id: str,
    config: PsaiConfig | None = None,
    *,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> bool:
    """Delete a PSAI blog by ID. Returns True when deleted or already absent."""
    resolved_config = config or load_psai_config()
    if resolved_config is None:
        raise EnvironmentError("PSAI posting is not configured.")

    endpoint = blog_endpoint(resolved_config, blog_id)
    logger.info("Deleting PSAI blog %s at %s", blog_id, endpoint)
    response = requests.delete(
        endpoint,
        headers={
            "Authorization": f"Bearer {resolved_config.api_key}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if response.status_code in {200, 204}:
        logger.info("Deleted PSAI blog %s", blog_id)
        return True
    if response.status_code == 404:
        logger.info("PSAI blog %s was already absent", blog_id)
        return True

    message = _parse_api_error(response)
    logger.error("PSAI delete failed with HTTP %s: %s", response.status_code, message)
    raise PsaiError(f"PSAI delete failed with HTTP {response.status_code}: {message}")


def psai_blog_id_from_report(report: dict[str, Any]) -> str | None:
    approval = report.get("approval")
    if not isinstance(approval, dict):
        return None
    psai = approval.get("psai")
    if not isinstance(psai, dict):
        return None
    blog_id = psai.get("blog_id")
    if blog_id is None:
        return None
    return str(blog_id).strip() or None


def clear_psai_publish_metadata(validation_path: Path, report: dict[str, Any]) -> None:
    approval = report.setdefault("approval", {})
    psai = approval.pop("psai", None)
    if isinstance(psai, dict):
        approval["psai_revoked"] = {
            "revoked_at": datetime.now(timezone.utc).isoformat(),
            "blog_id": psai.get("blog_id"),
            "url": psai.get("url"),
            "status": psai.get("status"),
        }
    save_validation_report(validation_path, report)


def undo_psai_publish_from_validation(
    validation_path: Path,
    report: dict[str, Any],
) -> tuple[bool, str]:
    """Delete PSAI blog for an approved draft and clear local publish metadata."""
    blog_id = psai_blog_id_from_report(report)
    if not blog_id:
        return False, ""

    if not psai_configured():
        clear_psai_publish_metadata(validation_path, report)
        return False, f"Cleared local PSAI metadata for `{blog_id}` (PSAI not configured here)."

    try:
        delete_blog_post(blog_id)
    except PsaiError as exc:
        return False, (
            f"Could not delete PSAI draft `{blog_id}` automatically ({exc}). "
            "Remove it manually in PSAI if needed."
        )

    clear_psai_publish_metadata(validation_path, report)
    return True, f"Removed PSAI draft `{blog_id}`."


def publish_markdown(
    markdown: str,
    report: dict[str, Any] | None = None,
    *,
    status: str | None = None,
    notify_subscribers: bool | None = None,
    config: PsaiConfig | None = None,
) -> dict[str, Any]:
    resolved_config = config or load_psai_config()
    if resolved_config is None:
        raise EnvironmentError("PSAI posting is not configured (set PSAI_API_KEY, PSAI_API_URL, PSAI_AUTHOR).")

    payload = build_blog_payload(
        markdown,
        report,
        resolved_config,
        status=status,
        notify_subscribers=notify_subscribers,
    )
    return create_blog_post(payload, resolved_config)


def resolve_validation_path(path: Path) -> Path:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    if resolved.suffix == ".json" and resolved.name.endswith("-validation.json"):
        return resolved
    if resolved.suffix == ".md":
        return draft_validation_json_path(resolved)
    raise ValueError(
        "Expected a Markdown draft path or *-validation.json file "
        f"(got {path})."
    )


def publish_from_validation_path(
    validation_path: Path,
    *,
    status: str | None = None,
    notify_subscribers: bool | None = None,
    published_by: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    validation_path = resolve_validation_path(validation_path)
    report = load_validation_report(validation_path)
    draft_path = resolve_draft_path_from_report(report)
    if not draft_path or not draft_path.is_file():
        raise FileNotFoundError(f"Draft Markdown not found for {validation_path}")

    markdown = draft_path.read_text(encoding="utf-8")
    config = load_psai_config()
    if config is None:
        raise EnvironmentError("PSAI posting is not configured.")

    payload = build_blog_payload(
        markdown,
        report,
        config,
        status=status,
        notify_subscribers=notify_subscribers,
    )

    if dry_run:
        return {"dry_run": True, "payload": payload, "validation_path": str(validation_path)}

    response = create_blog_post(payload, config)
    record_publish_result(
        validation_path,
        report,
        response,
        published_by=published_by,
        status=payload["status"],
    )
    return response


def record_publish_result(
    validation_path: Path,
    report: dict[str, Any],
    response: dict[str, Any],
    *,
    published_by: str | None = None,
    status: str,
    error: str | None = None,
) -> None:
    approval = report.setdefault("approval", {})
    psai = approval.setdefault("psai", {})
    psai["status"] = status
    if error:
        psai["error"] = error
        psai.pop("blog_id", None)
        psai.pop("url", None)
    else:
        psai["published_at"] = datetime.now(timezone.utc).isoformat()
        psai.pop("error", None)
        blog_id = _get_blog_id(response)
        if blog_id is not None:
            psai["blog_id"] = blog_id
        url = publish_result_url(response)
        if url:
            psai["url"] = url
    if published_by:
        psai["published_by"] = published_by
    save_validation_report(validation_path, report)


def parse_psai_post_command(text: str) -> str | None:
    """Map Slack thread command (`publish` / `draft`) to PSAI status, or None."""
    return PSAI_POST_COMMAND_STATUSES.get(text.strip().lower())


def psai_publish_command_prompt_text() -> str:
    return (
        "Reply `publish` to post live on the site, or `draft` to save in PSAI drafts only "
        "(only while your :white_check_mark: is still on the intro message above). "
        "Remove your :white_check_mark: to cancel before sending."
    )


def psai_publish_offer_text() -> str:
    settings = load_psai_settings_file()
    default_status = _env_or_file_str("PSAI_DEFAULT_STATUS", settings, "default_status", "draft")
    auto_publish = (
        _env_bool("PSAI_AUTO_PUBLISH")
        if os.getenv("PSAI_AUTO_PUBLISH") is not None
        else _file_bool(settings, "auto_publish", False)
    )
    lines = [
        "Website publishing is enabled. Click the :globe_with_meridians: reaction on this message "
        f"to post the approved draft to the site as `{default_status}`.",
    ]
    if not auto_publish:
        lines.append(f"Set `auto_publish` to true in `config/psai.{get_company_slug()}.json` to publish immediately on approval.")
    return " ".join(lines)


def psai_publish_success_text(response: dict[str, Any], *, status: str) -> str:
    blog_id = _get_blog_id(response)
    if status == "draft":
        lead = "Sent to PSAI as a *draft* (not live on the site yet)."
    else:
        lead = f"Posted to the website as *{status}*."
    parts = [lead]
    if blog_id:
        parts.append(f"Blog ID: `{blog_id}`.")
    return " ".join(parts)


def psai_already_published(report: dict[str, Any]) -> bool:
    psai = get_approval_block(report).get("psai")
    if not isinstance(psai, dict):
        return False
    return bool(psai.get("blog_id") or psai.get("url"))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish an approved blog draft to Predictive Sales AI (POST /v1/blogs).",
    )
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        help="Markdown draft path or drafts_json/*-validation.json path.",
    )
    parser.add_argument(
        "--status",
        choices=sorted(VALID_STATUSES),
        help="Override PSAI_DEFAULT_STATUS (published, draft, or submitted).",
    )
    parser.add_argument(
        "--notify-subscribers",
        action="store_true",
        help="Email subscribers when publishing (published, non-private posts only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the JSON payload without calling the API.",
    )
    parser.add_argument(
        "--decision",
        choices=("approve", "revise"),
        help="GitHub Actions input: only publish when decision is approve.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="[post] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.decision == "revise":
        print("[post] Decision is revise; skipping website publish.")
        return

    if not args.target:
        parser.error("target draft or validation JSON path is required unless --decision revise")

    result = publish_from_validation_path(
        args.target,
        status=args.status,
        notify_subscribers=True if args.notify_subscribers else None,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(json.dumps(result["payload"], indent=2))
        print(f"[post] Dry run only — no API call made for {result['validation_path']}")
        return

    url = publish_result_url(result)
    blog_id = _get_blog_id(result)
    logger.info("Created blog post (blog_id=%s, url=%s)", blog_id or "n/a", url or "n/a")


if __name__ == "__main__":
    try:
        main()
    except (PsaiError, EnvironmentError, ValueError, FileNotFoundError) as exc:
        print(f"[post] Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
