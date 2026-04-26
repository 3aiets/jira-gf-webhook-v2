"""FastAPI application — receives Jira Cloud Automation webhooks for project GF.

Endpoints
---------
GET  /healthz          -> liveness probe
POST /webhooks/jira    -> webhook receiver (validates secret, parses, persists)
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
from typing import Any, Dict

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import settings
from .models import JiraWebhookPayload, ParsedEvent
from .storage import save_event


# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #

class _JsonFormatter(logging.Formatter):
    """Render log records as a single-line JSON object — easy to ship to
    Datadog, ELK, CloudWatch, etc."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything attached via `logger.info(..., extra={...})` lands in __dict__
        for key, value in record.__dict__.items():
            if key in ("args", "msg", "levelname", "name", "exc_info", "exc_text",
                       "stack_info", "created", "msecs", "relativeCreated",
                       "levelno", "pathname", "filename", "module", "lineno",
                       "funcName", "thread", "threadName", "processName",
                       "process", "asctime", "message"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    return logging.getLogger("jira-webhook")


logger = _configure_logging()


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="Jira GF Webhook Receiver",
    version="2.0.0-dev",
    description="Receives Jira Cloud Automation webhooks for project GF.",
)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


def _verify_secret(provided: str | None) -> None:
    """Constant-time comparison so the response time doesn't leak the secret."""
    if not provided or not hmac.compare_digest(provided, settings.webhook_secret):
        # Log without echoing the provided value — we don't want secrets in logs
        # even if someone spams the wrong one.
        logger.warning("rejected webhook: invalid or missing secret")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Webhook-Secret",
        )


@app.post("/webhooks/jira")
async def receive_jira_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> JSONResponse:
    _verify_secret(x_webhook_secret)

    # Parse JSON ourselves so we control the error response shape on bad bodies.
    try:
        raw: Dict[str, Any] = await request.json()
    except json.JSONDecodeError as exc:
        logger.warning("rejected webhook: malformed JSON", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request body is not valid JSON",
        )

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON body must be an object",
        )

    try:
        payload = JiraWebhookPayload.model_validate(raw)
    except ValidationError as exc:
        # Most common cause: missing `issue.key`. Surface a useful error.
        logger.warning(
            "rejected webhook: payload missing required fields",
            extra={"errors": exc.errors()},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "invalid Jira webhook payload", "errors": exc.errors()},
        )

    parsed = ParsedEvent.from_payload(payload)

    # Optional project allow-list — protects against accidentally pointing
    # another project's automation at this endpoint.
    if settings.allowed_project_keys:
        if not parsed.project_key or parsed.project_key not in settings.allowed_project_keys:
            logger.warning(
                "rejected webhook: project not allowed",
                extra={
                    "issue_key": parsed.issue_key,
                    "project_key": parsed.project_key,
                    "allowed": settings.allowed_project_keys,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"project '{parsed.project_key}' is not in the allow-list",
            )

    try:
        path = save_event(settings.events_dir, parsed, raw)
    except OSError as exc:
        logger.error(
            "failed to persist event",
            extra={"issue_key": parsed.issue_key, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to persist event",
        )

    logger.info(
        "received jira webhook",
        extra={
            "issue_key": parsed.issue_key,
            "project_key": parsed.project_key,
            "event_type": parsed.event_type,
            "status": parsed.status,
            "priority": parsed.priority,
            "assignee": parsed.assignee,
            "stored_at": str(path),
        },
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"ok": True, "issue_key": parsed.issue_key},
    )


# --------------------------------------------------------------------------- #
# Local dev entrypoint: `python -m app.main`
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
