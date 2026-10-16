"""INGEST — Cloud Run, private.

Pub/Sub push (OIDC) -> authenticate + bind tenant -> normalise -> de-duplicate
-> enqueue analysis -> 2xx. Target p95 < 1 s.

Status codes are chosen for Pub/Sub's redelivery semantics: any non-2xx is a
nack, so the message is retried with backoff and dead-lettered after the
subscription's maximum delivery attempts.

* 204  processed (including suppressed duplicates)
* 400  unusable payload -> retried, then dead-lettered for inspection
* 401/403  authentication or tenant binding failed
* 404  unknown tenant
* 503  queue unavailable -> Pub/Sub redelivers; the group remembers that its
       analysis was never enqueued, so the retry enqueues it (never lose one)
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flask import Flask, jsonify, request

from asre.core import logging as slog
from asre.core.auth import AuthError, TokenVerifier, authenticate, insecure_local_mode_allowed
from asre.core.dedup import Action, Decision, DedupService, incident_id_for
from asre.core.keys import group_key
from asre.core.models import Alert
from asre.core.queue import TaskQueue, task_id_for
from asre.core.tenants import TenantRegistry, route
from asre.ingest.cloud_monitoring import PayloadError, decode_pubsub_envelope, to_alert

log = slog.get_logger("ingest")

TASK_VERSION = 1
_ENQUEUE_ACTIONS = {Action.ANALYSE, Action.UPDATE, Action.RESOLVE, Action.REOPEN}


@dataclass
class IngestDeps:
    tenants: TenantRegistry
    dedup: DedupService
    queue: TaskQueue
    verifier: TokenVerifier | None
    audience: str
    dedup_timeout_s: float = 0.5
    # Until the first de-duplication succeeds (cold instance: new Firestore
    # channel and token), allow this much instead of dedup_timeout_s.
    dedup_first_timeout_s: float = 3.0
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    readiness_checks: tuple[Callable[[], None], ...] = ()


def _task_payload(alert: Alert, decision: Decision | None, uncertain: bool, now: datetime) -> dict[str, Any]:
    key = (
        decision.group_key
        if decision
        else group_key(alert.customer_id, alert.service_name, alert.family, alert.environment)
    )
    # Without a decision, use the incident ID dedup would have given a new group
    # for this alert (same key, same ``now``). If the timed-out dedup call still
    # creates that group, a later takeover carries the same ID, so ANALYSE derives
    # the same Brief ID and drops the duplicate instead of writing a second Brief.
    incident_id = decision.incident_id if decision else incident_id_for(key, now)
    return {
        "version": TASK_VERSION,
        "kind": str(decision.action) if decision else str(Action.ANALYSE),
        "customer_id": alert.customer_id,
        "group_key": key,
        "incident_id": incident_id,
        "occurrence_count": decision.occurrence_count if decision else 1,
        "dedup_uncertain": uncertain,
        "alert": alert.model_dump(mode="json"),
    }


def create_app(deps: IngestDeps) -> Flask:
    app = Flask(__name__)
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dedup")
    warm = {"dedup": False}

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
        # Liveness only: no dependency checks, so a Firestore blip cannot make
        # Cloud Run restart every instance (P1-8).
        return jsonify(status="ok"), 200

    @app.get("/ready")
    def ready() -> Any:
        failures = []
        if len(deps.tenants) == 0:
            failures.append("no tenants configured")
        for check in deps.readiness_checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001 — report every failing dependency
                failures.append(f"{getattr(check, '__name__', 'check')}: {exc}")
        if failures:
            return jsonify(status="not_ready", failures=failures), 503
        return jsonify(status="ready"), 200

    @app.post("/pubsub/<customer_id>")
    def pubsub_push(customer_id: str) -> Any:
        tenant = deps.tenants.get(customer_id)
        if tenant is None:
            log.warning("unknown tenant", event="ingest_rejected", reason="unknown_tenant")
            return jsonify(error="unknown tenant"), 404
        slog.bind(customer_id=customer_id)

        if deps.verifier is not None:
            try:
                authenticate(request.headers, deps.verifier, deps.audience, tenant.push_service_account)
            except AuthError as exc:
                log.warning("push rejected", event="ingest_rejected", reason=exc.reason)
                return jsonify(error="unauthorised"), exc.status

        envelope = request.get_json(silent=True)
        try:
            if not isinstance(envelope, dict):
                raise PayloadError("body is not a JSON object")
            notification, _attributes, subscription = decode_pubsub_envelope(envelope)
            if tenant.subscription and subscription != tenant.subscription:
                log.warning("subscription mismatch", event="ingest_rejected", reason="subscription_mismatch")
                return jsonify(error="subscription does not belong to this tenant"), 403
            alert = to_alert(notification, customer_id, tenant.environment)
        except PayloadError as exc:
            log.warning("payload rejected", event="ingest_rejected", reason=str(exc))
            return jsonify(error="invalid payload"), 400

        # Every later log line of this request carries the service, so one incident
        # can be followed from receipt to decision (and the smoke test can filter on it).
        slog.bind(service_name=alert.service_name)
        assignment = route(alert, tenant)
        log.info(
            "alert received",
            event="alerts_received",
            alert_type=str(alert.alert_type),
            classified_by=alert.classified_by,
            status=str(alert.status),
            team=assignment.team,
        )

        now = deps.clock()
        decision: Decision | None = None
        uncertain = False
        try:
            budget = (
                deps.dedup_timeout_s
                if warm["dedup"]
                else max(deps.dedup_first_timeout_s, deps.dedup_timeout_s)
            )
            decision = executor.submit(deps.dedup.process, alert, now).result(timeout=budget)
            warm["dedup"] = True
        except FutureTimeout:
            uncertain = True
            log.warning("dedup timed out; enqueueing anyway", event="dedup_uncertain")
        except Exception as exc:  # noqa: BLE001 — never drop an alert because the dedup store failed
            uncertain = True
            log.error("dedup failed; enqueueing anyway", event="dedup_uncertain", error=str(exc))

        if decision is not None:
            slog.bind(group_key=decision.group_key, incident_id=decision.incident_id)
            if decision.action is Action.ANALYSE and decision.occurrence_count == 1:
                log.info("alert group created", event="groups_created")

        if uncertain or (decision is not None and decision.action in _ENQUEUE_ACTIONS):
            payload = _task_payload(alert, decision, uncertain, now)
            # One named task per incident hand-off, so a retry after a timed-out
            # (but successful) enqueue is recognised instead of duplicated.
            # Updates are periodic and not retried, so they stay unnamed.
            task_id = (
                task_id_for(payload["kind"], alert.customer_id, payload["group_key"], payload["incident_id"])
                if payload["kind"] in (str(Action.ANALYSE), str(Action.RESOLVE)) and payload["incident_id"]
                else None
            )
            try:
                deps.queue.enqueue(payload, slog.current_trace_header(), task_id=task_id)
            except Exception as exc:  # noqa: BLE001
                log.error("enqueue failed", event="enqueue_failed", error=str(exc))
                if decision is not None and decision.action is Action.ANALYSE:
                    try:
                        deps.dedup.release_lease(decision, deps.clock())
                    except Exception as release_exc:  # noqa: BLE001 — the lease lapses on its own
                        log.warning("could not release enqueue lease", error=str(release_exc))
                return jsonify(error="temporarily unavailable"), 503
            if decision is not None and decision.action is Action.ANALYSE:
                try:
                    deps.dedup.mark_enqueued(decision)
                except Exception as exc:  # noqa: BLE001 — worst case is a duplicate task, which ANALYSE absorbs
                    log.warning("could not mark analysis enqueued", error=str(exc))
            if decision is not None and decision.action is Action.RESOLVE:
                try:
                    deps.dedup.mark_resolution_enqueued(decision)
                except Exception as exc:  # noqa: BLE001 — worst case is a repeated resolution task
                    log.warning("could not mark resolution enqueued", error=str(exc))

        action = "uncertain" if uncertain else str(decision.action) if decision else "none"
        log.info("alert processed", event="alert_processed", action=action)
        return "", 204

    return app
