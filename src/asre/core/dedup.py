"""De-duplication: decide, per alert, whether it starts a new incident.

This is the module every correctness finding in MVP_Readiness_Review_v2 lived in.
Each fix is structural, and each has a regression test named after its finding:

* P1-1  Timestamps are written as ``datetime`` objects, never ``.isoformat()``
        strings, so Firestore's TTL policy can fire on ``expires_at``.
* P1-2  The customer-visible window (``dedup_until``) is a business rule
        checked here, in code, exactly. ``expires_at`` is storage hygiene only
        (best-effort TTL, 30 days). They are never the same field.
* P1-3  A new group is created with an atomic ``create()`` that fails if the
        document exists. A plain repeat inside the window (the bulk of any
        alert storm) is recorded with one atomic server-side increment, which
        cannot conflict with concurrent repeats. Every other decision (new
        incident after the window, time-based update, takeover, resolution)
        runs in a transaction whose body has no side effects. Fifty concurrent
        identical alerts make one group and count fifty occurrences.
* P1-4  Tenants are separated by document *path*
        (``customers/{customer_id}/alert_groups/{key}``) as well as by key.

The service talks to storage through ``GroupRepository`` so the same logic runs
against Firestore (production, and the emulator in CI) and an in-memory
repository (fast unit tests).
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from asre.core.keys import group_key
from asre.core.models import Alert, AlertStatus, GroupState

T = TypeVar("T")

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- storage


class Tx(Protocol):
    def get(self, path: str) -> dict[str, Any] | None: ...

    def set(self, path: str, data: dict[str, Any]) -> None: ...


class GroupRepository(Protocol):
    def create(self, path: str, data: dict[str, Any]) -> bool:
        """Atomically create ``path``. Return False if it already exists."""

    def transaction(self, fn: Callable[[Tx], T]) -> T:
        """Run ``fn`` in a serialisable transaction. ``fn`` may be retried: no side effects."""

    def get(self, path: str) -> dict[str, Any] | None: ...

    def update(self, path: str, fields: dict[str, Any]) -> None:
        """Merge ``fields`` into an existing document."""

    def increment(self, path: str, field: str, by: int, fields: dict[str, Any]) -> None:
        """Atomically add ``by`` to ``field`` and merge ``fields``, without a transaction.

        Concurrent increments never conflict (a server-side transform in Firestore).
        Raises KeyError if the document does not exist.
        """


class InMemoryRepository:
    """Thread-safe in-memory repository with the same create/transaction semantics."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, path: str, data: dict[str, Any]) -> bool:
        with self._lock:
            if path in self._docs:
                return False
            self._docs[path] = copy.deepcopy(data)
            return True

    def transaction(self, fn: Callable[[Tx], T]) -> T:
        with self._lock:
            tx = _BufferedTx(self._docs)
            result = fn(tx)
            self._docs.update(tx.writes)
            return result

    def get(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._docs.get(path)
            return copy.deepcopy(doc) if doc is not None else None

    def update(self, path: str, fields: dict[str, Any]) -> None:
        with self._lock:
            if path not in self._docs:
                raise KeyError(path)
            self._docs[path].update(copy.deepcopy(fields))

    def increment(self, path: str, field: str, by: int, fields: dict[str, Any]) -> None:
        with self._lock:
            if path not in self._docs:
                raise KeyError(path)
            doc = self._docs[path]
            doc.update(copy.deepcopy(fields))
            doc[field] = int(doc.get(field, 0)) + by

    def paths(self) -> list[str]:
        with self._lock:
            return sorted(self._docs)

    def latest(self, collection: str, order_by: str, limit: int) -> list[tuple[str, dict[str, Any]]]:
        prefix = collection.rstrip("/") + "/"
        with self._lock:
            docs = [
                (p[len(prefix) :], copy.deepcopy(d))
                for p, d in self._docs.items()
                if p.startswith(prefix) and "/" not in p[len(prefix) :] and d.get(order_by) is not None
            ]
        return sorted(docs, key=lambda kv: kv[1][order_by], reverse=True)[:limit]


class _BufferedTx:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self._docs = docs
        self.writes: dict[str, dict[str, Any]] = {}

    def get(self, path: str) -> dict[str, Any] | None:
        doc = self._docs.get(path)
        return copy.deepcopy(doc) if doc is not None else None

    def set(self, path: str, data: dict[str, Any]) -> None:
        self.writes[path] = copy.deepcopy(data)


# --------------------------------------------------------------------------- decisions


class Action(StrEnum):
    ANALYSE = "analyse"  # a new incident: produce a Brief
    UPDATE = "update"  # repeat inside the window, and an occurrence update is due
    SUPPRESS = "suppress"  # repeat inside the window, no update due
    RESOLVE = "resolve"  # resolution closed an open group
    REOPEN = "reopen"  # fired again soon after a resolution: the same incident, not a new one
    IGNORE = "ignore"  # resolution for nothing open


@dataclass(frozen=True)
class DedupConfig:
    window: timedelta = timedelta(minutes=60)
    retention: timedelta = timedelta(days=30)
    update_interval: timedelta = timedelta(minutes=15)
    # While one request is enqueueing a group's analysis, concurrent duplicates are
    # suppressed. If that request fails it releases the lease (or it lapses), and
    # the Pub/Sub redelivery — minimum backoff 30 s, longer than the lease — takes over.
    enqueue_lease: timedelta = timedelta(seconds=20)
    # A group that fires again this soon after it was resolved reopens the same
    # incident. Crash loops do this: Kubernetes backs off between restarts, a
    # "restarts in the last minute" alert sees a quiet minute and resolves, then
    # fires at the next crash. Zero turns reopening off.
    reopen_window: timedelta = timedelta(minutes=10)


@dataclass(frozen=True)
class Decision:
    action: Action
    group_key: str
    incident_id: str | None
    occurrence_count: int
    path: str


def group_path(customer_id: str, key: str) -> str:
    return f"customers/{customer_id}/alert_groups/{key}"


def history_path(customer_id: str, key: str, incident_id: str) -> str:
    return f"customers/{customer_id}/alert_groups/{key}/history/{incident_id}"


def incident_id_for(key: str, first_occurrence: datetime) -> str:
    return f"{key[:16]}-{int(first_occurrence.timestamp() * 1000)}"


class DedupService:
    def __init__(self, repo: GroupRepository, config: DedupConfig | None = None) -> None:
        self._repo = repo
        self._config = config or DedupConfig()

    # Public -----------------------------------------------------------------

    def process(self, alert: Alert, now: datetime) -> Decision:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        key = group_key(alert.customer_id, alert.service_name, alert.family, alert.environment)
        path = group_path(alert.customer_id, key)

        if alert.status is AlertStatus.RESOLVED:
            return self._repo.transaction(lambda tx: self._resolve(tx, path, key, now))

        fresh = self._new_group(alert, key, now)
        if self._repo.create(path, fresh):
            return Decision(Action.ANALYSE, key, fresh["incident_id"], 1, path)
        fast = self._plain_repeat(path, key, now)
        if fast is not None:
            return fast
        return self._repo.transaction(lambda tx: self._firing(tx, path, key, alert, now))

    def mark_enqueued(self, decision: Decision) -> None:
        """Record that the analysis task for ``decision`` was created."""
        self._repo.update(decision.path, {"analysis_enqueued": True})

    def mark_resolution_enqueued(self, decision: Decision) -> None:
        """Record that the resolution task for ``decision`` was created.

        Until then a closed group remembers that its resolution is still owed, so
        a redelivered resolution notification re-sends it instead of being ignored.
        """
        self._repo.update(decision.path, {"resolution_enqueued": True})

    def release_lease(self, decision: Decision, now: datetime) -> None:
        """Called when enqueueing failed, so the redelivered push can take over at once."""
        self._repo.update(decision.path, {"enqueue_lease_until": now})

    # Storm path: no transaction. ---------------------------------------------

    def _plain_repeat(self, path: str, key: str, now: datetime) -> Decision | None:
        """Record a repeat that needs no decision, or return None to decide in a transaction.

        Safe on a stale read: if the group changed after this read (resolved,
        archived, taken over), the only effect is one occurrence counted on the
        document that now holds the key, and one alert suppressed instead of
        triggering an update the next repeat will trigger. No incident is lost
        or duplicated: every decision that creates work runs in a transaction.
        """
        doc = self._repo.get(path)
        if (
            doc is None
            or doc["state"] != GroupState.OPEN
            or now >= doc["dedup_until"]
            or (not doc.get("analysis_enqueued", True) and now >= doc["enqueue_lease_until"])
            or now - doc["last_update_at"] >= self._config.update_interval
        ):
            return None
        try:
            self._repo.increment(
                path,
                "occurrence_count",
                1,
                {
                    "last_occurrence": now,
                    "dedup_until": now + self._config.window,
                    "expires_at": now + self._config.retention,
                },
            )
        except KeyError:  # removed since the read (e.g. by TTL): decide in a transaction
            return None
        # A lower bound under concurrency; the stored count is exact.
        return Decision(Action.SUPPRESS, key, doc["incident_id"], int(doc["occurrence_count"]) + 1, path)

    # Transaction bodies: read first, then write, and nothing else. ------------

    def _firing(self, tx: Tx, path: str, key: str, alert: Alert, now: datetime) -> Decision:
        doc = tx.get(path)
        if doc is None:  # removed between create() and here (e.g. by TTL)
            fresh = self._new_group(alert, key, now)
            tx.set(path, fresh)
            return Decision(Action.ANALYSE, key, fresh["incident_id"], 1, path)

        if doc["state"] == GroupState.OPEN and now < doc["dedup_until"]:
            count = int(doc["occurrence_count"]) + 1
            if not doc.get("analysis_enqueued", True) and now >= doc["enqueue_lease_until"]:
                # The group exists but its analysis was never enqueued (the request
                # that created it failed, and its lease has lapsed or been released).
                # Take over rather than suppress: never lose the analysis.
                tx.set(
                    path,
                    {
                        **doc,
                        "occurrence_count": count,
                        "last_occurrence": now,
                        "dedup_until": now + self._config.window,
                        "expires_at": now + self._config.retention,
                        "enqueue_lease_until": now + self._config.enqueue_lease,
                    },
                )
                return Decision(Action.ANALYSE, key, doc["incident_id"], count, path)
            due = now - doc["last_update_at"] >= self._config.update_interval
            updated = {
                **doc,
                "occurrence_count": count,
                "last_occurrence": now,
                "dedup_until": now + self._config.window,
                "expires_at": now + self._config.retention,
            }
            if due:
                updated["last_update_at"] = now
            tx.set(path, updated)
            return Decision(Action.UPDATE if due else Action.SUPPRESS, key, doc["incident_id"], count, path)

        resolved_at = doc.get("resolved_at")
        if (
            doc["state"] == GroupState.CLOSED
            and resolved_at is not None
            and now - resolved_at < self._config.reopen_window
        ):
            # Resolved moments ago and firing again: the resolution was premature.
            # Keep the incident (and its Brief and Chat thread) instead of starting over.
            count = int(doc["occurrence_count"]) + 1
            tx.set(
                path,
                {
                    **doc,
                    "state": str(GroupState.OPEN),
                    "occurrence_count": count,
                    "last_occurrence": now,
                    "last_update_at": now,
                    "dedup_until": now + self._config.window,
                    "expires_at": now + self._config.retention,
                    "resolved_at": None,
                    "reopened_count": int(doc.get("reopened_count") or 0) + 1,
                    "last_reopened_at": now,
                },
            )
            return Decision(Action.REOPEN, key, doc["incident_id"], count, path)

        # The previous incident is over (window lapsed, or it was resolved).
        # Archive it and start a new one — this is what makes the window exact.
        archived_state = GroupState.DORMANT if doc["state"] == GroupState.OPEN else doc["state"]
        tx.set(
            history_path(alert.customer_id, key, doc["incident_id"]),
            {**doc, "state": str(archived_state), "archived_at": now},
        )
        fresh = self._new_group(alert, key, now)
        tx.set(path, fresh)
        return Decision(Action.ANALYSE, key, fresh["incident_id"], 1, path)

    def _resolve(self, tx: Tx, path: str, key: str, now: datetime) -> Decision:
        doc = tx.get(path)
        if doc is not None and doc["state"] == GroupState.CLOSED and doc.get("resolution_enqueued") is False:
            # Closed by an earlier delivery whose enqueue failed: the resolution is
            # still owed, so send it again rather than ignore the redelivery.
            return Decision(Action.RESOLVE, key, doc["incident_id"], int(doc["occurrence_count"]), path)
        if doc is None or doc["state"] != GroupState.OPEN:
            return Decision(Action.IGNORE, key, doc["incident_id"] if doc else None, 0, path)
        tx.set(
            path,
            {
                **doc,
                "state": str(GroupState.CLOSED),
                "resolved_at": now,
                "dedup_until": now,
                "expires_at": now + self._config.retention,
                "resolution_enqueued": False,  # set by mark_resolution_enqueued()
            },
        )
        return Decision(Action.RESOLVE, key, doc["incident_id"], int(doc["occurrence_count"]), path)

    # Helpers ------------------------------------------------------------------

    def _new_group(self, alert: Alert, key: str, now: datetime) -> dict[str, Any]:
        # Every timestamp below is a datetime object. Firestore stores these as
        # Timestamps, which is the only type its TTL policy acts on (P1-1).
        return {
            "schema_version": SCHEMA_VERSION,
            "group_key": key,
            "incident_id": incident_id_for(key, now),
            "customer_id": alert.customer_id,
            "service_name": alert.service_name,
            "environment": alert.environment,
            "alert_family": str(alert.family),
            "alert_type": str(alert.alert_type),
            "alert_name": alert.alert_name,
            "source_incident_id": alert.source_incident_id,
            "state": str(GroupState.OPEN),
            "occurrence_count": 1,
            "first_occurrence": now,
            "last_occurrence": now,
            "last_update_at": now,
            "dedup_until": now + self._config.window,  # business rule, exact (P1-2)
            "expires_at": now + self._config.retention,  # storage TTL only (P1-2)
            "resolved_at": None,
            "brief_id": None,
            "analysis_enqueued": False,  # set by mark_enqueued() once the task exists
            "enqueue_lease_until": now + self._config.enqueue_lease,
        }
