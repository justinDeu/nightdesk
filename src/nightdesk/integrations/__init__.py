"""External forge integrations (GitLab in v1; Jira reserved for v3).

See docs/design/gitlab-jira-integrations.md. This package holds the per-provider
HTTP clients behind a small common interface plus the shared error vocabulary.
The domain layer (``nightdesk.domain.integrations``) owns persistence, the TTL
proxy cache, and the refresh loop; routes never talk to a forge directly.
"""
from __future__ import annotations


class IntegrationError(Exception):
    """Base for every forge-client failure. Carries a human message and,
    where meaningful, the upstream HTTP status."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class AuthError(IntegrationError):
    """401/403 — the credential is missing, wrong, or lacks scope."""


class NotFoundError(IntegrationError):
    """404 — the project/issue/MR does not exist (or the token can't see it)."""


class RateLimited(IntegrationError):
    """429 — honor ``retry_after`` (seconds) and back off."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message, status=429)
        self.retry_after = retry_after


class Unreachable(IntegrationError):
    """The host could not be reached (DNS/connect/timeout/TLS)."""


__all__ = [
    "IntegrationError",
    "AuthError",
    "NotFoundError",
    "RateLimited",
    "Unreachable",
]
