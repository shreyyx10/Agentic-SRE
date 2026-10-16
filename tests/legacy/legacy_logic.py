"""Pre-hackathon logic, reproduced ONLY as a test fixture.

These functions reproduce, deliberately, the defects that MVP_Readiness_Review_v2
found in the June 2026 codebase (agentic-sre@d4d532c). They are never imported by
product code. Their single purpose is to prove that each regression test in this
repository fails against the defect it guards — a test that has never seen its
bug proves nothing.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta


def legacy_group_key(service_name: str, alert_type: str, environment: str) -> str:
    # P1-4: no tenant dimension; '|' concatenation is ambiguous; MD5.
    return hashlib.md5(f"{service_name}|{alert_type}|{environment}".encode()).hexdigest()  # noqa: S324


def legacy_classify(alert_name: str) -> str:
    # P1-5: substring if-chain in the wrong order.
    if "CrashLoopBackOff" in alert_name:
        return "ContainerCrash"
    if "OOMKilled" in alert_name:
        return "OOMKilled"
    if "HighLatency" in alert_name or "Latency" in alert_name:
        return "HighLatency"
    if "Error" in alert_name or "5xx" in alert_name:
        return "ErrorRate"
    return "Unknown"


def legacy_new_group_doc(now: datetime) -> dict:
    # P1-1: the TTL field written as a string.
    return {"created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=60)).isoformat()}


class LegacyReadThenWriteStore:
    """P1-3 + P1-2: read-then-write with no precondition, and the dedup window
    left to a best-effort TTL (a document lives until Firestore deletes it,
    typically up to 24 h after expiry)."""

    TTL_DELETION_LAG = timedelta(hours=24)

    def __init__(self, read_barrier: threading.Barrier | None = None) -> None:
        self.docs: dict[str, dict] = {}
        self.creates = 0
        self._count_lock = threading.Lock()
        # Concurrent requests all read before any of them writes. A short sleep alone
        # left that to the scheduler: on a busy CI runner the threads sometimes ran one
        # after another and the race never happened (CI #63, 14 Oct 2026).
        self._read_barrier = read_barrier

    def get_or_create(self, key: str, now: datetime) -> bool:
        doc = self.docs.get(key)  # read
        if doc is not None and now < doc["expires"] + self.TTL_DELETION_LAG:
            return False
        if self._read_barrier is not None:
            self._read_barrier.wait(timeout=10)
        time.sleep(0.001)  # the gap every concurrent request falls into
        self.docs[key] = {"expires": now + timedelta(minutes=60)}  # unconditional write
        with self._count_lock:
            self.creates += 1
        return True
