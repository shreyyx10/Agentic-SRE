"""Tenant configuration and deterministic routing.

Routing is plain code, not a model: deterministic, sub-millisecond, auditable
and editable by the customer. Patterns are globs matched with
``fnmatchcase`` and are always anchored — the old router built regexes from
raw input, so a ``.`` in a service name matched any character.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from asre.core.models import Alert, validate_customer_id


class ServiceProfile(BaseModel):
    journey: str = "unknown"  # e.g. browse | cart | checkout | payment
    revenue_critical: bool = False
    owner_team: str | None = None
    runbook: str | None = None  # path or URI of the runbook in the knowledge base


class RoutingRule(BaseModel):
    id: str
    pattern: str  # service:<glob> | type:<alert_type> | name:<glob> | <glob on service>
    team: str


class TenantConfig(BaseModel):
    customer_id: str
    display_name: str
    environment: str = "prod"
    gcp_project_id: str
    # The identity Pub/Sub uses to push this tenant's alerts. A push is accepted
    # for this tenant only if its OIDC token was minted for this account.
    push_service_account: str
    subscription: str
    reader_service_account: str | None = None
    # Where the tenant's workloads run. Used when an alert does not say (alerts on
    # log-based metrics carry only the project), so evidence stays in the
    # tenant's namespace instead of reading the whole cluster.
    k8s_cluster: str | None = None
    k8s_namespace: str | None = None
    # Predictive mode: scan this tenant's namespace for trends heading past a limit.
    predictive: bool = True
    # Workloads the scan skips: a traffic generator's own CPU climbing is not a forecast.
    predictive_exclude: list[str] = Field(default_factory=list)
    services: dict[str, ServiceProfile] = Field(default_factory=dict)
    routing_rules: list[RoutingRule] = Field(default_factory=list)
    default_team: str = "platform"

    @field_validator("customer_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        return validate_customer_id(v)


class TenantRegistry:
    def __init__(self, tenants: list[TenantConfig]) -> None:
        self._by_id = {t.customer_id: t for t in tenants}
        if len(self._by_id) != len(tenants):
            raise ValueError("duplicate customer_id in tenant configuration")

    @classmethod
    def from_json(cls, text: str) -> TenantRegistry:
        raw = json.loads(text)
        items = raw if isinstance(raw, list) else [raw]
        return cls([TenantConfig.model_validate(item) for item in items])

    @classmethod
    def from_path(cls, path: str | Path) -> TenantRegistry:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def get(self, customer_id: str) -> TenantConfig | None:
        return self._by_id.get(customer_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def all(self) -> list[TenantConfig]:
        return list(self._by_id.values())


@dataclass(frozen=True)
class Assignment:
    team: str
    rule_id: str
    profile: ServiceProfile


def _matches(pattern: str, alert: Alert) -> bool:
    if pattern == "*":
        return True
    kind, sep, rest = pattern.partition(":")
    if sep:
        if kind == "service":
            return fnmatchcase(alert.service_name, rest)
        if kind == "type":
            return alert.alert_type == rest
        if kind in {"name", "category"}:
            return fnmatchcase(alert.alert_name, rest)
        return False  # unknown prefix never matches — fail closed to the default team
    return fnmatchcase(alert.service_name, pattern)


def route(alert: Alert, tenant: TenantConfig) -> Assignment:
    profile = tenant.services.get(alert.service_name, ServiceProfile())
    for rule in tenant.routing_rules:
        if _matches(rule.pattern, alert):
            return Assignment(rule.team, rule.id, profile)
    if profile.owner_team:
        return Assignment(profile.owner_team, "service_profile", profile)
    return Assignment(tenant.default_team, "default", profile)
