"""ANALYSE pipeline: evidence -> hypothesis -> critic -> Brief, including degraded mode."""

import json

import pytest

from asre.agent.critic import parse_review
from asre.agent.defaults import DEFAULT_ACTIONS, default_actions
from asre.agent.model import ModelError, ModelResult
from asre.analyse.app import AnalyseDeps, AnalyseTask, brief_id_for, brief_path, create_app
from asre.analyse.pipeline import AnalysisPipeline
from asre.core.dedup import DedupService, InMemoryRepository
from asre.core.models import AlertType
from asre.core.tenants import TenantRegistry
from tests.conftest import NOW, TASKS_SA, TENANT, FakeVerifier, make_alert

TASK = {"Authorization": "Bearer good-task"}
LABELS = {"cluster_name": "shop", "namespace_name": "default", "pod_name": "checkoutservice-7d9c-abcde"}


class Telemetry:
    def __init__(self):
        self.readers = []

    def list_logs(self, project, filter_, limit):
        if 'k8s-pod/app"="checkoutservice"' in filter_:
            return [
                {
                    "timestamp": "2026-10-01T08:59:00Z",
                    "severity": "ERROR",
                    "jsonPayload": {"message": "failed to charge card: paymentservice unavailable"},
                }
            ]
        if "cloudaudit" in filter_:
            return [
                {
                    "timestamp": "2026-10-01T08:50:00Z",
                    "insertId": "a1",
                    "protoPayload": {
                        "methodName": "io.k8s.apps.v1.deployments.scale.patch",
                        "resourceName": "apps/v1/namespaces/default/deployments/paymentservice/scale",
                        "request": {"spec": {"replicas": 0}},
                    },
                }
            ]
        return []

    def list_time_series(self, *a, **k):
        return []


class Model:
    """Returns queued answers (str) or raises queued ModelErrors; records prompts."""

    def __init__(self, *answers, name="gemini-3.1-pro-preview"):
        self.answers, self.name, self.calls = list(answers), name, []

    def generate(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return ModelResult(
            text=a, model=self.name, model_version=self.name, location="global", input_tokens=900
        )


def hypothesis_json(keys, **over):
    body = {
        "hypothesis": "paymentservice was scaled to zero, so checkout cannot charge cards",
        "confidence": 0.9,
        "evidence": [
            {"claim": "paymentservice scaled to 0", "source": "deployments", "citation": keys["deploy"]},
            {"claim": "checkout fails to charge", "source": "logs", "citation": keys["log"]},
        ],
        "alternatives": [{"hypothesis": "card network outage", "why_less_likely": "no external errors"}],
        "next_steps": [
            {
                "priority": 1,
                "action": "kubectl scale deployment/paymentservice --replicas=1",
                "expected_signal": "5xx stop",
                "risk": "low",
            }
        ],
        "insufficient_evidence": False,
        "missing": [],
    }
    body.update(over)
    return json.dumps(body)


def critic_json(verdict="supported", confidence=0.85, citation=None):
    return json.dumps(
        {
            "verdict": verdict,
            "confidence": confidence,
            "concerns": [{"problem": "check", "citation": citation}],
            "missing_checks": [],
        }
    )


@pytest.fixture
def setup():
    repo = InMemoryRepository()
    tenant = {**TENANT, "reader_service_account": "cust-example-shop@p.iam.gserviceaccount.com"}
    tenants = TenantRegistry.from_json(json.dumps(tenant))
    telemetry = Telemetry()

    def make(hypothesis, critic=None, deliver=None):
        def reader(r):
            telemetry.readers.append(r)
            return telemetry

        pipeline = AnalysisPipeline(repo, tenants, reader, hypothesis, critic, deliver, clock=lambda: NOW)
        app = create_app(
            AnalyseDeps(
                repo=repo,
                verifier=FakeVerifier(),
                audience="asre-analyse",
                tasks_invoker_sa=TASKS_SA,
                pipeline=pipeline,
                clock=lambda: NOW,
            )
        ).test_client()
        return app, pipeline

    return repo, telemetry, make


def body(repo, alert_type=AlertType.ERROR_RATE):
    alert = make_alert(alert_type=alert_type, resource_labels=LABELS)
    decision = DedupService(repo).process(alert, NOW)
    return {
        "version": 1,
        "kind": "analyse",
        "customer_id": "example-shop",
        "group_key": decision.group_key,
        "incident_id": decision.incident_id,
        "alert": alert.model_dump(mode="json"),
    }


def run(client, repo, b):
    r = client.post("/tasks/analyse", json=b, headers=TASK)
    bid = brief_id_for(AnalyseTask.model_validate(b))
    return r, repo.get(brief_path("example-shop", bid))


def pack_keys(brief):
    keys = {i["key"] for i in brief["evidence_items"]}
    return {
        "deploy": next(k for k in keys if k.startswith("deploy:paymentservice")),
        "log": next(k for k in keys if k.startswith("log:checkoutservice")),
    }


def keys_from_prompt(user_prompt):
    start = user_prompt.index("[")
    end = user_prompt.rindex("]") + 1
    items = json.loads(user_prompt[start:end])
    keys = {i["key"] for i in items}
    return {
        "deploy": next(k for k in keys if k.startswith("deploy:paymentservice")),
        "log": next(k for k in keys if k.startswith("log:checkoutservice")),
    }


class Lazy(Model):
    """Builds its answer from the evidence it is shown, as the real model would."""

    def __init__(self, **over):
        super().__init__()
        self.over = over

    def generate(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        return ModelResult(
            hypothesis_json(keys_from_prompt(user), **self.over),
            "gemini-3.1-pro-preview",
            "v",
            "global",
            input_tokens=900,
        )


# ------------------------------------------------------------------ complete


def test_complete_brief_with_review(setup):
    repo, telemetry, make = setup
    hyp = Lazy()
    critic = Model(critic_json("supported", 0.8), name="gemini-3.8-flash")
    client, _ = make(hyp, critic)
    r, brief = run(client, repo, body(repo))

    assert r.status_code == 200
    assert brief["status"] == "ready"  # no delivery channel yet
    a = brief["analysis"]
    assert a["outcome"] == "complete" and a["confidence"] == 0.9 and not a["disputed"]
    assert a["hypothesis"]["hypothesis"].startswith("paymentservice was scaled to zero")
    assert a["model"]["model"] == "gemini-3.1-pro-preview" and a["model"]["input_tokens"] == 900
    assert a["critic"]["status"] == "reviewed" and a["critic"]["verdict"] == "supported"
    assert a["critic"]["model"]["model"] == "gemini-3.8-flash"
    assert a["default_actions"] is None
    assert brief["model_id"] == "gemini-3.1-pro-preview" and brief["prompt_version"]
    assert brief["evidence"]["evidence_hash"].startswith("sha256:")
    assert telemetry.readers == ["cust-example-shop@p.iam.gserviceaccount.com"]
    # the critic saw the same fenced evidence plus the analysis under review
    critic_prompt = critic.calls[0][1]
    assert "UNTRUSTED_DATA_BEGIN" in critic_prompt and "ANALYSIS_UNDER_REVIEW" in critic_prompt
    assert hyp.calls[0][2]["title"] == "ModelOutput"  # the response schema is enforced


def test_contradicted_review_caps_confidence_and_flags_dispute(setup):
    repo, _, make = setup
    client, _ = make(Lazy(), Model(critic_json("contradicted", 0.3)))
    _, brief = run(client, repo, body(repo))
    a = brief["analysis"]
    assert a["confidence"] == 0.3 and a["disputed"] is True
    assert a["critic"]["concerns"][0]["cited"] is False


def test_weak_review_caps_without_dispute(setup):
    repo, _, make = setup
    client, _ = make(Lazy(), Model(critic_json("weak", 0.6)))
    _, brief = run(client, repo, body(repo))
    assert brief["analysis"]["confidence"] == 0.6 and brief["analysis"]["disputed"] is False


def test_critic_failure_ships_unreviewed(setup):
    repo, _, make = setup
    client, _ = make(Lazy(), Model(ModelError("503", 503)))
    _, brief = run(client, repo, body(repo))
    a = brief["analysis"]
    assert a["outcome"] == "complete" and a["critic"]["status"] == "unavailable" and a["confidence"] == 0.9


def test_unusable_critic_answer_is_recorded(setup):
    repo, _, make = setup
    client, _ = make(Lazy(), Model("not json"))
    _, brief = run(client, repo, body(repo))
    assert brief["analysis"]["critic"]["status"] == "invalid"


def test_uncited_claims_are_dropped(setup):
    repo, _, make = setup

    class Partly(Lazy):
        def generate(self, system, user, schema=None):
            keys = keys_from_prompt(user)
            ev = [
                {"claim": "real", "source": "deployments", "citation": keys["deploy"]},
                {"claim": "invented", "source": "logs", "citation": "log:made-up"},
            ]
            return ModelResult(hypothesis_json(keys, evidence=ev), "m", "v", "global")

    client, _ = make(Partly())
    _, brief = run(client, repo, body(repo))
    assert brief["analysis"]["dropped_claims"] == 1
    assert len(brief["analysis"]["hypothesis"]["evidence"]) == 1


# ------------------------------------------------------------------ insufficient / degraded


def test_insufficient_evidence_is_a_success_with_default_steps(setup):
    repo, _, make = setup
    answer = json.dumps(
        {
            "hypothesis": None,
            "confidence": 0.1,
            "insufficient_evidence": True,
            "missing": ["payment logs"],
            "alternatives": [],
        }
    )
    critic = Model()
    client, _ = make(Model(answer), critic)
    r, brief = run(client, repo, body(repo))
    a = brief["analysis"]
    assert r.status_code == 200 and a["outcome"] == "insufficient_evidence"
    assert a["default_actions"] == default_actions(AlertType.ERROR_RATE, "checkoutservice")
    assert critic.calls == []  # nothing to review


def test_degraded_when_no_model_answers(setup):
    repo, _, make = setup
    client, _ = make(Model(ModelError("404 model gone", 404)))
    r, brief = run(client, repo, body(repo, AlertType.OOM_KILLED))
    a = brief["analysis"]
    assert r.status_code == 200 and brief["status"] == "ready"
    assert a["outcome"] == "degraded" and "model unavailable" in a["reason"]
    assert a["default_actions"][0].startswith("Confirm the kill")
    assert brief["evidence_items"]  # evidence still ships
    assert a["model"] is None


def test_degraded_when_answer_is_off_schema(setup):
    repo, _, make = setup
    client, _ = make(Model('{"hypothesis": "x"}'))
    _, brief = run(client, repo, body(repo))
    a = brief["analysis"]
    assert a["outcome"] == "degraded" and a["reason"].startswith("schema")
    assert a["model"]["model"] == "gemini-3.1-pro-preview"  # we still record who answered
    assert a["default_actions"]


def test_runbook_from_the_service_profile_is_attached(setup):
    repo, _, make = setup
    client, _ = make(Model(ModelError("down", 503)))
    _, brief = run(client, repo, body(repo))
    assert brief["analysis"]["runbook"] == "runbooks/checkout.md"


def test_every_alert_type_has_default_steps():
    assert set(DEFAULT_ACTIONS) == set(AlertType)
    assert all(
        "cartservice" in s or "<service>" not in s
        for s in default_actions(AlertType.OOM_KILLED, "cartservice")
    )


# ------------------------------------------------------------------ retries and delivery


def test_delivery_failure_retries_without_rerunning_the_models(setup):
    repo, _, make = setup
    hyp = Lazy()
    sent = []

    def deliver(task, bid, brief):
        if not sent:
            sent.append("fail")
            raise RuntimeError("chat 500")
        sent.append(brief["analysis"]["outcome"])
        return {"channel": "chat", "message": "spaces/x/messages/1"}

    client, _ = make(hyp, deliver=deliver)
    b = body(repo)
    r, brief = run(client, repo, b)
    assert r.status_code == 503 and brief["status"] == "analysed"

    # Cloud Tasks retries after the lease is released
    r, brief = run(client, repo, b)
    assert r.status_code == 200 and brief["status"] == "delivered"
    assert brief["delivery"]["channel"] == "chat" and sent == ["fail", "complete"]
    assert len(hyp.calls) == 1  # analysis reused


def test_finished_brief_is_not_rerun(setup):
    repo, _, make = setup
    hyp = Lazy()
    client, _ = make(hyp)
    b = body(repo)
    run(client, repo, b)
    r, _ = run(client, repo, b)
    assert r.get_json()["status"] == "exists" and len(hyp.calls) == 1


def test_unknown_tenant_fails_terminally(setup):
    repo, _, make = setup
    client, _ = make(Model())
    b = body(repo)
    b["customer_id"] = "someone-else"
    b["alert"]["customer_id"] = "someone-else"
    r, brief = run(client, repo, b)
    bid = brief_id_for(AnalyseTask.model_validate(b))
    brief = repo.get(brief_path("someone-else", bid))
    assert r.status_code == 200 and brief["status"] == "failed"


def test_invalid_tenant_project_fails_terminally(setup):
    repo, _, _ = setup
    tenants = TenantRegistry.from_json(json.dumps({**TENANT, "gcp_project_id": "Bad_Project"}))
    pipeline = AnalysisPipeline(repo, tenants, lambda r: Telemetry(), Model(), clock=lambda: NOW)
    client = create_app(
        AnalyseDeps(
            repo=repo,
            verifier=FakeVerifier(),
            audience="asre-analyse",
            tasks_invoker_sa=TASKS_SA,
            pipeline=pipeline,
            clock=lambda: NOW,
        )
    ).test_client()
    _, brief = run(client, repo, body(repo))
    assert brief["status"] == "failed" and "gcp_project_id" in brief["failure"]


def test_parse_review_rejects_garbage():
    assert parse_review("[]", set()) is None
    assert parse_review(json.dumps({"verdict": "maybe", "confidence": 1}), set()) is None
