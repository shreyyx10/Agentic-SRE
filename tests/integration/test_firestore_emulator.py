"""Dedup against real Firestore semantics (the emulator). Runs in CI; skipped locally
unless FIRESTORE_EMULATOR_HOST is set:

    gcloud emulators firestore start --host-port=localhost:8081 &
    FIRESTORE_EMULATOR_HOST=localhost:8081 pytest tests/integration
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

import pytest

from asre.core.dedup import Action, DedupService
from tests import properties as p
from tests.conftest import NOW, make_alert

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("FIRESTORE_EMULATOR_HOST"), reason="Firestore emulator not running"),
]


@pytest.fixture(scope="module")
def repo():
    from google.cloud import firestore

    from asre.core.firestore_repo import FirestoreRepository

    return FirestoreRepository(firestore.Client(project="asre-test"))


@pytest.fixture
def customer():
    return f"t{uuid.uuid4().hex[:12]}"


def test_p1_1_stored_expires_at_is_a_firestore_timestamp(repo, customer):
    d = DedupService(repo).process(make_alert(customer_id=customer), NOW)
    stored = repo.get(d.path)
    # The client returns Firestore Timestamps as DatetimeWithNanoseconds (a datetime
    # subclass). A string here would mean the TTL policy can never fire.
    assert (
        p.expires_at_is_timestamp(stored) and type(stored["expires_at"]).__name__ == "DatetimeWithNanoseconds"
    )


def test_p1_3_50_concurrent_identical_alerts_create_one_group(repo, customer):
    svc = DedupService(repo)
    assert p.concurrent_identical_alerts_make_one_group(
        lambda: svc.process(make_alert(customer_id=customer), NOW).action is Action.ANALYSE
    )
    d = svc.process(make_alert(customer_id=customer), NOW)
    assert repo.get(d.path)["occurrence_count"] == 51


def test_p1_2_window_exact(repo, customer):
    svc = DedupService(repo)
    assert p.window_is_exact(
        lambda t: svc.process(make_alert(customer_id=customer), t).action is Action.ANALYSE, NOW
    )


def test_p1_4_tenants_are_separate_documents(repo, customer):
    svc = DedupService(repo)
    a = svc.process(make_alert(customer_id=customer), NOW)
    b = svc.process(make_alert(customer_id=customer + "x"), NOW)
    assert a.action is b.action is Action.ANALYSE and a.path != b.path


def test_create_conflict_and_update_contract(repo, customer):
    path = f"customers/{customer}/briefs/brf_1"
    assert repo.create(path, {"status": "pending", "at": NOW}) is True
    assert repo.create(path, {"status": "pending", "at": NOW}) is False
    repo.update(path, {"status": "complete"})
    assert repo.get(path)["status"] == "complete" and isinstance(repo.get(path)["at"], datetime)
    with pytest.raises(KeyError):
        repo.update(f"customers/{customer}/briefs/missing", {"a": 1})
