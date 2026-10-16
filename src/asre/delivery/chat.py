"""Google Chat delivery through an incoming webhook.

One incident is one Chat thread. The Brief is the first message (a card); repeat
alerts and the resolution are replies in that thread. The thread is chosen by
``threadKey`` = the Brief ID, so every message for an incident lands together
without storing any Chat state.

The webhook URL is a credential (anyone with it can post to the space). It lives
in Secret Manager (``chat-webhook-url``, the README's Deploy section), is fetched at send
time, must point at ``chat.googleapis.com``, and is never logged: errors carry
the HTTP status only.

Everything shown comes from the Brief record. Model text is HTML-escaped,
because Chat cards render a small set of HTML tags.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

from asre.core.logging import get_logger

log = get_logger(__name__)

WEBHOOK_SECRET = "chat-webhook-url"  # noqa: S105 — the secret's ID, not its value
WEBHOOK_HOST = "chat.googleapis.com"

ICON = {"complete": "🔎", "insufficient_evidence": "❔", "degraded": "⚠️"}
VERDICT = {
    "supported": "✅ critic agrees",
    "weak": "🟡 critic: weakly supported",
    "contradicted": "🔴 critic disputes",
}


class DeliveryError(RuntimeError):
    """The message was not accepted; Cloud Tasks will retry."""


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def thread_url(webhook: str, thread_key: str) -> str:
    parts = urlsplit(webhook)
    if parts.scheme != "https" or parts.hostname != WEBHOOK_HOST:
        raise DeliveryError("chat-webhook-url is not a Google Chat webhook")
    extra = urlencode({"threadKey": thread_key, "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"})
    return f"{webhook}&{extra}" if parts.query else f"{webhook}?{extra}"


# ------------------------------------------------------------------ rendering


def _paragraph(text: str) -> dict[str, Any]:
    return {"textParagraph": {"text": text}}


def _section(header: str, lines: list[str], collapsible: bool = False) -> dict[str, Any] | None:
    if not lines:
        return None
    section: dict[str, Any] = {"header": header, "widgets": [_paragraph("<br>".join(lines))]}
    if collapsible:
        section.update(collapsible=True, uncollapsibleWidgetsCount=0)
    return section


def render_brief(brief_id: str, brief: dict[str, Any]) -> dict[str, Any]:
    """The Chat message for a finished Brief: a card plus a plain-text notification line."""
    alert = brief.get("alert") or {}
    a = brief.get("analysis") or {}
    h = a.get("hypothesis") or {}
    outcome = a.get("outcome", "degraded")
    service = alert.get("service_name", "?")
    forecast = alert.get("alert_type") == "forecast"
    icon = "⏳" if forecast else ICON.get(outcome, "⚠️")
    title = f"{icon} {service}: {alert.get('alert_name', 'alert')}"

    if forecast:
        # Nothing has failed: lead with the trend, then the cause and the preventive fix.
        headline = (h.get("hypothesis") if outcome == "complete" else None) or alert.get("summary") or ""
        subtitle = "Forecast: nothing has failed yet" + (
            f", confidence {round(100 * float(a.get('confidence') or 0))}%" if outcome == "complete" else ""
        )
    elif outcome == "complete":
        headline = h.get("hypothesis") or "(no hypothesis)"
        subtitle = f"Likely cause found, confidence {round(100 * float(a.get('confidence') or 0))}%"
    elif outcome == "insufficient_evidence":
        headline = "Not enough evidence to name a cause. Start with the steps below."
        subtitle = "Insufficient evidence"
    else:
        headline = "The AI analysis was unavailable. The evidence and default first steps are below."
        subtitle = "Degraded: no AI diagnosis"

    sections: list[dict[str, Any] | None] = []

    summary = [f"<b>{esc(headline)}</b>"]
    if str(alert.get("summary") or "").startswith("[synthetic]"):
        summary.append("🧪 <i>Test incident: a simulated alert, checked against the real cluster.</i>")
    if forecast and outcome == "complete" and alert.get("summary"):
        summary.append(f"📈 {esc(alert['summary'])}")
    critic = a.get("critic") or {}
    if critic.get("status") == "reviewed":
        summary.append(VERDICT.get(critic.get("verdict"), esc(critic.get("verdict"))))
        summary += [f"• {esc(c['problem'])}" for c in critic.get("concerns", [])[:3]]
    if a.get("disputed"):
        summary.append("<i>Treat this as one option, not the answer.</i>")
    sections.append({"widgets": [_paragraph("<br>".join(summary))]})

    evidence = [f"• {esc(e['claim'])} <i>[{esc(e['citation'])}]</i>" for e in (h.get("evidence") or [])[:6]]
    sections.append(_section("Evidence", evidence))

    if outcome == "complete":
        steps = [
            f"{s['priority']}. {esc(s['action'])} "
            f"<i>(expect: {esc(s['expected_signal'])}; risk: {esc(s['risk'])})</i>"
            for s in (h.get("next_steps") or [])[:4]
        ]
        if forecast and steps:
            steps[0] = f"<b>Proposed fix, needs approval:</b> {steps[0]}"
            steps.append("<i>Nothing is changed automatically: a person approves the fix and runs it.</i>")
        sections.append(_section("Prevent it" if forecast else "Next steps", steps))
        alternatives = [
            f"• {esc(x['hypothesis'])}: <i>{esc(x['why_less_likely'])}</i>"
            for x in (h.get("alternatives") or [])[:3]
        ]
        sections.append(_section("Alternatives considered", alternatives, collapsible=True))
    else:
        steps = [f"{i}. {esc(s)}" for i, s in enumerate(a.get("default_actions") or [], 1)]
        sections.append(_section("First steps (default)", steps))
        missing = [f"• {esc(m)}" for m in (h.get("missing") or [])[:5]]
        sections.append(_section("What would help", missing))

    if a.get("runbook"):
        sections.append(_section("Runbook", [esc(a["runbook"])]))

    model = a.get("model") or {}
    footer = [
        f"Evidence: {esc(brief.get('context_quality') or 'unknown')}, "
        f"{len(brief.get('evidence_items') or [])} items",
        f"Model: {esc(model.get('model') or 'none')}"
        + (f" (fallback from {esc(model.get('requested_model'))})" if model.get("fallback_used") else ""),
        f"Brief: {esc(brief_id)}",
    ]
    sections.append(_section("Details", footer, collapsible=True))

    console = alert.get("console_url") or ""
    if console.startswith("https://console.cloud.google.com/"):
        sections.append(
            {
                "widgets": [
                    {
                        "buttonList": {
                            "buttons": [{"text": "Open alert", "onClick": {"openLink": {"url": console}}}]
                        }
                    }
                ]
            }
        )

    return {
        "text": f"{title}: {headline}",
        "cardsV2": [
            {
                "cardId": brief_id,
                "card": {
                    "header": {"title": title, "subtitle": subtitle},
                    "sections": [s for s in sections if s],
                },
            }
        ],
    }


def render_related(brief_id: str, brief: dict[str, Any]) -> dict[str, Any]:
    """A reply in the first incident's thread: another alert, the same deployment change behind it."""
    alert = brief.get("alert") or {}
    a = brief.get("analysis") or {}
    h = a.get("hypothesis") or {}
    cause = next(
        (i["text"] for i in brief.get("evidence_items") or [] if i.get("key") == brief.get("cause_key")), None
    )
    name, service = alert.get("alert_name", "alert"), alert.get("service_name", "?")
    where = "" if service in name else f" on {service}"
    if alert.get("alert_type") == "forecast":
        # Still a forecast: say nothing has failed, and keep the fix that needs approval.
        lines = [f"⏳ Related forecast: {name}{where}. Same cause as this incident; nothing has failed yet."]
    else:
        lines = [f"🔗 Related alert: {name}{where}. Same cause as this incident."]
    if cause:
        lines.append(f"Change: {cause}")
    if h.get("hypothesis"):
        lines.append(f"Diagnosis ({round(100 * float(a.get('confidence') or 0))}%): {h['hypothesis']}")
    steps = h.get("next_steps") or []
    if alert.get("alert_type") == "forecast" and steps and a.get("outcome") == "complete":
        lines.append(f"Proposed fix, needs approval: {steps[0]['action']}")
    lines.append(f"Brief: {brief_id}")
    return {"text": "\n".join(lines)}


def render_update(kind: str, occurrence_count: int, alert: dict[str, Any]) -> dict[str, Any]:
    if kind == "reopen":
        return {
            "text": f"🔁 Firing again: {alert.get('alert_name', 'alert')} "
            f"on {alert.get('service_name', '?')}. "
            "The earlier resolution was premature; this is the same incident."
        }
    if kind == "resolve":
        ended = alert.get("ended_at") or "now"
        return {"text": f"✅ Resolved: {alert.get('alert_name', 'alert')} cleared at {ended}."}
    return {
        "text": f"🔁 Still firing: {occurrence_count} alerts so far for {alert.get('service_name', '?')}."
    }


SEVERITY_ICON = {"block": "⛔", "warn": "⚠️", "info": "ℹ️"}


class ChatDelivery:
    """Posts to the space's webhook. Raises ``DeliveryError`` so Cloud Tasks retries."""

    def __init__(self, secrets: Any, post: Callable[..., Any] | None = None, timeout_s: float = 10.0):
        self.secrets = secrets
        self.timeout_s = timeout_s
        if post is None:
            import requests

            post = requests.post
        self._post = post

    def send(self, thread_key: str, message: dict[str, Any]) -> dict[str, Any]:
        url = thread_url(self.secrets.get(WEBHOOK_SECRET), thread_key)
        try:
            response = self._post(url, json=message, timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — network errors; the URL must not be logged
            raise DeliveryError(f"chat webhook unreachable: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise DeliveryError(f"chat webhook returned HTTP {response.status_code}")
        body = response.json() if hasattr(response, "json") else {}
        return {
            "channel": "google_chat",
            "message": body.get("name"),
            "thread": (body.get("thread") or {}).get("name"),
        }

    # pipeline.Deliver
    def deliver_brief(self, task: Any, brief_id: str, brief: dict[str, Any]) -> dict[str, Any]:
        related_to = brief.get("related_to")
        if related_to:
            record = self.send(related_to, render_related(brief_id, brief))
            log.info(
                "related brief posted to chat", event="chat_related", brief_id=brief_id, related_to=related_to
            )
            return {**record, "related_to": related_to}
        record = self.send(brief_id, render_brief(brief_id, brief))
        log.info("brief posted to chat", event="chat_delivered", brief_id=brief_id)
        return record

    # app.Notify
    def notify(self, task: Any, brief_id: str) -> None:
        alert = task.alert.model_dump(mode="json")
        self.send(brief_id, render_update(task.kind, task.occurrence_count, alert))
        log.info("occurrence posted to chat", event=f"chat_{task.kind}", brief_id=brief_id)
