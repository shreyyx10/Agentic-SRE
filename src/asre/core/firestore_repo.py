"""Firestore implementation of ``GroupRepository``.

``create()`` is Firestore's native compare-and-set: it fails with a 409 if the
document exists. Transactions use ``@firestore.transactional``; the client may
re-run the function if a concurrent write touches a document it read, which is
why transaction bodies in ``dedup.py`` are side-effect free.

Contention. In an alert storm most alerts are plain repeats, recorded with
``increment()`` (a server-side ``Increment`` transform, no transaction), so they
never contend. The transactions that remain are rare, but can still collide. The
client retries an aborted commit immediately, relying on the server to keep the
retried transaction's place in line; if that is not enough, the transaction is
re-run in further rounds with jittered exponential backoff. Only contention
(``Aborted``) is retried this way; any other error propagates at once.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

from google.api_core.exceptions import Aborted, Conflict, NotFound
from google.cloud import firestore

T = TypeVar("T")


class TransactionContention(RuntimeError):
    """A transaction kept losing to concurrent writers across every retry round."""


def is_contention(exc: BaseException) -> bool:
    """The client signals exhausted commit attempts as ValueError raised from Aborted."""
    return isinstance(exc, Aborted) or (isinstance(exc, ValueError) and isinstance(exc.__cause__, Aborted))


def run_with_contention_backoff(
    run_once: Callable[[], T],
    rounds: int,
    base_backoff_s: float,
    max_backoff_s: float,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> T:
    for round_no in range(rounds):
        try:
            return run_once()
        except (ValueError, Aborted) as exc:
            if not is_contention(exc):
                raise
            if round_no == rounds - 1:
                raise TransactionContention(f"transaction contended in all {rounds} rounds") from exc
            # Full jitter: a random point in [0, cap], so contenders spread out.
            sleep(jitter(0.0, min(max_backoff_s, base_backoff_s * 2**round_no)))
    raise AssertionError("unreachable")  # pragma: no cover


class FirestoreRepository:
    def __init__(
        self,
        client: firestore.Client,
        attempts_per_round: int = 5,
        contention_rounds: int = 8,
        base_backoff_s: float = 0.025,
        max_backoff_s: float = 1.0,
    ) -> None:
        self._db = client
        self._attempts = attempts_per_round
        self._rounds = contention_rounds
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s

    def create(self, path: str, data: dict[str, Any]) -> bool:
        try:
            self._db.document(path).create(data)
        except Conflict:  # AlreadyExists is a subclass
            return False
        return True

    def transaction(self, fn: Callable[[Any], T]) -> T:
        db = self._db

        @firestore.transactional
        def _run(transaction: firestore.Transaction) -> T:
            return fn(_FirestoreTx(db, transaction))

        return run_with_contention_backoff(
            lambda: _run(db.transaction(max_attempts=self._attempts)),
            self._rounds,
            self._base_backoff_s,
            self._max_backoff_s,
        )

    def get(self, path: str) -> dict[str, Any] | None:
        snap = self._db.document(path).get()
        return snap.to_dict() if snap.exists else None

    def update(self, path: str, fields: dict[str, Any]) -> None:
        try:
            self._db.document(path).update(fields)
        except NotFound as exc:  # same contract as the in-memory repository
            raise KeyError(path) from exc

    def increment(self, path: str, field: str, by: int, fields: dict[str, Any]) -> None:
        try:
            self._db.document(path).update({**fields, field: firestore.Increment(by)})
        except NotFound as exc:
            raise KeyError(path) from exc

    def latest(self, collection: str, order_by: str, limit: int) -> list[tuple[str, dict[str, Any]]]:
        """The newest documents of one collection (for dashboards), newest first."""
        query = (
            self._db.collection(collection)
            .order_by(order_by, direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [(snap.id, snap.to_dict() or {}) for snap in query.stream()]


class _FirestoreTx:
    def __init__(self, db: firestore.Client, transaction: firestore.Transaction) -> None:
        self._db = db
        self._t = transaction

    def get(self, path: str) -> dict[str, Any] | None:
        snap = self._db.document(path).get(transaction=self._t)
        return snap.to_dict() if snap.exists else None

    def set(self, path: str, data: dict[str, Any]) -> None:
        self._t.set(self._db.document(path), data)
