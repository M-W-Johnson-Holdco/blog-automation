"""Track blog source URLs so search can skip stories used in approved (published) blogs."""

from __future__ import annotations

from blog_automation.paths import PROJECT_ROOT

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


DEFAULT_USED_SOURCES_PATH = PROJECT_ROOT / "output" / "sources" / "used_sources.json"


def normalize_source_url(url: str) -> str:
    """Normalize a URL for stable deduplication."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""

    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


def source_url(item: dict[str, Any]) -> str:
    nested = item.get("source") if isinstance(item.get("source"), dict) else {}
    return str(item.get("url") or nested.get("url") or "").strip()


def source_title(item: dict[str, Any]) -> str:
    nested = item.get("source") if isinstance(item.get("source"), dict) else {}
    return str(item.get("title") or nested.get("title") or "Untitled source").strip()


def source_domain(item: dict[str, Any]) -> str:
    nested = item.get("source") if isinstance(item.get("source"), dict) else {}
    domain = str(item.get("domain") or nested.get("domain") or "").strip().lower()
    if domain:
        return domain.removeprefix("www.")
    normalized = normalize_source_url(source_url(item))
    if not normalized:
        return ""
    return urlparse(normalized).netloc.removeprefix("www.")


def load_used_sources(path: Path = DEFAULT_USED_SOURCES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []

    data = json.loads(raw)

    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        return [entry for entry in data["sources"] if isinstance(entry, dict)]
    raise ValueError(f"Expected a list of source records in {path}")


def used_source_urls(path: Path = DEFAULT_USED_SOURCES_PATH) -> set[str]:
    urls: set[str] = set()
    for entry in load_used_sources(path):
        normalized = entry.get("normalized_url") or normalize_source_url(str(entry.get("url", "")))
        if normalized:
            urls.add(normalized)
    return urls


def save_used_sources(records: list[dict[str, Any]], path: Path = DEFAULT_USED_SOURCES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(records),
        "sources": records,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def record_used_sources(
    selected_sources: list[dict[str, Any]],
    *,
    draft_path: str | Path,
    runner: str,
    path: Path = DEFAULT_USED_SOURCES_PATH,
) -> list[dict[str, Any]]:
    """Append or update registry entries for sources used in an approved blog draft."""
    now = datetime.now(timezone.utc).isoformat()
    draft_path_str = str(draft_path)
    records = load_used_sources(path)
    by_url = {
        str(entry.get("normalized_url") or normalize_source_url(str(entry.get("url", "")))): entry
        for entry in records
        if entry.get("normalized_url") or entry.get("url")
    }

    updated: list[dict[str, Any]] = []
    for item in selected_sources:
        url = source_url(item)
        normalized = normalize_source_url(url)
        if not normalized:
            continue

        nested = item.get("source") if isinstance(item.get("source"), dict) else {}
        record = by_url.get(normalized, {})
        record.update(
            {
                "url": url,
                "normalized_url": normalized,
                "domain": source_domain(item),
                "title": source_title(item),
                "strategy_cluster": item.get("strategy_cluster") or nested.get("strategy_cluster", ""),
                "first_used_at": record.get("first_used_at") or now,
                "last_used_at": now,
                "last_draft_path": draft_path_str,
                "last_runner": runner,
                "use_count": int(record.get("use_count") or 0) + 1,
            }
        )
        by_url[normalized] = record
        updated.append(record)

    if updated:
        save_used_sources(list(by_url.values()), path)
    return updated


def seed_used_sources_from_validation_json(
    validation_path: Path,
    *,
    runner: str = "seed-validation",
    path: Path = DEFAULT_USED_SOURCES_PATH,
) -> int:
    """Record sources from a draft validation JSON (e.g. already-approved blog)."""
    with validation_path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    if not isinstance(report, dict):
        raise ValueError(f"Expected a JSON object in {validation_path}")

    draft_path = report.get("draft_path") or f"seeded-from:{validation_path.name}"
    updated = record_used_sources_from_validation_report(
        report,
        draft_path=draft_path,
        runner=runner,
        path=path,
    )
    return len(updated)


def seed_used_sources_from_file(
    sources_path: Path,
    *,
    draft_path: str = "",
    runner: str = "seed",
    path: Path = DEFAULT_USED_SOURCES_PATH,
) -> int:
    """Record URLs from a kept/evaluated sources JSON file without generating a draft."""
    with sources_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list in {sources_path}")

    updated = record_used_sources(
        data,
        draft_path=draft_path or f"seeded-from:{sources_path.name}",
        runner=runner,
        path=path,
    )
    return len(updated)


def record_used_sources_from_validation_report(
    validation_report: dict[str, Any],
    *,
    draft_path: str | Path,
    runner: str,
    path: Path = DEFAULT_USED_SOURCES_PATH,
) -> list[dict[str, Any]]:
    """Record sources listed in a draft validation JSON after Slack approval."""
    sources = validation_report.get("sources_used")
    if not isinstance(sources, list) or not sources:
        return []
    return record_used_sources(
        sources,
        draft_path=draft_path,
        runner=runner,
        path=path,
    )


def remove_used_sources_from_validation_report(
    validation_report: dict[str, Any],
    *,
    draft_path: str | Path,
    path: Path = DEFAULT_USED_SOURCES_PATH,
) -> list[str]:
    """Undo used-source registry entries tied to one un-approved draft."""
    sources = validation_report.get("sources_used")
    if not isinstance(sources, list) or not sources:
        return []

    draft_path_str = str(draft_path)
    target_urls = {
        str(item.get("normalized_url") or normalize_source_url(str(item.get("url", ""))))
        for item in sources
        if isinstance(item, dict)
    }
    target_urls.discard("")

    records = load_used_sources(path)
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    changed = False

    for entry in records:
        normalized = str(
            entry.get("normalized_url") or normalize_source_url(str(entry.get("url", "")))
        ).strip()
        if normalized not in target_urls or entry.get("last_draft_path") != draft_path_str:
            kept.append(entry)
            continue

        use_count = int(entry.get("use_count") or 1) - 1
        changed = True
        if use_count <= 0:
            removed.append(normalized)
            continue

        entry["use_count"] = use_count
        kept.append(entry)

    if changed:
        save_used_sources(kept, path)
    return removed


def sources_used_payload(selected_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in selected_sources:
        url = source_url(item)
        if not url:
            continue
        payload.append(
            {
                "url": url,
                "normalized_url": normalize_source_url(url),
                "domain": source_domain(item),
                "title": source_title(item),
                "strategy_cluster": item.get("strategy_cluster")
                or (item.get("source") or {}).get("strategy_cluster", ""),
            }
        )
    return payload


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Manage used blog source URLs.")
    parser.add_argument("--list", action="store_true", help="Print recorded used sources.")
    parser.add_argument(
        "--seed",
        type=Path,
        help="Record every URL from a kept/evaluated sources JSON file.",
    )
    parser.add_argument(
        "--seed-validation",
        type=Path,
        help="Record sources from a draft validation JSON (approved blog backfill).",
    )
    args = parser.parse_args()

    if args.seed_validation:
        count = seed_used_sources_from_validation_json(args.seed_validation)
        print(f"[used_sources] Recorded {count} source URL(s) from {args.seed_validation}")
        return

    if args.seed:
        count = seed_used_sources_from_file(args.seed)
        print(f"[used_sources] Recorded {count} source URL(s) from {args.seed}")
        return

    records = load_used_sources()
    if not records:
        print("[used_sources] Registry is empty.")
        return

    print(f"[used_sources] {len(records)} recorded source URL(s):")
    for entry in sorted(records, key=lambda item: str(item.get("last_used_at", "")), reverse=True):
        print(f"  - {entry.get('title', 'Untitled')}")
        print(f"    {entry.get('url', '')}")
        print(f"    last used: {entry.get('last_used_at', 'unknown')}")


if __name__ == "__main__":
    main()
