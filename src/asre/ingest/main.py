"""Gunicorn entrypoint: ``gunicorn asre.ingest.main:app``."""

from __future__ import annotations

from asre.core.auth import GoogleOidcVerifier, insecure_local_mode_allowed
from asre.core.config import IngestSettings, load_tenants
from asre.core.dedup import DedupService
from asre.core.queue import CloudTasksQueue
from asre.ingest.app import IngestDeps, create_app


def build():  # pragma: no cover — wiring only; exercised by the deployed smoke test
    from google.cloud import firestore

    from asre.core.firestore_repo import FirestoreRepository

    settings = IngestSettings.from_env()
    db = firestore.Client(project=settings.project_id, database=settings.firestore_database)
    repo = FirestoreRepository(
        db,
        attempts_per_round=settings.tx_attempts_per_round,
        contention_rounds=settings.tx_contention_rounds,
        base_backoff_s=settings.tx_base_backoff_s,
        max_backoff_s=settings.tx_max_backoff_s,
    )

    def firestore_reachable() -> None:
        # Shallow: one small read. A missing document is fine; an error is not.
        db.document("_meta/ready").get(timeout=2)

    deps = IngestDeps(
        tenants=load_tenants(),
        dedup=DedupService(repo, settings.dedup),
        queue=CloudTasksQueue(
            settings.tasks_queue,
            settings.analyse_url,
            settings.tasks_invoker_sa,
            settings.analyse_audience,
            timeout_s=settings.enqueue_timeout_s,
            first_timeout_s=settings.enqueue_first_timeout_s,
        ),
        verifier=None if insecure_local_mode_allowed() else GoogleOidcVerifier(),
        audience=settings.audience,
        dedup_timeout_s=settings.dedup_timeout_s,
        dedup_first_timeout_s=settings.dedup_first_timeout_s,
        readiness_checks=(firestore_reachable,),
    )
    return create_app(deps)


app = build()
