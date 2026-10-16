import pytest

from asre.core.classify import PRECEDENCE, Signals, classify
from asre.core.models import AlertType
from tests import properties as p


def _by_name(name: str) -> str:
    return classify(Signals(alert_name=name)).alert_type.value


def test_p1_5_oom_beats_crashloop():
    assert p.oom_beats_crashloop(_by_name)


@pytest.mark.parametrize(
    "signals,expected,rule",
    [
        (
            Signals(user_labels={"alert_type": "high_latency"}, alert_name="OOMKilled"),
            AlertType.HIGH_LATENCY,
            "explicit_label",
        ),
        (Signals(exit_code=137, alert_name="CrashLoopBackOff"), AlertType.OOM_KILLED, "exit_code_137"),
        (
            Signals(reason="OOMKilled", metric_type="kubernetes.io/container/restart_count"),
            AlertType.OOM_KILLED,
            "reason_oom",
        ),
        (Signals(metric_type="logging.googleapis.com/user/oom_killed"), AlertType.OOM_KILLED, "metric_oom"),
        (Signals(reason="CrashLoopBackOff"), AlertType.CONTAINER_CRASH, "reason_crashloop"),
        (
            Signals(metric_type="kubernetes.io/container/restart_count"),
            AlertType.CONTAINER_CRASH,
            "metric_restart_count",
        ),
        (
            Signals(metric_type="logging.googleapis.com/user/checkout_latency_ms"),
            AlertType.HIGH_LATENCY,
            "metric_latency",
        ),
        (
            Signals(
                metric_type="run.googleapis.com/request_count", metric_labels={"response_code_class": "5xx"}
            ),
            AlertType.ERROR_RATE,
            "metric_error_class",
        ),
        (
            Signals(metric_type="logging.googleapis.com/user/frontend_5xx"),
            AlertType.ERROR_RATE,
            "metric_error_class",
        ),
        (
            Signals(metric_type="kubernetes.io/container/memory/used_bytes"),
            AlertType.SATURATION,
            "metric_saturation",
        ),
        (Signals(alert_name="Pod CrashLoopBackOff: payment"), AlertType.CONTAINER_CRASH, "name_crash"),
        (Signals(alert_name="HighLatency p99"), AlertType.HIGH_LATENCY, "name_latency"),
        (Signals(alert_name="High5xxErrorRate"), AlertType.ERROR_RATE, "name_error"),
        (Signals(alert_name="Something odd"), AlertType.UNKNOWN, "unknown"),
        (
            Signals(user_labels={"alert_type": "not-a-type"}, alert_name="HighLatency"),
            AlertType.HIGH_LATENCY,
            "name_latency",
        ),
    ],
)
def test_precedence_table(signals, expected, rule):
    result = classify(signals)
    assert (result.alert_type, result.rule) == (expected, rule)


def test_precedence_is_published_and_complete():
    assert PRECEDENCE[0] == "explicit_label" and PRECEDENCE[-1] == "unknown"
    assert PRECEDENCE.index("exit_code_137") < PRECEDENCE.index("metric_restart_count")
    assert PRECEDENCE.index("metric_restart_count") < PRECEDENCE.index("name_oom")
