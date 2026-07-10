"""Sync generated/ draft state with GitHub for cloud webhook handlers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from blog_automation.paths import GENERATED_DIR, PROJECT_ROOT
from blog_automation.company import get_profile

_DEFAULT_COMMITTER = f"{get_profile().COMPANY_SHORT} Slack Webhook"


def github_sync_enabled() -> bool:
    return bool(os.getenv("GITHUB_TOKEN", "").strip() and os.getenv("GITHUB_REPOSITORY", "").strip())


def _ensure_git_remote() -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        return
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _run_git(["remote", "set-url", "origin", remote_url])


def _ensure_git_identity() -> None:
    name = os.getenv("GIT_COMMITTER_NAME", _DEFAULT_COMMITTER).strip() or _DEFAULT_COMMITTER
    email = os.getenv("GIT_COMMITTER_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com").strip()
    _run_git(["config", "user.name", name])
    _run_git(["config", "user.email", email])


def _run_git(args: list[str]) -> None:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")


def pull_latest() -> None:
    if not github_sync_enabled():
        return
    _ensure_git_identity()
    _ensure_git_remote()
    branch = os.getenv("GITHUB_REF_NAME", os.getenv("GITHUB_BRANCH", "main")).strip() or "main"
    _run_git(["pull", "--rebase", "origin", branch])


def commit_paths(message: str, paths: list[Path | str]) -> bool:
    """Stage, commit, and push paths. Returns True if a commit was created."""
    if not github_sync_enabled():
        return False

    _ensure_git_identity()
    _ensure_git_remote()
    rel_paths = []
    for path in paths:
        rel = str(path)
        if isinstance(path, Path):
            rel = str(path.relative_to(PROJECT_ROOT)) if path.is_absolute() else str(path)
        candidate = PROJECT_ROOT / rel
        if candidate.exists() or rel.endswith("/"):
            rel_paths.append(rel)

    if not rel_paths:
        return False

    _run_git(["add", *rel_paths])
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    if status.returncode == 0:
        return False

    _run_git(["commit", "-m", message])
    _run_git(["push", "origin", "HEAD"])
    return True
