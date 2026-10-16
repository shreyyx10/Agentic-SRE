from types import SimpleNamespace

import pytest

from asre.core import config
from asre.core.auth import AuthError, bearer_token
from asre.core.queue import CloudTasksQueue, InMemoryQueue
from tests.conftest import TENANT


def test_bearer_token_parsing():
    assert bearer_token({"authorization": "Bearer abc "}) == "abc"
    with pytest.raises(AuthError):
        bearer_token({"Authorization": "Bearer"})


INGEST_ENV = {
    "GOOGLE_CLOUD_PROJECT": "asre-platform",
    "ASRE_INGEST_AUDIENCE": "asre-ingest",
    "ASRE_TASKS_QUEUE": "projects/p/locations/l/queues/analyse",
    "ASRE_ANALYSE_URL": "https://analyse.example/tasks/analyse",
    "ASRE_ANALYSE_AUDIENCE": "asre-analyse",
    "ASRE_TASKS_INVOKER_SA": "tasks@p.iam.gserviceaccount.com",
}


def test_ingest_settings_from_env(monkeypatch):
    for k, v in INGEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ASRE_DEDUP_WINDOW_MINUTES", "30")
    s = config.IngestSettings.from_env()
    assert s.dedup.window.total_seconds() == 1800 and s.dedup_timeout_s == 0.5
    assert s.firestore_database == "(default)"
    a = config.AnalyseSettings.from_env()
    assert a.audience == "asre-analyse"


def test_missing_setting_fails_fast(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError):
        config.IngestSettings.from_env()


def test_load_tenants_inline_and_file(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("ASRE_TENANTS_JSON", json.dumps([TENANT]))
    assert len(config.load_tenants()) == 1
    monkeypatch.delenv("ASRE_TENANTS_JSON")
    f = tmp_path / "tenants.json"
    f.write_text(json.dumps([TENANT]))
    monkeypatch.setenv("ASRE_TENANTS_FILE", str(f))
    assert config.load_tenants().get("example-shop") is not None


def test_cloud_tasks_queue_builds_oidc_task(monkeypatch):
    from google.cloud import tasks_v2

    captured = {}

    class FakeClient:
        def create_task(self, parent, task, timeout):
            captured.update(parent=parent, task=task, timeout=timeout)
            return SimpleNamespace(name=f"{parent}/tasks/1")

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", FakeClient)
    q = CloudTasksQueue(
        "projects/p/locations/l/queues/q", "https://a/tasks/analyse", "sa@p", "asre-analyse", 0.5, 0.5
    )
    name = q.enqueue({"kind": "analyse"}, trace_header="t/1")
    req = captured["task"].http_request
    assert name.endswith("/tasks/1") and captured["timeout"] == 0.5
    assert req.oidc_token.service_account_email == "sa@p" and req.oidc_token.audience == "asre-analyse"
    assert req.headers["X-Cloud-Trace-Context"] == "t/1" and b'"analyse"' in req.body


def test_in_memory_queue_failure():
    with pytest.raises(RuntimeError):
        InMemoryQueue(fail=True).enqueue({})


def test_every_dedup_setting_comes_from_environment(monkeypatch):
    for k, v in INGEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ASRE_DEDUP_WINDOW_MINUTES", "45")
    monkeypatch.setenv("ASRE_RETENTION_DAYS", "7")
    monkeypatch.setenv("ASRE_DEDUP_UPDATE_INTERVAL_MIN", "5")
    monkeypatch.setenv("ASRE_ENQUEUE_LEASE_S", "12")
    monkeypatch.setenv("ASRE_DEDUP_TIMEOUT_MS", "250")
    monkeypatch.setenv("ASRE_ENQUEUE_TIMEOUT_MS", "750")
    monkeypatch.setenv("ASRE_FIRESTORE_DATABASE", "asre-db")
    monkeypatch.setenv("ASRE_TX_ATTEMPTS_PER_ROUND", "3")
    monkeypatch.setenv("ASRE_TX_CONTENTION_ROUNDS", "6")
    monkeypatch.setenv("ASRE_TX_BASE_BACKOFF_MS", "40")
    monkeypatch.setenv("ASRE_TX_MAX_BACKOFF_MS", "2000")
    monkeypatch.setenv("ASRE_DEDUP_FIRST_TIMEOUT_MS", "2500")
    monkeypatch.setenv("ASRE_ENQUEUE_FIRST_TIMEOUT_MS", "4000")
    s = config.IngestSettings.from_env()
    assert (s.dedup_first_timeout_s, s.enqueue_first_timeout_s) == (2.5, 4.0)
    assert (s.tx_attempts_per_round, s.tx_contention_rounds, s.tx_base_backoff_s, s.tx_max_backoff_s) == (
        3,
        6,
        0.04,
        2.0,
    )
    d = s.dedup
    assert (d.window.total_seconds(), d.retention.days, d.update_interval.total_seconds()) == (2700, 7, 300)
    assert d.enqueue_lease.total_seconds() == 12
    assert (s.dedup_timeout_s, s.enqueue_timeout_s, s.firestore_database) == (0.25, 0.75, "asre-db")


def test_cloud_tasks_first_call_gets_the_cold_start_budget(monkeypatch):
    from google.cloud import tasks_v2

    timeouts = []

    class FakeClient:
        def create_task(self, parent, task, timeout):
            timeouts.append(timeout)
            return SimpleNamespace(name=f"{parent}/tasks/{len(timeouts)}")

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", FakeClient)
    q = CloudTasksQueue(
        "projects/p/locations/l/queues/q", "https://a", "sa@p", "aud", 0.5, first_timeout_s=5.0
    )
    q.enqueue({})
    q.enqueue({})
    assert timeouts == [5.0, 0.5]


def test_cloud_tasks_first_call_failure_keeps_the_cold_budget(monkeypatch):
    from google.cloud import tasks_v2

    timeouts = []

    class FlakyClient:
        def create_task(self, parent, task, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise TimeoutError("504 Deadline Exceeded")
            return SimpleNamespace(name="t")

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", FlakyClient)
    q = CloudTasksQueue(
        "projects/p/locations/l/queues/q", "https://a", "sa@p", "aud", 0.5, first_timeout_s=5.0
    )
    with pytest.raises(TimeoutError):
        q.enqueue({})
    q.enqueue({})
    q.enqueue({})
    assert timeouts == [5.0, 5.0, 0.5]  # still cold until one call succeeds
