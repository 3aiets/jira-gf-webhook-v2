"""FastAPI application — receives Jira Cloud webhook deliveries for project GF.

Endpoints
---------
GET  /healthz          -> liveness probe
POST /webhooks/jira    -> webhook receiver

Inbound auth is selected by ``settings.auth_mode``:
  * ``jwt``           — modern OAuth 2.0 platform webhook. Verifies the
                        ``Authorization: Bearer <JWT>`` header (HS256, signed
                        with the OAuth app's client_secret).
  * ``shared_secret`` — legacy Jira Automation rule. Verifies the
                        ``X-Webhook-Secret`` header against ``WEBHOOK_SECRET``.

Replay/dedup uses ``X-Atlassian-Webhook-Identifier`` with an in-process LRU
(plan flagged Redis/SQLite as a future swap). ``X-Atlassian-Webhook-Retry`` is
logged so retried deliveries are visible.
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional

import jwt as pyjwt
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import AUTH_MODE_JWT, AUTH_MODE_SHARED_SECRET, settings
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
# Inbound auth
# --------------------------------------------------------------------------- #

def _verify_shared_secret(provided: Optional[str]) -> None:
    """Legacy path — constant-time compare against WEBHOOK_SECRET."""
    if not provided or not settings.webhook_secret or not hmac.compare_digest(
        provided, settings.webhook_secret
    ):
        logger.warning("rejected webhook: invalid or missing secret")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Webhook-Secret",
        )


def _verify_jwt(authorization: Optional[str]) -> Optional[str]:
    """Verify Atlassian's HS256 bearer JWT signed with the app's client_secret.

    Empirically Atlassian webhook JWTs carry: iss, sub, exp, iat, jti, context.
    No aud, no nbf. We enforce signature + exp + iss==client_id and tolerate
    120s of clock skew. Returns the ``sub`` claim (the actor's Atlassian
    account id) so it can be logged as a breadcrumb on the success path.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("rejected webhook: missing bearer token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = pyjwt.decode(
            token,
            settings.atlassian_client_secret,
            algorithms=["HS256"],
            leeway=120,
            issuer=settings.atlassian_client_id,
            options={"require": ["exp", "iss"], "verify_aud": False},
        )
    except pyjwt.InvalidTokenError as exc:
        # Never log the token itself — only the failure class.
        logger.warning("rejected webhook: invalid JWT", extra={"reason": type(exc).__name__})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired bearer token",
        )
    return claims.get("sub")


# --------------------------------------------------------------------------- #
# In-memory LRU dedup keyed by X-Atlassian-Webhook-Identifier
# --------------------------------------------------------------------------- #

_DEDUP_MAX = 10_000
_dedup_seen: "OrderedDict[str, None]" = OrderedDict()
_dedup_lock = Lock()


def _dedup_check_and_record(delivery_id: Optional[str]) -> bool:
    """Return True if this delivery_id was already seen (caller should short-circuit)."""
    if not delivery_id:
        return False
    with _dedup_lock:
        if delivery_id in _dedup_seen:
            _dedup_seen.move_to_end(delivery_id)
            return True
        _dedup_seen[delivery_id] = None
        if len(_dedup_seen) > _DEDUP_MAX:
            _dedup_seen.popitem(last=False)
        return False


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="Jira GF Webhook Receiver",
    version="2.0.0-dev",
    description="Receives Jira Cloud webhooks (platform OAuth 3LO or legacy Automation) for project GF.",
)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/jira")
async def receive_jira_webhook(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
    x_atlassian_webhook_identifier: Optional[str] = Header(
        default=None, alias="X-Atlassian-Webhook-Identifier"
    ),
    x_atlassian_webhook_retry: Optional[str] = Header(
        default=None, alias="X-Atlassian-Webhook-Retry"
    ),
) -> JSONResponse:
    # Read raw bytes first — needed before we choose a parse path, and matches
    # the plan: parsing should not run before authn.
    body_bytes = await request.body()

    actor_sub: Optional[str] = None
    if settings.auth_mode == AUTH_MODE_JWT:
        actor_sub = _verify_jwt(authorization)
    elif settings.auth_mode == AUTH_MODE_SHARED_SECRET:
        _verify_shared_secret(x_webhook_secret)
    else:  # defensive — Settings.load() already validates this
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="server auth mode misconfigured",
        )

    # Dedup on Atlassian's delivery id (platform webhook only sends this; legacy
    # Automation does not, so this is a no-op there).
    if _dedup_check_and_record(x_atlassian_webhook_identifier):
        logger.info(
            "duplicate delivery ignored",
            extra={
                "delivery_id": x_atlassian_webhook_identifier,
                "retry": x_atlassian_webhook_retry,
                "dedup_hit": True,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"ok": True, "dedup": True},
        )

    try:
        raw: Dict[str, Any] = json.loads(body_bytes)
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
        logger.warning(
            "rejected webhook: payload missing required fields",
            extra={"errors": exc.errors()},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "invalid Jira webhook payload", "errors": exc.errors()},
        )

    parsed = ParsedEvent.from_payload(payload)

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
            "delivery_id": x_atlassian_webhook_identifier,
            "retry": x_atlassian_webhook_retry,
            "auth_mode": settings.auth_mode,
            "actor_sub": actor_sub,
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
