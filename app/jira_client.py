"""Atlassian OAuth 2.0 (3LO) helpers and Jira webhook CRUD wrappers.

Used by the admin CLI in ``app/admin/register_webhook.py`` to:
  1. drive the consent flow and persist refresh tokens,
  2. discover the Jira ``cloudId`` for the granted site,
  3. create / list / delete / refresh platform webhook subscriptions via
     ``/rest/api/3/webhook`` on ``https://api.atlassian.com/ex/jira/<cloudId>``.

Tokens are persisted to ``settings.oauth_token_file`` as JSON. The file is
gitignored. Access tokens are refreshed on demand when within 60s of expiry.
"""

from __future__ import annotations

import json
import time
from typing import Iterable, List, Optional
from urllib.parse import urlencode

import httpx

from .config import Settings, settings as default_settings

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_BASE = "https://api.atlassian.com/ex/jira"

# offline_access is required to get a refresh_token back from the token exchange.
DEFAULT_SCOPES = "read:jira-work manage:jira-webhook offline_access"

_REFRESH_LEEWAY_SECONDS = 60
_HTTP_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# Token persistence
# --------------------------------------------------------------------------- #

def load_tokens(s: Settings = default_settings) -> dict:
    path = s.oauth_token_file
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_tokens(tokens: dict, s: Settings = default_settings) -> None:
    path = s.oauth_token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# OAuth flow
# --------------------------------------------------------------------------- #

def oauth_authorize_url(state: str, s: Settings = default_settings) -> str:
    """Build the consent URL the user opens in their browser."""
    if not s.atlassian_client_id:
        raise RuntimeError("ATLASSIAN_CLIENT_ID is not set.")
    params = {
        "audience": "api.atlassian.com",
        "client_id": s.atlassian_client_id,
        "scope": DEFAULT_SCOPES,
        "redirect_uri": s.oauth_redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _persist_token_response(payload: dict, s: Settings) -> dict:
    """Normalize a token-endpoint response into our on-disk shape and persist."""
    existing = load_tokens(s)
    existing.update({
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", existing.get("refresh_token")),
        "scope": payload.get("scope"),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
    })
    save_tokens(existing, s)
    return existing


def exchange_code(code: str, s: Settings = default_settings) -> dict:
    """Exchange the OAuth ``code`` for access + refresh tokens; persist them."""
    body = {
        "grant_type": "authorization_code",
        "client_id": s.atlassian_client_id,
        "client_secret": s.atlassian_client_secret,
        "code": code,
        "redirect_uri": s.oauth_redirect_uri,
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(TOKEN_URL, json=body)
        resp.raise_for_status()
        return _persist_token_response(resp.json(), s)


def refresh_access_token(s: Settings = default_settings) -> dict:
    """Use the stored refresh_token to get a fresh access_token; persist it."""
    tokens = load_tokens(s)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "No refresh_token on disk. Run `oauth-init` first."
        )
    body = {
        "grant_type": "refresh_token",
        "client_id": s.atlassian_client_id,
        "client_secret": s.atlassian_client_secret,
        "refresh_token": refresh_token,
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(TOKEN_URL, json=body)
        resp.raise_for_status()
        return _persist_token_response(resp.json(), s)


def get_access_token(s: Settings = default_settings) -> str:
    """Return a valid access token, refreshing if it is missing or near expiry."""
    tokens = load_tokens(s)
    expires_at = tokens.get("expires_at", 0)
    if not tokens.get("access_token") or expires_at - time.time() < _REFRESH_LEEWAY_SECONDS:
        tokens = refresh_access_token(s)
    return tokens["access_token"]


def accessible_resources(access_token: str) -> list:
    """List the Atlassian sites the user granted this app access to."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.get(ACCESSIBLE_RESOURCES_URL, headers=headers)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- #
# Webhook CRUD
# --------------------------------------------------------------------------- #

def _api_base(s: Settings) -> str:
    if not s.atlassian_cloud_id:
        raise RuntimeError(
            "ATLASSIAN_CLOUD_ID is not set. Run `oauth-init` to discover it."
        )
    return f"{API_BASE}/{s.atlassian_cloud_id}"


def _auth_headers(s: Settings) -> dict:
    return {
        "Authorization": f"Bearer {get_access_token(s)}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def register_webhook(
    callback_url: str,
    events: List[str],
    jql_filter: str,
    s: Settings = default_settings,
) -> dict:
    """POST /rest/api/3/webhook — create a single webhook with one filter."""
    body = {
        "url": callback_url,
        "webhooks": [{"events": events, "jqlFilter": jql_filter}],
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(
            f"{_api_base(s)}/rest/api/3/webhook",
            headers=_auth_headers(s),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


def list_webhooks(s: Settings = default_settings) -> dict:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.get(
            f"{_api_base(s)}/rest/api/3/webhook",
            headers=_auth_headers(s),
        )
        resp.raise_for_status()
        return resp.json()


def delete_webhooks(ids: Iterable[int], s: Settings = default_settings) -> None:
    body = {"webhookIds": list(ids)}
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.request(
            "DELETE",
            f"{_api_base(s)}/rest/api/3/webhook",
            headers=_auth_headers(s),
            json=body,
        )
        resp.raise_for_status()


def refresh_webhooks(
    ids: Optional[Iterable[int]] = None,
    s: Settings = default_settings,
) -> dict:
    """PUT /rest/api/3/webhook/refresh — extend expiry by another 30 days."""
    if ids is None:
        ids = [w["id"] for w in list_webhooks(s).get("values", [])]
    body = {"webhookIds": list(ids)}
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.put(
            f"{_api_base(s)}/rest/api/3/webhook/refresh",
            headers=_auth_headers(s),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()
