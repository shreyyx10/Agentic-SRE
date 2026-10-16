"""Same cause, same thread: alerts whose Briefs cite one deployment change share a Chat thread."""

import json
from datetime import timedelta

from asre.analyse.app import AnalyseDeps, AnalyseTask, brief_id_for, brief_path, create_app
from asre.analyse.pipeline import AnalysisPipeline, cause_keys, cause_path
from asre.core.dedup import DedupService, InMemoryRepository
from asre.core.models import AlertType
from asre.core.tenants import TenantRegistry
from asre.delivery.chat import ChatDelivery, render_related
from tests.conftest import NOW, TASKS_SA, TENANT, FakeVerifier, make_alert
from tests.unit.test_pipeline import LABELS, TASK, Lazy, Telemetry

TENANTS = TenantRegistry.from_json(json.dumps(TENANT))


class Deliveries:
    def __init__(self):
        self.sent = []

    def __call__(self, task, brief_id, brief):
        self.sent.append((brief_id, brief.get("related_to")))
        return {"channel": "test"}


def task_body(repo, service, alert_type=AlertType.ERROR_RATE, when=NOW):
    alert = make_alert(service_name=service, alert_type=alert_type, resource_labels=LABELS)
    decision = DedupService(repo).process(alert, when)
    return {
        "version": 1,
        "kind": "analyse",
        "customer_id": "example-shop",
        "group_key": decision.group_key,
        "incident_id": decision.incident_id,
        "alert": alert.model_dump(mode="json"),
    }


def client_for(repo, deliver, clock):
    pipeline = AnalysisPipeline(repo, TENANTS, lambda r: Telemetry(), Lazy(), None, deliver, clock=clock)
    deps = AnalyseDeps(
        repo=repo,
        verifier=FakeVerifier(),
        audience="asre-analyse",
        tasks_invoker_sa=TASKS_SA,
        pipeline=pipeline,
        clock=clock,
    )
    return create_app(deps).test_client()


def post(client, b):
    assert client.post("/tasks/analyse", json=b, headers=TASK).status_code == 200
    return brief_id_for(AnalyseTask.model_validate(b))


def test_two_alerts_about_one_deploy_share_the_first_thread():
    repo, sent = InMemoryRepository(), Deliveries()
    client = client_for(repo, sent, lambda: NOW)
    first = post(client, task_body(repo, "checkoutservice"))
    second = post(client, task_body(repo, "checkoutservice", AlertType.HIGH_LATENCY))
    assert sent.sent == [(first, None), (second, first)]
    brief = repo.get(brief_path("example-shop", second))
    assert brief["related_to"] == first and brief["cause_key"].startswith("deploy:paymentservice")
    assert brief["status"] == "delivered"


def test_retry_does_not_link_a_brief_to_itself():
    repo, sent = InMemoryRepository(), Deliveries()
    client = client_for(repo, sent, lambda: NOW)
    b = task_body(repo, "checkoutservice")
    first = post(client, b)
    post(client, b)  # Cloud Tasks redelivery
    assert all(related is None for _, related in sent.sent)
    assert repo.get(brief_path("example-shop", first)).get("related_to") is None


def test_an_old_incident_does_not_absorb_a_new_alert():
    repo, sent = InMemoryRepository(), Deliveries()
    now = [NOW]
    client = client_for(repo, sent, lambda: now[0])
    first = post(client, task_body(repo, "checkoutservice"))
    now[0] = NOW + timedelta(minutes=31)
    later = post(client, task_body(repo, "checkoutservice", AlertType.HIGH_LATENCY, when=now[0]))
    assert sent.sent == [(first, None), (later, None)]
    holder = repo.get(next(p for p in repo._docs if "/causes/" in p))
    assert holder["brief_id"] == later  # the newer Brief now holds the thread


def test_only_complete_diagnoses_with_a_real_deploy_count():
    deploy = {"citation": "deploy:paymentservice:abc"}
    complete = {"outcome": "complete", "hypothesis": {"evidence": [deploy, {"citation": "log:x"}]}}
    assert cause_keys(complete) == ["deploy:paymentservice:abc"]
    assert cause_keys({**complete, "outcome": "degraded"}) == []
    none = {"outcome": "complete", "hypothesis": {"evidence": [{"citation": "deploy:none"}]}}
    assert cause_keys(none) == []
    assert cause_path("c", "deploy:a") != cause_path("c", "deploy:b")


def test_updates_for_a_related_brief_go_to_the_shared_thread():
    repo, notices = InMemoryRepository(), []
    client = create_app(
        AnalyseDeps(
            repo=repo,
            verifier=FakeVerifier(),
            audience="asre-analyse",
            tasks_invoker_sa=TASKS_SA,
            notify=lambda task, thread: notices.append(thread),
            clock=lambda: NOW,
        )
    ).test_client()
    b = {**task_body(repo, "frontend"), "kind": "update", "occurrence_count": 2}
    bid = brief_id_for(AnalyseTask.model_validate(b))
    repo.create(brief_path("example-shop", bid), {"status": "delivered", "related_to": "brf_first"})
    assert client.post("/tasks/analyse", json=b, headers=TASK).status_code == 200
    assert notices == ["brf_first"]


def test_chat_posts_a_related_reply_in_the_first_thread():
    posts = []

    class Resp:
        status_code = 200

        def json(self):
            return {"name": "m", "thread": {"name": "t"}}

    class Secrets:
        def get(self, _):
            return "https://chat.googleapis.com/v1/spaces/X/messages?key=k"

    chat = ChatDelivery(Secrets(), post=lambda url, json, timeout: posts.append((url, json)) or Resp())
    brief = {
        "alert": {"alert_name": "Checkout latency p95", "service_name": "frontend"},
        "analysis": {"outcome": "complete", "confidence": 0.8, "hypothesis": {"hypothesis": "redis <down>"}},
        "related_to": "brf_first",
        "cause_key": "deploy:redis-cart:1",
        "evidence_items": [{"key": "deploy:redis-cart:1", "text": "redis-cart rolled out at 04:41"}],
    }
    record = chat.deliver_brief(None, "brf_second", brief)
    url, message = posts[0]
    assert "threadKey=brf_first" in url and record["related_to"] == "brf_first"
    assert "cardsV2" not in message
    text = message["text"]
    assert text.startswith("🔗 Related alert: Checkout latency p95 on frontend. Same cause as this incident.")
    assert "Change: redis-cart rolled out at 04:41" in text and "Diagnosis (80%): redis <down>" in text
    assert render_related("b", {})["text"].startswith("🔗 Related alert: alert on ?")


def test_a_related_forecast_keeps_its_forecast_framing_and_proposed_fix():
    brief = {
        "alert": {
            "alert_name": "Forecast: currencyservice CPU reaches 90% of its limit in ~2 min",
            "service_name": "currencyservice",
            "alert_type": "forecast",
        },
        "analysis": {
            "outcome": "complete",
            "confidence": 0.95,
            "hypothesis": {
                "hypothesis": "Load generator traffic rose to 400 users.",
                "next_steps": [
                    {
                        "priority": 1,
                        "action": "kubectl -n default scale deployment/currencyservice --replicas=3",
                    }
                ],
            },
        },
    }
    text = render_related("b", brief)["text"]
    assert text.startswith(
        "⏳ Related forecast: Forecast: currencyservice CPU reaches 90% of its limit in ~2 min. "
        "Same cause as this incident; nothing has failed yet."
    )
    assert " on currencyservice" not in text  # the name already says which service
    assert (
        "Proposed fix, needs approval: kubectl -n default scale deployment/currencyservice --replicas=3"
        in text
    )
