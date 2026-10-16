"""Regression tests for review findings F2, F3 and F4 (week-1 review, 30 Sep 2026).

F2  A resolution whose enqueue failed is re-sent when Pub/Sub redelivers it.
F3  A dedup timeout followed by a takeover produces one Brief, not two.
F4  A Brief whose pipeline failed is resumed by the Cloud Tasks retry.
"""

import threading
import time
from datetime import timedelta

import pytest

from asre.analyse.app import AnalyseDeps, AnalyseTask, brief_id_for, brief_path
from asre.analyse.app import create_app as create_analyse
from asre.core.dedup import Action, DedupService, InMemoryRepository, group_path
from asre.core.models import AlertStatus
from asre.core.queue import InMemoryQueue
from asre.ingest.app import IngestDeps
from asre.ingest.app import create_app as create_ingest
from tests.conftest import NOW, TASKS_SA, FakeVerifier, envelope, make_alert, make_notification

PUSH = {"Authorization": "Bearer good-push"}
TASK = {"Authorization": "Bearer good-task"}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def ingest(tenants, repo, queue, clock, dedup=None, **kw):
    deps = IngestDeps(
        tenants=tenants,
        dedup=dedup or DedupService(repo),
        queue=queue,
        verifier=FakeVerifier(),
        audience="asre-ingest",
        clock=clock,
        **kw,
    )
    return create_ingest(deps).test_client()


def push(client, state):
    return client.post("/pubsub/example-shop", json=envelope(make_notification(state)), headers=PUSH)


# ------------------------------------------------------------------------- F2


def test_f2_resolution_resent_after_failed_enqueue(tenants):
    repo, queue, clock = InMemoryRepository(), InMemoryQueue(), Clock()
    client = ingest(tenants, repo, queue, clock)
    assert push(client, "open").status_code == 204

    queue.fail = True
    clock.now += timedelta(minutes=5)
    assert push(client, "closed").status_code == 503  # nack: Pub/Sub redelivers

    queue.fail = False
    clock.now += timedelta(seconds=30)
    assert push(client, "closed").status_code == 204
    assert [t["payload"]["kind"] for t in queue.tasks] == ["analyse", "resolve"]

    # Once the resolution is enqueued, a further duplicate is ignored as before.
    clock.now += timedelta(seconds=30)
    assert push(client, "closed").status_code == 204
    assert [t["payload"]["kind"] for t in queue.tasks] == ["analyse", "resolve"]


def test_f2_dedup_marks_resolution_owed_until_enqueued():
    repo = InMemoryRepository()
    dedup = DedupService(repo)
    dedup.process(make_alert(), NOW)
    closed = dedup.process(make_alert(status=AlertStatus.RESOLVED), NOW + timedelta(minutes=1))
    assert closed.action is Action.RESOLVE
    assert repo.get(closed.path)["resolution_enqueued"] is False

    again = dedup.process(make_alert(status=AlertStatus.RESOLVED), NOW + timedelta(minutes=2))
    assert again.action is Action.RESOLVE  # still owed

    dedup.mark_resolution_enqueued(again)
    done = dedup.process(make_alert(status=AlertStatus.RESOLVED), NOW + timedelta(minutes=3))
    assert done.action is Action.IGNORE


def test_f2_groups_closed_before_the_fix_are_still_ignored():
    """Documents written before this change have no resolution_enqueued field."""
    repo = InMemoryRepository()
    dedup = DedupService(repo)
    first = dedup.process(make_alert(), NOW)
    repo.update(first.path, {"state": "closed"})  # legacy closed group, field absent
    late = dedup.process(make_alert(status=AlertStatus.RESOLVED), NOW + timedelta(minutes=1))
    assert late.action is Action.IGNORE


# ------------------------------------------------------------------------- F3


class SlowDedup(DedupService):
    def __init__(self, repo, gate):
        super().__init__(repo)
        self.gate = gate

    def process(self, alert, now):
        self.gate.wait(2)  # Firestore is slow...
        return super().process(alert, now)  # ...but the write still lands


def test_f3_timeout_then_takeover_carry_the_same_incident_id(tenants):
    repo, queue, clock, gate = InMemoryRepository(), InMemoryQueue(), Clock(), threading.Event()
    client = ingest(
        tenants,
        repo,
        queue,
        clock,
        dedup=SlowDedup(repo, gate),
        dedup_timeout_s=0.05,
        dedup_first_timeout_s=0.05,
    )
    assert push(client, "open").status_code == 204
    gate.set()
    time.sleep(0.3)  # the timed-out dedup call finishes in the background

    clock.now += timedelta(seconds=25)  # past the 20 s enqueue lease: takeover
    assert push(client, "open").status_code == 204
    # The takeover carries the same incident ID as the uncertain task, so it maps
    # to the same named Cloud Tasks task and is absorbed there (idempotent
    # hand-off); even without that, ANALYSE would derive the same Brief ID.
    analyses = [t["payload"] for t in queue.tasks if t["payload"]["kind"] == "analyse"]
    assert len(analyses) == 1
    assert analyses[0]["dedup_uncertain"] is True and analyses[0]["incident_id"] is not None
    stored = next(repo.get(p) for p in repo.paths() if "/alert_groups/" in p)
    assert stored["incident_id"] == analyses[0]["incident_id"]


# ------------------------------------------------------------------------- analyse


@pytest.fixture
def analyse_env():
    repo, clock = InMemoryRepository(), Clock()
    state = {"fail": 0, "calls": []}

    def pipeline(task, bid):
        state["calls"].append(bid)
        if state["fail"]:
            state["fail"] -= 1
            raise RuntimeError("Gemini 429")
        repo.update(brief_path(task.customer_id, bid), {"status": "delivered"})

    deps = AnalyseDeps(
        repo=repo,
        verifier=FakeVerifier(),
        audience="asre-analyse",
        tasks_invoker_sa=TASKS_SA,
        pipeline=pipeline,
        pipeline_lease=timedelta(seconds=330),
        clock=clock,
    )
    return create_analyse(deps).test_client(), repo, clock, state


def task_body(repo, incident_id=None, uncertain=False):
    decision = DedupService(repo).process(make_alert(), NOW)
    return {
        "version": 1,
        "kind": "analyse",
        "customer_id": "example-shop",
        "group_key": decision.group_key,
        "incident_id": incident_id or decision.incident_id,
        "occurrence_count": 1,
        "dedup_uncertain": uncertain,
        "alert": make_alert().model_dump(mode="json"),
    }


def test_f3_second_task_for_the_same_incident_is_dropped(analyse_env):
    client, repo, _, state = analyse_env
    body = task_body(repo)
    uncertain = {**body, "dedup_uncertain": True}
    assert client.post("/tasks/analyse", json=uncertain, headers=TASK).status_code == 200
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    assert r.get_json()["status"] == "exists" and len(state["calls"]) == 1
    briefs = [p for p in repo.paths() if "/briefs/" in p]
    assert len(briefs) == 1


def test_f3_uncertain_brief_never_relinks_another_incident(analyse_env):
    client, repo, _, _ = analyse_env
    body = task_body(repo)
    real = client.post("/tasks/analyse", json=body, headers=TASK).get_json()["brief_id"]
    # An uncertain task whose guessed incident ID does not match the open group.
    other = {**body, "incident_id": "0000000000000000-1", "dedup_uncertain": True}
    assert client.post("/tasks/analyse", json=other, headers=TASK).status_code == 200
    assert repo.get(group_path("example-shop", body["group_key"]))["brief_id"] == real


def test_f4_failed_pipeline_is_resumed_by_the_retry(analyse_env):
    client, repo, clock, state = analyse_env
    body = task_body(repo)
    state["fail"] = 1
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    assert r.status_code == 503  # Cloud Tasks will retry
    bid = r.get_json()["brief_id"]
    assert repo.get(brief_path("example-shop", bid))["status"] == "pending"

    clock.now += timedelta(seconds=10)
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    brief = repo.get(brief_path("example-shop", bid))
    assert brief["status"] == "delivered" and brief["attempts"] == 2
    assert state["calls"] == [bid, bid]


def test_f4_retry_waits_while_another_worker_holds_the_lease(analyse_env):
    client, repo, clock, state = analyse_env
    body = task_body(repo)
    bid = brief_id_for(AnalyseTask.model_validate(body))
    # A worker created the Brief and is still running (or died) mid-pipeline.
    repo.create(
        brief_path("example-shop", bid),
        {"status": "pending", "lease_until": NOW + timedelta(seconds=330), "attempts": 1},
    )
    assert client.post("/tasks/analyse", json=body, headers=TASK).status_code == 409
    assert state["calls"] == []

    clock.now += timedelta(seconds=331)  # the lease lapsed: that worker is gone
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    assert r.status_code == 200 and state["calls"] == [bid]


def test_f4_finished_brief_is_not_rerun(analyse_env):
    client, repo, _, state = analyse_env
    body = task_body(repo)
    assert client.post("/tasks/analyse", json=body, headers=TASK).status_code == 200
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    assert r.get_json()["status"] == "exists" and len(state["calls"]) == 1


def test_f4_week1_behaviour_unchanged_without_a_pipeline():
    repo = InMemoryRepository()
    client = create_analyse(
        AnalyseDeps(
            repo=repo,
            verifier=FakeVerifier(),
            audience="asre-analyse",
            tasks_invoker_sa=TASKS_SA,
            clock=lambda: NOW,
        )
    ).test_client()
    body = task_body(repo)
    assert client.post("/tasks/analyse", json=body, headers=TASK).get_json()["status"] == "accepted"
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    assert r.status_code == 200 and r.get_json()["status"] == "exists"
