# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository. **Authoritative sources:** for setup, operator docs,
and configuration tables, follow [README.md](README.md) and
[docs/migration-platform-webhook.md](docs/migration-platform-webhook.md). This
file describes architecture conventions for code changes only.

## Project

FastAPI service that receives webhooks from Jira Cloud for project **GF**,
authenticates them, parses the payload into a flattened audit view, and
persists each event as a JSON file under `./events/`.

The receiver supports two inbound auth modes selected by `AUTH_MODE`:

- **`jwt`** *(default, production path)* — OAuth 2.0 (3LO) **platform**
  webhook. Subscriptions are code-defined and managed by the Typer CLI in
  [`app/admin/register_webhook.py`](app/admin/register_webhook.py), not by a
  Jira Automation rule. Atlassian sends `Authorization: Bearer <HS256 JWT>`
  signed with the app's `client_secret`.
- **`shared_secret`** *(legacy)* — original Jira Automation rule using a
  static `X-Webhook-Secret` header. Kept working so the v1 path can still be
  smoke-tested while migrating.

## Common commands

```bash
# Setup (Windows: .venv\Scripts\activate)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Run (dev with auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# Or as a module
python -m app.main

# OAuth + webhook subscription management (jwt mode)
python -m app.admin.register_webhook oauth-init     # one-time consent
python -m app.admin.register_webhook register       # create subscription
python -m app.admin.register_webhook list
python -m app.admin.register_webhook refresh        # run on ~25-day cron

# Docker
docker build -t jira-gf-webhook:latest .

# Legacy smoke test (only when AUTH_MODE=shared_secret)
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  --data @samples/jira_issue_created.json
```

There is **no test suite**. Validate changes via the curl / self-signed-JWT
examples in [README.md](README.md) (positive case + 401/400/422/403 negative
cases).

## Architecture

The handler in [`app/main.py`](app/main.py) enforces a strict request
pipeline; new features should slot into this order rather than reorganize it:

1. `await request.body()` — read raw bytes **before** any JSON parsing, so
   JWT verification (and any future HMAC) runs on the same bytes Atlassian
   signed.
2. Branch on `settings.auth_mode`:
   - `jwt` → `_verify_jwt(authorization)` — HS256 signature against
     `ATLASSIAN_CLIENT_SECRET`, requires `exp` and `iss == ATLASSIAN_CLIENT_ID`,
     120s clock-skew leeway on `iat`. No `aud` (Atlassian doesn't send one).
     Returns the `sub` claim for breadcrumb logging.
   - `shared_secret` → `_verify_shared_secret` — constant-time
     `hmac.compare_digest` against `WEBHOOK_SECRET`. Never logs the value.
   Either path returns 401 on failure.
3. `_dedup_check_and_record(X-Atlassian-Webhook-Identifier)` — in-memory LRU
   (10k entries) keyed off Atlassian's delivery id. Duplicate → 200 with
   `{"ok": true, "dedup": true}`, no further work. Legacy path has no
   identifier so dedup is a no-op there.
4. Manual `request.json()` parse → 400 on `JSONDecodeError` or non-dict body.
   Done explicitly (not via FastAPI body binding) so error shapes stay
   controllable.
5. `JiraWebhookPayload.model_validate` → 422 with `exc.errors()` on schema
   violations.
6. `ParsedEvent.from_payload` flattens the payload.
7. `ALLOWED_PROJECT_KEYS` allow-list check → 403.
8. `save_event` persists to disk → 500 on `OSError`.
9. Structured `logger.info` with issue/project/event/status/priority/
   assignee/path, plus `delivery_id`, `retry`, `auth_mode`, and `actor_sub`
   (jwt mode).

### Module roles
- [`app/config.py`](app/config.py) — `Settings` dataclass loaded once at
  import time via `Settings.load()`. Validates `AUTH_MODE` and the env vars
  required by the active mode (jwt: client id/secret/cloud id; shared_secret:
  `WEBHOOK_SECRET`). `.env` is loaded relative to the project root.
- [`app/models.py`](app/models.py) — Pydantic models. All Jira-side models
  use `extra="ignore"` so unknown/new Jira fields don't break parsing —
  preserve this when extending. `ParsedEvent` is the canonical shape passed
  onward; `model_dump()` is the contract that future DB rows / notifier
  payloads should target.
- [`app/storage.py`](app/storage.py) — One JSON file per event under
  `EVENTS_DIR`, named `<UTC-ts>_<safe-issue-key>_<uuid8>.json`. Write is
  atomic (temp file + `replace`). Each record contains both `parsed` and
  `raw` for full auditability. The `save_event` signature is the seam
  intended for swapping in a database.
- [`app/main.py`](app/main.py) — `_JsonFormatter` emits one-line JSON logs
  to stdout; anything passed via `logger.x(..., extra={...})` is merged into
  the record.
- [`app/jira_client.py`](app/jira_client.py) — sync `httpx`-based wrappers
  for the Atlassian OAuth 3LO flow (`oauth_authorize_url`, `exchange_code`,
  `refresh_access_token`, `get_access_token`, `accessible_resources`) and
  webhook CRUD (`register_webhook`, `list_webhooks`, `delete_webhooks`,
  `refresh_webhooks`). Tokens are persisted to `OAUTH_TOKEN_FILE` with an
  atomic write that mirrors `storage.save_event`'s pattern.
- [`app/admin/register_webhook.py`](app/admin/register_webhook.py) — Typer
  CLI. `DEFAULT_EVENTS` and `DEFAULT_JQL` (line 39) are the **canonical
  subscription contract**; keep them aligned with what the receiver actually
  consumes.

### Extension points (per README)
- Notifications (Slack/Teams): call after `save_event`, ideally via
  `BackgroundTasks` so the HTTP response stays fast.
- Database: replace `storage.save_event` body with an INSERT keyed off
  `ParsedEvent.model_dump()`.
- Calling Jira REST back: extend `app/jira_client.py` with the endpoints you
  need; reuse `get_access_token()` for auth.
- Production-grade dedup: replace the in-memory LRU in `main.py` with Redis
  or SQLite keyed off `X-Atlassian-Webhook-Identifier`.

## Configuration

The full env-var reference lives in [README.md](README.md#configuration-reference).
Relevant ones for code changes:

- **Mode switch:** `AUTH_MODE` (`jwt` default, `shared_secret` legacy).
- **JWT mode requires:** `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET`,
  `ATLASSIAN_CLOUD_ID` (populated by `oauth-init`), `OAUTH_REDIRECT_URI`,
  `OAUTH_TOKEN_FILE`, `PUBLIC_RECEIVER_URL`.
- **Legacy mode requires:** `WEBHOOK_SECRET`.
- **Shared:** `ALLOWED_PROJECT_KEYS`, `EVENTS_DIR`, `LOG_LEVEL`, `HOST`,
  `PORT`.

## Conventions

- Jira timestamps are **milliseconds** since epoch — convert via
  `ParsedEvent._iso_from_ms`.
- Never log the token, the shared secret, or the OAuth client secret —
  even on rejection. The verify functions are designed around this.
- Keep Pydantic models permissive (`extra="ignore"`) — Jira payloads vary by
  event type and evolve over time.
- Filenames sanitized through `_SAFE_KEY` regex; preserve this when adding
  any user-controlled string to a path.
- Subscriptions are managed via the CLI in `app/admin/`, not Automation
  rules. Don't document a curl-against-`/rest/webhooks/1.0` workaround as the
  supported path.
- Webhook JQL filters are restricted by Atlassian to `=`, `!=`, `IN`,
  `NOT IN`, `AND`, `OR`. Range operators (`>=`, `<=`) are rejected at
  registration time — keep `DEFAULT_JQL` consistent with that.
- Read the raw request body before `request.json()` so JWT verification (and
  any future HMAC) runs on the same bytes Atlassian signed.
