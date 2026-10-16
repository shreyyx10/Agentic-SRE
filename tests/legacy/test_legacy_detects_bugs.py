"""Proof that each regression test would have FAILED on the pre-hackathon code."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from tests import properties as p
from tests.legacy import legacy_logic as legacy

pytestmark = pytest.mark.legacy

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _legacy_key(customer: str, service: str, family: str, env: str) -> str:
    return legacy.legacy_group_key(service, family, env)  # customer is silently ignored


def test_p1_4_legacy_key_collides_across_tenants():
    assert p.tenants_do_not_collide(_legacy_key) is False


def test_p1_4_legacy_key_collides_on_separator_injection():
    assert p.separator_injection_does_not_collide(_legacy_key) is False


def test_p1_1_legacy_ttl_field_is_a_string():
    assert p.expires_at_is_timestamp(legacy.legacy_new_group_doc(NOW)) is False


def test_p1_5_legacy_classifier_calls_oom_a_crash():
    assert p.oom_beats_crashloop(legacy.legacy_classify) is False


def test_p1_2_legacy_window_swallows_a_new_incident():
    store = legacy.LegacyReadThenWriteStore()
    assert p.window_is_exact(lambda t: store.get_or_create("k", t), NOW) is False


def test_p1_3_legacy_read_then_write_creates_many_groups():
    n = 50
    store = legacy.LegacyReadThenWriteStore(read_barrier=threading.Barrier(n))
    assert p.concurrent_identical_alerts_make_one_group(lambda: store.get_or_create("k", NOW), n) is False
    assert store.creates == n  # every request created its own group
