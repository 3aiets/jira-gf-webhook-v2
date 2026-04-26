"""Admin CLI for the OAuth 2.0 (3LO) consent flow and webhook subscriptions.

Usage:
    python -m app.admin.register_webhook oauth-init
    python -m app.admin.register_webhook register
    python -m app.admin.register_webhook list
    python -m app.admin.register_webhook delete <id> [<id> ...]
    python -m app.admin.register_webhook refresh

`oauth-init` opens the consent URL in a browser, runs a tiny one-shot HTTP
listener on ``OAUTH_REDIRECT_URI`` to receive the ``?code=`` callback, exchanges
it for tokens, discovers ``cloudId`` via ``accessible-resources``, and prints
both. The cloudId must then be added to ``.env`` as ``ATLASSIAN_CLOUD_ID``.
"""

from __future__ import annotations

import http.server
import secrets as _secrets
import threading
import urllib.parse
import webbrowser
from typing import List, Optional

import typer

from .. import jira_client
from ..config import settings

app = typer.Typer(add_completion=False, help="Manage Jira platform webhook subscriptions.")

DEFAULT_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
]
DEFAULT_JQL = "project = GF AND priority IN (High, Highest)"


# --------------------------------------------------------------------------- #
# oauth-init: one-shot loopback HTTP server to capture ?code=...
# --------------------------------------------------------------------------- #

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.captured = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"OAuth callback received. You can close this tab and return to the terminal."
        )

    def log_message(self, format: str, *args) -> None:  # silence default logs
        return


def _run_callback_server(host: str, port: int) -> dict:
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)  # 5-minute window for consent
    server.server_close()
    return _CallbackHandler.captured


@app.command("oauth-init")
def oauth_init() -> None:
    """Run the OAuth 3LO consent flow and persist tokens + cloudId."""
    redirect = urllib.parse.urlparse(settings.oauth_redirect_uri)
    if redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1"):
        typer.echo(
            f"OAUTH_REDIRECT_URI must be http://localhost:<port>/<path>; "
            f"got {settings.oauth_redirect_uri!r}.",
            err=True,
        )
        raise typer.Exit(code=2)

    state = _secrets.token_urlsafe(24)
    url = jira_client.oauth_authorize_url(state)
    typer.echo(f"Opening consent URL in your browser:\n  {url}\n")
    webbrowser.open(url)

    captured = _run_callback_server(redirect.hostname, redirect.port or 80)
    if captured.get("state") != state:
        typer.echo("State mismatch — aborting (possible CSRF).", err=True)
        raise typer.Exit(code=1)
    code = captured.get("code")
    if not code:
        typer.echo(f"No authorization code received. Got: {captured}", err=True)
        raise typer.Exit(code=1)

    tokens = jira_client.exchange_code(code)
    resources = jira_client.accessible_resources(tokens["access_token"])
    if not resources:
        typer.echo("No accessible Atlassian sites returned for this grant.", err=True)
        raise typer.Exit(code=1)

    typer.echo("Tokens persisted to " + str(settings.oauth_token_file))
    typer.echo("\nAccessible sites:")
    for r in resources:
        typer.echo(f"  cloudId={r.get('id')}  url={r.get('url')}  name={r.get('name')}")
    typer.echo(
        "\nAdd the matching cloudId to your .env as ATLASSIAN_CLOUD_ID and re-run the CLI."
    )


# --------------------------------------------------------------------------- #
# Webhook subscription commands
# --------------------------------------------------------------------------- #

@app.command("register")
def register(
    callback_url: Optional[str] = typer.Option(
        None,
        "--callback-url",
        help="Public URL of /webhooks/jira (defaults to PUBLIC_RECEIVER_URL + /webhooks/jira).",
    ),
    jql: str = typer.Option(DEFAULT_JQL, "--jql", help="JQL filter for the subscription."),
) -> None:
    """Create the platform webhook subscription on the Jira site."""
    if callback_url is None:
        if not settings.public_receiver_url:
            typer.echo(
                "PUBLIC_RECEIVER_URL is not set in .env and --callback-url not given.",
                err=True,
            )
            raise typer.Exit(code=2)
        callback_url = settings.public_receiver_url.rstrip("/") + "/webhooks/jira"
    result = jira_client.register_webhook(callback_url, DEFAULT_EVENTS, jql)
    typer.echo(f"Registered: {result}")


@app.command("list")
def list_cmd() -> None:
    """List all webhook subscriptions visible to this app."""
    result = jira_client.list_webhooks()
    typer.echo(str(result))


@app.command("delete")
def delete(ids: List[int] = typer.Argument(..., help="Webhook IDs to delete.")) -> None:
    """Delete one or more webhook subscriptions by ID."""
    jira_client.delete_webhooks(ids)
    typer.echo(f"Deleted: {ids}")


@app.command("refresh")
def refresh(
    ids: Optional[List[int]] = typer.Argument(
        None, help="Webhook IDs to refresh (defaults to all)."
    ),
) -> None:
    """Refresh subscription expiry by another 30 days. Run this on a ~25-day cron."""
    result = jira_client.refresh_webhooks(ids)
    typer.echo(f"Refreshed: {result}")


if __name__ == "__main__":
    app()
