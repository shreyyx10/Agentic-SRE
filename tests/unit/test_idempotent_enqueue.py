"""Idempotent hand-off to Cloud Tasks (step-11 live finding, 7 Oct 2026).

Live, every enqueue returned ``504 Deadline Exceeded`` although the task had
been created (the Brief appeared ~0.5 s later). Ingest nacked, Pub/Sub
redelivered, and each redelivery created another task. With a task name derived
from the incident, the redelivery's enqueue finds the task already there and
counts as success: one task per incident hand-off, however many retries.
"""

import re
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

from google.api_core.exceptions import AlreadyExists

from asre.core.dedup import DedupService, InMemoryRepository
from asre.core.queue import CloudTasksQueue, InMemoryQueue, task_id_for
from asre.ingest.app import IngestDeps, create_app
from tests.conftest import NOW, FakeVerifier, envelope, make_notification

PUSH = {"Authorization": "Bearer good-push"}


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
    return create_app(deps).test_client()


def push(client, state="open"):
    return client.post("/pubsub/example-shop", json=envelope(make_notification(state)), headers=PUSH)


def group(repo):
    return next(repo.get(p) for p in repo.paths() if "/alert_groups/" in p and "/history/" not in p)


# ------------------------------------------------------------------ task IDs


def test_task_id_is_stable_distinct_per_kind_and_valid():
    a = task_id_for("analyse", "example-shop", "k" * 64, "inc-1")
    assert a == task_id_for("analyse", "example-shop", "k" * 64, "inc-1")
    assert a != task_id_for("resolve", "example-shop", "k" * 64, "inc-1")
    assert a != task_id_for("analyse", "example-shop", "k" * 64, "inc-2")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,500}", a)


# ------------------------------------------------------------------ Cloud Tasks client


def test_named_task_that_already_exists_counts_as_success(monkeypatch):
    from google.cloud import tasks_v2

    seen = []

    class Client:
        def create_task(self, parent, task, timeout):
            seen.append(task.name)
            if len(seen) > 1:
                raise AlreadyExists("task exists")
            return SimpleNamespace(name=task.name)

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", Client)
    q = CloudTasksQueue("projects/p/locations/l/queues/q", "https://a", "sa@p", "aud", 0.5, 0.5)
    first = q.enqueue({"kind": "analyse"}, task_id="analyse-abc")
    again = q.enqueue({"kind": "analyse"}, task_id="analyse-abc")
    assert first == again == "projects/p/locations/l/queues/q/tasks/analyse-abc"
    assert seen == [first, first]


def test_unnamed_task_is_still_unnamed(monkeypatch):
    from google.cloud import tasks_v2

    names = []

    class Client:
        def create_task(self, parent, task, timeout):
            names.append(task.name)
            return SimpleNamespace(name=f"{parent}/tasks/auto")

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", Client)
    q = CloudTasksQueue("projects/p/locations/l/queues/q", "https://a", "sa@p", "aud", 0.5, 0.5)
    q.enqueue({"kind": "update"})
    assert names == [""]  # Cloud Tasks assigns the name


# ------------------------------------------------------------------ ingest, end to end


def test_timed_out_enqueue_then_redelivery_makes_one_task(tenants):
    """The live sequence: enqueue times out after creating the task; Pub/Sub redelivers."""
    repo, queue, clock = InMemoryRepository(), InMemoryQueue(time_out_after_create=True), Clock()
    client = ingest(tenants, repo, queue, clock)
    assert push(client).status_code == 503  # 504 from Cloud Tasks -> nack
    assert len(queue.tasks) == 1  # ...but the task was created

    queue.time_out_after_create = False
    clock.now += timedelta(seconds=30)  # Pub/Sub minimum backoff
    assert push(client).status_code == 204  # takeover: same task name -> AlreadyExists -> success
    assert len(queue.tasks) == 1
    assert group(repo)["analysis_enqueued"] is True  # no further takeovers


def test_resolution_retried_after_timeout_is_not_duplicated(tenants):
    repo, queue, clock = InMemoryRepository(), InMemoryQueue(), Clock()
    client = ingest(tenants, repo, queue, clock)
    assert push(client).status_code == 204

    queue.time_out_after_create = True
    clock.now += timedelta(minutes=5)
    assert push(client, "closed").status_code == 503
    queue.time_out_after_create = False
    clock.now += timedelta(seconds=30)
    assert push(client, "closed").status_code == 204

    assert [t["payload"]["kind"] for t in queue.tasks] == ["analyse", "resolve"]
    assert group(repo)["resolution_enqueued"] is True


class SlowDedup(DedupService):
    def __init__(self, repo, gate):
        super().__init__(repo)
        self.gate = gate

    def process(self, alert, now):
        self.gate.wait(2)
        return super().process(alert, now)


def test_uncertain_task_and_later_takeover_share_one_task(tenants):
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
    assert push(client).status_code == 204  # dedup timed out: uncertain task
    gate.set()
    time.sleep(0.3)
    clock.now += timedelta(seconds=25)
    assert push(client).status_code == 204  # takeover maps to the same named task
    assert len([t for t in queue.tasks if t["payload"]["kind"] == "analyse"]) == 1


def test_update_tasks_stay_unnamed(tenants):
    repo, queue, clock = InMemoryRepository(), InMemoryQueue(), Clock()
    client = ingest(tenants, repo, queue, clock)
    push(client)
    clock.now += timedelta(minutes=16)  # past the 15-minute update interval
    push(client)
    kinds = [(t["payload"]["kind"], t["task_id"]) for t in queue.tasks]
    assert kinds[0][0] == "analyse" and kinds[0][1] is not None
    assert kinds[1] == ("update", None)
