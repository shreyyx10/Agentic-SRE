"""Adapter: Cloud Monitoring incident notification (schema 1.2) -> ``Alert``.

Cloud Monitoring publishes the same JSON body to a Pub/Sub notification channel
that it would POST to a webhook. The tenant is NOT read from this body: the
caller passes the customer_id it derived from the authenticated channel.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from asre.core.classify import Signals, classify
from asre.core.models import Alert, AlertStatus

_SERVICE_LABELS = ("service_name", "service", "module_id", "job")
# Container names that say nothing about the service (Online Boutique names every container "server").
_GENERIC_CONTAINER_NAMES = {"server", "main", "app", "container", "web"}
# Deployment pod: <deployment>-<pod-template-hash>-<suffix>
_POD_NAME_RE = re.compile(r"^(?P<name>[a-z0-9][a-z0-9-]*?)-[a-z0-9]{6,10}-[a-z0-9]{5}$")


class PayloadError(ValueError):
    """The message is not a usable Cloud Monitoring notification."""


def decode_pubsub_envelope(envelope: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], str]:
    """Return (notification, attributes, subscription) from a Pub/Sub push body."""
    message = envelope.get("message")
    if not isinstance(message, Mapping):
        raise PayloadError("missing message")
    data = message.get("data")
    if not isinstance(data, str) or not data:
        raise PayloadError("missing message.data")
    try:
        notification = json.loads(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise PayloadError("message.data is not base64 JSON") from exc
    if not isinstance(notification, dict):
        raise PayloadError("notification is not an object")
    attributes = {str(k): str(v) for k, v in (message.get("attributes") or {}).items()}
    return notification, attributes, str(envelope.get("subscription") or "")


def _labels(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _epoch(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PayloadError(f"invalid epoch timestamp: {value!r}") from exc


def _service_name(
    user_labels: dict[str, str], system_labels: dict[str, str], resource_labels: dict[str, str]
) -> str | None:
    """Most explicit source first: policy label, workload controller, pod name, then resource labels."""
    if user_labels.get("service"):
        return user_labels["service"]
    if system_labels.get("top_level_controller_name"):
        return system_labels["top_level_controller_name"]
    match = _POD_NAME_RE.match(resource_labels.get("pod_name", ""))
    if match:
        return match.group("name")
    container = resource_labels.get("container_name", "")
    if container and container not in _GENERIC_CONTAINER_NAMES:
        return container
    for key in _SERVICE_LABELS:
        if resource_labels.get(key):
            return resource_labels[key]
    return None


def to_alert(notification: Mapping[str, Any], customer_id: str, default_environment: str) -> Alert:
    incident = notification.get("incident")
    if not isinstance(incident, Mapping):
        raise PayloadError("missing incident")

    resource = incident.get("resource") if isinstance(incident.get("resource"), Mapping) else {}
    metric = incident.get("metric") if isinstance(incident.get("metric"), Mapping) else {}
    metadata = incident.get("metadata") if isinstance(incident.get("metadata"), Mapping) else {}
    resource_labels = _labels(resource.get("labels"))
    metric_labels = _labels(metric.get("labels"))
    user_labels = {**_labels(metadata.get("user_labels")), **_labels(incident.get("policy_user_labels"))}

    service = _service_name(user_labels, _labels(metadata.get("system_labels")), resource_labels)
    if not service:
        raise PayloadError("cannot determine service name (set a 'service' user label on the policy)")

    state = str(incident.get("state", "")).lower()
    if state not in {"open", "closed"}:
        raise PayloadError(f"unknown incident state: {state!r}")

    started_at = _epoch(incident.get("started_at"))
    if started_at is None:
        raise PayloadError("missing started_at")

    alert_name = str(incident.get("policy_name") or incident.get("condition_name") or "")
    classification = classify(
        Signals(
            alert_name=alert_name,
            metric_type=str(metric.get("type") or ""),
            metric_labels=metric_labels,
            resource_labels=resource_labels,
            user_labels=user_labels,
        )
    )

    try:
        return Alert(
            customer_id=customer_id,
            source="cloud_monitoring",
            source_incident_id=str(incident.get("incident_id") or ""),
            status=AlertStatus.FIRING if state == "open" else AlertStatus.RESOLVED,
            alert_name=alert_name,
            service_name=service,
            environment=user_labels.get("environment") or default_environment,
            alert_type=classification.alert_type,
            classified_by=classification.rule,
            severity=user_labels.get("severity") or str(incident.get("severity") or "unknown").lower(),
            started_at=started_at,
            ended_at=_epoch(incident.get("ended_at")),
            resource_type=str(resource.get("type") or incident.get("resource_type_display_name") or ""),
            resource_labels=resource_labels,
            metric_type=str(metric.get("type") or ""),
            metric_labels=metric_labels,
            summary=str(incident.get("summary") or "")[:4096],
            console_url=str(incident.get("url") or ""),
            observed_value=None
            if incident.get("observed_value") is None
            else str(incident["observed_value"]),
            threshold_value=None
            if incident.get("threshold_value") is None
            else str(incident["threshold_value"]),
        )
    except ValidationError as exc:
        raise PayloadError(f"invalid alert: {exc.errors()[0]['msg']}") from exc
