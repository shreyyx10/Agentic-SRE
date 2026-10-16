import pytest

from asre.analyse.app import AnalyseDeps, AnalyseTask, brief_id_for, brief_path, create_app
from asre.core.dedup import DedupService, InMemoryRepository
from tests.conftest import NOW, TASKS_SA, FakeVerifier, make_alert

AUD = "asre-analyse"
GOOD = {"Authorization": "Bearer good-task"}


@pytest.fixture
def env():
    repo = InMemoryRepository()
    calls = []
    deps = AnalyseDeps(
        repo=repo,
        verifier=FakeVerifier(),
        audience=AUD,
        tasks_invoker_sa=TASKS_SA,
        pipeline=lambda task, bid: calls.append(bid),
        clock=lambda: NOW,
    )
    return create_app(deps).test_client(), repo, calls


def task(repo, kind="analyse", uncertain=False):
    decision = DedupService(repo).process(make_alert(), NOW)
    return {
        "version": 1,
        "kind": kind,
        "customer_id": "example-shop",
        "group_key": decision.group_key,
        "incident_id": None if uncertain else decision.incident_id,
        "occurrence_count": 1,
        "dedup_uncertain": uncertain,
        "alert": make_alert().model_dump(mode="json"),
    }


def test_creates_pending_brief_links_group_and_runs_pipeline(env):
    client, repo, calls = env
    body = task(repo)
    r = client.post("/tasks/analyse", json=body, headers=GOOD)
    assert r.status_code == 200
    bid = r.get_json()["brief_id"]
    brief = repo.get(brief_path("example-shop", bid))
    assert brief["status"] == "pending" and brief["created_at"] == NOW
    group = repo.get(f"customers/example-shop/alert_groups/{body['group_key']}")
    assert group["brief_id"] == bid and calls == [bid]


def test_retried_task_is_idempotent(env):
    client, repo, calls = env
    body = task(repo)
    bid = client.post("/tasks/analyse", json=body, headers=GOOD).get_json()["brief_id"]
    # While the first run holds the lease, a retry neither re-runs the pipeline nor
    # returns 2xx (F4): Cloud Tasks keeps it until the Brief is finished.
    assert client.post("/tasks/analyse", json=body, headers=GOOD).status_code == 409
    repo.update(brief_path("example-shop", bid), {"status": "delivered"})
    r = client.post("/tasks/analyse", json=body, headers=GOOD)
    assert r.get_json()["status"] == "exists" and len(calls) == 1


def test_uncertain_task_gets_stable_id_without_group_link(env):
    client, repo, _ = env
    body = task(repo, uncertain=True)
    r = client.post("/tasks/analyse", json=body, headers=GOOD)
    assert r.status_code == 200
    assert r.get_json()["brief_id"] == brief_id_for(AnalyseTask.model_validate(body))


def test_group_missing_is_tolerated(env):
    client, repo, _ = env
    body = task(repo)
    body["group_key"] = "f" * 64
    assert client.post("/tasks/analyse", json=body, headers=GOOD).status_code == 200


@pytest.mark.parametrize("kind", ["update", "resolve"])
def test_update_and_resolve_are_noted(env, kind):
    client, repo, calls = env
    r = client.post("/tasks/analyse", json=task(repo, kind=kind), headers=GOOD)
    assert r.get_json()["status"] == "noted" and calls == []


def test_malformed_task_is_acknowledged_not_retried(env):
    client, *_ = env
    assert (
        client.post("/tasks/analyse", json={"kind": "analyse"}, headers=GOOD).get_json()["status"]
        == "dropped"
    )


@pytest.mark.parametrize("headers,status", [({}, 401), ({"Authorization": "Bearer good-push"}, 403)])
def test_only_the_tasks_invoker_may_call(env, headers, status):
    client, repo, _ = env
    assert client.post("/tasks/analyse", json=task(repo), headers=headers).status_code == status


def test_health(env):
    assert env[0].get("/health").status_code == 200


def test_verifier_required(monkeypatch):
    monkeypatch.delenv("ASRE_LOCAL_INSECURE", raising=False)
    with pytest.raises(RuntimeError):
        create_app(
            AnalyseDeps(repo=InMemoryRepository(), verifier=None, audience=AUD, tasks_invoker_sa=TASKS_SA)
        )


def test_brief_log_lines_carry_the_service_name(env, capsys):
    import json

    client, repo, _ = env
    body = task(repo)
    capsys.readouterr()
    assert client.post("/tasks/analyse", json=body, headers=GOOD).status_code == 200
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    [pending] = [x for x in lines if x.get("event") == "brief_pending"]
    assert pending["service_name"] == body["alert"]["service_name"]
