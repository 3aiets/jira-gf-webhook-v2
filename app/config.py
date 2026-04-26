"""Configuration loaded from environment / .env file.

Keeps secrets and runtime settings out of source code. Uses python-dotenv so
the same code works in local dev (.env) and in container deployments where
env vars are injected by the orchestrator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env from the project root (one level above the app/ package).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    allowed_project_keys: List[str] = field(default_factory=list)
    events_dir: Path = field(default_factory=lambda: Path("./events"))
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def load(cls) -> "Settings":
        secret = os.getenv("WEBHOOK_SECRET", "").strip()
        if not secret:
            # Fail fast at startup — refusing requests with a missing secret
            # is far better than silently accepting them.
            raise RuntimeError(
                "WEBHOOK_SECRET is not set. Copy .env.example to .env and set a value."
            )

        events_dir = Path(os.getenv("EVENTS_DIR", "./events")).resolve()
        events_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            webhook_secret=secret,
            allowed_project_keys=_split_csv(os.getenv("ALLOWED_PROJECT_KEYS", "")),
            events_dir=events_dir,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )


settings = Settings.load()
