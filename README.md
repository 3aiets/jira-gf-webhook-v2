# Jira GF Webhook Receiver (v2)

A small, enterprise-style Python service that receives webhook events from
Jira Cloud Automation for the **GF** project, validates them with a shared
secret, extracts useful issue fields, logs structured events, and persists
each payload to a local `./events/` directory for audit and debugging.

The code is intentionally compact and easy to extend later for Slack/Teams
notifications, a real database, or callbacks into the Jira REST API.

## Project layout

```
jira-gf-webhook/
  app/
    main.py        # FastAPI app, endpoint, logging
    config.py      # Loads .env and validates settings
    models.py      # Pydantic models for Jira payload + parsed view
    storage.py     # Atomic JSON-on-disk event store
  events/          # Received events land here (one JSON file per event)
  samples/
    jira_issue_created.json
  .env.example
  requirements.txt
  Dockerfile
  README.md
```

## Prerequisites

- Python 3.11+
- A Jira Cloud project (here: project key **GF**)
- A publicly reachable URL for the receiver (e.g. via `ngrok` for local dev,
  or a real deployment for production)

## 1. Setup

```bash
cd jira-gf-webhook
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set WEBHOOK_SECRET to a long random string.
```

Generate a strong secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 2. Run the service

```bash
# Option A: run as a module
python -m app.main

# Option B: run with uvicorn directly (auto-reload during dev)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The service exposes:

- `GET  /healthz` — liveness probe, returns `{"status": "ok"}`
- `POST /webhooks/jira` — webhook receiver

## 3. Test locally with curl

Replace `YOUR_SECRET` with the value from your `.env`.

**Health check**

```bash
curl -s http://localhost:8000/healthz
```

**Send the sample payload**

```bash
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  --data @samples/jira_issue_created.json
```

Expected response:

```json
{ "ok": true, "issue_key": "GF-123" }
```

**Inline payload**

```bash
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d '{
        "timestamp": 1745596800000,
        "webhookEvent": "jira:issue_updated",
        "issue": {
          "key": "GF-456",
          "self": "https://your-tenant.atlassian.net/rest/api/3/issue/10042",
          "fields": {
            "summary": "Quick test",
            "status":   { "name": "In Progress" },
            "priority": { "name": "Medium" },
            "project":  { "key": "GF" }
          }
        }
      }'
```

**Negative cases — verify the guards work**

```bash
# 401 — missing secret
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  --data @samples/jira_issue_created.json

# 401 — wrong secret
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: nope" \
  --data @samples/jira_issue_created.json

# 400 — malformed body
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d 'not json'

# 422 — missing required field (no issue.key)
curl -i -X POST http://localhost:8000/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d '{ "issue": { "fields": {} } }'
```

After each successful POST, a new file appears in `./events/` named like
`20260425T120145Z_GF-123_a1b2c3d4.json`. It contains both a `parsed` view
(flattened, audit-friendly) and the original `raw` body.

## 4. Configure Jira Automation to call the webhook

In Jira Cloud, on **project GF**:

1. Go to **Project settings → Automation**.
2. Click **Create rule**.
3. **Trigger**: pick the event you want to forward — e.g. *Issue created*,
   *Issue updated*, *Issue transitioned*. (You can add multiple triggers
   and route them all to the same web request.)
4. Optional **Conditions**: narrow scope, e.g. *only if Priority = High*.
5. **Action → Send web request**:
   - **Web request URL**: `https://<your-public-host>/webhooks/jira`
   - **Headers**:
     - `Content-Type: application/json`
     - `X-Webhook-Secret: <same value as WEBHOOK_SECRET in .env>`
   - **HTTP method**: `POST`
   - **Web request body**: *Issue data* (recommended). This sends the full
     issue payload that this service is built to parse.
   - Tick **Wait for response** if you want Jira to surface non-2xx errors
     in the audit log.
6. **Validate** — Jira’s rule editor lets you test against an existing issue.
7. **Turn on** the rule.

> Tip: For local development, expose your laptop with `ngrok http 8000`
> and use the resulting `https://…ngrok.app/webhooks/jira` URL in step 5.

## 5. Run in Docker

```bash
docker build -t jira-gf-webhook:latest .
docker run --rm -p 8000:8000 \
  -e WEBHOOK_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  -e ALLOWED_PROJECT_KEYS=GF \
  -v "$(pwd)/events:/app/events" \
  jira-gf-webhook:latest
```

The mounted volume keeps received events on the host so they survive
container restarts.

## Configuration reference

| Env var                | Default      | Purpose                                                          |
| ---------------------- | ------------ | ---------------------------------------------------------------- |
| `WEBHOOK_SECRET`       | *(required)* | Shared secret matched against the `X-Webhook-Secret` header.     |
| `ALLOWED_PROJECT_KEYS` | *(empty)*    | Comma-separated allow-list. Empty = accept all projects.         |
| `EVENTS_DIR`           | `./events`   | Directory where event JSON files are written.                    |
| `LOG_LEVEL`            | `INFO`       | `DEBUG`, `INFO`, `WARNING`, `ERROR`.                             |
| `HOST`                 | `0.0.0.0`    | Uvicorn bind host.                                               |
| `PORT`                 | `8000`       | Uvicorn bind port.                                               |

## Logging

Logs are emitted to stdout as one JSON object per line. They include the
issue key, project key, event type, status, priority, assignee and the
path the event was stored at. This format drops cleanly into Datadog, ELK,
CloudWatch, or any other JSON-aware log pipeline.

## Error responses

| Status | Cause                                                           |
| ------ | --------------------------------------------------------------- |
| `200`  | Event accepted and persisted.                                   |
| `400`  | Body is not valid JSON, or not a JSON object.                   |
| `401`  | `X-Webhook-Secret` missing or wrong.                            |
| `403`  | `project_key` not in `ALLOWED_PROJECT_KEYS` (when configured).  |
| `422`  | JSON is well-formed but missing required Jira fields.           |
| `500`  | Internal error persisting the event.                            |

## Extending this service

The codebase is structured so that v2 features slot in without rewrites:

- **Slack / Teams**: in `main.py` after `save_event(...)`, call a notifier
  module that takes a `ParsedEvent`. Keep the webhook handler thin —
  enqueue notifications rather than blocking the HTTP response.
- **Database**: replace the body of `storage.save_event` with an INSERT.
  The `ParsedEvent.model_dump()` shape maps neatly to a SQL row.
- **Calling Jira REST API back**: add an `app/jira_client.py` with an
  HTTPX client that authenticates via Atlassian API token. Call it from
  a background task (`fastapi.BackgroundTasks`) so the webhook still
  returns within Jira’s timeout.
- **Replay protection**: add a `Jira-Webhook-Identifier` (or your own
  delivery ID) check by tracking seen IDs in Redis or a small SQLite db.

## Security notes

- Always run behind HTTPS in production. Jira Automation requires it.
- The shared-secret comparison uses `hmac.compare_digest` to avoid timing
  attacks. Keep the secret in `.env` (or your secret manager) — never in
  source control.
- The `events/` directory is in `.gitignore` to avoid leaking real issue
  contents into commits.
- The Docker image runs as a non-root user.
