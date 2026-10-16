"""The evidence sources: the alert, container logs, Kubernetes events, metrics and deployments.

Each source is deterministic: it builds fixed queries from the incident
context, reads as the tenant's reader, and condenses what it finds into a few
citable items. Nothing here is chosen by the model.

What each one is for, in the four demo faults:

* **logs**: the failing service's warnings and errors, grouped by message shape
  (the payment fault shows as frontend errors calling paymentservice);
* **kubernetes events**: OOM kills, back-offs and failed probes in the namespace;
* **metrics**: memory and CPU against the container limit, and restarts, for the
  alerting service (the OOM fault shows memory pinned at its limit);
* **deployments**: every change to a Deployment in the namespace from the GKE
  audit log, with its ``change-cause`` (the latency, OOM and payment faults are
  all deployment changes, and often not to the service that alerts).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from asre.evidence.context import IncidentContext
from asre.evidence.telemetry import Telemetry, utc

MAX_ITEM_CHARS = 1200

# GKE's own namespaces. When neither the alert nor the tenant names a namespace,
# evidence from these is left out: it is the platform's noise, not the customer's.
SYSTEM_NAMESPACES = (
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "gmp-system",
    "gmp-public",
    "gke-managed-system",
    "gke-gmp-system",
    "gke-managed-cim",
)


@dataclass(frozen=True)
class Item:
    key: str
    source: str  # one of asre.agent.prompt.SourceKind
    text: str


@dataclass(frozen=True)
class Source:
    name: str
    collect: Callable[[IncidentContext, Telemetry], list[Item]]
    needs_telemetry: bool = True


def short_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


def q(value: str) -> str:
    """Quote a value for a Logging or Monitoring filter."""
    return json.dumps(value)


def clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hhmm(ts: str | datetime | None) -> str:
    if isinstance(ts, datetime):
        return utc(ts)
    return (ts or "?")[:19].replace("T", " ") + ("Z" if ts else "")


# --------------------------------------------------------------------- alert


def alert_items(ctx: IncidentContext, _telemetry: Telemetry | None = None) -> list[Item]:
    a = ctx.alert
    lines = [
        f"alert: {a.alert_name}",
        f"service: {a.service_name}  type: {a.alert_type.value}  severity: {a.severity}",
        f"environment: {a.environment}",
        f"started: {utc(a.started_at)}  status: {a.status.value}",
    ]
    if a.observed_value is not None or a.threshold_value is not None:
        lines.append(f"observed: {a.observed_value}  threshold: {a.threshold_value}")
    if a.metric_type:
        lines.append(f"metric: {a.metric_type}")
    if a.resource_labels:
        lines.append("resource: " + ", ".join(f"{k}={v}" for k, v in sorted(a.resource_labels.items())))
    if a.summary:
        lines.append(f"summary: {a.summary}")
    items = [Item("alert:summary", "alert", clip("\n".join(lines)))]
    if ctx.profile is not None:
        p = ctx.profile
        items.append(
            Item(
                "alert:service_profile",
                "alert",
                f"{a.service_name}: journey={p.journey} revenue_critical={p.revenue_critical} "
                f"owner_team={p.owner_team or 'unknown'}",
            )
        )
    return items


# --------------------------------------------------------------------- logs

_NOISE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\b"), "<time>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|m|Mi|Gi|Ki|%)?\b"), "<n>"),
]


def message_shape(message: str) -> str:
    """The message with IDs, times and numbers removed, so repeats group together."""
    shape = message
    for pattern, token in _NOISE:
        shape = pattern.sub(token, shape)
    return " ".join(shape.split())[:300]


def log_message(entry: dict[str, Any]) -> str:
    if entry.get("textPayload"):
        return str(entry["textPayload"]).strip()
    payload = entry.get("jsonPayload") or {}
    for field in ("message", "msg", "error", "err"):
        if payload.get(field):
            msg = str(payload[field])
            extra = {k: payload[k] for k in ("error", "http.req.path", "http.resp.status") if k in payload}
            extra.pop(field, None)
            return msg + (f" {json.dumps(extra, sort_keys=True)}" if extra else "")
    proto = entry.get("protoPayload") or {}
    if proto.get("status", {}).get("message"):
        return str(proto["status"]["message"])
    return json.dumps(payload, sort_keys=True)[:400] if payload else ""


@dataclass
class _Group:
    severity: str
    example: str
    first: str
    last: str
    count: int = 0


def group_entries(entries: list[dict[str, Any]], message: Callable[[dict[str, Any]], str]) -> list[_Group]:
    groups: dict[str, _Group] = {}
    for entry in entries:
        text = message(entry)
        if not text:
            continue
        ts = entry.get("timestamp", "")
        g = groups.get(shape := message_shape(text))
        if g is None:
            g = groups[shape] = _Group(entry.get("severity", "DEFAULT"), text, ts, ts)
        g.count += 1
        g.first, g.last = min(g.first, ts), max(g.last, ts)
    return sorted(groups.values(), key=lambda g: (-g.count, g.first))


def collect_logs(ctx: IncidentContext, telemetry: Telemetry, limit: int = 500, keep: int = 12) -> list[Item]:
    if ctx.service is None:
        return []
    parts = [
        'resource.type="k8s_container"',
        f'labels."k8s-pod/app"={q(ctx.service)}',
        f"timestamp>={q(utc(ctx.since(ctx.windows.logs_before)))}",
        f"timestamp<={q(utc(ctx.now))}",
        '(severity>=WARNING OR jsonPayload."http.resp.status">=500)',
    ]
    if ctx.namespace:
        parts.append(f"resource.labels.namespace_name={q(ctx.namespace)}")
    if ctx.cluster:
        parts.append(f"resource.labels.cluster_name={q(ctx.cluster)}")
    entries = telemetry.list_logs(ctx.project_id, "\n".join(parts), limit)
    items = []
    for g in group_entries(entries, log_message)[:keep]:
        key = f"log:{ctx.service}:{short_hash(message_shape(g.example))}"
        text = f"{ctx.service} {g.severity} ×{g.count} ({hhmm(g.first)} to {hhmm(g.last)}): {g.example}"
        items.append(Item(key, "logs", clip(text)))
    if len(entries) >= limit:
        items.append(
            Item(
                f"log:{ctx.service}:volume",
                "logs",
                f"{ctx.service}: at least {limit} warning/error lines in the window (query cap reached).",
            )
        )
    return items


# --------------------------------------------------------------------- Kubernetes events


def event_message(entry: dict[str, Any]) -> str:
    p = entry.get("jsonPayload") or {}
    obj = p.get("involvedObject") or {}
    name = obj.get("name", "")
    return f"{p.get('reason', '?')} {obj.get('kind', '')}/{name}: {p.get('message', '')}".strip()


def collect_events(
    ctx: IncidentContext, telemetry: Telemetry, limit: int = 300, keep: int = 10
) -> list[Item]:
    scope = []
    if ctx.cluster:
        scope.append(f"resource.labels.cluster_name={q(ctx.cluster)}")
    if ctx.namespace:
        # OOM kills are reported by the node, outside any namespace.
        scope.append(
            f'(resource.labels.namespace_name={q(ctx.namespace)} OR jsonPayload.reason="OOMKilling")'
        )
    else:
        scope.extend(f"-resource.labels.namespace_name={q(ns)}" for ns in SYSTEM_NAMESPACES)
        scope.append('(jsonPayload.involvedObject.kind!="Node" OR jsonPayload.reason="OOMKilling")')
    filter_ = "\n".join(
        [
            f"logName={q(f'projects/{ctx.project_id}/logs/events')}",
            'jsonPayload.type="Warning"',
            f"timestamp>={q(utc(ctx.since(ctx.windows.logs_before)))}",
            f"timestamp<={q(utc(ctx.now))}",
            *scope,
        ]
    )
    entries = telemetry.list_logs(ctx.project_id, filter_, limit)
    items = []
    for g in group_entries(entries, event_message)[:keep]:
        key = f"log:event:{short_hash(message_shape(g.example))}"
        items.append(
            Item(key, "logs", clip(f"k8s event ×{g.count} ({hhmm(g.first)} to {hhmm(g.last)}): {g.example}"))
        )
    return items


# --------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class MetricQuery:
    key: str
    label: str
    metric: str
    aligner: str
    reducer: str
    unit: str  # "%" scales a 0..1 ratio; "" prints counts


METRICS = (
    MetricQuery(
        "metric:memory_limit_utilization",
        "memory used / limit",
        "kubernetes.io/container/memory/limit_utilization",
        "ALIGN_MAX",
        "REDUCE_MAX",
        "%",
    ),
    MetricQuery(
        "metric:cpu_limit_utilization",
        "CPU used / limit",
        "kubernetes.io/container/cpu/limit_utilization",
        "ALIGN_MAX",
        "REDUCE_MAX",
        "%",
    ),
    MetricQuery(
        "metric:restart_count",
        "container restarts",
        "kubernetes.io/container/restart_count",
        "ALIGN_DELTA",
        "REDUCE_SUM",
        "",
    ),
)


def point_value(point: dict[str, Any]) -> float | None:
    v = point.get("value") or {}
    for field in ("doubleValue", "int64Value"):
        if field in v:
            return float(v[field])
    return None


def series_points(series: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """(end time, value) oldest first, from the first reduced series."""
    if not series:
        return []
    out = []
    for p in series[0].get("points", []):
        value = point_value(p)
        if value is not None:
            out.append(((p.get("interval") or {}).get("endTime", ""), value))
    return sorted(out)


def describe_series(
    m: MetricQuery, service: str, points: list[tuple[str, float]], anchor: datetime, step: int = 5
) -> str:
    def fmt(v: float) -> str:
        return f"{v * 100:.0f}%" if m.unit == "%" else f"{v:.0f}"

    values = [v for _, v in points]
    # The peak in the minutes around the alert: the value that made it fire.
    lo, hi = utc(anchor - timedelta(minutes=3)), utc(anchor + timedelta(minutes=1))
    at_alert = [v for t, v in points if lo <= t <= hi]
    summary = (
        f"{service} {m.label}: max {fmt(max(values))}, latest {fmt(values[-1])}"
        + (f", around the alert {fmt(max(at_alert))}" if at_alert else "")
        + (f", total {fmt(sum(values))} in window" if m.unit == "" else "")
    )
    # A compact trace: one value per `step` minutes (the max inside each bucket).
    trace = []
    for i in range(0, len(points), step):
        bucket = points[i : i + step]
        trace.append(f"{bucket[-1][0][11:16]} {fmt(max(v for _, v in bucket))}")
    return clip(f"{summary}. Per {step} min (UTC): " + ", ".join(trace))


def collect_metrics(ctx: IncidentContext, telemetry: Telemetry) -> list[Item]:
    if ctx.service is None:
        return []
    scope = [
        'resource.type="k8s_container"',
        f"resource.labels.pod_name=starts_with({q(ctx.service + '-')})",
    ]
    if ctx.namespace:
        scope.append(f"resource.labels.namespace_name={q(ctx.namespace)}")
    if ctx.cluster:
        scope.append(f"resource.labels.cluster_name={q(ctx.cluster)}")
    start = ctx.since(ctx.windows.metrics_before)
    items = []
    for m in METRICS:
        filter_ = " AND ".join([f"metric.type={q(m.metric)}", *scope])
        series = telemetry.list_time_series(
            ctx.project_id,
            filter_,
            start,
            ctx.now,
            {"alignmentPeriod": "60s", "perSeriesAligner": m.aligner, "crossSeriesReducer": m.reducer},
        )
        points = series_points(series)
        if points:
            items.append(Item(m.key, "metrics", describe_series(m, ctx.service, points, ctx.anchor)))
    return items


# --------------------------------------------------------------------- deployments

# No backslashes: the filter is a quoted string, and an escaped "\\." would reach the
# regex engine as a literal backslash. "[.]" means a literal dot without escaping.
_DEPLOY_METHOD = "[.]deployments([.]scale)?[.](patch|update|create|delete)$"
_SKIP_KEYS = {
    "@type",  # the audit log's own marker (k8s.io/Patch), not part of the change
    "managedFields",
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "generation",
    "status",
    "selfLink",
}


def flatten(value: Any, prefix: str = "", out: list[str] | None = None, limit: int = 14) -> list[str]:
    """Leaf paths of a request body, e.g. ``spec.replicas=0``; enough to say what changed."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(value, dict):
        for k in sorted(value):
            if k not in _SKIP_KEYS and not k.startswith("$"):
                flatten(value[k], f"{prefix}.{k}" if prefix else k, out, limit)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            name = v.get("name") if isinstance(v, dict) else None
            if name is not None and set(v) <= {"name", "value"}:
                out.append(f"{prefix}[{name}]={v.get('value', '')}")  # an env var: env[USERS]=400
            elif name is not None:
                # The name is already in the path; listing it again as a change is noise.
                flatten({k: x for k, x in v.items() if k != "name"}, f"{prefix}[{name}]", out, limit)
            else:
                flatten(v, f"{prefix}[{i}]", out, limit)
    else:
        out.append(f"{prefix}={value}")
    return out


_POD = "spec.template.spec."


def readable_changes(request: dict[str, Any]) -> list[str]:
    """What a patch changed, briefly: pod-template paths shortened, init-container echoes dropped.

    ``kubectl set env`` writes the same variables into every container, init containers
    included; the main container's line says it once.
    """
    changed = [c.removeprefix(_POD) for c in flatten(request) if "change-cause" not in c]
    main = {c.split("]", 1)[1] for c in changed if c.startswith("containers[") and "]" in c}
    return [c for c in changed if not (c.startswith("initContainers[") and c.split("]", 1)[-1] in main)]


def describe_change(entry: dict[str, Any]) -> tuple[str, str]:
    p = entry.get("protoPayload") or {}
    method = str(p.get("methodName", "")).rsplit(".deployments", 1)[-1].lstrip(".") or "change"
    name = str(p.get("resourceName", "")).rsplit("/", 1)[-1] or "?"
    if "/scale" in str(p.get("resourceName", "")):
        name = str(p.get("resourceName", "")).split("/deployments/")[-1].split("/")[0]
    who = (p.get("authenticationInfo") or {}).get("principalEmail", "unknown")
    request = p.get("request") or {}
    cause = ((request.get("metadata") or {}).get("annotations") or {}).get("kubernetes.io/change-cause")
    changed = readable_changes(request)
    failed = (p.get("status") or {}).get("code") not in (None, 0)
    text = f"{hhmm(entry.get('timestamp'))} deployment/{name} {method} by {who}"
    if failed:
        text += " (FAILED)"
    if cause:
        text += f'; change-cause: "{cause}"'
    if changed:
        text += "; changed: " + ", ".join(changed)
    return name, clip(text)


def collect_deployments(
    ctx: IncidentContext, telemetry: Telemetry, limit: int = 50, keep: int = 10
) -> list[Item]:
    parts = [
        f"logName={q(f'projects/{ctx.project_id}/logs/cloudaudit.googleapis.com%2Factivity')}",
        'resource.type="k8s_cluster"',
        f"protoPayload.methodName=~{q(_DEPLOY_METHOD)}",
        f"timestamp>={q(utc(ctx.since(ctx.windows.deploys_before)))}",
        f"timestamp<={q(utc(ctx.now))}",
    ]
    if ctx.cluster:
        parts.append(f"resource.labels.cluster_name={q(ctx.cluster)}")
    if ctx.namespace:
        parts.append(f"protoPayload.resourceName:{q(f'namespaces/{ctx.namespace}/deployments/')}")
    else:
        parts.extend(f"-protoPayload.resourceName:{q(f'namespaces/{ns}/')}" for ns in SYSTEM_NAMESPACES)
    entries = telemetry.list_logs(ctx.project_id, "\n".join(parts), limit)
    items = []
    for entry in entries[:keep]:  # newest first
        name, text = describe_change(entry)
        key = f"deploy:{name}:{short_hash(entry.get('insertId', ''), entry.get('timestamp', ''))}"
        items.append(Item(key, "deployments", text))
    if not items:
        items.append(
            Item(
                "deploy:none",
                "deployments",
                f"No Deployment changes in namespace {ctx.namespace or '(any)'} in the "
                f"{int(ctx.windows.deploys_before.total_seconds() // 3600)} h before the alert.",
            )
        )
    return items


HOTSPOT_WINDOW = timedelta(minutes=10)


def service_of(pod_name: str) -> str:
    """``productcatalogservice-6d8f-x2k9p`` -> ``productcatalogservice`` (Deployment pod naming)."""
    parts = pod_name.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod_name


def collect_hotspots(ctx: IncidentContext, telemetry: Telemetry, top: int = 3) -> list[Item]:
    """The busiest services in the namespace by CPU and memory against their limits.

    The alerting service is often not the one in trouble (checkout is slow because
    the catalog is saturated), so this looks across every service.
    """
    if ctx.namespace is None:
        return []
    scope = ['resource.type="k8s_container"', f"resource.labels.namespace_name={q(ctx.namespace)}"]
    if ctx.cluster:
        scope.append(f"resource.labels.cluster_name={q(ctx.cluster)}")
    parts = []
    for label, metric in (
        ("CPU", "kubernetes.io/container/cpu/limit_utilization"),
        ("memory", "kubernetes.io/container/memory/limit_utilization"),
    ):
        series = telemetry.list_time_series(
            ctx.project_id,
            " AND ".join([f"metric.type={q(metric)}", *scope]),
            ctx.now - HOTSPOT_WINDOW,
            ctx.now,
            {
                "alignmentPeriod": "60s",
                "perSeriesAligner": "ALIGN_MAX",
                "crossSeriesReducer": "REDUCE_MAX",
                "groupByFields": "resource.labels.pod_name",
            },
        )
        peak: dict[str, float] = {}
        for s in series:
            pod = ((s.get("resource") or {}).get("labels") or {}).get("pod_name", "")
            values = [v for p in s.get("points", []) if (v := point_value(p)) is not None]
            if pod and values:
                svc = service_of(pod)
                peak[svc] = max(peak.get(svc, 0.0), max(values))
        if peak:
            ranked = sorted(peak.items(), key=lambda kv: -kv[1])[:top]
            parts.append(f"{label}: " + ", ".join(f"{svc} {100 * v:.0f}%" for svc, v in ranked))
    if not parts:
        return []
    minutes = int(HOTSPOT_WINDOW.total_seconds() // 60)
    return [
        Item(
            "metric:hotspots",
            "metrics",
            f"Busiest services (peak of limit, last {minutes} min). " + "; ".join(parts),
        )
    ]


SOURCES: tuple[Source, ...] = (
    Source("alert", alert_items, needs_telemetry=False),
    Source("logs", collect_logs),
    Source("events", collect_events),
    Source("metrics", collect_metrics),
    Source("deployments", collect_deployments),
    Source("hotspots", collect_hotspots),
)
