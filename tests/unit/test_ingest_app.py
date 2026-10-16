import time
from datetime import timedelta

import pytest

from asre.core.dedup import DedupService, InMemoryRepository, incident_id_for
from asre.core.queue import InMemoryQueue
from asre.ingest.app import IngestDeps, create_app
from tests.conftest import NOW, FakeVerifier, envelope, make_notification

AUD = "asre-ingest"
GOOD = {"Authorization": "Bearer good-push"}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def env(tenants):
    repo, queue, clock = InMemoryRepository(), InMemoryQueue(), Clock()
    deps = IngestDeps(
        tenants=tenants,
        dedup=DedupService(repo),
        queue=queue,
        verifier=FakeVerifier(),
        audience=AUD,
        clock=clock,
    )
    return create_app(deps).test_client(), deps, repo, queue, clock


def post(client, body, headers=GOOD, tenant="example-shop"):
    return client.post(f"/pubsub/{tenant}", json=body, headers=headers)


def test_health_is_dependency_free(env):
    client, deps, *_ = env
    deps.readiness_checks = (lambda: (_ for _ in ()).throw(RuntimeError("firestore down")),)
    assert client.get("/health").status_code == 200


def test_ready_reports_failing_dependencies(env):
    client, deps, *_ = env
    assert client.get("/ready").status_code == 200

    def firestore_reachable():
        raise RuntimeError("down")

    deps.readiness_checks = (firestore_reachable,)
    r = client.get("/ready")
    assert r.status_code == 503 and "firestore_reachable: down" in r.get_json()["failures"][0]


def test_first_alert_enqueues_analysis_and_marks_group(env):
    client, _, repo, queue, _ = env
    r = post(
        client,
        envelope(make_notification()),
        headers={**GOOD, "X-Cloud-Trace-Context": "0123456789abcdef0123456789abcdef/1;o=1"},
    )
    assert r.status_code == 204
    [task] = queue.tasks
    assert task["payload"]["kind"] == "analyse" and task["payload"]["customer_id"] == "example-shop"
    assert task["trace"] == "0123456789abcdef0123456789abcdef/1"
    [path] = repo.paths()
    assert repo.get(path)["analysis_enqueued"] is True


def test_duplicate_is_suppressed_without_a_task(env):
    client, _, _, queue, clock = env
    post(client, envelope(make_notification()))
    clock.now = NOW + timedelta(minutes=1)
    assert post(client, envelope(make_notification())).status_code == 204
    assert len(queue.tasks) == 1


def test_resolution_enqueues_resolve(env):
    client, _, _, queue, clock = env
    post(client, envelope(make_notification()))
    clock.now = NOW + timedelta(minutes=5)
    post(client, envelope(make_notification(state="closed")))
    assert [t["payload"]["kind"] for t in queue.tasks] == ["analyse", "resolve"]


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer nope"}, 401),
        ({"Authorization": "Basic x"}, 401),
        ({"Authorization": "Bearer other-tenant"}, 403),
        ({"Authorization": "Bearer unverified"}, 403),
    ],
)
def test_unauthenticated_or_wrong_tenant_rejected(env, headers, status):
    client, _, _, queue, _ = env
    assert post(client, envelope(make_notification()), headers=headers).status_code == status
    assert queue.tasks == []


def test_verifier_checks_audience(env):
    client, deps, *_ = env
    post(client, envelope(make_notification()))
    assert deps.verifier.audiences == [AUD]


def test_unknown_tenant_404(env):
    client, *_ = env
    assert post(client, envelope(make_notification()), tenant="nobody-here").status_code == 404


def test_subscription_must_belong_to_tenant(env):
    client, _, _, queue, _ = env
    body = envelope(make_notification(), subscription="projects/x/subscriptions/someone-elses")
    assert post(client, body).status_code == 403 and queue.tasks == []


@pytest.mark.parametrize("body", [None, [], {"message": {}}, envelope({"incident": {"state": "open"}})])
def test_bad_payload_400(env, body):
    client, *_ = env
    assert post(client, body).status_code == 400


def test_queue_failure_nacks_and_redelivery_takes_over(env):
    client, _, repo, queue, clock = env
    queue.fail = True
    assert post(client, envelope(make_notification())).status_code == 503
    queue.fail = False
    clock.now = NOW + timedelta(seconds=1)  # redelivery; lease was released on failure
    assert post(client, envelope(make_notification())).status_code == 204
    assert [t["payload"]["kind"] for t in queue.tasks] == ["analyse"]


class ExplodingDedup:
    def process(self, alert, now):
        raise RuntimeError("firestore unavailable")


class SlowDedup:
    def process(self, alert, now):
        time.sleep(0.3)


def test_dedup_failure_enqueues_anyway_marked_uncertain(tenants):
    queue = InMemoryQueue()
    deps = IngestDeps(
        tenants=tenants,
        dedup=ExplodingDedup(),
        queue=queue,
        verifier=FakeVerifier(),
        audience=AUD,
        clock=lambda: NOW,
    )
    assert post(create_app(deps).test_client(), envelope(make_notification())).status_code == 204
    [task] = queue.tasks
    # F3: the uncertain task carries the incident ID dedup would have assigned, not None.
    payload = task["payload"]
    assert payload["dedup_uncertain"] is True
    assert payload["incident_id"] == incident_id_for(payload["group_key"], NOW)
    assert len(task["payload"]["group_key"]) == 64


def test_dedup_timeout_enqueues_anyway(tenants):
    queue = InMemoryQueue()
    deps = IngestDeps(
        tenants=tenants,
        dedup=SlowDedup(),
        queue=queue,
        verifier=FakeVerifier(),
        audience=AUD,
        dedup_timeout_s=0.05,
        dedup_first_timeout_s=0.05,
        clock=lambda: NOW,
    )
    assert post(create_app(deps).test_client(), envelope(make_notification())).status_code == 204
    assert queue.tasks[0]["payload"]["dedup_uncertain"] is True


class DelayedDedup:
    """A real DedupService whose every call takes `delay` seconds (a slow store)."""

    def __init__(self, delay):
        self._svc = DedupService(InMemoryRepository())
        self.delay = delay

    def process(self, alert, now):
        time.sleep(self.delay)
        return self._svc.process(alert, now)

    def mark_enqueued(self, decision):
        self._svc.mark_enqueued(decision)


def test_cold_instance_gets_a_longer_first_dedup_budget_then_the_steady_one(tenants):
    queue, clock = InMemoryQueue(), Clock()
    dedup = DelayedDedup(0.2)
    deps = IngestDeps(
        tenants=tenants,
        dedup=dedup,
        queue=queue,
        verifier=FakeVerifier(),
        audience=AUD,
        dedup_timeout_s=0.05,
        dedup_first_timeout_s=1.0,
        clock=clock,
    )
    client = create_app(deps).test_client()
    # First call (cold): 0.2 s fits the 1 s first-call budget -> a certain decision.
    assert post(client, envelope(make_notification())).status_code == 204
    assert queue.tasks[0]["payload"]["dedup_uncertain"] is False
    # Warm now: the same 0.2 s exceeds the 0.05 s steady budget -> uncertain, still enqueued.
    clock.now = NOW + timedelta(hours=2)
    assert post(client, envelope(make_notification(incident_id="i-2"))).status_code == 204
    assert queue.tasks[-1]["payload"]["dedup_uncertain"] is True


def test_verifier_required_outside_local(tenants, monkeypatch):
    monkeypatch.delenv("ASRE_LOCAL_INSECURE", raising=False)
    with pytest.raises(RuntimeError):
        create_app(IngestDeps(tenants=tenants, dedup=None, queue=None, verifier=None, audience=AUD))


def test_local_insecure_mode_is_refused_on_cloud_run(tenants, monkeypatch):
    monkeypatch.setenv("ASRE_LOCAL_INSECURE", "1")
    monkeypatch.setenv("K_SERVICE", "asre-ingest")
    with pytest.raises(RuntimeError):
        create_app(IngestDeps(tenants=tenants, dedup=None, queue=None, verifier=None, audience=AUD))


def test_local_insecure_mode_skips_auth_off_cloud_run(tenants, monkeypatch):
    monkeypatch.setenv("ASRE_LOCAL_INSECURE", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    queue = InMemoryQueue()
    deps = IngestDeps(
        tenants=tenants,
        dedup=DedupService(InMemoryRepository()),
        queue=queue,
        verifier=None,
        audience=AUD,
        clock=lambda: NOW,
    )
    assert post(create_app(deps).test_client(), envelope(make_notification()), headers={}).status_code == 204


def test_every_decision_log_line_carries_the_service_name(env, capsys):
    import json

    client, *_ = env
    capsys.readouterr()
    assert post(client, envelope(make_notification(service="cartservice"))).status_code == 204
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    by_event = {x.get("event"): x for x in lines}
    for event in ("alerts_received", "groups_created", "alert_processed"):
        assert by_event[event]["service_name"] == "cartservice", event
