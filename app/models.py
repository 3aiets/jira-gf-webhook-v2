"""Pydantic models describing the slice of the Jira webhook payload we care about.

The Jira webhook body is large and varies by event type. Rather than mirroring
every field, we extract the common ones we need and keep `extra="ignore"` so
unknown fields don't break parsing. This makes the parser resilient to Jira
adding/renaming fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class JiraUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accountId: Optional[str] = None
    displayName: Optional[str] = None
    emailAddress: Optional[str] = None


class JiraStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None


class JiraPriority(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None


class JiraProject(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: Optional[str] = None
    name: Optional[str] = None


class JiraIssueFields(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: Optional[str] = None
    status: Optional[JiraStatus] = None
    priority: Optional[JiraPriority] = None
    assignee: Optional[JiraUser] = None
    reporter: Optional[JiraUser] = None
    project: Optional[JiraProject] = None


class JiraIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    self: Optional[str] = None  # API URL of the issue
    fields: Optional[JiraIssueFields] = None


class JiraWebhookPayload(BaseModel):
    """Top-level Jira webhook body.

    Both classic Jira webhooks and Automation "Send web request" payloads
    typically include `issue` and an event identifier. Timestamp is sent as
    a millisecond Unix epoch in `timestamp`.
    """

    model_config = ConfigDict(extra="ignore")

    timestamp: Optional[int] = Field(
        default=None,
        description="Event time in milliseconds since the Unix epoch (Jira convention).",
    )
    webhookEvent: Optional[str] = None
    issue_event_type_name: Optional[str] = None
    issue: JiraIssue


class ParsedEvent(BaseModel):
    """Flattened, audit-friendly view of the webhook event."""

    issue_key: str
    summary: Optional[str]
    status: Optional[str]
    priority: Optional[str]
    assignee: Optional[str]
    reporter: Optional[str]
    project_key: Optional[str]
    issue_url: Optional[str]
    event_type: Optional[str]
    event_timestamp: str  # ISO 8601 UTC
    received_at: str      # ISO 8601 UTC

    @staticmethod
    def _iso_from_ms(ms: Optional[int]) -> str:
        if ms is None:
            return datetime.now(tz=timezone.utc).isoformat()
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

    @classmethod
    def from_payload(cls, payload: JiraWebhookPayload) -> "ParsedEvent":
        issue = payload.issue
        fields = issue.fields or JiraIssueFields()
        return cls(
            issue_key=issue.key,
            summary=fields.summary,
            status=fields.status.name if fields.status else None,
            priority=fields.priority.name if fields.priority else None,
            assignee=fields.assignee.displayName if fields.assignee else None,
            reporter=fields.reporter.displayName if fields.reporter else None,
            project_key=fields.project.key if fields.project else None,
            issue_url=issue.self,
            event_type=payload.webhookEvent or payload.issue_event_type_name,
            event_timestamp=cls._iso_from_ms(payload.timestamp),
            received_at=datetime.now(tz=timezone.utc).isoformat(),
        )
