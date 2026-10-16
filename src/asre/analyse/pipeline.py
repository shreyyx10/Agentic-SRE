"""The ANALYSE pipeline: evidence → hypothesis → critic → Brief → delivery.

It runs behind ``AnalyseDeps.pipeline`` and is called with the Brief's lease
held, so only one worker runs it per Brief at a time.

Stages and what survives a retry:

1. **Evidence** (deterministic, parallel, sanitised) and **analysis** run once.
   Their result is written to the Brief as ``analysis`` before delivery, so a
   retry after a delivery failure re-sends the Brief without paying for the
   models again.
2. **Hypothesis**: Gemini 3.1 Pro, falling back to 3.8 Flash (ADR-0002). The
   answer is checked by ``asre.agent.contract.validate``: uncited claims are
   dropped, ``insufficient_evidence`` is a valid outcome.
3. **Critic**: 3.8 Flash checks a complete hypothesis against the same pack. If
   the critic fails, the Brief ships without a review (recorded as such).
4. **Degraded mode**: if no model answers usefully, the Brief still ships with
   the evidence, the runbook link and the default first steps for the alert
   type (``asre.agent.defaults``). No silent failure.
5. **Delivery** (Google Chat, later): an error here propagates, so Cloud Tasks
   retries; the stored analysis is reused.

Brief ``status`` is the lifecycle (``gathering`` → ``analysed`` → ``delivered``,
or ``ready`` when no delivery channel is configured, or ``failed``). The
diagnosis itself is ``outcome``: ``complete``, ``insufficient_evidence`` or ``degraded``.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from asre.agent.contract import Outcome, response_schema, validate
from asre.agent.critic import (
    CRITIC_PROMPT_VERSION,
    CRITIC_SYSTEM_PROMPT,
    critic_schema,
    parse_review,
    render_critic_prompt,
)
from asre.agent.defaults import default_actions
from asre.agent.model import ModelError, ModelResult
from asre.agent.prompt import PROMPT_VERSION, SYSTEM_PROMPT, render_user_prompt
from asre.analyse.app import AnalyseTask, brief_path
from asre.core.dedup import GroupRepository
from asre.core.logging import get_logger
from asre.core.tenants import TenantRegistry
from asre.evidence.context import context_for
from asre.evidence.gather import EvidenceConfig, GatherResult, gather
from asre.evidence.telemetry import Telemetry

log = get_logger(__name__)


class Generates(Protocol):
    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> ModelResult: ...


# Briefs that cite the same deployment change this close together share one Chat thread.
CORRELATION_WINDOW = timedelta(minutes=30)
CAUSE_RETENTION = timedelta(days=30)


def cause_keys(analysis: dict[str, Any]) -> list[str]:
    """The deployment changes a complete diagnosis cites as evidence.

    Deterministic: the keys come from the GKE audit-log entry (its insertId and
    time), so two Briefs about one change cite the identical key.
    """
    if analysis.get("outcome") != "complete":
        return []
    evidence = (analysis.get("hypothesis") or {}).get("evidence") or []
    return sorted(
        {e["citation"] for e in evidence if e.get("citation", "").startswith("deploy:")} - {"deploy:none"}
    )


def cause_path(customer_id: str, key: str) -> str:
    return f"customers/{customer_id}/causes/{hashlib.sha256(key.encode()).hexdigest()[:32]}"


# Delivery: (task, brief_id, brief record) -> delivery record. Raises to make Cloud Tasks retry.
Deliver = Callable[[AnalyseTask, str, dict[str, Any]], dict[str, Any]]


@dataclass
class AnalysisPipeline:
    repo: GroupRepository
    tenants: TenantRegistry
    telemetry: Callable[[str | None], Telemetry]  # reader identity -> client
    hypothesis: Generates
    critic: Generates | None = None
    deliver: Deliver | None = None
    evidence_config: EvidenceConfig = field(default_factory=EvidenceConfig)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __call__(self, task: AnalyseTask, brief_id: str) -> None:
        path = brief_path(task.customer_id, brief_id)
        brief = self.repo.get(path) or {}
        analysis = brief.get("analysis")
        if analysis is None:
            tenant = self.tenants.get(task.customer_id)
            if tenant is None:
                self._update(path, status="failed", failure="no configuration for this customer")
                log.error("unknown tenant", event="brief_failed", brief_id=brief_id)
                return
            self._update(path, status="gathering")
            try:
                ctx = context_for(task.alert, tenant, self.clock())
            except ValueError as exc:
                self._update(path, status="failed", failure=str(exc))
                log.error(
                    "invalid tenant configuration", event="brief_failed", brief_id=brief_id, error=str(exc)
                )
                return
            evidence = gather(ctx, self.telemetry(tenant.reader_service_account), config=self.evidence_config)
            runbook = ctx.profile.runbook if ctx.profile else None
            analysis = self.analyse(task, evidence, runbook)
            self._update(
                path,
                status="analysed",
                analysis=analysis,
                evidence=evidence.as_record(),
                evidence_items=[
                    {"key": i.key, "source": i.source, "text": i.text} for i in evidence.pack.items
                ],
                context_quality=evidence.pack.context_quality,
                model_id=(analysis.get("model") or {}).get("model"),
                prompt_version=PROMPT_VERSION,
            )
            brief = self.repo.get(path) or brief

        if brief.get("correlated") is None:
            related = self._correlate(task.customer_id, brief_id, analysis)
            self._update(path, correlated=True, **(related or {}))
            brief = self.repo.get(path) or brief

        if self.deliver is None:
            self._update(path, status="ready")
            log.info(
                "brief ready (no delivery channel)",
                event="brief_ready",
                brief_id=brief_id,
                outcome=analysis["outcome"],
            )
            return
        delivery = self.deliver(task, brief_id, brief)  # raises -> Cloud Tasks retries, analysis reused
        self._update(path, status="delivered", delivery=delivery)
        log.info("brief delivered", event="briefs_delivered", brief_id=brief_id, outcome=analysis["outcome"])

    # ------------------------------------------------------------------ correlation

    def _correlate(self, customer_id: str, brief_id: str, analysis: dict[str, Any]) -> dict[str, Any] | None:
        """Claim each cited deployment change; if another recent Brief already holds one, join its thread.

        ``create`` is atomic, so of several Briefs about one change exactly one becomes
        the thread; the others are posted as related replies in it.
        """
        now = self.clock()
        for key in cause_keys(analysis):
            path = cause_path(customer_id, key)
            record = {
                "brief_id": brief_id,
                "cause_key": key,
                "created_at": now,
                "expires_at": now + CAUSE_RETENTION,
            }
            if self.repo.create(path, record):
                continue
            holder = self.repo.get(path) or {}
            if holder.get("brief_id") == brief_id:
                continue
            created = holder.get("created_at")
            if created is not None and now - created < CORRELATION_WINDOW:
                log.info(
                    "same cause as an open incident",
                    event="brief_correlated",
                    brief_id=brief_id,
                    related_to=holder["brief_id"],
                    cause_key=key,
                )
                return {"related_to": holder["brief_id"], "cause_key": key}
            self.repo.update(path, record)  # an old incident's change: this Brief starts the thread now
        return None

    # ------------------------------------------------------------------ analysis

    def analyse(self, task: AnalyseTask, evidence: GatherResult, runbook: str | None) -> dict[str, Any]:
        started = time.monotonic()
        pack = evidence.pack
        user_prompt = render_user_prompt(pack)
        record: dict[str, Any] = {
            "outcome": Outcome.DEGRADED.value,
            "hypothesis": None,
            "confidence": 0.0,
            "disputed": False,
            "dropped_claims": 0,
            "reason": None,
            "model": None,
            "critic": None,
            "default_actions": None,
            "runbook": runbook,
            "prompt_version": PROMPT_VERSION,
        }

        try:
            answer = self.hypothesis.generate(SYSTEM_PROMPT, user_prompt, response_schema())
        except ModelError as exc:
            record["reason"] = f"model unavailable: {str(exc)[:200]}"
            log.error("no model answered; degraded brief", event="model_unavailable", error=str(exc)[:300])
            return self._degraded(record, task, started)

        record["model"] = answer.as_record()
        result = validate(answer.text, pack.keys)
        record["outcome"] = result.outcome.value
        record["dropped_claims"] = result.dropped_claims
        record["reason"] = result.reason
        if result.output is None:  # unparseable or off-schema
            log.warning(
                "model answer unusable; degraded brief", event="model_output_invalid", reason=result.reason
            )
            return self._degraded(record, task, started)

        record["hypothesis"] = result.output.model_dump(mode="json")
        record["confidence"] = result.output.confidence
        if result.outcome is Outcome.INSUFFICIENT_EVIDENCE:
            record["default_actions"] = default_actions(task.alert.alert_type, task.alert.service_name)
        elif self.critic is not None:
            record["critic"] = self._review(user_prompt, result.output, pack.keys, record)

        record["analysis_ms"] = int((time.monotonic() - started) * 1000)
        log.info(
            "analysis finished",
            event="analysis_finished",
            outcome=record["outcome"],
            model=answer.model,
            fallback_used=answer.fallback_used,
            confidence=record["confidence"],
            disputed=record["disputed"],
            dropped_claims=record["dropped_claims"],
        )
        return record

    def _review(self, user_prompt: str, output: Any, keys: Any, record: dict[str, Any]) -> dict[str, Any]:
        assert self.critic is not None
        try:
            answer = self.critic.generate(
                CRITIC_SYSTEM_PROMPT, render_critic_prompt(user_prompt, output), critic_schema()
            )
        except ModelError as exc:
            log.warning(
                "critic unavailable; brief ships unreviewed", event="critic_unavailable", error=str(exc)[:200]
            )
            return {"status": "unavailable", "error": str(exc)[:200], "prompt_version": CRITIC_PROMPT_VERSION}
        review = parse_review(answer.text, keys)
        if review is None:
            return {"status": "invalid", "model": answer.as_record(), "prompt_version": CRITIC_PROMPT_VERSION}
        record["confidence"], record["disputed"] = review.apply(record["confidence"])
        return {
            "status": "reviewed",
            **review.as_record(),
            "model": answer.as_record(),
            "prompt_version": CRITIC_PROMPT_VERSION,
        }

    def _degraded(self, record: dict[str, Any], task: AnalyseTask, started: float) -> dict[str, Any]:
        record["outcome"] = Outcome.DEGRADED.value
        record["default_actions"] = default_actions(task.alert.alert_type, task.alert.service_name)
        record["analysis_ms"] = int((time.monotonic() - started) * 1000)
        return record

    def _update(self, path: str, **fields: Any) -> None:
        self.repo.update(path, {**fields, "updated_at": self.clock()})
