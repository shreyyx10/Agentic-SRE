"""Google Chat delivery: card content, threading, secret handling, retries (T4.8)."""

import json

import pytest

from asre.analyse.app import AnalyseDeps, AnalyseTask, brief_id_for, brief_path, create_app
from asre.core.dedup import DedupService, InMemoryRepository
from asre.core.models import AlertStatus, AlertType
from asre.delivery.chat import (
    WEBHOOK_SECRET,
    ChatDelivery,
    DeliveryError,
    render_brief,
    render_update,
    thread_url,
)
from tests.conftest import NOW, TASKS_SA, FakeVerifier, make_alert

WEBHOOK = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=K&token=T"
TASK = {"Authorization": "Bearer good-task"}


class Secrets:
    def __init__(self, value=WEBHOOK):
        self.value = value

    def get(self, secret_id):
        assert secret_id == WEBHOOK_SECRET
        return self.value

    def available(self):
        return [WEBHOOK_SECRET]


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = (
            body
            if body is not None
            else {"name": "spaces/AAA/messages/1", "thread": {"name": "spaces/AAA/threads/9"}}
        )

    def json(self):
        return self._body


class Post:
    def __init__(self, *responses):
        self.responses = list(responses) or [Resp()]
        self.calls = []

    def __call__(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def complete_brief(**analysis_over):
    analysis = {
        "outcome": "complete",
        "confidence": 0.95,
        "disputed": False,
        "hypothesis": {
            "hypothesis": "paymentservice was scaled to 0 <replicas>",
            "evidence": [
                {"claim": "scaled to 0", "source": "deployments", "citation": "deploy:paymentservice:a1"}
            ],
            "alternatives": [{"hypothesis": "card network down", "why_less_likely": "no external errors"}],
            "next_steps": [
                {"priority": 1, "action": "scale it back up", "expected_signal": "5xx stop", "risk": "low"}
            ],
            "missing": [],
        },
        "critic": {"status": "reviewed", "verdict": "supported", "concerns": []},
        "model": {"model": "gemini-3.1-pro-preview", "fallback_used": False},
        "default_actions": None,
        "runbook": "runbooks/payment.md",
        **analysis_over,
    }
    return {
        "alert": {
            "alert_name": "Storefront 5xx errors",
            "service_name": "frontend",
            "console_url": "https://console.cloud.google.com/monitoring/alerting/incidents/0.x",
        },
        "analysis": analysis,
        "context_quality": "complete",
        "evidence_items": [{"key": "deploy:paymentservice:a1"}] * 9,
    }


def texts(message):
    card = message["cardsV2"][0]["card"]
    out = [card["header"]["title"], card["header"]["subtitle"]]
    for s in card["sections"]:
        out.append(s.get("header", ""))
        for w in s["widgets"]:
            out.append(json.dumps(w))
    return "\n".join(out)


# ------------------------------------------------------------------ rendering


def test_complete_brief_card():
    m = render_brief("brf_1", complete_brief())
    t = texts(m)
    assert m["cardsV2"][0]["card"]["header"]["title"] == "🔎 frontend: Storefront 5xx errors"
    assert "Likely cause found, confidence 95%" in t
    assert "paymentservice was scaled to 0 &lt;replicas&gt;" in t  # model text is escaped
    assert "critic agrees" in t and "deploy:paymentservice:a1" in t
    assert "Next steps" in t and "scale it back up" in t and "Alternatives considered" in t
    assert "runbooks/payment.md" in t and "Brief: brf_1" in t and "9 items" in t
    assert "Open alert" in t
    assert m["text"].startswith("🔎 frontend: Storefront 5xx errors: paymentservice")


def test_disputed_brief_says_so():
    critic = {
        "status": "reviewed",
        "verdict": "contradicted",
        "concerns": [{"problem": "logs point at cart"}],
    }
    t = texts(render_brief("b", complete_brief(critic=critic, disputed=True, confidence=0.3)))
    assert "critic disputes" in t and "logs point at cart" in t and "one option, not the answer" in t
    assert "confidence 30%" in t


def test_degraded_brief_shows_default_steps():
    brief = complete_brief(
        outcome="degraded",
        hypothesis=None,
        model=None,
        critic=None,
        default_actions=["Read the errors", "Check replicas"],
    )
    t = texts(render_brief("b", brief))
    assert "⚠️" in t and "Degraded: no AI diagnosis" in t and "AI analysis was unavailable" in t
    assert "First steps (default)" in t and "1. Read the errors" in t and "2. Check replicas" in t
    assert "Model: none" in t and "Next steps" not in t


def test_insufficient_evidence_lists_what_would_help():
    h = {"hypothesis": None, "missing": ["payment logs"], "evidence": []}
    t = texts(
        render_brief(
            "b", complete_brief(outcome="insufficient_evidence", hypothesis=h, default_actions=["Look"])
        )
    )
    assert "Insufficient evidence" in t and "What would help" in t and "payment logs" in t and "1. Look" in t


def test_fallback_model_is_shown():
    t = texts(
        render_brief(
            "b",
            complete_brief(
                model={
                    "model": "gemini-3.8-flash",
                    "fallback_used": True,
                    "requested_model": "gemini-3.1-pro-preview",
                }
            ),
        )
    )
    assert "gemini-3.8-flash (fallback from gemini-3.1-pro-preview)" in t


def test_non_console_links_are_not_turned_into_buttons():
    brief = complete_brief()
    brief["alert"]["console_url"] = "https://evil.example/phish"
    assert "Open alert" not in texts(render_brief("b", brief))


def test_update_and_resolution_messages():
    assert (
        render_update("update", 7, {"service_name": "frontend"})["text"]
        == "🔁 Still firing: 7 alerts so far for frontend."
    )
    assert (
        "Resolved"
        in render_update("resolve", 1, {"alert_name": "5xx", "ended_at": "2026-10-01T09:10:00Z"})["text"]
    )


# ------------------------------------------------------------------ sending


def test_thread_key_is_added_to_the_webhook():
    url = thread_url(WEBHOOK, "brf_1")
    assert url.startswith(WEBHOOK + "&")
    assert "threadKey=brf_1" in url and "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" in url
    assert thread_url("https://chat.googleapis.com/v1/spaces/A/messages", "k").endswith(
        "?threadKey=k&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    )


@pytest.mark.parametrize(
    "bad", ["http://chat.googleapis.com/x", "https://evil.example/x", "https://chat.googleapis.com.evil.io/x"]
)
def test_only_google_chat_webhooks_are_used(bad):
    with pytest.raises(DeliveryError):
        thread_url(bad, "k")


def test_deliver_posts_card_and_returns_record():
    post = Post()
    record = ChatDelivery(Secrets(), post).deliver_brief(None, "brf_1", complete_brief())
    assert record == {
        "channel": "google_chat",
        "message": "spaces/AAA/messages/1",
        "thread": "spaces/AAA/threads/9",
    }
    url, body, timeout = post.calls[0]
    assert "threadKey=brf_1" in url and body["cardsV2"][0]["cardId"] == "brf_1" and timeout == 10.0


@pytest.mark.parametrize("failure", [Resp(429, {}), Resp(500, {}), ConnectionError("reset")])
def test_failures_raise_without_leaking_the_url(failure):
    with pytest.raises(DeliveryError) as e:
        ChatDelivery(Secrets(), Post(failure)).deliver_brief(None, "b", complete_brief())
    assert "token" not in str(e.value) and "key=" not in str(e.value)
    assert e.value.__cause__ is None  # the original exception (which may hold the URL) is not chained


# ------------------------------------------------------------------ through ANALYSE


def app_with(notify, repo):
    return create_app(
        AnalyseDeps(
            repo=repo,
            verifier=FakeVerifier(),
            audience="asre-analyse",
            tasks_invoker_sa=TASKS_SA,
            notify=notify,
            clock=lambda: NOW,
        )
    ).test_client()


def task_body(repo, kind):
    decision = DedupService(repo).process(make_alert(alert_type=AlertType.ERROR_RATE), NOW)
    alert = make_alert(
        alert_type=AlertType.ERROR_RATE,
        status=AlertStatus.RESOLVED if kind == "resolve" else AlertStatus.FIRING,
    )
    return {
        "version": 1,
        "kind": kind,
        "customer_id": "example-shop",
        "group_key": decision.group_key,
        "incident_id": decision.incident_id,
        "occurrence_count": 4,
        "alert": alert.model_dump(mode="json"),
    }


def test_update_posts_to_the_incident_thread():
    repo, post = InMemoryRepository(), Post()
    client = app_with(ChatDelivery(Secrets(), post).notify, repo)
    body = task_body(repo, "update")
    r = client.post("/tasks/analyse", json=body, headers=TASK)
    bid = brief_id_for(AnalyseTask.model_validate(body))
    assert r.status_code == 200 and r.get_json()["brief_id"] == bid
    assert f"threadKey={bid}" in post.calls[0][0] and "Still firing: 4" in post.calls[0][1]["text"]


def test_resolution_marks_the_brief_and_posts():
    repo, post = InMemoryRepository(), Post()
    client = app_with(ChatDelivery(Secrets(), post).notify, repo)
    body = task_body(repo, "resolve")
    bid = brief_id_for(AnalyseTask.model_validate(body))
    repo.create(brief_path("example-shop", bid), {"status": "delivered"})
    assert client.post("/tasks/analyse", json=body, headers=TASK).status_code == 200
    assert repo.get(brief_path("example-shop", bid))["resolved_at"] == NOW
    assert "Resolved" in post.calls[0][1]["text"]


def test_resolution_without_a_brief_still_posts():
    repo, post = InMemoryRepository(), Post()
    client = app_with(ChatDelivery(Secrets(), post).notify, repo)
    assert client.post("/tasks/analyse", json=task_body(repo, "resolve"), headers=TASK).status_code == 200
    assert len(post.calls) == 1


def test_failed_notice_is_retried():
    repo = InMemoryRepository()
    client = app_with(ChatDelivery(Secrets(), Post(Resp(503, {}))).notify, repo)
    r = client.post("/tasks/analyse", json=task_body(repo, "update"), headers=TASK)
    assert r.status_code == 503 and r.get_json()["status"] == "retry"


def test_without_chat_updates_are_just_noted():
    repo = InMemoryRepository()
    r = app_with(None, repo).post("/tasks/analyse", json=task_body(repo, "update"), headers=TASK)
    assert r.status_code == 200 and r.get_json()["status"] == "noted"
