from datetime import UTC, datetime, timedelta

import pytest

from asre.core.dedup import Action, DedupConfig, DedupService, InMemoryRepository, history_path
from asre.core.models import AlertStatus, AlertType
from tests import properties as p
from tests.conftest import NOW, make_alert


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def svc(repo):
    return DedupService(repo, DedupConfig())


def test_first_alert_creates_group_and_asks_for_analysis(svc, repo):
    d = svc.process(make_alert(), NOW)
    assert d.action is Action.ANALYSE and d.occurrence_count == 1
    doc = repo.get(d.path)
    assert d.path.startswith("customers/example-shop/alert_groups/")
    assert doc["state"] == "open" and doc["analysis_enqueued"] is False


def test_p1_1_expires_at_is_timestamp_type(svc, repo):
    d = svc.process(make_alert(), NOW)
    doc = repo.get(d.path)
    assert p.expires_at_is_timestamp(doc)
    for field in ("first_occurrence", "last_occurrence", "dedup_until", "last_update_at"):
        assert isinstance(doc[field], datetime), field


def test_p1_2_dedup_window_is_exact_not_ttl(repo):
    svc = DedupService(repo)
    assert p.window_is_exact(lambda t: svc.process(make_alert(), t).action is Action.ANALYSE, NOW)


def test_p1_2_dedup_until_and_expires_at_are_different_fields(svc, repo):
    doc = repo.get(svc.process(make_alert(), NOW).path)
    assert doc["dedup_until"] == NOW + timedelta(minutes=60)
    assert doc["expires_at"] == NOW + timedelta(days=30)


def test_p1_3_50_concurrent_identical_alerts_create_one_group(repo):
    svc = DedupService(repo)
    assert p.concurrent_identical_alerts_make_one_group(
        lambda: svc.process(make_alert(), NOW).action is Action.ANALYSE
    )
    [doc_path] = [x for x in repo.paths() if "/history/" not in x]
    assert repo.get(doc_path)["occurrence_count"] == 50


def test_p1_4_two_tenants_with_same_service_get_separate_groups(svc):
    a = svc.process(make_alert(customer_id="acme-corp"), NOW)
    b = svc.process(make_alert(customer_id="globex-inc"), NOW)
    assert a.action is Action.ANALYSE and b.action is Action.ANALYSE and a.path != b.path


def test_oom_and_crashloop_from_two_policies_are_one_incident(svc):
    first = svc.process(make_alert(alert_type=AlertType.OOM_KILLED), NOW)
    second = svc.process(make_alert(alert_type=AlertType.CONTAINER_CRASH), NOW + timedelta(seconds=5))
    assert first.group_key == second.group_key and second.action is Action.SUPPRESS


def test_repeat_inside_window_suppresses_then_updates_by_time_not_count(svc, repo):
    svc.process(make_alert(), NOW)
    svc.mark_enqueued(svc.process(make_alert(), NOW))  # harmless: repeat mark
    actions = [svc.process(make_alert(), NOW + timedelta(minutes=m)).action for m in (1, 5, 14)]
    assert actions == [Action.SUPPRESS, Action.SUPPRESS, Action.SUPPRESS]
    assert svc.process(make_alert(), NOW + timedelta(minutes=15, seconds=1)).action is Action.UPDATE
    assert svc.process(make_alert(), NOW + timedelta(minutes=16)).action is Action.SUPPRESS


def test_500_flaps_produce_bounded_updates(svc):
    svc.mark_enqueued(svc.process(make_alert(), NOW))
    updates = sum(
        svc.process(make_alert(), NOW + timedelta(seconds=6 * i)).action is Action.UPDATE
        for i in range(1, 501)
    )
    assert updates <= 4  # 50 minutes of flapping -> at most one update per 15 minutes


def test_window_slides_with_each_occurrence(svc):
    svc.mark_enqueued(svc.process(make_alert(), NOW))
    svc.process(make_alert(), NOW + timedelta(minutes=50))
    assert svc.process(make_alert(), NOW + timedelta(minutes=100)).action is not Action.ANALYSE


def test_lapsed_group_is_archived_as_dormant(svc, repo):
    first = svc.process(make_alert(), NOW)
    svc.mark_enqueued(first)
    second = svc.process(make_alert(), NOW + timedelta(minutes=61))
    assert second.action is Action.ANALYSE and second.incident_id != first.incident_id
    archived = repo.get(history_path("example-shop", first.group_key, first.incident_id))
    assert archived["state"] == "dormant"


def test_resolution_closes_group_and_next_firing_is_new(svc, repo):
    first = svc.process(make_alert(), NOW)
    svc.mark_enqueued(first)
    resolved = svc.process(make_alert(status=AlertStatus.RESOLVED), NOW + timedelta(minutes=10))
    assert resolved.action is Action.RESOLVE
    doc = repo.get(first.path)
    assert doc["state"] == "closed" and doc["resolved_at"] == NOW + timedelta(minutes=10)
    # Past the reopen window (10 min); a re-fire inside it reopens instead (test_reopen.py).
    again = svc.process(make_alert(), NOW + timedelta(minutes=21))
    assert again.action is Action.ANALYSE
    assert repo.get(history_path("example-shop", first.group_key, first.incident_id))["state"] == "closed"


def test_resolution_without_open_group_is_ignored(svc):
    assert svc.process(make_alert(status=AlertStatus.RESOLVED), NOW).action is Action.IGNORE


def test_unenqueued_analysis_is_suppressed_while_leased_then_retried(svc):
    first = svc.process(make_alert(), NOW)  # enqueue "failed": mark_enqueued never called
    assert svc.process(make_alert(), NOW + timedelta(seconds=5)).action is Action.SUPPRESS
    retry = svc.process(make_alert(), NOW + timedelta(seconds=30))  # lease lapsed
    assert retry.action is Action.ANALYSE and retry.incident_id == first.incident_id
    svc.mark_enqueued(retry)
    assert svc.process(make_alert(), NOW + timedelta(seconds=60)).action is Action.SUPPRESS


def test_released_lease_lets_redelivery_take_over_immediately(svc):
    first = svc.process(make_alert(), NOW)
    svc.release_lease(first, NOW + timedelta(seconds=1))
    assert svc.process(make_alert(), NOW + timedelta(seconds=2)).action is Action.ANALYSE


def test_group_removed_between_create_and_transaction_is_recreated(repo):
    svc = DedupService(repo)
    first = svc.process(make_alert(), NOW)
    repo.create = lambda path, data: False  # simulate: create lost the race...
    repo._docs.pop(first.path)  # ...and the document then vanished (e.g. TTL)
    assert svc.process(make_alert(), NOW).action is Action.ANALYSE


def test_naive_now_is_rejected(svc):
    with pytest.raises(ValueError):
        svc.process(make_alert(), datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001


def test_in_memory_update_of_missing_doc_raises(repo):
    with pytest.raises(KeyError):
        repo.update("customers/x/alert_groups/y", {"a": 1})


def test_now_utc_helper_sanity():
    assert NOW.tzinfo is UTC


class CountingRepository(InMemoryRepository):
    """Counts transactions, so tests can prove the storm path avoids them."""

    def __init__(self) -> None:
        super().__init__()
        self.transactions = 0

    def transaction(self, fn):
        self.transactions += 1
        return super().transaction(fn)


def test_alert_storm_repeats_use_no_transactions_and_count_exactly():
    repo = CountingRepository()
    svc = DedupService(repo)
    assert p.concurrent_identical_alerts_make_one_group(
        lambda: svc.process(make_alert(), NOW).action is Action.ANALYSE
    )
    [doc_path] = repo.paths()
    assert repo.get(doc_path)["occurrence_count"] == 50
    assert repo.transactions == 0  # 1 create + 49 atomic increments, no hot-document transactions


def test_decisions_that_create_work_still_run_in_a_transaction():
    repo = CountingRepository()
    svc = DedupService(repo)
    first = svc.process(make_alert(), NOW)
    svc.mark_enqueued(first)
    assert svc.process(make_alert(), NOW + timedelta(minutes=1)).action is Action.SUPPRESS
    assert repo.transactions == 0
    # Time-based update is due -> decided in a transaction (exactly one repeat gets UPDATE).
    assert svc.process(make_alert(), NOW + timedelta(minutes=16)).action is Action.UPDATE
    assert repo.transactions == 1
    assert svc.process(make_alert(), NOW + timedelta(minutes=17)).action is Action.SUPPRESS
    assert repo.transactions == 1
    # Resolution and a firing after the window are transactional too.
    svc.process(make_alert(status=AlertStatus.RESOLVED), NOW + timedelta(minutes=18))
    assert svc.process(make_alert(), NOW + timedelta(minutes=29)).action is Action.ANALYSE
    assert repo.transactions == 3


def test_storm_path_falls_back_to_a_transaction_if_the_group_vanishes_mid_way(repo):
    svc = DedupService(repo)
    first = svc.process(make_alert(), NOW)
    svc.mark_enqueued(first)

    def vanished(path, field, by, fields):
        repo._docs.pop(path)  # e.g. TTL deleted it between the read and the increment
        raise KeyError(path)

    repo.increment = vanished
    assert svc.process(make_alert(), NOW + timedelta(minutes=1)).action is Action.ANALYSE


def test_storm_path_count_is_exact_sequentially(svc, repo):
    first = svc.process(make_alert(), NOW)
    svc.mark_enqueued(first)
    decisions = [svc.process(make_alert(), NOW + timedelta(seconds=i)) for i in range(1, 6)]
    assert [d.occurrence_count for d in decisions] == [2, 3, 4, 5, 6]
    assert repo.get(first.path)["occurrence_count"] == 6
    assert repo.get(first.path)["last_occurrence"] == NOW + timedelta(seconds=5)


def test_in_memory_increment_of_missing_doc_raises(repo):
    with pytest.raises(KeyError):
        repo.increment("customers/x/alert_groups/y", "occurrence_count", 1, {})
