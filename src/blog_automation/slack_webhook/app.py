"""HTTP entrypoint for Slack Events API (deploy to Render, Railway, Fly.io, etc.)."""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

from blog_automation.paths import PROJECT_ROOT
from blog_automation.company import get_profile
from blog_automation.slack_webhook.events import process_slack_event_with_git_sync, verify_slack_signature

load_dotenv(PROJECT_ROOT / ".env")

try:
    from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError as exc:
    raise RuntimeError(
        "Missing FastAPI. Install webhook dependencies with "
        "`python -m pip install -r requirements.txt`."
    ) from exc

app = FastAPI(title=f"{get_profile().COMPANY_SHORT} Blog Slack Webhook", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=400, detail="Missing Slack signature headers.")

    try:
        verify_slack_signature(timestamp=timestamp, body=body, signature=signature)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    if payload.get("type") == "event_callback":
        event = payload.get("event") or {}
        if event.get("bot_id") or event.get("subtype") in {
            "bot_message",
            "message_changed",
            "message_deleted",
        }:
            return JSONResponse({"ok": True})
        background_tasks.add_task(process_slack_event_with_git_sync, event)

    return JSONResponse({"ok": True})


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        "blog_automation.slack_webhook.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
