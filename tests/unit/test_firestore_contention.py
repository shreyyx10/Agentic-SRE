import pytest
from google.api_core.exceptions import Aborted

from asre.core.firestore_repo import TransactionContention, is_contention, run_with_contention_backoff


def _exhausted() -> ValueError:
    # What google-cloud-firestore raises when a transaction exhausts max_attempts.
    try:
        raise ValueError("Failed to commit transaction in 5 attempts.") from Aborted("contention")
    except ValueError as exc:
        return exc


def test_contention_is_recognised_and_other_errors_are_not():
    assert is_contention(_exhausted()) and is_contention(Aborted("x"))
    assert not is_contention(ValueError("a bug in the transaction body"))
    assert not is_contention(RuntimeError("x"))


def test_contended_transaction_is_retried_with_growing_jittered_backoff():
    calls, sleeps, caps = [], [], []

    def run_once():
        calls.append(1)
        if len(calls) < 4:
            raise _exhausted()
        return "committed"

    def jitter(lo, hi):
        caps.append(hi)
        return hi / 2

    result = run_with_contention_backoff(run_once, 8, 0.025, 0.06, sleep=sleeps.append, jitter=jitter)
    assert result == "committed" and len(calls) == 4
    assert caps == [0.025, 0.05, 0.06]  # doubles, then capped at max_backoff
    assert sleeps == [0.0125, 0.025, 0.03]


def test_gives_up_after_the_last_round_with_a_clear_error():
    sleeps = []
    with pytest.raises(TransactionContention) as exc:
        run_with_contention_backoff(
            lambda: (_ for _ in ()).throw(_exhausted()), 3, 0.01, 1, sleep=sleeps.append
        )
    assert len(sleeps) == 2 and isinstance(exc.value.__cause__, ValueError)


def test_non_contention_errors_propagate_immediately():
    sleeps = []
    with pytest.raises(ValueError, match="bug"):
        run_with_contention_backoff(
            lambda: (_ for _ in ()).throw(ValueError("bug")), 5, 0.01, 1, sleep=sleeps.append
        )
    assert sleeps == []


def test_alert_storm_simulation_every_contender_commits():
    """49 writers on one document, optimistic concurrency, immediate client retries.

    Without the jittered rounds most contenders exhaust their attempts (this is
    what the Firestore emulator showed in CI); with them, every write lands.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    state = {"version": 0, "count": 1}
    lock = threading.Lock()

    def client_transaction(max_attempts=5):  # mimics google-cloud-firestore's retry loop
        last = None
        for _ in range(max_attempts):
            seen = dict(state)
            time.sleep(0.003)  # one round trip
            with lock:
                if state["version"] == seen["version"]:
                    state.update(version=seen["version"] + 1, count=seen["count"] + 1)
                    return
            last = Aborted("contention")
        raise ValueError(f"Failed to commit transaction in {max_attempts} attempts.") from last

    errors = []

    def one(_):
        try:
            run_with_contention_backoff(client_transaction, 8, 0.025, 1.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(49) as pool:
        list(pool.map(one, range(49)))
    assert errors == [] and state["count"] == 50


def test_firestore_increment_uses_a_server_side_transform_and_maps_not_found():
    from google.api_core.exceptions import NotFound
    from google.cloud import firestore

    from asre.core.firestore_repo import FirestoreRepository

    written = {}

    class Doc:
        def __init__(self, path):
            self.path = path

        def update(self, fields):
            if self.path.endswith("missing"):
                raise NotFound("gone")
            written.update(fields)

    class Client:
        def document(self, path):
            return Doc(path)

    repo = FirestoreRepository(Client())
    repo.increment("customers/c/alert_groups/k", "occurrence_count", 1, {"last_occurrence": "t"})
    assert isinstance(written["occurrence_count"], firestore.Increment) and written["last_occurrence"] == "t"
    with pytest.raises(KeyError):
        repo.increment("customers/c/alert_groups/missing", "occurrence_count", 1, {})
