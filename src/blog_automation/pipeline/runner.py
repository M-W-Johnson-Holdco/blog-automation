"""Run pipeline stages from ``src/blog_automation`` via ``python -m``."""

from __future__ import annotations

from blog_automation.paths import BYTECODE_CACHE_DIR, LOGS_DIR, PIPELINE_RUN_LOG_PATH, PROJECT_ROOT, SRC_DIR
from blog_automation.company import get_profile
from blog_automation.pipeline.write_serverless import (
    DEFAULT_SERVERLESS_MODEL,
)

import os
import subprocess
import sys
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Pipeline run log
# ---------------------------------------------------------------------------
# One .log file is created per pipeline session under output/logs/.  The path
# is stored in PIPELINE_LOG_FILE env-var so all stages in the same session
# append to the same file.  Subprocess stdout is tee'd to it while stderr is
# left un-redirected so interactive prompts stay visible in the terminal.

_PIPELINE_LOG_ENV = "PIPELINE_LOG_FILE"


def _log_file() -> Path | None:
    """Return the active pipeline log Path, or None if logging is not yet set up."""
    existing = os.environ.get(_PIPELINE_LOG_ENV, "").strip()
    return Path(existing) if existing else None


def _append_to_log(text: str) -> None:
    """Silently append *text* to the active log file (no-op when not set)."""
    path = _log_file()
    if path is None:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def init_pipeline_log_at(
    log_path: Path,
    *,
    title: str = f"{get_profile().COMPANY_SHORT} Blog Pipeline Log",
) -> Path:
    """Create (or replace) a session log at *log_path* and register it in the environment."""
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    header = (
        f"{title}\n"
        f"Started : {run_stamp}\n"
        f"Log file: {log_path.relative_to(PROJECT_ROOT)}\n"
        "\n"
    )
    log_path.write_text(header, encoding="utf-8")
    os.environ[_PIPELINE_LOG_ENV] = str(log_path)
    print(
        f"[pipeline] Log: {log_path.relative_to(PROJECT_ROOT)}",
        flush=True,
    )
    return log_path


def publish_canonical_pipeline_log(log_path: Path) -> Path | None:
    """Copy the active run log to ``output/drafts/pipeline_run.log`` for Slack/CI handoff.

    Slack uploads this canonical log as ``run.txt`` so the client previews it as text.
    """
    if not log_path.is_file():
        return None
    PIPELINE_RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(log_path, PIPELINE_RUN_LOG_PATH)
    return PIPELINE_RUN_LOG_PATH


def resolve_pipeline_log_path(report: dict[str, Any] | None = None) -> Path | None:
    """Find the pipeline run log for a draft (validation JSON, multi_run dir, or canonical copy)."""
    candidates: list[Path] = []

    if report:
        explicit = str(report.get("pipeline_log_path") or "").strip()
        if explicit:
            candidates.append(Path(explicit))

        generation = report.get("generation")
        if isinstance(generation, dict):
            multi_run_dir = str(generation.get("multi_run_dir") or "").strip()
            if multi_run_dir:
                candidates.append(Path(multi_run_dir) / "run.log")

    env_path = os.environ.get(_PIPELINE_LOG_ENV, "").strip()
    if env_path:
        candidates.append(Path(env_path))

    candidates.append(PIPELINE_RUN_LOG_PATH)

    seen: set[str] = set()
    for raw in candidates:
        path = raw if raw.is_absolute() else PROJECT_ROOT / raw
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    return None


def _ensure_pipeline_log() -> Path:
    """Return the active pipeline log, creating a new one if none exists yet."""
    existing = os.environ.get(_PIPELINE_LOG_ENV, "").strip()
    if existing:
        return Path(existing)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return init_pipeline_log_at(LOGS_DIR / f"pipeline_{run_stamp}.log")


# ---------------------------------------------------------------------------
# Keys used by pipeline.py and approve subprocess rewrites.
PIPELINE_MODULES: dict[str, str] = {
    "search": "blog_automation.pipeline.search",
    "evaluate": "blog_automation.pipeline.evaluate",
    "write_serverless": "blog_automation.pipeline.write_serverless",
    "write_multi": "blog_automation.pipeline.write_multi",
    "approve_listen": "blog_automation.pipeline.approve_listen",
    "post": "blog_automation.post",
    "clean_output": "blog_automation.tools.clean_output",
}

DEFAULT_REWRITE_MODULE_KEY = "write_serverless"

# Standalone search uses the CI-strong Tavily plan but prompts before the
# national trade fallback so a local operator can decide whether to spend the
# extra credits.  CI / full-pipeline runs omit --confirm-national-fallback so
# the fallback fires automatically when no local news is found.
DEFAULT_SEARCH_MODULE_ARGS: tuple[str, ...] = (
    "--all-queries",
    "--domain-stages",
    "--confirm-national-fallback",
)

# Same Tavily plan as CI / write_tournament --with-search (no interactive fallback prompt).
SEARCH_EVAL_CI_ARGS: tuple[str, ...] = tuple(
    arg for arg in DEFAULT_SEARCH_MODULE_ARGS if arg != "--confirm-national-fallback"
)

WRITE_TOURNAMENT_SCRIPT = "write_tournament.py"


def module_name(module_key: str) -> str:
    try:
        return PIPELINE_MODULES[module_key]
    except KeyError as exc:
        raise ValueError(f"Unknown pipeline module key: {module_key}") from exc


def pipeline_subprocess_env() -> dict[str, str]:
    """Environment for child processes so ``blog_automation`` imports resolve."""
    cache_dir = BYTECODE_CACHE_DIR.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(cache_dir)
    env["PYTHONUNBUFFERED"] = "1"
    src = str(SRC_DIR)
    existing = env.get("PYTHONPATH", "")
    if src not in existing.split(os.pathsep):
        env["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
    return env


def build_module_command(module_key: str, *args: str) -> list[str]:
    """Argv to run a package module from the repo root (``PYTHONPATH=src``)."""
    return [sys.executable, "-u", "-m", module_name(module_key), *args]


def build_root_script_command(script_name: str, *args: str) -> list[str]:
    """Argv to run a repo-root script (e.g. ``write_tournament.py``)."""
    return [sys.executable, "-u", str(PROJECT_ROOT / script_name), *args]


def build_write_tournament_args(
    *,
    with_search: bool = False,
    clear_drafts: bool = False,
    all_queries: bool = False,
    include_used_sources: bool = False,
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    """CLI args aligned with ``weekly.yml`` / ``write_tournament.py``."""
    args: list[str] = []
    if with_search:
        args.append("--with-search")
    if clear_drafts:
        args.append("--clear-drafts")
    if all_queries:
        args.append("--all-queries")
    if include_used_sources:
        args.append("--include-used-sources")
    args.extend(extra)
    return tuple(args)


def _run_subprocess_command(command: list[str], *, label: str) -> int:
    log_path = _ensure_pipeline_log()
    sep = "=" * STAGE_BANNER_WIDTH
    header_lines = [
        "",
        sep,
        f"[pipeline] Stage: {label}",
        f"[pipeline] Command: {' '.join(command)}",
        sep,
        "",
    ]
    header_text = "\n".join(header_lines) + "\n"
    print(header_text, end="", flush=True)
    _append_to_log(header_text)

    env = pipeline_subprocess_env()
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
    with open(log_path, "a", encoding="utf-8") as log_fh:
        for line in (process.stdout or []):
            sys.stdout.write(line)
            sys.stdout.flush()
            log_fh.write(line)
            log_fh.flush()
    return_code = process.wait()

    result_line = (
        f"[pipeline] {label} finished\n"
        if return_code == 0
        else f"[pipeline] {label} failed (exit {return_code})\n"
    )
    print(result_line, end="", flush=True)
    _append_to_log(result_line)
    return return_code


def run_root_script(script_name: str, *args: str, label: str | None = None) -> int:
    """Run a repo-root script with pipeline log tee (same as ``run_module``)."""
    command = build_root_script_command(script_name, *args)
    return _run_subprocess_command(command, label=label or script_name)


def run_repo_script(relative_path: str, *args: str, label: str | None = None) -> int:
    """Run a script under the repo root (e.g. ``scripts/archive_ci_draft.py``)."""
    command = [sys.executable, "-u", str(PROJECT_ROOT / relative_path), *args]
    return _run_subprocess_command(command, label=label or relative_path)


def run_write_tournament(*args: str, label: str = "Write tournament") -> int:
    return run_root_script(WRITE_TOURNAMENT_SCRIPT, *args, label=label)


STAGE_BANNER_WIDTH = 60


def print_stage_banner(*, stage_index: int, stage_total: int, label: str) -> None:
    line = "=" * STAGE_BANNER_WIDTH
    print(f"\n{line}", flush=True)
    print(f"[pipeline] Stage {stage_index}/{stage_total}: {label}", flush=True)
    print(line, flush=True)


def run_module(module_key: str, *args: str, label: str | None = None) -> int:
    command = build_module_command(module_key, *args)
    stage_label = label or module_key
    return _run_subprocess_command(command, label=stage_label)


def run_modules(stages: Sequence[tuple[str, tuple[str, ...]]]) -> None:
    for module_key, args in stages:
        code = run_module(module_key, *args)
        if code != 0:
            raise SystemExit(code)


def run_pipeline_restart(
    *,
    write_model: str | None = None,
    clear_drafts: bool = True,
    preferred_cluster: str | None = None,
    rotation_offset: int = 1,
    all_queries: bool = False,
    include_used_sources: bool = False,
    use_domain_stages: bool = True,
) -> int:
    """Run ``write_tournament.py --with-search`` (CI-aligned). Returns last stage exit code."""
    del preferred_cluster, rotation_offset, use_domain_stages  # search flags live in SEARCH_EVAL_CI_ARGS
    extra: list[str] = []
    if write_model:
        extra.extend(["--model", write_model])
    tournament_args = build_write_tournament_args(
        with_search=True,
        clear_drafts=clear_drafts,
        all_queries=all_queries,
        include_used_sources=include_used_sources,
        extra=extra,
    )
    line = "=" * STAGE_BANNER_WIDTH
    print(f"\n{line}", flush=True)
    print("[pipeline] Full restart: search + evaluate → write tournament", flush=True)
    print(f"{line}", flush=True)
    return run_write_tournament(
        *tournament_args,
        label="Search + evaluate → write tournament (CI)",
    )
