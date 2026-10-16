"""Properties shared by the regression tests and the legacy proofs.

Each returns True when the property holds. Unit tests assert True against the
new code; tests/legacy asserts False against the pre-hackathon logic.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta


def tenants_do_not_collide(key_fn: Callable[[str, str, str, str], str]) -> bool:
    return key_fn("acme-corp", "payment-service", "container_failure", "prod") != key_fn(
        "globex-inc", "payment-service", "container_failure", "prod"
    )


def separator_injection_does_not_collide(key_fn: Callable[[str, str, str, str], str]) -> bool:
    try:
        a = key_fn("acme-corp", "payment", "svc|container_failure", "prod")
        b = key_fn("acme-corp", "payment|svc", "container_failure", "prod")
    except ValueError:
        return True  # rejected at the edge: cannot collide
    return a != b


def expires_at_is_timestamp(doc: dict) -> bool:
    return isinstance(doc["expires_at"], datetime)


def oom_beats_crashloop(classify_name: Callable[[str], str]) -> bool:
    return classify_name("Container OOMKilled CrashLoopBackOff").lower().replace("_", "") == "oomkilled"


def window_is_exact(is_new_at: Callable[[datetime], bool], start: datetime) -> bool:
    """First alert at start; another 61 minutes later must start a new incident."""
    first = is_new_at(start)
    later = is_new_at(start + timedelta(minutes=61))
    return first and later


def concurrent_identical_alerts_make_one_group(fire: Callable[[], bool], n: int = 50) -> bool:
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: fire(), range(n)))
    return sum(1 for r in results if r) == 1
