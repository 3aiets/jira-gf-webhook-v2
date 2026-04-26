# Plan: Migrate from Jira Automation outgoing webhook → Code-defined Platform Webhook (OAuth 2.0 3LO)

## Context

The v2 receiver currently accepts webhooks pushed from a Jira **Automation rule** in project GF. Authentication is a static `X-Webhook-Secret` header validated in `app/main.py::_verify_secret` against `WEBHOOK_SECRET` from `.env`. This is fine for a v1 learning prototype but isn't representative of how enterprise teams integrate with Jira.

The user wants to graduate to **code-defined platform webhooks**:

- Subscriptions are registered via the Jira REST API instead of being clicked together inside Automation. They live in source-controlled config (events list + JQL filter), not in a per-project rule.
- Inbound requests are authenticated by a **bearer JWT signed with the app's client secret**, instead of a shared header value. This is the mechanism Atlassian uses for OAuth 2.0 (3LO) apps and is the modern enterprise primitive.
- The receiver also captures `X-Atlassian-Webhook-Identifier` for replay/dedup, which the platform sends but Automation does not guarantee.

Outcome: same `events/` storage on disk, same `ParsedEvent` shape, but the trust model and the way the subscription is created/managed change to mirror real enterprise practice.

**User-confirmed choices:**
- Path: **Modern `POST /rest/api/3/webhook` with OAuth 2.0 (3LO).**
- Events: `jira:issue_created`, `jira:issue_updated`, `jira:issue_deleted`, `comment_created`, `comment_updated`.
- JQL filter: `project = GF AND priority IN (High, Highest)`.
- Registration: **small Python CLI** added to this repo (`app/admin/register_webhook.py`).

## New architecture (high level)

```
   Atlassian developer console                   This repo
   ┌──────────────────────────┐
   │ OAuth 2.0 (3LO) app      │
   │  client_id, client_secret│
   │  scopes: manage:jira-    │
   │   webhook, read:jira-work│
   └────────┬─────────────────┘
            │ one-time consent (browser)
            ▼
   ┌──────────────────────────┐    code (refresh_token in .env / secrets file)
   │ User grants access to    │───────────────────────────────────────┐
   │ their Jira site (cloudId)│                                       │
   └──────────────────────────┘                                       ▼
                                                          ┌──────────────────┐
   ┌──────────────────────────┐    POST /rest/api/3/webhook│ register CLI     │
   │ Jira Cloud (site)        │◀───────────────────────────│ (httpx + PyJWT)  │
   │ webhook subscription     │   events + jqlFilter       │ refresh / delete │
   │ (expires in 30 days)     │                            └──────────────────┘
   └────────┬─────────────────┘
            │ on event: HTTPS POST with
            │   Authorization: Bearer <JWT signed HS256 w/ client_secret>
            │   X-Atlassian-Webhook-Identifier
            │   X-Atlassian-Webhook-Retry
            ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ FastAPI receiver  (app/main.py)                                  │
   │  1. read raw body bytes                                          │
   │  2. _verify_jwt(Authorization) — PyJWT, HS256, client_secret     │
   │  3. dedup on X-Atlassian-Webhook-Identifier (in-memory LRU)      │
   │  4. JiraWebhookPayload.model_validate                            │
   │  5. ParsedEvent.from_payload                                     │
   │  6. ALLOWED_PROJECT_KEYS allow-list                              │
   │  7. save_event → ./events/                                       │
   └──────────────────────────────────────────────────────────────────┘
```

## Tech stack additions

| Concern | Library | Reason |
|---|---|---|
| Outbound calls to Atlassian (token exchange, webhook CRUD) | `httpx` | Async-capable, modern; matches the README's planned `app/jira_client.py` extension point |
| Inbound JWT verification | `PyJWT` | Verifies HS256 bearer token signed with client secret; small, well-maintained |
| CLI for `register/list/delete/refresh/oauth-init` | `typer` | Friendly subcommands; integrates cleanly with `app/` package |

Add to `requirements.txt`: `httpx>=0.27`, `PyJWT>=2.8`, `typer>=0.12`.

## Files to add / modify

### Modify
- **`app/config.py`** — extend `Settings` with: `atlassian_client_id`, `atlassian_client_secret`, `atlassian_cloud_id`, `jira_base_url` (e.g. `https://your-domain.atlassian.net`), `oauth_redirect_uri`, `oauth_token_file` (default `./.oauth_tokens.json`, gitignored). Drop `webhook_secret` from required, or keep it as a fallback toggle (`AUTH_MODE=jwt|shared_secret`) for the learning curve. Recommend: keep both, default to `jwt`.
- **`app/main.py`** — replace `_verify_secret` with `_verify_jwt(authorization_header, raw_body)`. Read raw bytes via `await request.body()` *before* `request.json()` (currently parses straight to JSON; needs reordering). Add dedup step on `X-Atlassian-Webhook-Identifier` using a simple in-process `collections.OrderedDict` LRU keyed by id (~10k entries, ~24h TTL — acceptable for a learning project; README already lists Redis/SQLite as future work). Log `X-Atlassian-Webhook-Retry` in the structured log line.
- **`requirements.txt`** — add the three libs above.
- **`.env.example`** — add the new vars; keep `WEBHOOK_SECRET` documented as legacy.
- **`README.md`** — replace the curl smoke-test with the new flow: `oauth-init` → `register` → trigger an event in Jira. Document the 30-day expiration and the refresh command. Note: receiver must be reachable from the public internet — recommend `ngrok http 8000` for local dev and document the resulting URL.
- **`.gitignore`** — add `.oauth_tokens.json`.

### Add
- **`app/jira_client.py`** (new, ~120 LOC) — three responsibilities:
  1. `oauth_authorize_url(state) -> str` — build the consent URL `https://auth.atlassian.com/authorize?audience=api.atlassian.com&client_id=...&scope=...&redirect_uri=...&state=...&response_type=code&prompt=consent`. Scopes: `read:jira-work manage:jira-webhook offline_access` (offline_access is required to receive a refresh_token).
  2. `exchange_code(code) -> tokens`, `refresh_access_token(refresh_token) -> tokens` — POST to `https://auth.atlassian.com/oauth/token`. Persist to `oauth_token_file`.
  3. `accessible_resources(access_token) -> list` — GET `https://api.atlassian.com/oauth/token/accessible-resources` to discover `cloudId` (needed for the API base URL `https://api.atlassian.com/ex/jira/<cloudId>`).
  4. `register_webhook(...)`, `list_webhooks()`, `delete_webhooks(ids)`, `refresh_webhooks(ids)` — wrappers around `/rest/api/3/webhook` and `/rest/api/3/webhook/refresh`. Each call refreshes the access token if expired.

- **`app/admin/__init__.py`** + **`app/admin/register_webhook.py`** (new, ~80 LOC) — Typer CLI with subcommands:
  - `python -m app.admin.register_webhook oauth-init` — prints the consent URL, runs a tiny one-shot HTTP listener on `oauth_redirect_uri` to receive the `?code=...`, exchanges it, stores tokens, and prints the discovered `cloudId`.
  - `register` — calls `POST /rest/api/3/webhook` with the user-confirmed event list and `jqlFilter="project = GF AND priority IN (High, Highest)"`, pointing at `${PUBLIC_RECEIVER_URL}/webhooks/jira`.
  - `list`, `delete <id>`, `refresh` — thin wrappers. `refresh` is what you run on a cron every ~25 days to keep subscriptions alive (the API: `PUT /rest/api/3/webhook/refresh`).

### Verification of inbound JWT (the core security swap)
For OAuth 2.0 3LO webhooks, Atlassian sets `Authorization: Bearer <JWT>` where the JWT is **HS256-signed with the app's client_secret**.

**Verified empirically against live traffic** — Atlassian's webhook JWT carries the claims `iss`, `sub`, `exp`, `iat`, `jti`, `context`. There is **no `aud`** and **no `nbf`**. `iss` equals the OAuth app's `client_id`; `sub` is the Atlassian account id of the actor.

Verification logic in `app/main.py`:

```python
token = authorization.split(" ", 1)[1].strip()
claims = jwt.decode(
    token,
    settings.atlassian_client_secret,
    algorithms=["HS256"],
    leeway=120,
    issuer=settings.atlassian_client_id,
    options={"require": ["exp", "iss"], "verify_aud": False},
)
# claims["sub"] is logged as a breadcrumb on the success path.
```
Returns 401 on `jwt.InvalidTokenError`, never logs the token.

## Functions/utilities to reuse (do not re-create)

- `app/main.py::_JsonFormatter` — keep as-is; new flow still emits the same structured log line, just with `webhook_id`, `retry`, and `delivery_id` fields added via `extra={...}`.
- `app/models.py::JiraWebhookPayload` and `ParsedEvent.from_payload` — payload shape from platform webhooks matches the Automation payload (`webhookEvent`, `issue_event_type_name`, `issue`, `user`, `changelog`). The `extra="ignore"` permissive parsing already handles new top-level keys.
- `app/storage.py::save_event` — unchanged. The `<UTC-ts>_<safe-issue-key>_<uuid8>.json` filename + atomic write + `parsed`+`raw` record stays the canonical contract.
- `app/config.py::Settings.load()` — extend, don't replace. Keep the fail-fast pattern.

## Verification (end-to-end)

1. **Unit-ish smoke**: craft a JWT locally with `jwt.encode({"iss":"https://api.atlassian.com","aud":CLIENT_ID,"exp":...}, CLIENT_SECRET, algorithm="HS256")` and POST a sample payload with `Authorization: Bearer <jwt>`. Expect 200 + new file in `./events/`. Negative: tampered token → 401, expired → 401, wrong secret → 401, missing header → 401.
2. **Replay/dedup**: POST same event twice with same `X-Atlassian-Webhook-Identifier`. Expect first → 200 + file written; second → 200 + no new file (logged as `dedup_hit=true`).
3. **OAuth init**: `python -m app.admin.register_webhook oauth-init` → browser opens consent → tokens persisted → `cloudId` printed.
4. **Register**: start receiver + `ngrok http 8000`; run `register` with the ngrok URL; `list` shows one entry with the JQL filter and 5 events.
5. **Live event**: in the GF project, create an issue with priority=High → receiver writes a file; create one with priority=Low → no delivery (filter excludes it). Verify both via Jira UI and `events/` directory contents.
6. **Refresh**: run `refresh` and re-`list`; `lastUpdated` advances. (Optional: schedule via Windows Task Scheduler or document a cron line.)

## Out of scope (intentionally) for this migration

- Migrating to Forge or Connect (heavier, separate decision).
- Replacing on-disk JSON with a database (already an extension point).
- Slack/Teams notifications (extension point).
- Production-grade dedup (Redis/SQLite) — in-memory LRU is fine for the learning goal; README already lists this as future work.
- Removing the GF Automation rule — leave it disabled (not deleted) until the new path is verified end-to-end, then delete.

---

## Resume in a new Claude Code session

Open the project (`jira-gf-webhook-v2`) in a fresh Claude Code terminal and paste this prompt:

> Read `docs/migration-platform-webhook.md` in this repo — that is the approved migration plan from Jira Automation outgoing webhook to a code-defined platform webhook using OAuth 2.0 (3LO) with bearer-JWT inbound auth. Use it as the source of truth. Implement it in this order: (1) extend `app/config.py` with the new settings; (2) add `httpx`, `PyJWT`, `typer` to `requirements.txt`; (3) create `app/jira_client.py` with OAuth helpers + webhook CRUD wrappers; (4) create `app/admin/register_webhook.py` Typer CLI exposing `oauth-init`, `register`, `list`, `delete`, `refresh`; (5) modify `app/main.py` to read raw body first, replace `_verify_secret` with `_verify_jwt`, add `X-Atlassian-Webhook-Identifier` in-memory LRU dedup, and log `X-Atlassian-Webhook-Retry`; (6) update `.env.example`, `.gitignore`, and `README.md`. Reuse `JiraWebhookPayload`, `ParsedEvent.from_payload`, `save_event`, and `_JsonFormatter` unchanged. Keep `WEBHOOK_SECRET` working behind an `AUTH_MODE` toggle so the legacy path still smoke-tests. After each step, stop and show me the diff before proceeding to the next. Do not delete the GF Automation rule.
