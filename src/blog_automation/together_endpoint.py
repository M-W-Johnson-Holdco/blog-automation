"""Start and stop Together AI dedicated endpoints around inference runs.

When using `--dedicated-endpoint`, write_serverless uses this module to
start the endpoint before generation and stop it afterward, even if generation
fails. Set Together's endpoint auto-shutdown (inactive timeout) to 10-30 minutes
as a billing safety net if cleanup fails.
"""

from __future__ import annotations

from blog_automation.paths import PROJECT_ROOT

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

from blog_automation.cli_progress import StatusTicker, format_duration, progress_enabled, progress_interval_seconds



READY_STATES = frozenset({"STARTED"})
STOPPED_STATES = frozenset({"STOPPED"})
DEPLOYING_STATES = frozenset({"PENDING"})
STARTING_STATES = frozenset({"STARTING"})
TRANSITIONAL_STATES = DEPLOYING_STATES | STARTING_STATES | frozenset({"STOPPING"})
TERMINAL_FAILURE_STATES = frozenset({"ERROR", "FAILED"})


def get_together_client():
    try:
        from together import Together
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: together. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("TOGETHER_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "TOGETHER_API_KEY is not set. Add it to .env before managing dedicated endpoints."
        )

    return Together(api_key=api_key)


def fetch_endpoint_raw(client, endpoint_id: str) -> dict[str, Any]:
    """Fetch endpoint JSON without SDK pydantic validation (handles FAILED state)."""
    from together.abstract import api_requestor
    from together.types import TogetherRequest

    requestor = api_requestor.APIRequestor(client=client.client)
    response, _, _ = requestor.request(
        options=TogetherRequest(
            method="GET",
            url=f"endpoints/{endpoint_id}",
        ),
        stream=False,
    )
    if not isinstance(response.data, dict):
        raise RuntimeError(f"Unexpected Together endpoint response for {endpoint_id}.")
    return response.data


def endpoint_state_from_raw(data: dict[str, Any]) -> str:
    return str(data.get("state", "") or "").upper()


def endpoint_failure_reason(data: dict[str, Any]) -> str:
    reason = str(data.get("reason_for_state", "") or "").strip()
    return reason or "No reason provided by Together."


def check_terminal_failure(endpoint_id: str, state: str, data: dict[str, Any] | None = None) -> None:
    if state in TERMINAL_FAILURE_STATES:
        reason = endpoint_failure_reason(data or {})
        raise RuntimeError(
            f"Together endpoint {endpoint_id} entered {state} state. "
            f"Reason: {reason} "
            "Check the Together dashboard, then rerun write_serverless --dedicated-endpoint."
        )


def wait_until_started(
    client,
    endpoint_id: str,
    *,
    poll_interval_seconds: float,
    deploy_timeout_seconds: float,
    start_timeout_seconds: float,
    max_retries: int = 2,
) -> str:
    """Wait for STARTED using separate deploy and ready budgets.

    - PENDING (deploying hardware): bounded by deploy_timeout_seconds, no % bar.
    - STARTING (becoming ready): bounded by start_timeout_seconds, % bar uses this budget.
    """
    deploy_deadline = time.monotonic() + deploy_timeout_seconds
    ready_started_at: float | None = None
    phase: str | None = None
    saw_pending = False
    ready_phase_locked = False
    announced_ready_phase = False
    retries_left = max_retries
    interval = poll_interval_seconds
    next_tick = time.monotonic()
    last_detail = ""
    ticker = StatusTicker("[endpoint] starting", interval_seconds=interval, estimated_seconds=None)

    while True:
        data = fetch_endpoint_raw(client, endpoint_id)
        state = endpoint_state_from_raw(data)
        model_name = str(data.get("name", "") or "")
        detail = f"state={state}"
        percent: float | None = None

        if state in READY_STATES:
            ticker.finish("[endpoint] waiting for ready")
            return model_name

        if state in TERMINAL_FAILURE_STATES:
            reason = endpoint_failure_reason(data)
            if retries_left > 0:
                retries_left -= 1
                print(
                    f"[endpoint] {state} while starting: {reason} "
                    f"Retrying ({retries_left} retries left)..."
                )
                client.endpoints.update(endpoint_id, state="STARTED")
                phase = None
                ready_started_at = None
                saw_pending = False
                ready_phase_locked = False
                announced_ready_phase = False
                deploy_deadline = time.monotonic() + deploy_timeout_seconds
                ticker.reset_phase("[endpoint] deploying", estimated_seconds=None)
                next_tick = time.monotonic()
                last_detail = ""
                time.sleep(interval)
                continue

            check_terminal_failure(endpoint_id, state, data)

        now = time.monotonic()

        if state in STARTING_STATES or (ready_phase_locked and state in DEPLOYING_STATES):
            if state in DEPLOYING_STATES:
                detail = f"state={state} (settling)"

            if not ready_phase_locked:
                ready_phase_locked = True
                ready_started_at = now
                phase = "ready"
                ticker.clear_line()
                if not announced_ready_phase:
                    announced_ready_phase = True
                    if saw_pending:
                        print("[endpoint] Deploy complete — waiting for endpoint to become ready...")
                    else:
                        print("[endpoint] Endpoint is starting up — waiting for ready...")
                ticker.reset_phase(
                    "[endpoint] waiting for ready",
                    estimated_seconds=start_timeout_seconds,
                )
                next_tick = now
            elif phase != "ready":
                phase = "ready"
                ticker.reset_phase(
                    "[endpoint] waiting for ready",
                    estimated_seconds=start_timeout_seconds,
                )

            ready_elapsed = now - (ready_started_at or now)
            if ready_elapsed > start_timeout_seconds:
                raise TimeoutError(
                    f"[endpoint] Ready wait timed out after {format_duration(start_timeout_seconds)} "
                    f"(state={state})."
                )
            percent = min(99.0, 100.0 * ready_elapsed / start_timeout_seconds)
            if not progress_enabled() and detail != last_detail:
                print(f"[endpoint] waiting for ready: {detail}")
        elif state in DEPLOYING_STATES:
            saw_pending = True
            if phase != "deploy":
                phase = "deploy"
                ticker.reset_phase("[endpoint] deploying", estimated_seconds=None)
            if now > deploy_deadline:
                raise TimeoutError(
                    f"[endpoint] Deploy timed out after {format_duration(deploy_timeout_seconds)} "
                    f"(last state: {state})."
                )
            if not progress_enabled() and detail != last_detail:
                print(f"[endpoint] deploying: {detail}")
        elif state in STOPPED_STATES:
            raise RuntimeError(
                f"Together endpoint {endpoint_id} returned to STOPPED while starting."
            )
        else:
            raise RuntimeError(
                f"Together endpoint {endpoint_id} has unexpected state '{state}' while starting."
            )

        if progress_enabled() and now >= next_tick:
            ticker.tick(detail=detail, percent=percent)
            next_tick = now + interval

        last_detail = detail
        time.sleep(min(interval, max(0.25, deploy_deadline - now)))


def wait_for_endpoint_state(
    client,
    endpoint_id: str,
    *,
    target_states: frozenset[str],
    label: str,
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_detail = ""

    while time.monotonic() < deadline:
        data = fetch_endpoint_raw(client, endpoint_id)
        state = endpoint_state_from_raw(data)
        detail = f"state={state}"

        if state in target_states:
            if not progress_enabled():
                print(f"{label} complete")
            return

        check_terminal_failure(endpoint_id, state, data)

        if detail != last_detail and not progress_enabled():
            print(f"{label}: {detail}")
            last_detail = detail

        time.sleep(poll_interval_seconds)

    data = fetch_endpoint_raw(client, endpoint_id)
    state = endpoint_state_from_raw(data)
    raise TimeoutError(
        f"{label} timed out after {format_duration(timeout_seconds)} (last state: {state})."
    )


def start_endpoint(
    client,
    endpoint_id: str,
    *,
    poll_interval_seconds: float = 5.0,
    deploy_timeout_seconds: float = 2400.0,
    start_timeout_seconds: float = 900.0,
    max_retries: int | None = None,
) -> str:
    """Start a dedicated endpoint and wait until it accepts inference."""
    data = fetch_endpoint_raw(client, endpoint_id)
    state = endpoint_state_from_raw(data)
    model_name = str(data.get("name", "") or "")

    if state in READY_STATES:
        print(f"[endpoint] Already running: {endpoint_id}")
        return model_name

    if state in STOPPED_STATES:
        print(f"[endpoint] Starting {endpoint_id}...")
        client.endpoints.update(endpoint_id, state="STARTED")
    elif state in TERMINAL_FAILURE_STATES:
        print(f"[endpoint] Retrying start after {state} on {endpoint_id}...")
        client.endpoints.update(endpoint_id, state="STARTED")
    elif state in TRANSITIONAL_STATES:
        print(f"[endpoint] Waiting for in-progress transition on {endpoint_id}...")
    else:
        print(f"[endpoint] Requesting start for {endpoint_id} (current state: {state})...")
        client.endpoints.update(endpoint_id, state="STARTED")

    if max_retries is None:
        max_retries = int(os.getenv("TOGETHER_ENDPOINT_MAX_RETRIES", "2"))

    return wait_until_started(
        client,
        endpoint_id,
        poll_interval_seconds=poll_interval_seconds,
        deploy_timeout_seconds=deploy_timeout_seconds,
        start_timeout_seconds=start_timeout_seconds,
        max_retries=max_retries,
    )


def stop_endpoint(
    client,
    endpoint_id: str,
    *,
    poll_interval_seconds: float = 5.0,
    timeout_seconds: float = 300.0,
) -> None:
    """Stop a dedicated endpoint and wait until billing pauses."""
    data = fetch_endpoint_raw(client, endpoint_id)
    state = endpoint_state_from_raw(data)

    if state in STOPPED_STATES:
        print(f"[endpoint] Already stopped: {endpoint_id}")
        return

    if state in TERMINAL_FAILURE_STATES:
        print(f"[endpoint] Skipping stop for {endpoint_id} (state={state}).")
        return

    print(f"[endpoint] Stopping {endpoint_id}...")
    client.endpoints.update(endpoint_id, state="STOPPED")
    wait_for_endpoint_state(
        client,
        endpoint_id,
        target_states=STOPPED_STATES,
        label="[endpoint] waiting for stop",
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )


@contextmanager
def managed_dedicated_endpoint(
    endpoint_id: str | None = None,
    *,
    poll_interval_seconds: float | None = None,
    deploy_timeout_seconds: float | None = None,
    start_timeout_seconds: float | None = None,
    stop_timeout_seconds: float | None = None,
) -> Iterator[str | None]:
    """Context manager that starts an endpoint on enter and stops it on exit."""
    load_dotenv(PROJECT_ROOT / ".env")
    endpoint_id = endpoint_id or os.getenv("TOGETHER_DEDICATED_ENDPOINT_ID")
    if not endpoint_id:
        yield None
        return

    poll_interval_seconds = poll_interval_seconds or float(
        os.getenv("TOGETHER_ENDPOINT_POLL_INTERVAL", str(progress_interval_seconds()))
    )
    deploy_timeout_seconds = deploy_timeout_seconds or float(
        os.getenv("TOGETHER_ENDPOINT_DEPLOY_TIMEOUT", "2400")
    )
    start_timeout_seconds = start_timeout_seconds or float(
        os.getenv("TOGETHER_ENDPOINT_START_TIMEOUT", "900")
    )
    stop_timeout_seconds = stop_timeout_seconds or float(
        os.getenv("TOGETHER_ENDPOINT_STOP_TIMEOUT", "300")
    )

    client = get_together_client()
    model_name = None
    try:
        model_name = start_endpoint(
            client,
            endpoint_id,
            poll_interval_seconds=poll_interval_seconds,
            deploy_timeout_seconds=deploy_timeout_seconds,
            start_timeout_seconds=start_timeout_seconds,
        )
        if model_name:
            print(f"[endpoint] Inference model: {model_name}")
        yield model_name
    finally:
        try:
            stop_endpoint(
                client,
                endpoint_id,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=stop_timeout_seconds,
            )
        except Exception as exc:
            print(f"[endpoint] Warning: failed to stop {endpoint_id}: {exc}")
