"""Configuration loaded from environment / .env file.

Keeps secrets and runtime settings out of source code. Uses python-dotenv so
the same code works in local dev (.env) and in container deployments where
env vars are injected by the orchestrator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Load .env from the project root (one level above the app/ package).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


# Inbound auth modes for the /webhooks/jira endpoint.
AUTH_MODE_JWT = "jwt"
AUTH_MODE_SHARED_SECRET = "shared_secret"
_VALID_AUTH_MODES = {AUTH_MODE_JWT, AUTH_MODE_SHARED_SECRET}


@dataclass(frozen=True)
class Settings:
    # Inbound auth
    auth_mode: str = AUTH_MODE_JWT
    webhook_secret: Optional[str] = None  # legacy shared-secret mode

    # Atlassian OAuth 2.0 (3LO) app — required for jwt mode and the register CLI
    atlassian_client_id: Optional[str] = None
    atlassian_client_secret: Optional[str] = None
    atlassian_cloud_id: Optional[str] = None  # discovered via oauth-init
    jira_base_url: Optional[str] = None  # e.g. https://your-domain.atlassian.net
    oauth_redirect_uri: str = "http://localhost:8765/callback"
    oauth_token_file: Path = field(
        default_factory=lambda: Path("./.oauth_tokens.json")
    )

    # Public URL where Atlassian can reach this receiver (used by register CLI).
    public_receiver_url: Optional[str] = None

    # Existing settings
    allowed_project_keys: List[str] = field(default_factory=list)
    events_dir: Path = field(default_factory=lambda: Path("./events"))
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def load(cls) -> "Settings":
        auth_mode = os.getenv("AUTH_MODE", AUTH_MODE_JWT).strip().lower()
        if auth_mode not in _VALID_AUTH_MODES:
            raise RuntimeError(
                f"AUTH_MODE must be one of {sorted(_VALID_AUTH_MODES)}; got {auth_mode!r}."
            )

        webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip() or None
        client_id = os.getenv("ATLASSIAN_CLIENT_ID", "").strip() or None
        client_secret = os.getenv("ATLASSIAN_CLIENT_SECRET", "").strip() or None

        if auth_mode == AUTH_MODE_SHARED_SECRET and not webhook_secret:
            raise RuntimeError(
                "AUTH_MODE=shared_secret but WEBHOOK_SECRET is not set. "
                "Copy .env.example to .env and set a value."
            )
        if auth_mode == AUTH_MODE_JWT and not (client_id and client_secret):
            raise RuntimeError(
                "AUTH_MODE=jwt requires ATLASSIAN_CLIENT_ID and "
                "ATLASSIAN_CLIENT_SECRET to be set."
            )

        events_dir = Path(os.getenv("EVENTS_DIR", "./events")).resolve()
        events_dir.mkdir(parents=True, exist_ok=True)

        token_file = Path(
            os.getenv("OAUTH_TOKEN_FILE", "./.oauth_tokens.json")
        ).resolve()

        return cls(
            auth_mode=auth_mode,
            webhook_secret=webhook_secret,
            atlassian_client_id=client_id,
            atlassian_client_secret=client_secret,
            atlassian_cloud_id=os.getenv("ATLASSIAN_CLOUD_ID", "").strip() or None,
            jira_base_url=os.getenv("JIRA_BASE_URL", "").strip() or None,
            oauth_redirect_uri=os.getenv(
                "OAUTH_REDIRECT_URI", "http://localhost:8765/callback"
            ).strip(),
            oauth_token_file=token_file,
            public_receiver_url=os.getenv("PUBLIC_RECEIVER_URL", "").strip() or None,
            allowed_project_keys=_split_csv(os.getenv("ALLOWED_PROJECT_KEYS", "")),
            events_dir=events_dir,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )


settings = Settings.load()
