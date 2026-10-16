"""ANALYSE — Cloud Run, private, invoked only by Cloud Tasks (OIDC).

Week 1 scope: receive the task, create the Incident Brief record
idempotently in ``pending`` state, and link it to its alert group. Week 2 adds
the evidence agents, Gemini hypothesis, render and delivery (T4.1–T4.9) behind
the ``pipeline`` hook, which is where the degraded-mode handling lives.

Cloud Tasks retries any non-2xx, so the handler is idempotent: the Brief ID is
derived from the incident, and ``create()`` refuses a second record.

Retries must also be able to *finish* a Brief. With a pipeline configured, a
Brief that exists but has not reached a terminal status is resumed, not skipped.
One worker at a time holds a lease on it (longer than the request timeout); a
task that finds the lease held gets a non-2xx, so Cloud Tasks retries it later,
by which time the Brief is either finished or its lease has lapsed. A pipeline
error releases the lease and returns non-2xx, so Cloud Tasks backs off and retries.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from flask import Flask, jsonify, request
from pydantic import BaseModel, ConfigDict, ValidationError

from asre.core import logging as slog
from asre.core.auth import AuthError, TokenVerifier, authenticate, insecure_local_mode_allowed
from asre.core.dedup import Action, DedupService, GroupRepository, group_path
from asre.core.models import Alert
from asre.core.secrets import SecretReader

log = slog.get_logger("analyse")


class AnalyseTask(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: int
    kind: Literal["analyse", "update", "resolve", "reopen"]
    customer_id: str
    group_key: str
    incident_id: str | None = None
    occurrence_count: int = 1
    dedup_uncertain: bool = False
    alert: Alert


def brief_id_for(task: AnalyseTask) -> str:
    anchor = task.incident_id or f"uncertain:{task.alert.source_incident_id}"
    digest = hashlib.sha256(f"{task.customer_id}|{task.group_key}|{anchor}".encode()).hexdigest()
    return f"brf_{digest[:24]}"


def brief_path(customer_id: str, brief_id: str) -> str:
    return f"customers/{customer_id}/briefs/{brief_id}"


# Statuses the pipeline sets when a Brief is finished. Anything else (``pending``,
# ``gathering``, ``analysed``) is unfinished and may be resumed by a retry.
# ``ready`` = analysed, no delivery channel configured. The older outcome-named
# statuses stay terminal so Briefs written before the pipeline existed are left alone.
TERMINAL_STATUSES = frozenset({"delivered", "ready", "failed", "degraded", "insufficient_evidence"})


# The week-2 pipeline: (task, brief_id) -> None. It gathers evidence, calls the
# model, renders and delivers, updating the Brief record as it goes.
Pipeline = Callable[[AnalyseTask, str], None]

# Occurrence updates and resolutions: (task, brief_id) -> None. Posts to the
# incident's Chat thread; raises to make Cloud Tasks retry.
Notify = Callable[[AnalyseTask, str], None]

# Predictive scan: now -> forecast alerts across every tenant (asre.predict.scan.Scanner).
Scan = Callable[[datetime], list[Alert]]


@dataclass
class AnalyseDeps:
    repo: GroupRepository
    verifier: TokenVerifier | None
    audience: str
    tasks_invoker_sa: str
    pipeline: Pipeline | None = None
    notify: Notify | None = None
    # Secret Manager reader for week-2 delivery (Google Chat webhook); values are
    # fetched on use, never held in configuration.
    secrets: SecretReader | None = None
    # How long one worker owns a Brief while running the pipeline. Must exceed
    # ANALYSE's request timeout, so a lease never lapses under a live request.
    pipeline_lease: timedelta = timedelta(seconds=330)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    # Predictive mode (Cloud Scheduler -> /tasks/scan). Forecasts are de-duplicated like
    # alerts, so a trend that persists across scans is one Brief, not one every scan.
    scan: Scan | None = None
    dedup: DedupService | None = None


def create_app(deps: AnalyseDeps) -> Flask:
    app = Flask(__name__)

    if deps.verifier is None and not insecure_local_mode_allowed():
        raise RuntimeError("a token verifier is required outside local development")

    @app.before_request
    def _bind_trace() -> None:
        slog.bind_request(
            slog.parse_trace_header(
                request.headers.get("X-Cloud-Trace-Context"), request.headers.get("traceparent")
            )
        )

    @app.teardown_request
    def _clear(_exc: BaseException | None) -> None:
        slog.clear()

    @app.get("/health")
    def health() -> Any:
        return jsonify(status="ok"), 200

    @app.post("/tasks/analyse")
    def analyse() -> Any:
        if deps.verifier is not None:
            try:
                authenticate(request.headers, deps.verifier, deps.audience, deps.tasks_invoker_sa)
            except AuthError as exc:
                log.warning("task rejected", event="task_rejected", reason=exc.reason)
                return jsonify(error="unauthorised"), exc.status

        try:
            task = AnalyseTask.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            # A malformed task will never succeed: acknowledge it (2xx) so Cloud Tasks
            # stops retrying, and log loudly.
            log.error("malformed task dropped", event="task_malformed", error=str(exc.errors()[0]))
            return jsonify(status="dropped"), 200

        slog.bind(
            customer_id=task.customer_id,
            service_name=task.alert.service_name,
            group_key=task.group_key,
            incident_id=task.incident_id,
        )
        body, status = run_task(task)
        return jsonify(body), status

    @app.post("/tasks/scan")
    def scan() -> Any:
        """Predictive mode: forecast every tenant's services; a new forecast becomes a Brief."""
        if deps.verifier is not None:
            try:
                authenticate(request.headers, deps.verifier, deps.audience, deps.tasks_invoker_sa)
            except AuthError as exc:
                log.warning("scan rejected", event="task_rejected", reason=exc.reason)
                return jsonify(error="unauthorised"), exc.status
        if deps.scan is None or deps.dedup is None:
            return jsonify(status="disabled"), 200
        now = deps.clock()
        results = []
        for alert in deps.scan(now):
            slog.bind(customer_id=alert.customer_id, service_name=alert.service_name)
            decision = deps.dedup.process(alert, now)
            if decision.action is not Action.ANALYSE:
                # The same forecast as an earlier scan: its Brief already exists.
                results.append({"service": alert.service_name, "action": str(decision.action)})
                continue
            task = AnalyseTask(
                version=1,
                kind="analyse",
                customer_id=alert.customer_id,
                group_key=decision.group_key,
                incident_id=decision.incident_id,
                alert=alert,
            )
            log.info("forecast", event="forecast_new", summary=alert.summary)
            body, status = run_task(task)
            if status == 200:
                deps.dedup.mark_enqueued(decision)
            else:
                # Not finished: let the next scan pick the forecast up again.
                with contextlib.suppress(Exception):
                    deps.dedup.release_lease(decision, deps.clock())
            results.append({"service": alert.service_name, "action": "analyse", **body})
        log.info("predictive scan finished", event="scan_finished", forecasts=len(results))
        return jsonify(status="scanned", forecasts=results), 200

    def run_task(task: AnalyseTask) -> tuple[dict[str, Any], int]:
        if task.kind != "analyse":
            log.info("occurrence event", event=f"group_{task.kind}", occurrence_count=task.occurrence_count)
            brief_id = brief_id_for(task)
            if task.kind == "reopen":
                # The earlier resolution was premature: the incident is live again.
                with contextlib.suppress(KeyError):
                    deps.repo.update(
                        brief_path(task.customer_id, brief_id),
                        {"resolved_at": None, "reopened_at": deps.clock(), "updated_at": deps.clock()},
                    )
            if task.kind == "resolve":
                # No Brief may exist for this incident (e.g. created before the pipeline existed).
                with contextlib.suppress(KeyError):
                    deps.repo.update(
                        brief_path(task.customer_id, brief_id),
                        {"resolved_at": task.alert.ended_at or deps.clock(), "updated_at": deps.clock()},
                    )
            if deps.notify is not None:
                # A Brief that joined another incident's thread (same cause) keeps posting there.
                brief = deps.repo.get(brief_path(task.customer_id, brief_id)) or {}
                thread = brief.get("related_to") or brief_id
                try:
                    deps.notify(task, thread)
                except Exception as exc:  # noqa: BLE001 — retried by Cloud Tasks
                    log.warning("occurrence notice failed; will retry", event="notify_failed", error=str(exc))
                    return {"status": "retry", "brief_id": brief_id}, 503
            return {"status": "noted", "brief_id": brief_id}, 200

        brief_id = brief_id_for(task)
        path = brief_path(task.customer_id, brief_id)
        now = deps.clock()
        lease_until = now + deps.pipeline_lease if deps.pipeline is not None else None
        created = deps.repo.create(
            path,
            {
                "brief_id": brief_id,
                "customer_id": task.customer_id,
                "group_key": task.group_key,
                "incident_id": task.incident_id,
                "status": "pending",
                "dedup_uncertain": task.dedup_uncertain,
                "created_at": now,
                "updated_at": now,
                "alert": task.alert.model_dump(mode="json"),
                "context_quality": None,
                "model_id": None,
                "prompt_version": None,
                "attempts": 1 if deps.pipeline is not None else 0,
                "lease_until": lease_until,
            },
        )

        if created:
            _link_group(task, brief_id)
            log.info("brief created", event="brief_pending", brief_id=brief_id)
        elif deps.pipeline is None:
            log.info("brief already exists (task retry)", event="brief_duplicate_task", brief_id=brief_id)
            return {"status": "exists", "brief_id": brief_id}, 200
        else:
            claim = deps.repo.transaction(lambda tx: _claim(tx, path, now))
            if claim == "done":
                log.info(
                    "brief already finished (task retry)", event="brief_duplicate_task", brief_id=brief_id
                )
                return {"status": "exists", "brief_id": brief_id}, 200
            if claim == "busy":
                # Another worker holds the lease. Not 2xx: if that worker dies, this
                # retry is what finishes the Brief once the lease lapses.
                log.info("brief in progress elsewhere", event="brief_in_progress", brief_id=brief_id)
                return {"status": "in_progress", "brief_id": brief_id}, 409
            log.info("resuming unfinished brief", event="brief_resumed", brief_id=brief_id)

        if deps.pipeline is not None:
            try:
                deps.pipeline(task, brief_id)
            except Exception as exc:  # noqa: BLE001 — every pipeline failure is retried by Cloud Tasks
                log.error(
                    "pipeline failed; will retry", event="pipeline_failed", brief_id=brief_id, error=str(exc)
                )
                try:
                    deps.repo.update(path, {"lease_until": deps.clock(), "updated_at": deps.clock()})
                except Exception as release_exc:  # noqa: BLE001 — the lease lapses on its own
                    log.warning("could not release brief lease", error=str(release_exc))
                return {"status": "retry", "brief_id": brief_id}, 503

        return {"status": "accepted", "brief_id": brief_id}, 200

    def _link_group(task: AnalyseTask, brief_id: str) -> None:
        """Point the alert group at this Brief, only if the group is this incident.

        An uncertain task carries the incident ID dedup *would* have assigned. If
        the group actually holds a different incident, linking would overwrite
        that incident's Brief, so the link is skipped.
        """
        if task.incident_id is None:
            return
        gpath = group_path(task.customer_id, task.group_key)

        def link(tx: Any) -> str:
            group = tx.get(gpath)
            if group is None:
                return "missing"
            if group.get("incident_id") != task.incident_id:
                return "other_incident"
            tx.set(gpath, {**group, "brief_id": brief_id})
            return "linked"

        outcome = deps.repo.transaction(link)
        if outcome != "linked":
            log.warning("brief not linked to group", brief_id=brief_id, reason=outcome)

    def _claim(tx: Any, path: str, now: datetime) -> str:
        brief = tx.get(path)
        if brief is None or brief.get("status") in TERMINAL_STATUSES:
            return "done"
        held = brief.get("lease_until")
        if held is not None and now < held:
            return "busy"
        tx.set(
            path,
            {
                **brief,
                "lease_until": now + deps.pipeline_lease,
                "attempts": int(brief.get("attempts") or 0) + 1,
                "updated_at": now,
            },
        )
        return "claimed"

    return app
