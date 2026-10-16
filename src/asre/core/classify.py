"""Alert-type classification from structured signals, with an explicit precedence table.

Fixes MVP_Readiness_Review_v2 P1-5: the old classifier tested substrings of the
alert *name* in if-chain order, so "Container OOMKilled CrashLoopBackOff" came
out as the generic ContainerCrash. Here:

1. structured signals win (an explicit policy label, exit code 137, the k8s
   ``reason``, the metric type);
2. name matching is only a fallback, ordered most-specific-first;
3. the table is data, so the precedence is visible and testable, and every
   result says which rule produced it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from asre.core.models import AlertType


@dataclass(frozen=True)
class Signals:
    alert_name: str = ""
    metric_type: str = ""
    metric_labels: Mapping[str, str] = field(default_factory=dict)
    resource_labels: Mapping[str, str] = field(default_factory=dict)
    user_labels: Mapping[str, str] = field(default_factory=dict)
    reason: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class Classification:
    alert_type: AlertType
    rule: str


def _explicit_label(s: Signals) -> AlertType | None:
    value = (s.user_labels.get("alert_type") or "").strip().lower()
    try:
        alert_type = AlertType(value) if value else None
    except ValueError:
        return None
    # Only the platform's own scan makes forecasts; a customer label cannot claim one.
    return None if alert_type is AlertType.FORECAST else alert_type


def _lower(text: str) -> str:
    return text.lower()


_ERROR_CLASSES = {"5xx", "500"}

# (rule name, predicate, result) — evaluated top to bottom; first match wins.
_RULES: list[tuple[str, Callable[[Signals], bool], AlertType]] = [
    ("exit_code_137", lambda s: s.exit_code == 137, AlertType.OOM_KILLED),
    ("reason_oom", lambda s: (s.reason or "") in {"OOMKilled", "OOMKilling"}, AlertType.OOM_KILLED),
    (
        "metric_oom",
        lambda s: any(t in _lower(s.metric_type) for t in ("oom_kill", "oomkill", "/oom")),
        AlertType.OOM_KILLED,
    ),
    ("reason_crashloop", lambda s: (s.reason or "") == "CrashLoopBackOff", AlertType.CONTAINER_CRASH),
    (
        "metric_restart_count",
        lambda s: _lower(s.metric_type).endswith("container/restart_count"),
        AlertType.CONTAINER_CRASH,
    ),
    (
        "metric_latency",
        lambda s: any(t in _lower(s.metric_type) for t in ("latenc", "took_ms", "response_time")),
        AlertType.HIGH_LATENCY,
    ),
    (
        "metric_error_class",
        lambda s: (
            (s.metric_labels.get("response_code_class") or "").lower() in _ERROR_CLASSES
            or any(t in _lower(s.metric_type) for t in ("5xx", "error"))
        ),
        AlertType.ERROR_RATE,
    ),
    (
        "metric_saturation",
        lambda s: any(t in _lower(s.metric_type) for t in ("memory/used_bytes", "limit_utilization")),
        AlertType.SATURATION,
    ),
    # --- name fallback, most specific first -----------------------------------
    (
        "name_oom",
        lambda s: "oomkill" in _lower(s.alert_name) or "out of memory" in _lower(s.alert_name),
        AlertType.OOM_KILLED,
    ),
    (
        "name_crash",
        lambda s: any(t in _lower(s.alert_name) for t in ("crashloop", "restart", "crash")),
        AlertType.CONTAINER_CRASH,
    ),
    ("name_latency", lambda s: "latenc" in _lower(s.alert_name), AlertType.HIGH_LATENCY),
    ("name_error", lambda s: any(t in _lower(s.alert_name) for t in ("5xx", "error")), AlertType.ERROR_RATE),
]

PRECEDENCE: tuple[str, ...] = ("explicit_label", *(name for name, _, _ in _RULES), "unknown")


def classify(signals: Signals) -> Classification:
    explicit = _explicit_label(signals)
    if explicit is not None:
        return Classification(explicit, "explicit_label")
    for name, predicate, result in _RULES:
        if predicate(signals):
            return Classification(result, name)
    return Classification(AlertType.UNKNOWN, "unknown")
