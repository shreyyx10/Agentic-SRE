from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from asre.core.auth import AuthError
from asre.core.classify import Signals, classify
from asre.core.models import Alert, AlertStatus
from asre.core.tenants import TenantRegistry

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PUSH_SA = "push-example-shop@asre-platform.iam.gserviceaccount.com"
TASKS_SA = "tasks-invoker@asre-platform.iam.gserviceaccount.com"
SUBSCRIPTION = "projects/asre-platform/subscriptions/alerts-example-shop-push"

TENANT: dict[str, Any] = {
    "customer_id": "example-shop",
    "display_name": "Example Shop (fictional)",
    "environment": "prod",
    "gcp_project_id": "example-customer-project",
    "push_service_account": PUSH_SA,
    "subscription": SUBSCRIPTION,
    "services": {
        "checkoutservice": {
            "journey": "checkout",
            "revenue_critical": True,
            "owner_team": "checkout-squad",
            "runbook": "runbooks/checkout.md",
        },
        "adservice": {"journey": "browse", "revenue_critical": False, "owner_team": "growth"},
    },
    "routing_rules": [{"id": "payments", "pattern": "service:payment*", "team": "payments-squad"}],
    "default_team": "platform",
}


def make_alert(**overrides: Any) -> Alert:
    fields: dict[str, Any] = {
        "customer_id": "example-shop",
        "source_incident_id": "0.abc123",
        "status": AlertStatus.FIRING,
        "alert_name": "Container restarts — checkout",
        "service_name": "checkoutservice",
        "environment": "prod",
        "alert_type": classify(Signals(metric_type="kubernetes.io/container/restart_count")).alert_type,
        "started_at": NOW,
    }
    fields.update(overrides)
    return Alert(**fields)


def make_notification(
    state: str = "open",
    service: str = "checkoutservice",
    metric_type: str = "kubernetes.io/container/restart_count",
    policy_name: str = "Container restarts — revenue path",
    user_labels: dict[str, str] | None = None,
    incident_id: str = "0.nxz3q7",
    **incident_extra: Any,
) -> dict[str, Any]:
    incident: dict[str, Any] = {
        "incident_id": incident_id,
        "scoping_project_id": "example-customer-project",
        "url": "https://console.cloud.google.com/monitoring/alerting/incidents/0.nxz3q7",
        "started_at": int(NOW.timestamp()),
        "ended_at": int(NOW.timestamp()) + 600 if state == "closed" else None,
        "state": state,
        "summary": "Restart count for checkoutservice is above the threshold of 0.",
        "resource": {
            "type": "k8s_container",
            "labels": {
                "cluster_name": "shop",
                "namespace_name": "default",
                "container_name": service,
                "pod_name": f"{service}-7d9c-abcde",
            },
        },
        "metric": {"type": metric_type, "displayName": "Restart count", "labels": {}},
        "policy_name": policy_name,
        "policy_user_labels": user_labels or {},
        "condition_name": "restarts > 0",
        "observed_value": "3.000",
        "threshold_value": "0",
    }
    incident.update(incident_extra)
    return {"incident": incident, "version": "1.2"}


def envelope(notification: dict[str, Any], subscription: str = SUBSCRIPTION) -> dict[str, Any]:
    data = base64.b64encode(json.dumps(notification).encode()).decode()
    return {
        "message": {
            "data": data,
            "attributes": {},
            "messageId": "123",
            "publishTime": "2026-10-01T09:00:00Z",
        },
        "subscription": subscription,
    }


class FakeVerifier:
    """Maps a token to claims; anything else is an invalid token."""

    def __init__(self, tokens: dict[str, dict[str, Any]] | None = None) -> None:
        self.tokens = tokens or {
            "good-push": {"email": PUSH_SA, "email_verified": True},
            "other-tenant": {
                "email": "push-other@asre-platform.iam.gserviceaccount.com",
                "email_verified": True,
            },
            "unverified": {"email": PUSH_SA, "email_verified": False},
            "good-task": {"email": TASKS_SA, "email_verified": True},
        }
        self.audiences: list[str] = []

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        self.audiences.append(audience)
        if token not in self.tokens:
            raise AuthError(401, "invalid token")
        return self.tokens[token]


@pytest.fixture
def tenants() -> TenantRegistry:
    return TenantRegistry.from_json(json.dumps([TENANT]))
