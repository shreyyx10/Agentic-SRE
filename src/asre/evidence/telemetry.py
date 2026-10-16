"""Read-only access to a customer's Cloud Logging and Cloud Monitoring.

ANALYSE never reads a customer project with its own identity. It impersonates
that tenant's reader (``cust-<customer_id>``), which the customer granted only
``roles/logging.viewer`` and ``roles/monitoring.viewer`` (docs/customer-access.md).
The token is minted with read-only scopes, so even a mis-granted role could not
be used to write.

The REST APIs are called directly through ``AuthorizedSession`` (already a
dependency through google-auth), rather than adding two client libraries.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Protocol

LOGGING_URL = "https://logging.googleapis.com/v2/entries:list"
MONITORING_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
READ_SCOPES = [
    "https://www.googleapis.com/auth/logging.read",
    "https://www.googleapis.com/auth/monitoring.read",
]


class TelemetryError(Exception):
    pass


class Telemetry(Protocol):
    def list_logs(self, project: str, filter_: str, limit: int) -> list[dict[str, Any]]:
        """Newest first."""
        ...

    def list_time_series(
        self, project: str, filter_: str, start: datetime, end: datetime, aggregation: dict[str, str]
    ) -> list[dict[str, Any]]: ...


def utc(t: datetime) -> str:
    """RFC 3339 in UTC, as both APIs expect. ``t`` must be timezone-aware."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RestTelemetry:
    """Calls the Logging and Monitoring REST APIs as one identity.

    ``reader`` is the service account to impersonate; ``None`` uses the caller's
    own credentials (local live checks only).
    """

    def __init__(self, reader: str | None, timeout_s: float = 10.0, session: Any = None):
        self.reader = reader
        self.timeout_s = timeout_s
        self._session = session
        self._lock = threading.Lock()

    def _authorized(self) -> Any:  # pragma: no cover — needs Google credentials
        with self._lock:
            if self._session is None:
                import google.auth
                from google.auth import impersonated_credentials
                from google.auth.transport.requests import AuthorizedSession

                source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                creds = source
                if self.reader:
                    creds = impersonated_credentials.Credentials(
                        source_credentials=source,
                        target_principal=self.reader,
                        target_scopes=READ_SCOPES,
                        lifetime=900,
                    )
                self._session = AuthorizedSession(creds)
            return self._session

    def _check(self, response: Any) -> dict[str, Any]:
        if response.status_code != 200:
            raise TelemetryError(f"{response.status_code}: {response.text[:300]}")
        return response.json() or {}

    def list_logs(self, project: str, filter_: str, limit: int) -> list[dict[str, Any]]:
        body = {
            "resourceNames": [f"projects/{project}"],
            "filter": filter_,
            "orderBy": "timestamp desc",
            "pageSize": min(limit, 1000),
        }
        data = self._check(self._authorized().post(LOGGING_URL, json=body, timeout=self.timeout_s))
        return list(data.get("entries", []))[:limit]

    def list_time_series(
        self, project: str, filter_: str, start: datetime, end: datetime, aggregation: dict[str, str]
    ) -> list[dict[str, Any]]:
        params = {
            "filter": filter_,
            "interval.startTime": utc(start),
            "interval.endTime": utc(end),
            "view": "FULL",
            **{f"aggregation.{k}": v for k, v in aggregation.items()},
        }
        url = MONITORING_URL.format(project=project)
        data = self._check(self._authorized().get(url, params=params, timeout=self.timeout_s))
        return list(data.get("timeSeries", []))


class TelemetryPool:
    """One ``RestTelemetry`` per reader identity, reused across requests."""

    def __init__(self, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self._by_reader: dict[str | None, RestTelemetry] = {}
        self._lock = threading.Lock()

    def for_reader(self, reader: str | None) -> RestTelemetry:
        with self._lock:
            if reader not in self._by_reader:
                self._by_reader[reader] = RestTelemetry(reader, self.timeout_s)
            return self._by_reader[reader]
