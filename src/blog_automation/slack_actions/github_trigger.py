"""Trigger GitHub Actions workflows from Slack handlers."""

from __future__ import annotations

import os

import requests


def trigger_github_workflow(workflow_file: str, inputs: dict[str, str] | None = None) -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    ref = os.getenv("GITHUB_REF_NAME", os.getenv("GITHUB_BRANCH", "main")).strip() or "main"
    if not token or not repo:
        raise EnvironmentError("GITHUB_TOKEN and GITHUB_REPOSITORY are required to trigger workflows.")

    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": ref, "inputs": inputs or {}},
        timeout=30,
    )
    if response.status_code not in {201, 204}:
        raise RuntimeError(f"Failed to trigger {workflow_file}: HTTP {response.status_code} {response.text}")
