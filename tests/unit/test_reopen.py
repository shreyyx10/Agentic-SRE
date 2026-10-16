"""A re-fire soon after a resolution reopens the same incident (crash-loop flapping, live 12 Oct)."""

from datetime import timedelta

from asre.analyse.app import AnalyseDeps, AnalyseTask, brief_id_for, brief_path, create_app
from asre.core.config import load_dedup_config
from asre.core.dedup import Action, DedupConfig, DedupService, InMemoryRepository
from asre.core.models import AlertStatus
from asre.core.queue import InMemoryQueue
from asre.delivery.chat import render_update
from asre.ingest.app import IngestDeps
from asre.ingest.app import create_app as create_ingest
from tests.conftest import NOW, TASKS_SA, FakeVerifier, envelope, make_alert, make_notification

FIRING, RESOLVED = make_alert(), make_alert(status=AlertStatus.RESOLVED)


def flap(dedup, gap):
    first = dedup.process(FIRING, NOW)
    dedup.process(RESOLVED, NOW + timedelta(minutes=3))
    return first, dedup.process(FIRING, NOW + timedelta(minutes=3) + gap)


def test_refire_within_the_window_reopens_the_same_incident():
    repo = InMemoryRepository()
    first, again = flap(DedupService(repo), timedelta(minutes=1, seconds=20))  # the live gap
    assert again.action is Action.REOPEN
    assert again.incident_id == first.incident_id and again.occurrence_count == 2
    group = repo.get(again.path)
    assert group["state"] == "open" and group["resolved_at"] is None and group["reopened_count"] == 1


def test_a_later_refire_is_a_new_incident():
    first, again = flap(DedupService(InMemoryRepository()), timedelta(minutes=11))
    assert again.action is Action.ANALYSE and again.incident_id != first.incident_id


def test_reopening_can_be_turned_off():
    dedup = DedupService(InMemoryRepository(), DedupConfig(reopen_window=timedelta(0)))
    _, again = flap(dedup, timedelta(seconds=30))
    assert again.action is Action.ANALYSE


def test_reopened_incident_then_resolves_and_reopens_again():
    repo = InMemoryRepository()
    dedup = DedupService(repo)
    first, _ = flap(dedup, timedelta(minutes=1))
    assert dedup.process(RESOLVED, NOW + timedelta(minutes=8)).action is Action.RESOLVE
    third = dedup.process(FIRING, NOW + timedelta(minutes=10))
    assert third.action is Action.REOPEN and third.incident_id == first.incident_id
    assert repo.get(third.path)["reopened_count"] == 2


def test_window_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ASRE_DEDUP_REOPEN_MINUTES", "4")
    assert load_dedup_config().reopen_window == timedelta(minutes=4)


def test_ingest_sends_a_reopen_task(tenants):
    repo, queue = InMemoryRepository(), InMemoryQueue()
    times = iter([NOW, NOW + timedelta(minutes=3), NOW + timedelta(minutes=4)])
    client = create_ingest(
        IngestDeps(
            tenants=tenants,
            dedup=DedupService(repo),
            queue=queue,
            verifier=FakeVerifier(),
            audience="asre-ingest",
            clock=lambda: next(times),
        )
    ).test_client()
    for state in ("open", "closed", "open"):
        r = client.post(
            "/pubsub/example-shop",
            json=envelope(make_notification(state)),
            headers={"Authorization": "Bearer good-push"},
        )
        assert r.status_code == 204
    kinds = [t["payload"]["kind"] for t in queue.tasks]
    assert kinds == ["analyse", "resolve", "reopen"]
    assert len({t["payload"]["incident_id"] for t in queue.tasks}) == 1


def test_analyse_posts_firing_again_and_clears_the_resolution():
    repo, notices = InMemoryRepository(), []
    client = create_app(
        AnalyseDeps(
            repo=repo,
            verifier=FakeVerifier(),
            audience="asre-analyse",
            tasks_invoker_sa=TASKS_SA,
            notify=lambda task, bid: notices.append((task.kind, bid)),
            clock=lambda: NOW,
        )
    ).test_client()
    decision = DedupService(repo).process(FIRING, NOW)
    body = {
        "version": 1,
        "kind": "reopen",
        "customer_id": "example-shop",
        "group_key": decision.group_key,
        "incident_id": decision.incident_id,
        "occurrence_count": 3,
        "alert": FIRING.model_dump(mode="json"),
    }
    bid = brief_id_for(AnalyseTask.model_validate(body))
    repo.create(brief_path("example-shop", bid), {"status": "delivered", "resolved_at": NOW})
    r = client.post("/tasks/analyse", json=body, headers={"Authorization": "Bearer good-task"})
    assert r.status_code == 200 and notices == [("reopen", bid)]
    brief = repo.get(brief_path("example-shop", bid))
    assert brief["resolved_at"] is None and brief["reopened_at"] == NOW


def test_firing_again_message():
    text = render_update("reopen", 3, {"alert_name": "Container restarts", "service_name": "redis-cart"})[
        "text"
    ]
    assert text.startswith("🔁 Firing again: Container restarts on redis-cart.") and "premature" in text
