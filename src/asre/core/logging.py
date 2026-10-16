"""Structured logging in the shape Cloud Logging understands.

Fixes MVP_Readiness_Review_v2 P2-1: the old logger emitted ``timestamp`` and
``trace_id`` (neither is recognised) and minted a fresh UUID per request instead
of propagating the inbound trace. Here we emit ``time``, ``severity``,
``logging.googleapis.com/trace`` and ``logging.googleapis.com/spanId``, taken
from ``X-Cloud-Trace-Context`` or W3C ``traceparent``.

Every line may carry an ``event`` field; log-based metrics (alerts_received,
groups_created, briefs_delivered, …) are defined on those event names.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

_trace: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar("trace", default=None)
_fields: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("fields", default=None)

_CLOUD_TRACE_RE = re.compile(r"^(?P<trace>[0-9a-fA-F]{32})(?:/(?P<span>\d+))?(?:;o=\d)?$")
_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-[0-9a-f]{2}$")


def parse_trace_header(cloud_trace: str | None, traceparent: str | None) -> tuple[str, str | None] | None:
    if cloud_trace:
        m = _CLOUD_TRACE_RE.match(cloud_trace.strip())
        if m:
            return m.group("trace").lower(), m.group("span")
    if traceparent:
        m = _TRACEPARENT_RE.match(traceparent.strip())
        if m:
            return m.group("trace"), m.group("span")
    return None


def bind_request(trace: tuple[str, str | None] | None, **fields: Any) -> None:
    _trace.set(trace)
    _fields.set(dict(fields))


def bind(**fields: Any) -> None:
    current = dict(_fields.get() or {})
    current.update(fields)
    _fields.set(current)


def clear() -> None:
    _trace.set(None)
    _fields.set(None)


def current_trace_header() -> str | None:
    """Value to forward as X-Cloud-Trace-Context on outbound calls (e.g. Cloud Tasks)."""
    trace = _trace.get()
    if not trace:
        return None
    trace_id, span = trace
    return f"{trace_id}/{span}" if span else trace_id


class Logger:
    def __init__(self, name: str, stream: TextIO | None = None) -> None:
        self._name = name
        self._stream = stream

    def _emit(self, level: str, text: str, /, **extra: Any) -> None:
        entry: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "severity": level,
            "message": text,
            "logger": self._name,
        }
        entry.update(_fields.get() or {})
        for key, value in extra.items():
            entry[f"field_{key}" if key in {"severity", "time", "message"} else key] = value
        trace = _trace.get()
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
        if trace and project:
            entry["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace[0]}"
            if trace[1]:
                entry["logging.googleapis.com/spanId"] = trace[1]
        stream = self._stream or sys.stdout
        stream.write(json.dumps(entry, default=str) + "\n")
        stream.flush()

    def debug(self, message: str, /, **extra: Any) -> None:
        if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
            self._emit("DEBUG", message, **extra)

    def info(self, message: str, /, **extra: Any) -> None:
        self._emit("INFO", message, **extra)

    def warning(self, message: str, /, **extra: Any) -> None:
        self._emit("WARNING", message, **extra)

    def error(self, message: str, /, **extra: Any) -> None:
        self._emit("ERROR", message, **extra)


def get_logger(name: str) -> Logger:
    return Logger(name)
