"""Predictive mode: the trend fit, the namespace scan, forecasts as Briefs, and the card."""

from datetime import timedelta

import pytest

from asre.analyse.app import AnalyseDeps, create_app
from asre.core.classify import Signals, classify
from asre.core.dedup import InMemoryRepository
from asre.core.models import AlertFamily, AlertType
from asre.core.tenants import TenantConfig, TenantRegistry
from asre.predict.forecast import CPU, ERRORS, MEMORY, Scanner, fit, forecast, forecast_alert, scan
from tests.conftest import NOW, TASKS_SA, TENANT, FakeVerifier


def climb(start, per_min, minutes=30, wobble=0.0):
    return [
        (NOW - timedelta(minutes=minutes - i), start + per_min * i + (wobble if i % 2 else -wobble))
        for i in range(minutes + 1)
    ]


# ------------------------------------------------------------------ the fit


def test_a_steady_climb_is_forecast_with_its_time_to_limit():
    f = forecast("productcatalogservice", MEMORY, climb(0.5, 0.01), NOW)
    assert f is not None and f.signal is MEMORY
    assert 19 <= f.eta_minutes <= 21  # 80% now, +1 point a minute -> 100% in 20 min
    assert f.crosses_at == NOW + timedelta(minutes=f.eta_minutes)
    assert "Forecast, not an outage" in f.describe() and "50% to 80%" in f.describe()


def test_spikes_do_not_bend_the_line():
    points = climb(0.5, 0.01)
    points[10] = (points[10][0], 0.99)  # one spike near the limit, early in the window
    f = forecast("svc", MEMORY, points, NOW)
    assert f is not None and 19 <= f.eta_minutes <= 21


@pytest.mark.parametrize(
    "points,why",
    [
        (climb(0.6, 0.0, wobble=0.02), "flat with jitter"),
        (climb(0.9, -0.01), "falling"),
        (climb(0.1, 0.01), "rising but still low (40%, below the floor)"),
        (climb(0.55, 0.002), "too slow: 8 points in 30 min, limit 3 hours away"),
        (climb(0.74, 0.009) + [(NOW, 1.0)], "already at the limit: the reactive alert's job"),
        (climb(0.5, 0.01)[:8], "too few points"),
    ],
)
def test_no_forecast(points, why):
    assert forecast("svc", MEMORY, points, NOW) is None, why


def test_a_noisy_series_without_a_real_trend_is_ignored():
    zigzag = [(NOW - timedelta(minutes=30 - i), 0.55 + (0.3 if i % 3 == 0 else 0.0)) for i in range(31)]
    assert forecast("svc", MEMORY, zigzag, NOW) is None


def test_cpu_and_errors_have_their_own_limits():
    cpu = forecast("svc", CPU, climb(0.4, 0.012), NOW)  # 76% now, limit 90%
    assert cpu is not None and 10 <= cpu.eta_minutes <= 13
    errors = forecast("svc", ERRORS, climb(0.0, 0.2), NOW)  # 6/min now, limit 10/min
    assert errors is not None and 18 <= errors.eta_minutes <= 22
    assert "6.0/min" in errors.describe()


def test_fit_reports_the_level_now():
    t = fit(climb(0.5, 0.01), NOW)
    assert abs(t.level - 0.8) < 1e-9 and abs(t.slope - 0.01) < 1e-9 and t.rank_correlation > 0.99


# ------------------------------------------------------------------ forecasts as alerts


def test_a_forecast_is_an_alert_of_its_own_family():
    f = forecast("productcatalogservice", MEMORY, climb(0.5, 0.01), NOW)
    alert = forecast_alert(f, "example-shop", "prod", "shop", "default", NOW)
    assert alert.alert_type is AlertType.FORECAST and alert.family is AlertFamily.FORECAST
    assert alert.source == "forecast" and alert.classified_by == "forecast"
    assert alert.alert_name == "Forecast: productcatalogservice memory reaches its limit in ~20 min"
    assert alert.observed_value == "80%" and alert.threshold_value == "100%"


def test_a_customer_alert_cannot_claim_to_be_a_forecast():
    c = classify(Signals(user_labels={"alert_type": "forecast"}, alert_name="Checkout 5xx errors"))
    assert c.alert_type is AlertType.ERROR_RATE


def test_scan_is_off_without_a_scanner():
    deps = AnalyseDeps(
        repo=InMemoryRepository(), verifier=FakeVerifier(), audience="asre-analyse", tasks_invoker_sa=TASKS_SA
    )
    r = create_app(deps).test_client().post("/tasks/scan", headers={"Authorization": "Bearer good-task"})
    assert r.get_json()["status"] == "disabled"


# ------------------------------------------------------------------ the card


def test_demo_tenant_is_predictive_by_default():
    assert TenantConfig.model_validate(TENANT).predictive is True


# ------------------------------------------------------------------ the scan


class ClimbingMemory:
    """Telemetry where one service's memory climbs steadily towards its limit."""

    def __init__(self, pod="api-7d9f8c6b5-x2k9p", start=0.55, per_min=0.01):
        self.pod, self.start, self.per_min, self.calls = pod, start, per_min, []

    def list_time_series(self, project, filter_, start, end, aggregation):
        self.calls.append(filter_)
        if "memory/limit_utilization" not in filter_:
            return []
        points = [
            {
                "interval": {"endTime": (end - timedelta(minutes=30 - i)).strftime("%Y-%m-%dT%H:%M:00Z")},
                "value": {"doubleValue": self.start + self.per_min * i},
            }
            for i in range(31)
        ]
        return [{"resource": {"labels": {"pod_name": self.pod}}, "points": points}]

    def list_logs(self, project, filter_, limit):
        return []


def test_the_scan_forecasts_a_service_climbing_towards_its_memory_limit():
    telemetry = ClimbingMemory()
    found = scan(telemetry, "example-project", "example-cluster", "default", NOW)
    assert [f.service for f in found] == ["api"]
    assert 0 < found[0].eta_minutes <= 60
    assert all('namespace_name="default"' in c for c in telemetry.calls)


def test_excluded_and_flat_services_are_not_forecast():
    assert scan(ClimbingMemory(), "p", None, "default", NOW, exclude={"api"}) == []
    assert scan(ClimbingMemory(per_min=0.0), "p", None, "default", NOW) == []


def test_the_scanner_turns_forecasts_into_alerts_for_predictive_tenants():
    tenant = TenantConfig.model_validate({**TENANT, "k8s_namespace": "default"})
    alerts = Scanner(TenantRegistry([tenant]), lambda reader: ClimbingMemory())(NOW)
    assert len(alerts) == 1 and alerts[0].alert_type is AlertType.FORECAST
    assert alerts[0].service_name == "api"
