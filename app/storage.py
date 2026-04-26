"""Local filesystem store for received webhook events.

v1 keeps things simple — one JSON file per event. The format is intentionally
designed so a future migration to a database (Postgres, S3, etc.) is a small
change: replace `save_event` with a different implementation behind the same
function signature.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .models import ParsedEvent

# Restrict filename characters to a safe ASCII subset to avoid path issues
# across Windows/Linux/macOS.
_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(value: str) -> str:
    return _SAFE_KEY.sub("_", value)


def save_event(
    events_dir: Path,
    parsed: ParsedEvent,
    raw_payload: Dict[str, Any],
) -> Path:
    """Persist both the parsed view and the raw body for full auditability.

    Returns the path of the written file.
    """
    events_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Short uuid suffix prevents collisions when multiple events for the same
    # issue arrive in the same second.
    suffix = uuid.uuid4().hex[:8]
    filename = f"{ts}_{_safe(parsed.issue_key)}_{suffix}.json"
    path = events_dir / filename

    record = {
        "parsed": parsed.model_dump(),
        "raw": raw_payload,
    }

    # Write atomically: temp file + rename. Avoids half-written files if the
    # process is killed mid-write.
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path
