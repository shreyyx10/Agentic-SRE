"""Domain models shared by every service.

Everything that arrives from a customer's alerting system is *untrusted input*:
identifiers are validated here, at the edge, and free text (summaries, labels)
is carried as data and never interpreted.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Identifiers become part of Firestore document paths and of the de-duplication
# key. They must be unambiguous and must not contain path separators that could
# address another tenant's documents.
CUSTOMER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class AlertType(StrEnum):
    """Fine-grained alert type, shown to responders and used by routing."""

    OOM_KILLED = "oom_killed"
    CONTAINER_CRASH = "container_crash"
    HIGH_LATENCY = "high_latency"
    ERROR_RATE = "error_rate"
    SATURATION = "saturation"
    # Made only by the platform's predictive scan, never from a customer's alert labels.
    FORECAST = "forecast"
    UNKNOWN = "unknown"


class AlertFamily(StrEnum):
    """Coarse family used in the de-duplication key.

    An OOM kill and the CrashLoopBackOff it causes are one incident, often
    reported by two different alert policies. Keying de-duplication on the
    family (not the fine type) keeps them in one group — see ADR-0005.
    """

    CONTAINER_FAILURE = "container_failure"
    LATENCY = "latency"
    ERRORS = "errors"
    SATURATION = "saturation"
    FORECAST = "forecast"  # a prediction is its own incident, separate from the alert it pre-empts
    UNKNOWN = "unknown"


FAMILY_OF: dict[AlertType, AlertFamily] = {
    AlertType.OOM_KILLED: AlertFamily.CONTAINER_FAILURE,
    AlertType.CONTAINER_CRASH: AlertFamily.CONTAINER_FAILURE,
    AlertType.HIGH_LATENCY: AlertFamily.LATENCY,
    AlertType.ERROR_RATE: AlertFamily.ERRORS,
    AlertType.SATURATION: AlertFamily.SATURATION,
    AlertType.FORECAST: AlertFamily.FORECAST,
    AlertType.UNKNOWN: AlertFamily.UNKNOWN,
}


class GroupState(StrEnum):
    OPEN = "open"  # inside its de-duplication window
    DORMANT = "dormant"  # window expired without a resolution; archived
    CLOSED = "closed"  # resolved by a resolution notification


def validate_customer_id(value: str) -> str:
    if not CUSTOMER_ID_RE.fullmatch(value):
        raise ValueError(f"invalid customer_id: {value!r}")
    return value


def validate_identifier(value: str, field_name: str = "identifier") -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {field_name}: {value!r}")
    return value


class Alert(BaseModel):
    """A normalised alert. Built by an ingest adapter, never from raw client JSON."""

    model_config = ConfigDict(frozen=True)

    customer_id: str
    source: str = "cloud_monitoring"
    source_incident_id: str = Field(min_length=1, max_length=256)
    status: AlertStatus
    alert_name: str = Field(max_length=512)
    service_name: str
    environment: str
    alert_type: AlertType
    classified_by: str = "unknown"
    severity: str = "unknown"
    started_at: datetime
    ended_at: datetime | None = None
    resource_type: str = ""
    resource_labels: dict[str, str] = Field(default_factory=dict)
    metric_type: str = ""
    metric_labels: dict[str, str] = Field(default_factory=dict)
    # Free text from the customer's system: untrusted, displayed and cited, never obeyed.
    summary: str = Field(default="", max_length=4096)
    console_url: str = ""
    observed_value: str | None = None
    threshold_value: str | None = None

    @field_validator("customer_id")
    @classmethod
    def _customer_id(cls, v: str) -> str:
        return validate_customer_id(v)

    @field_validator("service_name")
    @classmethod
    def _service_name(cls, v: str) -> str:
        return validate_identifier(v, "service_name")

    @field_validator("environment")
    @classmethod
    def _environment(cls, v: str) -> str:
        return validate_identifier(v, "environment")

    @field_validator("started_at", "ended_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v

    @property
    def family(self) -> AlertFamily:
        return FAMILY_OF[self.alert_type]
