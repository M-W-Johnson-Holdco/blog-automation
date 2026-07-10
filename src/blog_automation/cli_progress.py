"""Live terminal status lines for long-running write_serverless stages."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, TypeVar


T = TypeVar("T")

DEFAULT_INTERVAL_SECONDS = 5.0


def progress_enabled() -> bool:
    if os.getenv("WRITE_NO_PROGRESS", "").lower() in {"1", "true", "yes"}:
        return False
    return sys.stdout.isatty()


def progress_interval_seconds() -> float:
    return DEFAULT_INTERVAL_SECONDS


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def progress_bar(percent: float, width: int = 24) -> str:
    bounded = max(0.0, min(100.0, percent))
    filled = int(round(width * bounded / 100))
    return f"[{'#' * filled}{'-' * (width - filled)}] {bounded:5.1f}%"


class StatusTicker:
    """Print a single updating status line every N seconds."""

    def __init__(
        self,
        label: str,
        *,
        interval_seconds: float | None = None,
        estimated_seconds: float | None = None,
    ) -> None:
        self.label = label
        self.interval_seconds = interval_seconds or progress_interval_seconds()
        self.estimated_seconds = estimated_seconds
        self.started_at = time.monotonic()
        self._last_line_length = 0
        self._enabled = progress_enabled()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def percent_from_elapsed(self, cap: float = 99.0) -> float:
        if not self.estimated_seconds or self.estimated_seconds <= 0:
            return cap
        return min(cap, 100.0 * self.elapsed() / self.estimated_seconds)

    def render(self, *, detail: str = "", percent: float | None = None) -> str:
        elapsed = format_duration(self.elapsed())
        if percent is None and self.estimated_seconds:
            percent = self.percent_from_elapsed()

        if percent is not None:
            body = f"{self.label} {progress_bar(percent)}  elapsed {elapsed}"
        else:
            body = f"{self.label} ...  elapsed {elapsed}"

        if detail:
            body = f"{body}  |  {detail}"
        return body

    def tick(self, *, detail: str = "", percent: float | None = None) -> None:
        if not self._enabled:
            return
        line = self.render(detail=detail, percent=percent)
        padding = max(0, self._last_line_length - len(line))
        sys.stdout.write("\r" + line + (" " * padding))
        sys.stdout.flush()
        self._last_line_length = len(line)

    def clear_line(self) -> None:
        if self._enabled and self._last_line_length:
            sys.stdout.write("\r" + (" " * self._last_line_length) + "\r")
            sys.stdout.flush()
            self._last_line_length = 0

    def finish(self, message: str) -> None:
        elapsed = format_duration(self.elapsed())
        self.clear_line()
        print(f"{message} ({elapsed})")

    def reset_phase(self, label: str, *, estimated_seconds: float | None = None) -> None:
        """Switch to a new phase label and restart elapsed time for progress."""
        self.clear_line()
        self.label = label
        self.estimated_seconds = estimated_seconds
        self.started_at = time.monotonic()


def wait_with_progress(
    label: str,
    *,
    timeout_seconds: float,
    poll_fn: Callable[[], tuple[bool, str]],
    interval_seconds: float | None = None,
) -> None:
    """Poll until `poll_fn` returns (done=True, detail=...)."""
    interval = interval_seconds or progress_interval_seconds()

    if not progress_enabled():
        deadline = time.monotonic() + timeout_seconds
        last_detail = ""
        while time.monotonic() < deadline:
            done, detail = poll_fn()
            if detail != last_detail:
                print(f"{label}: {detail}")
                last_detail = detail
            if done:
                print(f"{label} complete")
                return
            time.sleep(interval)

        detail = poll_fn()[1]
        raise TimeoutError(
            f"{label} timed out after {format_duration(timeout_seconds)} (last status: {detail})."
        )

    ticker = StatusTicker(
        label,
        interval_seconds=interval,
        estimated_seconds=timeout_seconds,
    )
    deadline = time.monotonic() + timeout_seconds
    next_tick = ticker.started_at

    while time.monotonic() < deadline:
        done, detail = poll_fn()
        now = time.monotonic()
        if now >= next_tick:
            ticker.tick(detail=detail)
            next_tick = now + ticker.interval_seconds

        if done:
            ticker.finish(f"{label} complete")
            return

        time.sleep(min(ticker.interval_seconds, max(0.25, deadline - now)))

    detail = poll_fn()[1]
    raise TimeoutError(
        f"{label} timed out after {format_duration(timeout_seconds)} (last status: {detail})."
    )


def run_with_progress(
    label: str,
    fn: Callable[[], T],
    *,
    estimated_seconds: float = 180.0,
    interval_seconds: float | None = None,
) -> T:
    """Run a blocking function while showing elapsed time and an estimated bar."""
    if not progress_enabled():
        print(f"{label}...")
        return fn()

    ticker = StatusTicker(
        label,
        interval_seconds=interval_seconds,
        estimated_seconds=estimated_seconds,
    )
    result: list[T] = []
    error: list[BaseException] = []
    done = threading.Event()

    def worker() -> None:
        try:
            result.append(fn())
        except BaseException as exc:
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=worker, name="write-progress-worker", daemon=True)
    thread.start()

    while not done.wait(timeout=ticker.interval_seconds):
        ticker.tick(detail="in progress")

    thread.join()
    if error:
        if ticker._last_line_length:
            sys.stdout.write("\r" + (" " * ticker._last_line_length) + "\r")
            sys.stdout.flush()
        raise error[0]

    ticker.finish(f"{label} complete")
    return result[0]
