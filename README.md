# Jira GF Webhook Receiver (v2)

A small, enterprise-style Python service that receives webhook events from
Jira Cloud for the **GF** project, validates them, extracts useful issue
fields, logs structured events, and persists each payload to a local
`./events/` directory for audit and debugging.

v2 supports two inbound auth modes, switchable via `AUTH_MODE` in `.env`:

- **`jwt`** *(recommended)* — modern **OAuth 2.0 (3LO) platform webhook**.
  Subscriptions are registered via `POST /rest/api/3/webhook` from a small
  Typer CLI in this repo. Inbound deliveries carry an HS256 bearer JWT signed
  with your app's client secret.
- **`shared_secret`** *(legacy)* — Jira Automation rule using
  `X-Webhook-Secret`. Kept working so you can smoke-test the legacy path
  while migrating.

The code is intentionally compact and easy to extend later for Slack/Teams
notifications, a real database, or callbacks into the Jira REST API.

## Project layout

```
jira-gf-webhook-v2/
  app/
    main.py                       # FastAPI app, JWT/shared-secret auth, dedup, logging
    config.py                     # Loads .env and validates settings
    models.py                     # Pydantic models for Jira payload + parsed view
    storage.py                    # Atomic JSON-on-disk event store
    jira_client.py                # OAuth 3LO + webhook CRUD against /rest/api/3/webhook
    admin/
      register_webhook.py         # Typer CLI: oauth-init, register, list, delete, refresh
  events/                         # Received events land here (one JSON file per event)
  samples/
    jira_issue_created.json
  docs/migration-platform-webhook.md
  .env.example
  requirements.txt
  Dockerfile
  README.md
```

## Prerequisites

- Python 3.11+
- A Jira Cloud project (here: project key **GF**)
- An Atlassian OAuth 2.0 (3LO) app — created at
  <https://developer.atlassian.com/console/myapps/>. Required scopes:
  `read:jira-work`, `manage:jira-webhook`. The CLI also requests
  `offline_access` (needed to receive a refresh token).
- A publicly reachable URL for the receiver (e.g. via `ngrok` for local dev,
  or a real deployment for production)

## 1. Setup

```bash
cd jira-gf-webhook-v2
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — paste ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET from the
# developer console; keep AUTH_MODE=jwt.
```

In the developer console, set the app's **Authorization callback URL** to
exactly `http://localhost:8765/callback` (must match `OAUTH_REDIRECT_URI`).

Generate a strong shared secret (only needed if you flip to legacy mode):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 2. Run the OAuth consent flow (one-time)

```bash
python -m app.admin.register_webhook oauth-init
```

This:

1. Opens the consent URL in your browser.
2. Spins up a one-shot HTTP listener on `OAUTH_REDIRECT_URI` to capture the
   `?code=` callback (CSRF-protected via `state`).
3. Exchanges the code for access + refresh tokens, persists them to
   `./.oauth_tokens.json` (gitignored).
4. Calls `accessible-resources` and prints your Jira site's `cloudId`.

Copy the printed `cloudId` into `.env` as `ATLASSIAN_CLOUD_ID`.

## 3. Expose the receiver and register the webhook

Start the receiver:

```bash
python -m app.main
# or: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

In another terminal, expose port 8000 publicly (Atlassian needs a reachable
HTTPS URL — `ngrok` is the easiest for local dev):

```bash
ngrok http 8000
# copy the https://<random>.ngrok.app URL into .env as PUBLIC_RECEIVER_URL
```

Register the subscription:

```bash
python -m app.admin.register_webhook register
# defaults: events = jira:issue_created/updated/deleted, comment_created/updated
#           jqlFilter = "project = GF AND priority IN (High, Highest)"
#           url       = $PUBLIC_RECEIVER_URL/webhooks/jira

# Verify
python -m app.admin.register_webhook list
```

> **JQL note:** Atlassian's webhook JQL only supports `=`, `!=`, `IN`,
> `NOT IN`, `AND`, `OR`. Range operators like `>=` / `<=` and functions are
> rejected at registration time, even though they work in the regular issue
> search UI.

> **Heads up:** Atlassian platform webhooks **expire 30 days** after the last
> refresh. Run `python -m app.admin.register_webhook refresh` on a ~25-day
> cadence (Windows Task Scheduler / cron) to keep them alive.

## 4. Trigger a real event

In project GF, create an issue with `priority = High`. Within seconds you
should see a structured log line in the receiver and a new file in
`./events/`. Creating a `priority = Low` issue should produce **no** delivery
(the JQL filter excludes it).

The service exposes:

- `GET  /healthz` — liveness probe, returns `{"status": "ok"}`
- `POST /webhooks/jira` — webhook receiver

## 5. Test locally with a self-signed JWT

You can verify the receiver without going through Jira by crafting a JWT
signed with the same `client_secret`:

```bash
python - <<'PY'
import jwt, time, os, json, urllib.request
secret = os.environ["ATLASSIAN_CLIENT_SECRET"]
client_id = os.environ["ATLASSIAN_CLIENT_ID"]
token = jwt.encode(
    {"iss": client_id, "exp": int(time.time())+300},
    secret, algorithm="HS256",
)
req = urllib.request.Request(
    "http://localhost:8000/webhooks/jira",
    data=open("samples/jira_issue_created.json", "rb").read(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Atlassian-Webhook-Identifier": "local-test-1",
        "X-Atlassian-Webhook-Retry": "0",
    },
)
print(urllib.request.urlopen(req).read().decode())
PY
```

Negative cases (all return `401`):

- Missing `Authorization` header.
- `Authorization: Bearer <token-signed-with-wrong-key>`.
- Expired `exp` claim.
- `iss` claim missing or not equal to `ATLASSIAN_CLIENT_ID`.

Re-sending the same `X-Atlassian-Webhook-Identifier` returns `200` with
`{"ok": true, "dedup": true}` and writes **no** new file (in-memory LRU
dedup of the last 10k delivery IDs).

## 6. Legacy: smoke-test the shared-secret path

To exercise the v1 Jira Automation flow, set `AUTH_MODE=shared_secret` in
`.env` and restart the service. Then:

```bash
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  --data @samples/jira_issue_created.json
```

The legacy Jira Automation rule on project GF can stay disabled (not
deleted) until the OAuth path has been verified end-to-end.

## 7. Run in Docker

```bash
docker build -t jira-gf-webhook:latest .
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/events:/app/events" \
  -v "$(pwd)/.oauth_tokens.json:/app/.oauth_tokens.json" \
  jira-gf-webhook:latest
```

The mounted volumes keep received events and OAuth tokens on the host so
they survive container restarts. The CLI (`oauth-init`, `register`, ...) is
typically run on the host, not inside the container.

## Configuration reference

| Env var                   | Default                          | Purpose                                                                   |
| ------------------------- | -------------------------------- | ------------------------------------------------------------------------- |
| `AUTH_MODE`               | `jwt`                            | `jwt` (OAuth platform webhook) or `shared_secret` (legacy Automation).    |
| `WEBHOOK_SECRET`          | *(required for shared_secret)*   | Shared secret matched against `X-Webhook-Secret`.                         |
| `ATLASSIAN_CLIENT_ID`     | *(required for jwt)*             | OAuth 3LO client ID from the developer console.                           |
| `ATLASSIAN_CLIENT_SECRET` | *(required for jwt)*             | OAuth 3LO client secret. Also used as the HS256 key for inbound JWTs.     |
| `ATLASSIAN_CLOUD_ID`      | *(populated by `oauth-init`)*    | Site UUID for `https://api.atlassian.com/ex/jira/<cloudId>`.              |
| `JIRA_BASE_URL`           | *(empty)*                        | Site URL (informational, e.g. `https://your-tenant.atlassian.net`).            |
| `OAUTH_REDIRECT_URI`      | `http://localhost:8765/callback` | Loopback URL for the consent callback. Must match the developer console. |
| `OAUTH_TOKEN_FILE`        | `./.oauth_tokens.json`           | Where access + refresh tokens are persisted. Gitignored.                  |
| `PUBLIC_RECEIVER_URL`     | *(empty)*                        | Public base URL for the receiver, used by `register`.                     |
| `ALLOWED_PROJECT_KEYS`    | *(empty)*                        | Comma-separated allow-list. Empty = accept all projects.                  |
| `EVENTS_DIR`              | `./events`                       | Directory where event JSON files are written.                             |
| `LOG_LEVEL`               | `INFO`                           | `DEBUG`, `INFO`, `WARNING`, `ERROR`.                                      |
| `HOST`                    | `0.0.0.0`                        | Uvicorn bind host.                                                        |
| `PORT`                    | `8000`                           | Uvicorn bind port.                                                        |

## Logging

Logs are emitted to stdout as one JSON object per line. Each accepted
delivery line includes the issue key, project key, event type, status,
priority, assignee, the path the event was stored at, the active
`auth_mode`, and (for platform webhooks) `delivery_id` and `retry`. This
format drops cleanly into Datadog, ELK, CloudWatch, or any other JSON-aware
log pipeline.

## Error responses

| Status | Cause                                                                          |
| ------ | ------------------------------------------------------------------------------ |
| `200`  | Event accepted and persisted (or duplicate ignored: `{"ok": true, "dedup": true}`). |
| `400`  | Body is not valid JSON, or not a JSON object.                                  |
| `401`  | Missing/invalid `Authorization` JWT (jwt mode) or `X-Webhook-Secret` (legacy). |
| `403`  | `project_key` not in `ALLOWED_PROJECT_KEYS` (when configured).                 |
| `422`  | JSON is well-formed but missing required Jira fields.                          |
| `500`  | Internal error persisting the event.                                           |

## Extending this service

- **Slack / Teams**: in `main.py` after `save_event(...)`, call a notifier
  module that takes a `ParsedEvent`. Keep the webhook handler thin —
  enqueue notifications via `BackgroundTasks` rather than blocking the
  HTTP response.
- **Database**: replace the body of `storage.save_event` with an INSERT.
  The `ParsedEvent.model_dump()` shape maps neatly to a SQL row.
- **Calling Jira REST API back**: extend `app/jira_client.py` with the
  endpoints you need; reuse `get_access_token()` for auth.
- **Production-grade dedup**: replace the in-memory LRU in `main.py` with
  Redis or SQLite keyed off `X-Atlassian-Webhook-Identifier`.

## Security notes

- Always run behind HTTPS in production. Atlassian platform webhooks
  require it; Jira Automation does too.
- JWT verification uses HS256 with the OAuth app's client secret as the key.
  It enforces signature, the `exp` claim, and that `iss` equals the configured
  `ATLASSIAN_CLIENT_ID`; tolerates 120s of clock skew on `iat`. Atlassian's
  webhook JWTs do not carry an `aud` claim, so audience verification is off.
  The token is never logged.
- The shared-secret comparison (legacy mode) uses `hmac.compare_digest` to
  avoid timing attacks.
- `.env` and `.oauth_tokens.json` are gitignored. Rotate the client secret
  in the developer console (**Settings → Rotate client secret**) if you
  suspect exposure — the new value goes in `.env`, the old refresh token
  remains valid until you revoke it.
- The `events/` directory is in `.gitignore` to avoid leaking real issue
  contents into commits.
- The Docker image runs as a non-root user.

## Improvement
- Respond quickly + enqueue async work	Not implemented. Current handler still validates, parses, saves file, then responds. Status: No queue/worker yet.
- X-Atlassian-Webhook-Identifier. Status:	Partly implemented. Used for in-memory LRU dedup. Not durable after restart.
- X-Atlassian-Webhook-Retry. Status:	Implemented only as logging. Good first step. Not stored durably.
- Sync-state table. Status:	Not implemented. No SQLite DB yet.
- Dead-letter queue. Status:	Not implemented.
- Explicit loop prevention. Status:	Not implemented. Not needed until your app writes back to Jira, but good to add before bidirectional sync.
- Rate limits: 429, Retry-After, exponential backoff. Status:	Not implemented in jira_client.py. It currently uses raise_for_status().
- Control concurrency / queue workers. Status:	Not implemented.
- Batching. Status:	Not implemented.
- OAuth 2.0. Status:	Implemented. OAuth init, token persistence, refresh, webhook CRUD exist.
- X-Hub-Signature: method=signature. Status:	Not implemented. Current modern auth is JWT bearer.
- X-RateLimit-*. Status:	Not implemented. Should be captured from outbound Jira API responses.
- Use SQLite. Status:	Not implemented. Current persistence is JSON files + in-memory dedup.
- 
