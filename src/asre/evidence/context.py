"""What an evidence source may read, derived from the tenant and the alert.

The alert is untrusted input. It may name the cluster, namespace and service,
but never the project: the project always comes from the tenant's own
configuration, so an alert cannot point the reader at another project. Every
name that goes into a query filter must be a plain Kubernetes name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from asre.core.models import Alert
from asre.core.tenants import ServiceProfile, TenantConfig

# RFC 1123 label: what Kubernetes allows for namespaces and most object names,
# and safe to place inside a quoted filter string.
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
GCP_NAME_RE = re.compile(r"^[a-z][-a-z0-9]{0,61}[a-z0-9]$")


def k8s_name(value: str | None) -> str | None:
    return value if value and K8S_NAME_RE.fullmatch(value) else None


@dataclass(frozen=True)
class EvidenceWindows:
    logs_before: timedelta = timedelta(minutes=15)
    metrics_before: timedelta = timedelta(minutes=30)
    deploys_before: timedelta = timedelta(hours=6)


@dataclass(frozen=True)
class IncidentContext:
    customer_id: str
    project_id: str
    reader: str | None
    alert: Alert
    service: str | None  # validated Kubernetes name, or None if the alert's service is not one
    cluster: str | None
    namespace: str | None
    profile: ServiceProfile | None
    now: datetime
    windows: EvidenceWindows = EvidenceWindows()

    @property
    def anchor(self) -> datetime:
        """The incident start, clamped so a skewed clock cannot move the windows into the future."""
        return min(self.alert.started_at, self.now)

    def since(self, before: timedelta) -> datetime:
        return self.anchor - before


def context_for(
    alert: Alert, tenant: TenantConfig, now: datetime, windows: EvidenceWindows | None = None
) -> IncidentContext:
    if not GCP_NAME_RE.fullmatch(tenant.gcp_project_id):
        raise ValueError(f"tenant {tenant.customer_id} has an invalid gcp_project_id")
    labels = alert.resource_labels
    return IncidentContext(
        customer_id=tenant.customer_id,
        project_id=tenant.gcp_project_id,
        reader=tenant.reader_service_account,
        alert=alert,
        service=k8s_name(alert.service_name),
        cluster=k8s_name(labels.get("cluster_name")) or k8s_name(tenant.k8s_cluster),
        namespace=k8s_name(labels.get("namespace_name")) or k8s_name(tenant.k8s_namespace),
        profile=tenant.services.get(alert.service_name),
        now=now,
        windows=windows or EvidenceWindows(),
    )
