"""Runtime settings, read once from the environment.

Every variable here is read by the code that uses it — no dead configuration
(MVP_Readiness_Review_v2 P2-2). Tenant configuration contains identities and
project IDs, never secrets. Secrets are referenced by Secret Manager resource
name (``ASRE_SECRET_*``) and their values are fetched at runtime.

In the deployed services Terraform sets every variable below (infra/platform
``ingest_env`` / ``analyse_env``). The fallbacks here are the ``DedupConfig``
defaults, used only by local runs and tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from asre.core.dedup import DedupConfig
from asre.core.tenants import TenantRegistry


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _duration(name: str, unit: str, default: timedelta) -> timedelta:
    raw = os.getenv(name)
    return default if not raw else timedelta(**{unit: int(raw)})


def _millis(name: str, default_ms: int) -> float:
    return int(os.getenv(name) or default_ms) / 1000


def secret_names() -> dict[str, str]:
    """Map secret ID -> resource name from ``ASRE_SECRET_*`` variables.

    ``ASRE_SECRET_CHAT_WEBHOOK_URL=projects/p/secrets/chat-webhook-url``
    becomes ``{"chat-webhook-url": "projects/p/secrets/chat-webhook-url"}``.
    """
    prefix = "ASRE_SECRET_"
    return {
        key[len(prefix) :].lower().replace("_", "-"): value
        for key, value in os.environ.items()
        if key.startswith(prefix) and value
    }


def load_dedup_config() -> DedupConfig:
    d = DedupConfig()
    return DedupConfig(
        window=_duration("ASRE_DEDUP_WINDOW_MINUTES", "minutes", d.window),
        retention=_duration("ASRE_RETENTION_DAYS", "days", d.retention),
        update_interval=_duration("ASRE_DEDUP_UPDATE_INTERVAL_MIN", "minutes", d.update_interval),
        enqueue_lease=_duration("ASRE_ENQUEUE_LEASE_S", "seconds", d.enqueue_lease),
        reopen_window=_duration("ASRE_DEDUP_REOPEN_MINUTES", "minutes", d.reopen_window),
    )


def load_tenants() -> TenantRegistry:
    inline = os.getenv("ASRE_TENANTS_JSON")
    if inline:
        return TenantRegistry.from_json(inline)
    return TenantRegistry.from_path(_required("ASRE_TENANTS_FILE"))


@dataclass(frozen=True)
class IngestSettings:
    project_id: str
    firestore_database: str
    audience: str
    tasks_queue: str
    analyse_url: str
    analyse_audience: str
    tasks_invoker_sa: str
    dedup: DedupConfig
    dedup_timeout_s: float
    enqueue_timeout_s: float
    dedup_first_timeout_s: float
    enqueue_first_timeout_s: float
    tx_attempts_per_round: int
    tx_contention_rounds: int
    tx_base_backoff_s: float
    tx_max_backoff_s: float

    @classmethod
    def from_env(cls) -> IngestSettings:
        return cls(
            project_id=_required("GOOGLE_CLOUD_PROJECT"),
            firestore_database=os.getenv("ASRE_FIRESTORE_DATABASE", "(default)"),
            audience=_required("ASRE_INGEST_AUDIENCE"),
            tasks_queue=_required("ASRE_TASKS_QUEUE"),
            analyse_url=_required("ASRE_ANALYSE_URL"),
            analyse_audience=_required("ASRE_ANALYSE_AUDIENCE"),
            tasks_invoker_sa=_required("ASRE_TASKS_INVOKER_SA"),
            dedup=load_dedup_config(),
            dedup_timeout_s=_millis("ASRE_DEDUP_TIMEOUT_MS", 500),
            enqueue_timeout_s=_millis("ASRE_ENQUEUE_TIMEOUT_MS", 500),
            dedup_first_timeout_s=_millis("ASRE_DEDUP_FIRST_TIMEOUT_MS", 3000),
            enqueue_first_timeout_s=_millis("ASRE_ENQUEUE_FIRST_TIMEOUT_MS", 5000),
            tx_attempts_per_round=int(os.getenv("ASRE_TX_ATTEMPTS_PER_ROUND") or 5),
            tx_contention_rounds=int(os.getenv("ASRE_TX_CONTENTION_ROUNDS") or 8),
            tx_base_backoff_s=_millis("ASRE_TX_BASE_BACKOFF_MS", 25),
            tx_max_backoff_s=_millis("ASRE_TX_MAX_BACKOFF_MS", 1000),
        )


@dataclass(frozen=True)
class ModelSettings:
    """Gemini models for the analysis agents (ADR-0002). Defaults are the ADR's decision."""

    location: str = "global"
    hypothesis: str = "gemini-3.1-pro-preview"
    hypothesis_fallback: str = "gemini-3.8-flash"
    critic: str = "gemini-3.8-flash"
    hypothesis_thinking: str = "medium"
    critic_thinking: str = "low"
    hypothesis_timeout_s: float = 60.0
    fallback_timeout_s: float = 45.0
    critic_timeout_s: float = 30.0
    cooldown_s: float = 600.0

    @classmethod
    def from_env(cls) -> ModelSettings:
        d = cls()
        return cls(
            location=os.getenv("ASRE_MODEL_LOCATION") or d.location,
            hypothesis=os.getenv("ASRE_MODEL_HYPOTHESIS") or d.hypothesis,
            # Empty string disables the fallback; unset keeps the default.
            hypothesis_fallback=os.getenv("ASRE_MODEL_HYPOTHESIS_FALLBACK", d.hypothesis_fallback),
            critic=os.getenv("ASRE_MODEL_CRITIC") or d.critic,
            hypothesis_thinking=os.getenv("ASRE_MODEL_HYPOTHESIS_THINKING") or d.hypothesis_thinking,
            critic_thinking=os.getenv("ASRE_MODEL_CRITIC_THINKING") or d.critic_thinking,
            hypothesis_timeout_s=float(
                os.getenv("ASRE_MODEL_HYPOTHESIS_TIMEOUT_S") or d.hypothesis_timeout_s
            ),
            fallback_timeout_s=float(os.getenv("ASRE_MODEL_FALLBACK_TIMEOUT_S") or d.fallback_timeout_s),
            critic_timeout_s=float(os.getenv("ASRE_MODEL_CRITIC_TIMEOUT_S") or d.critic_timeout_s),
            cooldown_s=float(os.getenv("ASRE_MODEL_COOLDOWN_S") or d.cooldown_s),
        )


@dataclass(frozen=True)
class AnalyseSettings:
    project_id: str
    firestore_database: str
    audience: str
    tasks_invoker_sa: str
    secrets: dict[str, str]
    # Brief lease while the pipeline runs: Cloud Run's request timeout (set by
    # infra/platform as ASRE_REQUEST_TIMEOUT_S) plus a margin, so it never lapses
    # under a live request.
    pipeline_lease: timedelta = timedelta(seconds=330)
    models: ModelSettings = ModelSettings()

    @classmethod
    def from_env(cls) -> AnalyseSettings:
        return cls(
            project_id=_required("GOOGLE_CLOUD_PROJECT"),
            firestore_database=os.getenv("ASRE_FIRESTORE_DATABASE", "(default)"),
            audience=_required("ASRE_ANALYSE_AUDIENCE"),
            tasks_invoker_sa=_required("ASRE_TASKS_INVOKER_SA"),
            secrets=secret_names(),
            pipeline_lease=timedelta(seconds=int(os.getenv("ASRE_REQUEST_TIMEOUT_S") or 300) + 30),
            models=ModelSettings.from_env(),
        )
