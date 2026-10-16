import json
import subprocess
import sys
from pathlib import Path

import pytest

from asre.core.models import AlertStatus, AlertType
from asre.ingest.cloud_monitoring import to_alert

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "synthetic_alert.py"


@pytest.mark.parametrize("kind", ["container_crash", "error_rate", "high_latency"])
def test_synthetic_alert_is_a_valid_notification(kind):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--type", kind, "--service", "cartservice"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    alert = to_alert(json.loads(out), "example-shop", "prod")
    assert alert.service_name == "cartservice" and alert.alert_type is AlertType(kind)
    assert alert.status is AlertStatus.FIRING


def test_synthetic_alert_takes_labels_from_environment(monkeypatch):
    env = {
        "CUSTOMER_PROJECT": "r-proj",
        "CLUSTER_NAME": "c1",
        "K8S_NAMESPACE": "shop",
        "ALERT_ENVIRONMENT": "staging",
    }
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--service", "cartservice"],
        check=True,
        capture_output=True,
        text=True,
        env={**env, "PATH": "/usr/bin:/bin"},
    ).stdout
    incident = json.loads(out)["incident"]
    assert incident["scoping_project_id"] == "r-proj"
    assert incident["resource"]["labels"]["cluster_name"] == "c1"
    assert incident["resource"]["labels"]["namespace_name"] == "shop"
    assert to_alert(json.loads(out), "example-shop", "prod").environment == "staging"
