"""Read secret values from Secret Manager at runtime.

Secrets never live in the repository, in Terraform variables or state, or in
Cloud Run environment variables. Terraform passes only the secret's resource
name (``ASRE_SECRET_<ID>=projects/<p>/secrets/<id>``, see ``config.secret_names``)
and grants the service ``secretAccessor`` on that one secret. The value is
fetched here, over the Secret Manager REST API with the service's own
identity (ADC), cached briefly so a rotated value is picked up within minutes,
and never logged.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

_API = "https://secretmanager.googleapis.com/v1"


class SecretError(RuntimeError):
    """A secret is unknown, has no enabled version, or could not be read."""


class _Session(Protocol):
    def get(self, url: str, timeout: float) -> Any: ...


def _default_session() -> _Session:  # pragma: no cover — needs real credentials
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


class SecretReader:
    def __init__(
        self,
        names: Mapping[str, str],
        session_factory: Callable[[], _Session] = _default_session,
        ttl_s: float = 300.0,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._names = dict(names)
        self._session_factory = session_factory
        self._session: _Session | None = None
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._clock = clock
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def available(self) -> list[str]:
        return sorted(self._names)

    def get(self, secret_id: str) -> str:
        """Latest enabled version of ``secret_id`` (the short ID, e.g. ``chat-webhook-url``)."""
        name = self._names.get(secret_id)
        if not name:
            raise SecretError(f"secret {secret_id!r} is not configured for this service")
        now = self._clock()
        with self._lock:
            hit = self._cache.get(secret_id)
            if hit and now - hit[0] < self._ttl_s:
                return hit[1]
            if self._session is None:
                self._session = self._session_factory()
            session = self._session
        response = session.get(f"{_API}/{name}/versions/latest:access", timeout=self._timeout_s)
        if response.status_code != 200:
            # Status only: the response body could echo request details, never the value.
            raise SecretError(f"secret {secret_id!r}: Secret Manager returned HTTP {response.status_code}")
        try:
            value = base64.b64decode(response.json()["payload"]["data"]).decode("utf-8")
        except (KeyError, ValueError, TypeError) as exc:
            raise SecretError(f"secret {secret_id!r}: unexpected response shape") from exc
        with self._lock:
            self._cache[secret_id] = (now, value)
        return value
