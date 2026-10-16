"""Gemini on Vertex AI, with an automatic fallback model (ADR-0002).

The hypothesis runs on a Pro model that is still a preview
(``gemini-3.1-pro-preview``). A preview can be rate-limited, changed or
withdrawn during judging, so every call goes through :class:`FallbackModel`:

* if the primary model fails in a way another model could fix (not found,
  quota, overloaded, timed out, empty or blocked answer), the same request is
  sent once to the fallback model (``gemini-3.8-flash``);
* a primary that is *gone* (404) or *out of quota* (429) is skipped for a
  cool-down period, so later incidents do not pay its latency first;
* the result records which model answered, its dated version, token counts and
  whether, and why, the fallback was used. ANALYSE writes this onto the Brief.

Errors the fallback cannot fix (permission denied, bad credentials) are raised
unchanged, because the fallback would fail the same way.

Model IDs, location, thinking levels and timeouts are configuration
(``ModelSettings``), so moving between the options in ADR-0002 is a
``terraform apply``, not a release.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from asre.core.logging import get_logger

log = get_logger(__name__)

ThinkingLevel = Literal["minimal", "low", "medium", "high"]

# HTTP statuses the fallback model can plausibly fix.
FALLBACK_STATUSES = frozenset({404, 408, 429, 499, 500, 502, 503, 504})
# Statuses meaning "this model is unusable for a while": skip it during the cool-down.
COOLDOWN_STATUSES = frozenset({404, 429})


@dataclass(frozen=True)
class ModelSpec:
    model: str
    location: str = "global"
    thinking: ThinkingLevel = "low"
    timeout_s: float = 60.0
    max_output_tokens: int = 8192
    temperature: float | None = None


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str  # the model that answered
    model_version: str | None  # dated version reported by the API
    location: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    latency_ms: int = 0
    requested_model: str | None = None  # set when the fallback answered
    fallback_reason: str | None = None

    @property
    def fallback_used(self) -> bool:
        return self.requested_model is not None

    def as_record(self) -> dict[str, Any]:
        """Fields stored on the Brief (ADR-0002, Consequences)."""
        return {
            "model": self.model,
            "model_version": self.model_version,
            "model_location": self.location,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "latency_ms": self.latency_ms,
            "fallback_used": self.fallback_used,
            "requested_model": self.requested_model,
            "fallback_reason": self.fallback_reason,
        }


class ModelError(Exception):
    """A model call failed. ``status`` is the HTTP status, or None for a local timeout/empty answer."""

    def __init__(self, message: str, status: int | None = None, *, fallback_ok: bool = True):
        super().__init__(message)
        self.status = status
        self.fallback_ok = fallback_ok


class Generator(Protocol):
    def generate(
        self, spec: ModelSpec, system: str, user: str, schema: dict[str, Any] | None
    ) -> ModelResult: ...


class GeminiClient:
    """Calls Gemini through Vertex AI with the service's own identity (ADC, no API key)."""

    def __init__(self, project: str):
        self.project = project
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _client(self, location: str):  # pragma: no cover — needs Vertex AI
        with self._lock:
            if location not in self._clients:
                from google import genai

                self._clients[location] = genai.Client(vertexai=True, project=self.project, location=location)
            return self._clients[location]

    @staticmethod
    def build_config(spec: ModelSpec, system: str, schema: dict[str, Any] | None):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=spec.max_output_tokens,
            temperature=spec.temperature,
            response_mime_type="application/json" if schema is not None else None,
            response_json_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=spec.thinking.upper()),
            # The agents have no tools, so the SDK's automatic function calling is off.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # One attempt: on failure the fallback model is the retry, which is faster
            # than the SDK backing off on a model that is overloaded or gone.
            http_options=types.HttpOptions(
                timeout=int(spec.timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    def generate(self, spec: ModelSpec, system: str, user: str, schema: dict[str, Any] | None) -> ModelResult:
        import httpx
        from google.auth.exceptions import GoogleAuthError
        from google.genai import errors

        config = self.build_config(spec, system, schema)
        started = time.monotonic()
        try:
            response = self._client(spec.location).models.generate_content(
                model=spec.model, contents=user, config=config
            )
        except errors.APIError as exc:
            raise classify_api_error(exc) from exc
        except (httpx.TransportError, TimeoutError) as exc:  # timeouts, connection resets
            raise ModelError(f"{type(exc).__name__}: {exc}") from exc
        except GoogleAuthError as exc:  # no or expired credentials: the fallback would fail the same way
            raise ModelError(f"{type(exc).__name__}: {exc}", fallback_ok=False) from exc
        return result_from_response(response, spec, int((time.monotonic() - started) * 1000))


def classify_api_error(exc: Any) -> ModelError:
    code = getattr(exc, "code", None)
    message = f"{code} {getattr(exc, 'status', '')}: {getattr(exc, 'message', '') or exc}"[:300]
    return ModelError(message, code, fallback_ok=code in FALLBACK_STATUSES)


def result_from_response(response: Any, spec: ModelSpec, latency_ms: int) -> ModelResult:
    text = getattr(response, "text", None)
    if not text or not text.strip():
        reason = _finish_reason(response)
        raise ModelError(f"empty response (finish_reason={reason})")
    usage = getattr(response, "usage_metadata", None)
    return ModelResult(
        text=text,
        model=spec.model,
        model_version=getattr(response, "model_version", None),
        location=spec.location,
        input_tokens=getattr(usage, "prompt_token_count", None) or 0,
        output_tokens=getattr(usage, "candidates_token_count", None) or 0,
        thinking_tokens=getattr(usage, "thoughts_token_count", None) or 0,
        latency_ms=latency_ms,
    )


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        reason = getattr(candidates[0], "finish_reason", None)
        return getattr(reason, "name", None) or (str(reason) if reason else None)
    feedback = getattr(response, "prompt_feedback", None)
    blocked = getattr(feedback, "block_reason", None)
    return f"prompt_blocked:{getattr(blocked, 'name', blocked)}" if blocked else None


@dataclass
class FallbackModel:
    """Primary model with one fallback, and a cool-down for a primary that is gone or out of quota."""

    generator: Generator
    primary: ModelSpec
    fallback: ModelSpec | None = None
    cooldown_s: float = 600.0
    clock: Callable[[], float] = time.monotonic
    _skip_primary_until: float = field(default=0.0, init=False)
    _skip_reason: str | None = field(default=None, init=False)

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> ModelResult:
        fallback = self.fallback if self.fallback and self.fallback.model != self.primary.model else None

        if fallback and self.clock() < self._skip_primary_until:
            return self._use_fallback(fallback, system, user, schema, f"cooldown: {self._skip_reason}")

        try:
            return self.generator.generate(self.primary, system, user, schema)
        except ModelError as exc:
            if not (fallback and exc.fallback_ok):
                raise
            if exc.status in COOLDOWN_STATUSES:
                self._skip_primary_until = self.clock() + self.cooldown_s
                self._skip_reason = str(exc)[:120]
            log.warning(
                "primary model failed; using fallback",
                event="model_fallback",
                model=self.primary.model,
                fallback_model=fallback.model,
                status=exc.status,
                reason=str(exc)[:300],
            )
            return self._use_fallback(fallback, system, user, schema, str(exc))

    def _use_fallback(
        self, fallback: ModelSpec, system: str, user: str, schema: dict[str, Any] | None, reason: str
    ) -> ModelResult:
        result = self.generator.generate(fallback, system, user, schema)
        return replace(result, requested_model=self.primary.model, fallback_reason=reason[:300])


def build_models(
    settings: Any, project: str, generator: Generator | None = None
) -> tuple[FallbackModel, FallbackModel]:
    """The hypothesis model (Pro, falling back to Flash) and the critic model, from ``ModelSettings``."""
    gen = generator or GeminiClient(project)
    hypothesis = FallbackModel(
        gen,
        primary=ModelSpec(
            settings.hypothesis,
            settings.location,
            settings.hypothesis_thinking,
            settings.hypothesis_timeout_s,
        ),
        fallback=ModelSpec(
            settings.hypothesis_fallback,
            settings.location,
            settings.hypothesis_thinking,
            settings.fallback_timeout_s,
        )
        if settings.hypothesis_fallback
        else None,
        cooldown_s=settings.cooldown_s,
    )
    critic = FallbackModel(
        gen,
        primary=ModelSpec(
            settings.critic, settings.location, settings.critic_thinking, settings.critic_timeout_s
        ),
    )
    return hypothesis, critic
