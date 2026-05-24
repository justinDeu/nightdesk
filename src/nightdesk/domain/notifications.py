"""Run-completion notification dispatch.

Fires a best-effort webhook POST when a run transitions to review.
Failures are logged but never block the caller.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_WEBHOOK_TIMEOUT_SECONDS = 10


@dataclass
class RunCompletionPayload:
    """Structured payload sent to the configured webhook URL."""
    ticket_id: str
    title: str
    run_id: str
    exit_status: str
    error_summary: Optional[str]
    cost_usd: Optional[float]
    duration_seconds: Optional[float]
    url: str

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "run_id": self.run_id,
            "exit_status": self.exit_status,
            "error_summary": self.error_summary,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "url": self.url,
        }


def fire_webhook(webhook_url: str, payload: RunCompletionPayload) -> None:
    """POST a JSON payload to the configured webhook URL. Best-effort."""
    if not webhook_url or not webhook_url.strip():
        return
    webhook_url = webhook_url.strip()
    body = json.dumps(payload.to_dict()).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_SECONDS) as resp:
            log.info("webhook POST to %s returned %s", webhook_url, resp.status)
    except (urllib.error.URLError, OSError) as exc:
        log.warning("webhook POST to %s failed: %s", webhook_url, exc)


def build_run_completion_payload(
    *,
    ticket_id: str,
    title: str,
    run_id: str,
    exit_status: str,
    error_summary: Optional[str],
    cost_usd: Optional[float],
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
    base_url: str,
) -> RunCompletionPayload:
    """Build the standard run-completion notification payload."""
    duration = None
    if started_at and finished_at:
        s = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        f = finished_at if finished_at.tzinfo else finished_at.replace(tzinfo=timezone.utc)
        duration = (f - s).total_seconds()
    url = f"{base_url.rstrip('/')}/tickets/{ticket_id}"
    return RunCompletionPayload(
        ticket_id=ticket_id,
        title=title,
        run_id=run_id,
        exit_status=exit_status,
        error_summary=error_summary,
        cost_usd=cost_usd,
        duration_seconds=duration,
        url=url,
    )


def build_test_payload(base_url: str) -> RunCompletionPayload:
    """Build a synthetic payload for the "Test notification" button."""
    return RunCompletionPayload(
        ticket_id="test-ticket-id",
        title="Test notification from nightdesk",
        run_id="test-run-id",
        exit_status="success",
        error_summary=None,
        cost_usd=0.0,
        duration_seconds=42.0,
        url=f"{base_url.rstrip('/')}/tickets/test-ticket-id",
    )
