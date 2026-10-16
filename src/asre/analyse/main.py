"""Gunicorn entrypoint: ``gunicorn asre.analyse.main:app``."""

from __future__ import annotations

from asre.agent.model import build_models
from asre.analyse.app import AnalyseDeps, create_app
from asre.analyse.pipeline import AnalysisPipeline
from asre.core.auth import GoogleOidcVerifier, insecure_local_mode_allowed
from asre.core.config import AnalyseSettings, load_dedup_config, load_tenants
from asre.core.dedup import DedupService
from asre.core.secrets import SecretReader
from asre.delivery.chat import WEBHOOK_SECRET, ChatDelivery
from asre.evidence.telemetry import TelemetryPool
from asre.predict.forecast import Scanner


def build():  # pragma: no cover — wiring only
    from google.cloud import firestore

    from asre.core.firestore_repo import FirestoreRepository

    settings = AnalyseSettings.from_env()
    db = firestore.Client(project=settings.project_id, database=settings.firestore_database)
    repo = FirestoreRepository(db)
    secrets = SecretReader(settings.secrets)
    # Chat delivery only when Terraform has granted the webhook secret (step 9b).
    chat = ChatDelivery(secrets) if WEBHOOK_SECRET in secrets.available() else None
    hypothesis, critic = build_models(settings.models, settings.project_id)
    tenants, pool = load_tenants(), TelemetryPool()
    pipeline = AnalysisPipeline(
        repo=repo,
        tenants=tenants,
        telemetry=pool.for_reader,
        hypothesis=hypothesis,
        critic=critic,
        deliver=chat.deliver_brief if chat else None,
    )
    return create_app(
        AnalyseDeps(
            repo=repo,
            verifier=None if insecure_local_mode_allowed() else GoogleOidcVerifier(),
            audience=settings.audience,
            tasks_invoker_sa=settings.tasks_invoker_sa,
            pipeline=pipeline,
            notify=chat.notify if chat else None,
            secrets=secrets,
            pipeline_lease=settings.pipeline_lease,
            scan=Scanner(tenants, pool.for_reader),
            dedup=DedupService(repo, load_dedup_config()),
        )
    )


app = build()
