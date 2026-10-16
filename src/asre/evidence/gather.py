"""Run the evidence sources in parallel and turn their output into a safe evidence pack.

Rules, in order:

1. **A source failing never fails the Brief.** Each source runs with a shared
   deadline; one that errors or runs late is recorded in the report, and the
   pack is marked ``degraded`` so the Brief and the model both know.
2. **Nothing unsanitised leaves.** Every item goes through
   ``asre.agent.sanitise`` and then the independent ``verify`` pass. An item that
   still matches a secret pattern is dropped and ``sanitiser_bypass`` is logged.
3. **The pack has a size budget.** Items are kept in priority order (alert,
   deployments, metrics, events, logs) until the budget is spent; the pack is
   then marked ``truncated``.

The pack's hash is recorded on the Brief, so a Brief can be traced to exactly
the evidence the model saw.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from asre.agent.prompt import EvidencePack
from asre.agent.sanitise import sanitise, verify
from asre.core.logging import get_logger
from asre.evidence.context import IncidentContext
from asre.evidence.sources import SOURCES, Item, Source
from asre.evidence.telemetry import Telemetry

log = get_logger(__name__)

# Lower number = kept first when the budget runs out.
PRIORITY = {"alert": 0, "deployments": 1, "metrics": 2, "logs": 3, "knowledge": 4}


@dataclass(frozen=True)
class EvidenceConfig:
    deadline_s: float = 25.0  # all sources together
    budget_chars: int = 40_000  # about 10K tokens


@dataclass
class SourceReport:
    status: str  # ok | empty | failed | timeout | skipped
    items: int = 0
    ms: int = 0
    error: str | None = None


@dataclass
class GatherResult:
    pack: EvidencePack
    report: dict[str, SourceReport] = field(default_factory=dict)
    redactions: int = 0
    dropped_unsafe: int = 0
    dropped_budget: int = 0

    @property
    def pack_hash(self) -> str:
        body = json.dumps([[i.key, i.source, i.text] for i in self.pack.items], ensure_ascii=True)
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    def as_record(self) -> dict[str, object]:
        """Fields stored on the Brief."""
        return {
            "context_quality": self.pack.context_quality,
            "evidence_hash": self.pack_hash,
            "evidence_items": len(self.pack.items),
            "evidence_sources": {
                name: {
                    "status": r.status,
                    "items": r.items,
                    "ms": r.ms,
                    **({"error": r.error} if r.error else {}),
                }
                for name, r in self.report.items()
            },
            "redactions": self.redactions,
            "dropped_unsafe": self.dropped_unsafe,
            "dropped_budget": self.dropped_budget,
        }


def _run(source: Source, ctx: IncidentContext, telemetry: Telemetry) -> tuple[list[Item], int]:
    started = time.monotonic()
    items = source.collect(ctx, telemetry)
    return items, int((time.monotonic() - started) * 1000)


def gather(
    ctx: IncidentContext,
    telemetry: Telemetry,
    sources: tuple[Source, ...] = SOURCES,
    config: EvidenceConfig | None = None,
) -> GatherResult:
    config = config or EvidenceConfig()
    report: dict[str, SourceReport] = {}
    collected: list[Item] = []

    pool = ThreadPoolExecutor(max_workers=max(1, len(sources)), thread_name_prefix="evidence")
    try:
        futures = {pool.submit(_run, s, ctx, telemetry): s for s in sources}
        done, pending = wait(futures, timeout=config.deadline_s)
        for future, source in futures.items():
            if future in pending:
                future.cancel()
                report[source.name] = SourceReport("timeout", error=f"over {config.deadline_s:.0f}s")
                continue
            try:
                items, ms = future.result()
            except Exception as exc:  # noqa: BLE001 — a source failure degrades the pack, never the Brief
                report[source.name] = SourceReport("failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
                continue
            report[source.name] = SourceReport("ok" if items else "empty", len(items), ms)
            collected.extend(items)
    finally:
        # Do not wait for a late source: its result is already ignored.
        pool.shutdown(wait=False, cancel_futures=True)

    result = GatherResult(EvidencePack(), report)
    _admit(result, collected, config.budget_chars)

    if any(r.status in ("failed", "timeout") for r in report.values()):
        result.pack.context_quality = "degraded"
    elif result.dropped_budget:
        result.pack.context_quality = "truncated"

    for name, r in report.items():
        if r.status in ("failed", "timeout"):
            log.warning(
                "evidence source unavailable",
                event="evidence_source_failed",
                source=name,
                status=r.status,
                error=r.error,
            )
    log.info(
        "evidence gathered",
        event="evidence_gathered",
        context_quality=result.pack.context_quality,
        items=len(result.pack.items),
        redactions=result.redactions,
        sources={n: r.status for n, r in report.items()},
    )
    return result


def _admit(result: GatherResult, items: list[Item], budget: int) -> None:
    seen: set[str] = set()
    spent = 0
    for item in sorted(items, key=lambda i: PRIORITY.get(i.source, 9)):  # stable: keeps each source's order
        if item.key in seen:
            continue
        clean = sanitise(item.text)
        survivors = verify(clean.text)
        if survivors:
            result.dropped_unsafe += 1
            log.error(
                "unsanitisable evidence dropped", event="sanitiser_bypass", key=item.key, patterns=survivors
            )
            continue
        if spent + len(clean.text) > budget:
            result.dropped_budget += 1
            continue
        seen.add(item.key)
        spent += len(clean.text)
        result.redactions += clean.total
        result.pack.add(item.key, item.source, clean.text)  # type: ignore[arg-type]
