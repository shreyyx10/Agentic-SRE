"""Evidence gathering: queries, condensing, sanitising, budget and degraded mode (T4.1–T4.3)."""

import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from asre.core.models import AlertType
from asre.core.tenants import TenantConfig
from asre.evidence import gather as gather_mod
from asre.evidence.context import context_for, k8s_name
from asre.evidence.gather import EvidenceConfig, gather
from asre.evidence.sources import (
    SOURCES,
    Item,
    Source,
    collect_deployments,
    collect_events,
    collect_logs,
    collect_metrics,
    describe_change,
    flatten,
    message_shape,
)
from asre.evidence.telemetry import RestTelemetry, TelemetryError, TelemetryPool, utc
from tests.conftest import NOW, TENANT, make_alert

LABELS = {"cluster_name": "shop", "namespace_name": "default", "pod_name": "cartservice-7d9c-abcde"}


def ctx_for(**alert_overrides):
    fields = {
        "service_name": "cartservice",
        "alert_type": AlertType.CONTAINER_CRASH,
        "resource_labels": LABELS,
        "started_at": NOW,
        **alert_overrides,
    }
    alert = make_alert(**fields)
    tenant = TenantConfig.model_validate(
        {**TENANT, "reader_service_account": "cust-example-shop@asre-platform.iam.gserviceaccount.com"}
    )
    return context_for(alert, tenant, NOW + timedelta(minutes=2))


def entry(ts, msg, severity="ERROR", **extra):
    return {"timestamp": ts, "severity": severity, "jsonPayload": {"message": msg, **extra}, "insertId": ts}


class FakeTelemetry:
    def __init__(self, logs=None, series=None, fail=None):
        self.logs = logs or {}  # marker substring in filter -> entries
        self.series = series or {}  # metric type -> timeSeries list
        self.fail = fail or set()
        self.log_filters, self.ts_calls = [], []

    def list_logs(self, project, filter_, limit):
        self.log_filters.append((project, filter_, limit))
        for marker, entries in self.logs.items():
            if marker in filter_:
                if marker in self.fail:
                    raise TelemetryError("403: PERMISSION_DENIED")
                return entries[:limit]
        return []

    def list_time_series(self, project, filter_, start, end, aggregation):
        self.ts_calls.append((project, filter_, start, end, aggregation))
        for metric, series in self.series.items():
            if f'"{metric}"' in filter_:
                return series
        return []


# ------------------------------------------------------------------ context


def test_project_comes_from_the_tenant_never_the_alert():
    ctx = ctx_for(resource_labels={**LABELS, "project_id": "someone-elses-project"})
    assert ctx.project_id == "example-customer-project"
    assert ctx.reader.startswith("cust-example-shop@")
    assert (ctx.service, ctx.cluster, ctx.namespace) == ("cartservice", "shop", "default")


@pytest.mark.parametrize("bad", ['default" OR true OR "', "Default", "a" * 64, "", "ns_1", "-x"])
def test_unsafe_names_are_not_used_in_queries(bad):
    assert k8s_name(bad) is None
    ctx = ctx_for(resource_labels={**LABELS, "namespace_name": bad})
    assert ctx.namespace is None


def test_service_with_dots_or_colons_is_not_queried():
    ctx = ctx_for(service_name="svc:cart.v2")
    assert ctx.service is None
    t = FakeTelemetry()
    assert collect_logs(ctx, t) == [] and collect_metrics(ctx, t) == []
    assert t.log_filters == [] and t.ts_calls == []


def test_anchor_never_moves_into_the_future():
    ctx = ctx_for(started_at=NOW + timedelta(hours=1))
    assert ctx.anchor == ctx.now


def test_invalid_tenant_project_is_refused():
    tenant = TenantConfig.model_validate({**TENANT, "gcp_project_id": "Bad_Project"})
    with pytest.raises(ValueError):
        context_for(make_alert(), tenant, NOW)


# ------------------------------------------------------------------ logs


def test_logs_query_is_scoped_and_repeats_are_grouped():
    ctx = ctx_for()
    t = FakeTelemetry(
        logs={
            'k8s-pod/app"="cartservice"': [
                entry(
                    "2026-10-01T09:01:00Z",
                    "Redis timeout after 5000ms id=3fa85f64-5717-4562-b3fc-2c963f66afa6",
                ),
                entry(
                    "2026-10-01T08:59:00Z",
                    "Redis timeout after 4800ms id=9fa85f64-5717-4562-b3fc-2c963f66afa6",
                ),
                entry("2026-10-01T08:58:00Z", "health check slow", severity="WARNING"),
            ]
        }
    )
    items = collect_logs(ctx, t)
    project, filter_, _ = t.log_filters[0]
    assert project == "example-customer-project"
    for part in (
        'labels."k8s-pod/app"="cartservice"',
        'namespace_name="default"',
        'cluster_name="shop"',
        'timestamp>="2026-10-01T08:45:00Z"',
        'timestamp<="2026-10-01T09:02:00Z"',
        "severity>=WARNING",
    ):
        assert part in filter_
    assert len(items) == 2
    assert items[0].text.startswith("cartservice ERROR ×2 (2026-10-01 08:59:00Z to 2026-10-01 09:01:00Z)")
    assert items[0].key.startswith("log:cartservice:") and items[0].source == "logs"


def test_query_cap_is_reported():
    ctx = ctx_for()
    many = [entry(f"2026-10-01T09:00:{i:02d}Z", "boom") for i in range(5)]
    t = FakeTelemetry(logs={"k8s-pod/app": many})
    items = collect_logs(ctx, t, limit=5)
    assert items[-1].key == "log:cartservice:volume"


def test_message_shape_ignores_ids_numbers_and_times():
    a = message_shape("rpc error after 512ms at 2026-10-01T09:00:01Z trace=4bf92f3577b34da6a3ce929d0e0e4736")
    b = message_shape("rpc error after 77ms at 2026-10-02T10:11:12Z trace=00f067aa0ba902b7aaaaaaaaaaaaaaaa")
    assert a == b


def test_text_payload_and_frontend_status_fields_are_kept():
    from asre.evidence.sources import log_message

    assert log_message({"textPayload": " plain line "}) == "plain line"
    msg = log_message(
        {
            "jsonPayload": {
                "message": "request error",
                "error": "paymentservice unavailable",
                "http.resp.status": 500,
            }
        }
    )
    assert "paymentservice unavailable" in msg and '"http.resp.status": 500' in msg
    assert log_message({"protoPayload": {"status": {"message": "denied"}}}) == "denied"
    assert log_message({}) == ""


# ------------------------------------------------------------------ events


def test_events_include_node_oom_kills_and_namespace_warnings():
    ctx = ctx_for()
    oom = {
        "timestamp": "2026-10-01T08:59:30Z",
        "jsonPayload": {
            "type": "Warning",
            "reason": "OOMKilling",
            "message": "Memory cgroup out of memory: Killed process 1234 (cartservice)",
            "involvedObject": {"kind": "Node", "name": "gk3-node-1"},
        },
    }
    backoff = {
        "timestamp": "2026-10-01T09:00:30Z",
        "jsonPayload": {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "involvedObject": {"kind": "Pod", "name": "cartservice-7d9c-abcde"},
        },
    }
    t = FakeTelemetry(logs={"/logs/events": [backoff, oom]})
    items = collect_events(ctx, t)
    filter_ = t.log_filters[0][1]
    assert 'logName="projects/example-customer-project/logs/events"' in filter_
    assert '(resource.labels.namespace_name="default" OR jsonPayload.reason="OOMKilling")' in filter_
    texts = " | ".join(i.text for i in items)
    assert "OOMKilling Node/gk3-node-1" in texts and "BackOff Pod/cartservice-7d9c-abcde" in texts


def test_events_without_namespace_have_a_valid_filter():
    import re

    ctx = ctx_for(resource_labels={"cluster_name": "shop"})
    t = FakeTelemetry()
    collect_events(ctx, t)
    filter_ = t.log_filters[0][1]
    assert "true" not in filter_
    assert not re.search(r"(?<!-)resource[.]labels[.]namespace_name=", filter_)  # only exclusions


# ------------------------------------------------------------------ metrics


def series(points, kind="doubleValue"):
    return [{"points": [{"interval": {"endTime": t}, "value": {kind: v}} for t, v in reversed(points)]}]


def test_metrics_report_memory_against_limit_and_restarts():
    ctx = ctx_for()
    mem = [(f"2026-10-01T08:{m:02d}:00Z", 0.4 + m / 100) for m in range(30, 60)] + [
        ("2026-10-01T09:00:00Z", 1.0),
        ("2026-10-01T09:01:00Z", 0.35),
    ]
    restarts = [(f"2026-10-01T08:{m:02d}:00Z", 1 if m in (55, 57, 59) else 0) for m in range(30, 60)]
    t = FakeTelemetry(
        series={
            "kubernetes.io/container/memory/limit_utilization": series(mem),
            "kubernetes.io/container/restart_count": series(restarts, "int64Value"),
        }
    )
    items = {i.key: i for i in collect_metrics(ctx, t)}
    assert set(items) == {"metric:memory_limit_utilization", "metric:restart_count"}
    mem_text = items["metric:memory_limit_utilization"].text
    assert "cartservice memory used / limit: max 100%, latest 35%, around the alert 100%" in mem_text
    assert "total 3 in window" in items["metric:restart_count"].text
    _, filter_, start, end, agg = t.ts_calls[0]
    assert 'resource.labels.pod_name=starts_with("cartservice-")' in filter_
    assert start == NOW - timedelta(minutes=30) and end == NOW + timedelta(minutes=2)
    assert agg == {
        "alignmentPeriod": "60s",
        "perSeriesAligner": "ALIGN_MAX",
        "crossSeriesReducer": "REDUCE_MAX",
    }


def test_metric_with_no_points_is_left_out():
    t = FakeTelemetry(
        series={"kubernetes.io/container/memory/limit_utilization": [{"points": [{"value": {}}]}]}
    )
    assert collect_metrics(ctx_for(), t) == []


# ------------------------------------------------------------------ deployments


def audit(ts, resource, method, request=None, status=None, who="dev@shop.example"):
    return {
        "timestamp": ts,
        "insertId": f"id-{ts}",
        "protoPayload": {
            "methodName": f"io.k8s.apps.v1.{method}",
            "resourceName": resource,
            "authenticationInfo": {"principalEmail": who},
            "request": request or {},
            **({"status": status} if status else {}),
        },
    }


def test_deployment_changes_show_cause_and_what_changed():
    ctx = ctx_for()
    oom = audit(
        "2026-10-01T08:55:00Z",
        "apps/v1/namespaces/default/deployments/cartservice",
        "deployments.patch",
        {
            "metadata": {
                "annotations": {
                    "kubernetes.io/change-cause": "release 2026.10-demo: tune cartservice memory to 52Mi"
                }
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "server", "resources": {"limits": {"memory": "52Mi"}}}],
                        "$setElementOrder/containers": [{"name": "server"}],
                    }
                }
            },
        },
    )
    scale = audit(
        "2026-10-01T08:50:00Z",
        "apps/v1/namespaces/default/deployments/paymentservice/scale",
        "deployments.scale.patch",
        {"spec": {"replicas": 0}},
    )
    t = FakeTelemetry(logs={"cloudaudit.googleapis.com%2Factivity": [oom, scale]})
    items = collect_deployments(ctx, t)
    filter_ = t.log_filters[0][1]
    assert 'protoPayload.resourceName:"namespaces/default/deployments/"' in filter_
    assert 'timestamp>="2026-10-01T03:00:00Z"' in filter_  # six hours before the alert
    assert items[0].key.startswith("deploy:cartservice:")
    assert 'change-cause: "release 2026.10-demo: tune cartservice memory to 52Mi"' in items[0].text
    assert "changed: containers[server].resources.limits.memory=52Mi" in items[0].text
    assert "$setElementOrder" not in items[0].text
    assert items[1].text.startswith(
        "2026-10-01 08:50:00Z deployment/paymentservice scale.patch by dev@shop.example"
    )
    assert "spec.replicas=0" in items[1].text


def test_failed_change_is_marked_and_no_changes_is_itself_evidence():
    _, text = describe_change(
        audit(
            "2026-10-01T08:55:00Z",
            "apps/v1/namespaces/default/deployments/cartservice",
            "deployments.update",
            status={"code": 7},
        )
    )
    assert "(FAILED)" in text
    items = collect_deployments(ctx_for(), FakeTelemetry())
    assert [i.key for i in items] == ["deploy:none"] and "6 h" in items[0].text


def test_flatten_stops_at_its_limit():
    assert len(flatten({f"k{i}": i for i in range(50)}, limit=5)) == 5


# ------------------------------------------------------------------ gather


def full_telemetry():
    return FakeTelemetry(
        logs={
            'k8s-pod/app"="cartservice"': [
                entry("2026-10-01T09:00:00Z", "redis error password=hunter2 card 4111 1111 1111 1111")
            ],
            "/logs/events": [],
            "cloudaudit": [
                audit(
                    "2026-10-01T08:55:00Z",
                    "apps/v1/namespaces/default/deployments/cartservice",
                    "deployments.patch",
                    {"spec": {"replicas": 1}},
                )
            ],
        },
        series={"kubernetes.io/container/restart_count": series([("2026-10-01T09:00:00Z", 2)], "int64Value")},
    )


def test_gather_builds_a_sanitised_prioritised_pack():
    result = gather(ctx_for(), full_telemetry())
    keys = [i.key for i in result.pack.items]
    assert keys[:2] == ["alert:summary", "alert:service_profile"] or keys[0] == "alert:summary"
    sources = [i.source for i in result.pack.items]
    assert sources == sorted(sources, key=lambda s: gather_mod.PRIORITY[s])
    log_text = next(i.text for i in result.pack.items if i.source == "logs")
    assert "hunter2" not in log_text and "4111" not in log_text
    assert "[REDACTED:secret]" in log_text and "[REDACTED:pan]" in log_text
    assert "dev@shop.example" not in " ".join(i.text for i in result.pack.items)  # emails are masked too
    assert result.redactions >= 3
    assert result.pack.context_quality == "complete"
    assert result.report["events"].status == "empty" and result.report["logs"].status == "ok"
    rec = result.as_record()
    assert rec["evidence_hash"].startswith("sha256:") and rec["evidence_items"] == len(keys)
    assert gather(ctx_for(), full_telemetry()).pack_hash == result.pack_hash  # deterministic


def test_a_failing_source_degrades_the_pack_but_not_the_brief():
    t = full_telemetry()
    t.fail = {"cloudaudit"}
    result = gather(ctx_for(), t)
    assert result.report["deployments"].status == "failed"
    assert "PERMISSION_DENIED" in result.report["deployments"].error
    assert result.pack.context_quality == "degraded"
    assert any(i.source == "logs" for i in result.pack.items)
    assert result.as_record()["evidence_sources"]["deployments"]["error"].startswith("TelemetryError")


def test_a_slow_source_times_out():
    release = threading.Event()

    def slow(ctx, t):
        release.wait(5)
        return [Item("log:slow", "logs", "late")]

    sources = (SOURCES[0], Source("slow", slow))
    try:
        result = gather(ctx_for(), FakeTelemetry(), sources, EvidenceConfig(deadline_s=0.1))
    finally:
        release.set()
    assert result.report["slow"].status == "timeout"
    assert result.pack.context_quality == "degraded"
    assert [i.key for i in result.pack.items if i.key == "log:slow"] == []


def test_budget_keeps_high_priority_items_and_marks_truncation():
    big = [Item(f"log:x:{n}", "logs", "x" * 500) for n in range(5)]
    sources = (
        SOURCES[0],
        Source("big", lambda c, t: big),
        Source("deploys", lambda c, t: [Item("deploy:a", "deployments", "d" * 300)]),
    )
    result = gather(ctx_for(), FakeTelemetry(), sources, EvidenceConfig(budget_chars=1800))
    keys = [i.key for i in result.pack.items]
    assert keys[0] == "alert:summary" and "deploy:a" in keys
    assert result.dropped_budget >= 2 and result.pack.context_quality == "truncated"


def test_item_that_survives_sanitising_is_dropped(monkeypatch):
    monkeypatch.setattr(gather_mod, "verify", lambda text: ["jwt"] if "leak" in text else [])
    sources = (SOURCES[0], Source("leaky", lambda c, t: [Item("log:leak", "logs", "leak")]))
    result = gather(ctx_for(), FakeTelemetry(), sources)
    assert result.dropped_unsafe == 1 and "log:leak" not in result.pack.keys


def test_duplicate_keys_are_kept_once():
    sources = (Source("a", lambda c, t: [Item("log:x", "logs", "one"), Item("log:x", "logs", "two")]),)
    result = gather(ctx_for(), FakeTelemetry(), sources)
    assert [i.text for i in result.pack.items] == ["one"]


# ------------------------------------------------------------------ REST client


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, json, timeout))
        return self.resp

    def get(self, url, params, timeout):
        self.calls.append(("GET", url, params, timeout))
        return self.resp


def test_rest_client_builds_logging_and_monitoring_requests():
    s = Session(Resp(200, {"entries": [{"a": 1}, {"a": 2}], "timeSeries": [{"points": []}]}))
    rt = RestTelemetry("cust@p", timeout_s=7, session=s)
    assert rt.list_logs("proj", "f", 1) == [{"a": 1}]
    method, url, body, timeout = s.calls[0]
    assert (method, url, timeout) == ("POST", "https://logging.googleapis.com/v2/entries:list", 7)
    assert body == {
        "resourceNames": ["projects/proj"],
        "filter": "f",
        "orderBy": "timestamp desc",
        "pageSize": 1,
    }

    assert rt.list_time_series("proj", "m", NOW, NOW + timedelta(minutes=1), {"alignmentPeriod": "60s"}) == [
        {"points": []}
    ]
    method, url, params, _ = s.calls[1]
    assert url == "https://monitoring.googleapis.com/v3/projects/proj/timeSeries"
    assert (
        params["interval.startTime"] == "2026-10-01T09:00:00Z"
        and params["aggregation.alignmentPeriod"] == "60s"
    )


def test_rest_client_raises_on_api_errors():
    rt = RestTelemetry(None, session=Session(Resp(403, {"error": "denied"})))
    with pytest.raises(TelemetryError, match="403"):
        rt.list_logs("p", "f", 10)


def test_pool_reuses_one_client_per_reader():
    pool = TelemetryPool()
    assert pool.for_reader("a") is pool.for_reader("a") and pool.for_reader("a") is not pool.for_reader("b")


def test_utc_formats_aware_datetimes():
    assert utc(NOW) == "2026-10-01T09:00:00Z"


def test_alert_item_carries_observed_and_threshold():
    from asre.evidence.sources import alert_items

    ctx = ctx_for(
        observed_value="7",
        threshold_value="0",
        summary="Restart count above 0",
        metric_type="kubernetes.io/container/restart_count",
    )
    text = alert_items(ctx)[0].text
    assert "observed: 7  threshold: 0" in text and "summary: Restart count above 0" in text
    assert "resource: cluster_name=shop, namespace_name=default" in text
    _ = SimpleNamespace  # keep import used


def test_deployment_method_filter_has_no_escapes_and_matches_real_method_names():
    import re

    from asre.evidence.sources import _DEPLOY_METHOD

    assert "\\" not in _DEPLOY_METHOD
    t = FakeTelemetry()
    collect_deployments(ctx_for(), t)
    assert "\\" not in t.log_filters[0][1]
    for name in ("io.k8s.apps.v1.deployments.patch", "io.k8s.apps.v1.deployments.scale.patch"):
        assert re.search(_DEPLOY_METHOD, name)
    for name in ("io.k8s.apps.v1.deployments.status.update", "io.k8s.apps.v1.deployments.get"):
        assert not re.search(_DEPLOY_METHOD, name)


def test_patch_type_marker_is_not_reported_as_a_change():
    _, text = describe_change(
        audit(
            "2026-10-01T08:55:00Z",
            "apps/v1/namespaces/default/deployments/paymentservice",
            "deployments.patch",
            {"@type": "k8s.io/Patch"},
        )
    )
    assert "@type" not in text and "changed:" not in text


# ------------------------------------------------------------------ scope when the alert does not say


def tenant_ctx(alert_labels, **tenant_fields):
    alert = make_alert(service_name="frontend", alert_type=AlertType.ERROR_RATE, resource_labels=alert_labels)
    tenant = TenantConfig.model_validate({**TENANT, **tenant_fields})
    return context_for(alert, tenant, NOW)


def test_log_metric_alert_uses_the_tenant_namespace_and_cluster():
    # Live finding (12 Oct): "Storefront 5xx" carries only project_id.
    ctx = tenant_ctx({"project_id": "example-customer-project"}, k8s_cluster="shop", k8s_namespace="default")
    assert (ctx.cluster, ctx.namespace) == ("shop", "default")
    t = FakeTelemetry()
    collect_events(ctx, t)
    collect_deployments(ctx, t)
    events, deploys = t.log_filters[0][1], t.log_filters[1][1]
    assert 'resource.labels.namespace_name="default"' in events
    assert 'protoPayload.resourceName:"namespaces/default/deployments/"' in deploys


def test_alert_labels_win_over_tenant_defaults():
    ctx = tenant_ctx(
        {"namespace_name": "shop", "cluster_name": "c2"}, k8s_cluster="shop", k8s_namespace="default"
    )
    assert (ctx.cluster, ctx.namespace) == ("c2", "shop")


def test_unsafe_tenant_names_are_ignored():
    ctx = tenant_ctx({}, k8s_namespace='default" OR "x')
    assert ctx.namespace is None


def test_without_any_namespace_gke_system_namespaces_are_excluded():
    from asre.evidence.sources import SYSTEM_NAMESPACES

    ctx = tenant_ctx({"project_id": "example-customer-project"})
    assert ctx.namespace is None
    t = FakeTelemetry()
    collect_events(ctx, t)
    collect_deployments(ctx, t)
    events, deploys = t.log_filters[0][1], t.log_filters[1][1]
    for ns in SYSTEM_NAMESPACES:
        assert f'-resource.labels.namespace_name="{ns}"' in events
        assert f'-protoPayload.resourceName:"namespaces/{ns}/"' in deploys
    assert '(jsonPayload.involvedObject.kind!="Node" OR jsonPayload.reason="OOMKilling")' in events


def test_env_changes_read_as_variables_and_init_container_echoes_are_dropped():
    from asre.evidence.sources import readable_changes

    env = [{"name": "USERS", "value": "400"}, {"name": "RATE", "value": "0.25"}]
    request = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "main", "env": env}],
                    "initContainers": [{"name": "frontend-check", "env": env}],
                }
            }
        }
    }
    assert readable_changes(request) == ["containers[main].env[USERS]=400", "containers[main].env[RATE]=0.25"]
    only_init = {
        "spec": {"template": {"spec": {"initContainers": [{"name": "setup", "image": "busybox:1"}]}}}
    }
    assert readable_changes(only_init) == ["initContainers[setup].image=busybox:1"]
