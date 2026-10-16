"""Predictive mode: find a steady climb that will cross a limit soon, before any alert fires.

Every few minutes the scan reads, for every service in the tenant's namespace,
the last 30 minutes of:

* memory used / limit   -> at 100% the container is OOM-killed;
* CPU used / limit      -> at 90% the container is throttled and slows down;
* error log lines/min   -> at 10 a minute customers see failures.

For each series it fits a robust straight line (Theil-Sen: the median of all
pairwise slopes, so a few spikes do not bend it) and checks that the climb is
real: a strong rank correlation with time, a meaningful rise over the window,
and a current level well above normal. If the line crosses the limit within the
horizon, that is a forecast. Steady services, noise and one-off spikes do not
qualify; a value already over the limit is the reactive alerts' job.

A forecast becomes an ``Alert`` of type ``forecast`` (made only here, never
from a customer's alert labels) and goes through the same ANALYSE pipeline as
a real alert: evidence, hypothesis, critic, Brief. Nothing is changed on the
cluster: the Brief proposes a fix and a person approves and runs it.

Pure functions over the ``Telemetry`` protocol, so the replay harness runs the
exact same code on recorded telemetry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from asre.core.logging import get_logger
from asre.core.models import Alert, AlertStatus, AlertType
from asre.core.tenants import TenantRegistry
from asre.evidence.context import k8s_name
from asre.evidence.sources import point_value, q, service_of
from asre.evidence.telemetry import Telemetry, utc

log = get_logger(__name__)

LOOKBACK = timedelta(minutes=30)
HORIZON_MINUTES = 60.0
MIN_POINTS = 12
MIN_RANK_CORRELATION = 0.8
MAX_FORECASTS = 3  # per scan, most urgent first: each one becomes a Brief


@dataclass(frozen=True)
class Signal:
    name: str
    label: str  # "memory", for "productcatalogservice memory"
    limit_label: str  # what crossing the limit means
    threshold: float
    floor: float  # the current level must be at least this to be worth a forecast
    min_rise: float  # over the lookback, by the fitted line
    unit: str  # "%" for a 0..1 ratio, "/min" for a rate
    metric: str | None = None  # Monitoring metric; None = counted from error logs

    def fmt(self, value: float) -> str:
        return f"{value * 100:.0f}%" if self.unit == "%" else f"{value:.1f}/min"

    def fmt_slope(self, slope: float) -> str:
        return f"+{slope * 100:.1f} points/min" if self.unit == "%" else f"+{slope:.2f}/min each minute"


MEMORY = Signal(
    "memory",
    "memory",
    "its memory limit (the container is OOM-killed)",
    1.0,
    0.5,
    0.10,
    "%",
    "kubernetes.io/container/memory/limit_utilization",
)
CPU = Signal(
    "cpu",
    "CPU",
    "90% of its CPU limit (requests are throttled and slow down)",
    0.9,
    0.5,
    0.15,
    "%",
    "kubernetes.io/container/cpu/limit_utilization",
)
ERRORS = Signal("errors", "errors", "10 errors a minute (customers see failures)", 10.0, 1.5, 3.0, "/min")
SIGNALS = (MEMORY, CPU, ERRORS)


@dataclass(frozen=True)
class Trend:
    slope: float  # per minute
    level: float  # fitted value now
    rise: float  # fitted value now minus fitted value at the start of the window
    rank_correlation: float
    points: int


@dataclass(frozen=True)
class Forecast:
    service: str
    signal: Signal
    trend: Trend
    first: float  # observed value at the start of the window
    latest: float  # observed value now
    eta_minutes: float
    crosses_at: datetime

    def describe(self) -> str:
        s, t = self.signal, self.trend
        return (
            f"Forecast, not an outage: {self.service} {s.label} has risen steadily from {s.fmt(self.first)} "
            f"to {s.fmt(self.latest)} over the last {int(LOOKBACK.total_seconds() // 60)} min "
            f"({s.fmt_slope(t.slope)}, trend fit {t.rank_correlation:.2f}). At this rate it reaches "
            f"{s.limit_label} around {self.crosses_at:%H:%M} UTC, in about {round(self.eta_minutes)} min."
        )

    def title(self) -> str:
        what = {"memory": "memory reaches its limit", "cpu": "CPU reaches 90% of its limit"}.get(
            self.signal.name, "errors reach 10 a minute"
        )
        return f"Forecast: {self.service} {what} in ~{round(self.eta_minutes)} min"


# ------------------------------------------------------------------ the fit


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2  # ties share their average rank
        i = j + 1
    return ranks


def rank_correlation(xs: list[float], ys: list[float]) -> float:
    """Spearman's rho: 1.0 when y only ever goes up with x."""
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / (vx * vy) ** 0.5 if vx and vy else 0.0


def fit(points: list[tuple[datetime, float]], now: datetime) -> Trend | None:
    """Theil-Sen line through (minutes before ``now``, value)."""
    if len(points) < MIN_POINTS:
        return None
    xs = [(t - now).total_seconds() / 60 for t, _ in points]
    ys = [v for _, v in points]
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    if not slopes:
        return None
    slope = median(slopes)
    intercept = median(y - slope * x for x, y in zip(xs, ys, strict=True))  # the fitted value at now (x = 0)
    return Trend(
        slope=slope,
        level=intercept,
        rise=slope * (0 - min(xs)),
        rank_correlation=rank_correlation(xs, ys),
        points=len(points),
    )


def forecast(
    service: str, signal: Signal, points: list[tuple[datetime, float]], now: datetime
) -> Forecast | None:
    points = sorted(p for p in points if now - LOOKBACK <= p[0] <= now)
    trend = fit(points, now)
    if trend is None or trend.slope <= 0:
        return None
    recent = [v for t, v in points if t >= now - timedelta(minutes=5)]
    if any(v >= signal.threshold for v in recent):
        return None  # already over the limit: the reactive alert's job
    if trend.rank_correlation < MIN_RANK_CORRELATION or trend.rise < signal.min_rise:
        return None
    if trend.level < signal.floor or trend.level >= signal.threshold:
        return None
    eta = (signal.threshold - trend.level) / trend.slope
    if eta > HORIZON_MINUTES:
        return None
    return Forecast(service, signal, trend, points[0][1], points[-1][1], eta, now + timedelta(minutes=eta))


# ------------------------------------------------------------------ reading the namespace


def _scope(cluster: str | None, namespace: str) -> list[str]:
    parts = ['resource.type="k8s_container"', f"resource.labels.namespace_name={q(namespace)}"]
    if cluster:
        parts.append(f"resource.labels.cluster_name={q(cluster)}")
    return parts


def metric_points(
    telemetry: Telemetry, project: str, cluster: str | None, namespace: str, metric: str, now: datetime
) -> dict[str, list[tuple[datetime, float]]]:
    """Per service, the per-minute maximum across its pods."""
    series = telemetry.list_time_series(
        project,
        " AND ".join([f"metric.type={q(metric)}", *_scope(cluster, namespace)]),
        now - LOOKBACK,
        now,
        {
            "alignmentPeriod": "60s",
            "perSeriesAligner": "ALIGN_MAX",
            "crossSeriesReducer": "REDUCE_MAX",
            "groupByFields": "resource.labels.pod_name",
        },
    )
    per_service: dict[str, dict[datetime, float]] = {}
    for s in series:
        pod = ((s.get("resource") or {}).get("labels") or {}).get("pod_name", "")
        if not pod:
            continue
        minutes = per_service.setdefault(service_of(pod), {})
        for p in s.get("points", []):
            value = point_value(p)
            end = (p.get("interval") or {}).get("endTime")
            if value is None or not end:
                continue
            t = datetime.fromisoformat(end.replace("Z", "+00:00")).replace(second=0, microsecond=0)
            minutes[t] = max(minutes.get(t, 0.0), value)
    return {svc: sorted(m.items()) for svc, m in per_service.items()}


def error_points(
    telemetry: Telemetry, project: str, cluster: str | None, namespace: str, now: datetime, limit: int = 3000
) -> dict[str, list[tuple[datetime, float]]]:
    """Per service (the pod's ``app`` label), error log lines in each minute, zeros included."""
    filter_ = "\n".join(
        [
            *_scope(cluster, namespace),
            f"timestamp>={q(utc(now - LOOKBACK))}",
            f"timestamp<={q(utc(now))}",
            "severity>=ERROR",
        ]
    )
    counts: dict[str, dict[datetime, int]] = {}
    for entry in telemetry.list_logs(project, filter_, limit):
        app = (entry.get("labels") or {}).get("k8s-pod/app")
        ts = entry.get("timestamp")
        if not app or not ts:
            continue
        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(second=0, microsecond=0)
        counts.setdefault(app, {})
        counts[app][t] = counts[app].get(t, 0) + 1
    start = (now - LOOKBACK).replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes = [start + timedelta(minutes=i) for i in range(int(LOOKBACK.total_seconds() // 60))]
    # The last minute is still filling up; leave it out rather than read it as a drop.
    return {
        app: [(m, float(c.get(m, 0))) for m in minutes if m < now - timedelta(minutes=1)]
        for app, c in counts.items()
    }


def scan(
    telemetry: Telemetry,
    project: str,
    cluster: str | None,
    namespace: str | None,
    now: datetime,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> list[Forecast]:
    """Every qualifying forecast in the namespace, most urgent first (at most ``MAX_FORECASTS``)."""
    namespace = k8s_name(namespace)
    if namespace is None:
        return []
    cluster = k8s_name(cluster)
    found: list[Forecast] = []
    for signal in SIGNALS:
        if signal.metric:
            series = metric_points(telemetry, project, cluster, namespace, signal.metric, now)
        else:
            series = error_points(telemetry, project, cluster, namespace, now)
        for service, points in sorted(series.items()):
            if k8s_name(service) is None or service in exclude:
                continue
            f = forecast(service, signal, points, now)
            if f is not None:
                found.append(f)
    best: dict[str, Forecast] = {}
    for f in sorted(found, key=lambda f: f.eta_minutes):
        best.setdefault(f.service, f)  # one forecast per service: the most urgent signal
    return list(best.values())[:MAX_FORECASTS]


def forecast_alert(
    f: Forecast,
    customer_id: str,
    environment: str,
    cluster: str | None,
    namespace: str,
    now: datetime,
    console_url: str = "",
) -> Alert:
    """The forecast as an alert, so it takes the same path as a real one (dedup, ANALYSE, Brief)."""
    labels = {"namespace_name": namespace, "container_name": "server"}
    if cluster:
        labels["cluster_name"] = cluster
    return Alert(
        customer_id=customer_id,
        source="forecast",
        source_incident_id=f"forecast:{f.service}:{f.signal.name}",
        status=AlertStatus.FIRING,
        alert_name=f.title(),
        service_name=f.service,
        environment=environment,
        alert_type=AlertType.FORECAST,
        classified_by="forecast",
        severity="warning",
        started_at=now,
        resource_type="k8s_container",
        resource_labels=labels,
        metric_type=f.signal.metric or "logging: severity>=ERROR lines per minute",
        summary=f.describe(),
        console_url=console_url,
        observed_value=f.signal.fmt(f.latest),
        threshold_value=f.signal.fmt(f.signal.threshold),
    )


def as_record(f: Forecast) -> dict[str, Any]:
    return {
        "service": f.service,
        "signal": f.signal.name,
        "eta_minutes": round(f.eta_minutes, 1),
        "crosses_at": f.crosses_at,
        "level": round(f.trend.level, 4),
        "slope_per_min": round(f.trend.slope, 5),
        "rank_correlation": round(f.trend.rank_correlation, 3),
    }


@dataclass
class Scanner:
    """The scan behind ANALYSE's ``/tasks/scan``: every predictive tenant, read as its reader identity."""

    tenants: TenantRegistry
    telemetry: Callable[[str | None], Telemetry]

    def __call__(self, now: datetime) -> list[Alert]:
        alerts: list[Alert] = []
        for tenant in self.tenants.all():
            namespace = k8s_name(tenant.k8s_namespace)
            if not tenant.predictive or namespace is None:
                continue
            try:
                found = scan(
                    self.telemetry(tenant.reader_service_account),
                    tenant.gcp_project_id,
                    tenant.k8s_cluster,
                    namespace,
                    now,
                    exclude=set(tenant.predictive_exclude),
                )
            except Exception as exc:  # noqa: BLE001 — one tenant's failure must not stop the others
                log.warning(
                    "predictive scan failed",
                    event="scan_failed",
                    customer_id=tenant.customer_id,
                    error=str(exc)[:300],
                )
                continue
            for f in found:
                log.info(
                    "trend heading past a limit",
                    event="forecast_found",
                    customer_id=tenant.customer_id,
                    **as_record(f),
                )
                alerts.append(
                    forecast_alert(
                        f,
                        tenant.customer_id,
                        tenant.environment,
                        k8s_name(tenant.k8s_cluster),
                        namespace,
                        now,
                    )
                )
        return alerts
