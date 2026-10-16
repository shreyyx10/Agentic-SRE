"""Gemini model layer: Pro with automatic Flash fallback (ADR-0002)."""

from types import SimpleNamespace

import pytest

from asre.agent.contract import response_schema
from asre.agent.model import (
    FallbackModel,
    GeminiClient,
    ModelError,
    ModelResult,
    ModelSpec,
    build_models,
    classify_api_error,
    result_from_response,
)
from asre.core.config import AnalyseSettings, ModelSettings

PRO = ModelSpec("gemini-3.1-pro-preview", thinking="medium", timeout_s=60)
FLASH = ModelSpec("gemini-3.8-flash", thinking="medium", timeout_s=45)


class FakeGenerator:
    def __init__(self, failures=None):
        self.failures = dict(failures or {})  # model -> ModelError to raise (always)
        self.calls = []

    def generate(self, spec, system, user, schema):
        self.calls.append(spec.model)
        if spec.model in self.failures:
            raise self.failures[spec.model]
        return ModelResult(
            text='{"ok": true}', model=spec.model, model_version=f"{spec.model}-001", location=spec.location
        )


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def model(gen, clock=None, fallback=FLASH):
    return FallbackModel(gen, PRO, fallback, cooldown_s=600, clock=clock or Clock())


# ------------------------------------------------------------------ fallback


def test_primary_answers_when_healthy():
    gen = FakeGenerator()
    r = model(gen).generate("sys", "user")
    assert r.model == "gemini-3.1-pro-preview" and not r.fallback_used
    assert gen.calls == ["gemini-3.1-pro-preview"]


@pytest.mark.parametrize("status", [404, 429, 500, 503, 504, None])
def test_fallback_answers_when_primary_fails(status):
    gen = FakeGenerator({PRO.model: ModelError("boom", status)})
    r = model(gen).generate("sys", "user")
    assert r.model == "gemini-3.8-flash"
    assert r.fallback_used and r.requested_model == "gemini-3.1-pro-preview"
    assert "boom" in r.fallback_reason
    rec = r.as_record()
    assert rec["fallback_used"] is True and rec["model"] == "gemini-3.8-flash"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_errors_the_fallback_cannot_fix_are_raised(status):
    gen = FakeGenerator(
        {PRO.model: classify_api_error(SimpleNamespace(code=status, status="X", message="no"))}
    )
    with pytest.raises(ModelError):
        model(gen).generate("sys", "user")
    assert gen.calls == [PRO.model]


def test_both_failing_raises_the_fallback_error():
    gen = FakeGenerator({PRO.model: ModelError("pro down", 503), FLASH.model: ModelError("flash down", 503)})
    with pytest.raises(ModelError, match="flash down"):
        model(gen).generate("sys", "user")


def test_withdrawn_preview_is_skipped_during_cooldown_then_retried():
    clock = Clock()
    gen = FakeGenerator({PRO.model: ModelError("404 NOT_FOUND", 404)})
    m = model(gen, clock)
    m.generate("s", "u")
    m.generate("s", "u")  # within the cool-down: straight to Flash
    assert gen.calls == [PRO.model, FLASH.model, FLASH.model]
    assert "cooldown" in m.generate("s", "u").fallback_reason

    gen.failures.clear()
    clock.t += 601  # cool-down over: the primary is tried again
    r = m.generate("s", "u")
    assert r.model == PRO.model and not r.fallback_used


def test_transient_errors_do_not_start_a_cooldown():
    gen = FakeGenerator({PRO.model: ModelError("503", 503)})
    m = model(gen)
    m.generate("s", "u")
    gen.failures.clear()
    assert m.generate("s", "u").model == PRO.model


def test_no_fallback_configured_raises():
    gen = FakeGenerator({PRO.model: ModelError("down", 503)})
    with pytest.raises(ModelError):
        model(gen, fallback=None).generate("s", "u")


def test_fallback_identical_to_primary_is_ignored():
    gen = FakeGenerator({PRO.model: ModelError("down", 503)})
    with pytest.raises(ModelError):
        model(gen, fallback=PRO).generate("s", "u")
    assert gen.calls == [PRO.model]


# ------------------------------------------------------------------ Gemini response handling


def response(text, finish="STOP", version="gemini-3.1-pro-preview-2026-09"):
    return SimpleNamespace(
        text=text,
        model_version=version,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        prompt_feedback=None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=20000, candidates_token_count=900, thoughts_token_count=2100
        ),
    )


def test_result_records_version_and_tokens():
    r = result_from_response(response('{"a":1}'), PRO, 1234)
    assert (r.model_version, r.input_tokens, r.output_tokens, r.thinking_tokens, r.latency_ms) == (
        "gemini-3.1-pro-preview-2026-09",
        20000,
        900,
        2100,
        1234,
    )


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_or_blocked_answer_triggers_fallback(text):
    with pytest.raises(ModelError) as e:
        result_from_response(response(text, finish="SAFETY"), PRO, 1)
    assert e.value.fallback_ok and "SAFETY" in str(e.value)


def test_prompt_block_reason_is_reported():
    r = SimpleNamespace(text=None, candidates=[], prompt_feedback=SimpleNamespace(block_reason="OTHER"))
    with pytest.raises(ModelError, match="prompt_blocked"):
        result_from_response(r, PRO, 1)


def test_request_config_uses_schema_thinking_and_single_attempt():
    cfg = GeminiClient.build_config(PRO, "system", response_schema())
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema["title"] == "ModelOutput"
    assert cfg.thinking_config.thinking_level.name == "MEDIUM"
    assert cfg.http_options.timeout == 60000
    assert cfg.http_options.retry_options.attempts == 1
    assert cfg.automatic_function_calling.disable is True


def test_genai_errors_are_classified(monkeypatch):
    from google.genai import errors

    class Models:
        def __init__(self, exc):
            self.exc = exc

        def generate_content(self, **kw):
            raise self.exc

    client = GeminiClient("p")
    for exc, status, ok in (
        (
            errors.ClientError(
                429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
            ),
            429,
            True,
        ),
        (
            errors.ClientError(
                403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}
            ),
            403,
            False,
        ),
        (
            errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}),
            503,
            True,
        ),
    ):
        monkeypatch.setattr(client, "_client", lambda loc, e=exc: SimpleNamespace(models=Models(e)))
        with pytest.raises(ModelError) as caught:
            client.generate(PRO, "s", "u", None)
        assert (caught.value.status, caught.value.fallback_ok) == (status, ok)


def test_network_timeout_falls_back(monkeypatch):
    import httpx

    client = GeminiClient("p")

    class Models:
        def generate_content(self, **kw):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(client, "_client", lambda loc: SimpleNamespace(models=Models()))
    with pytest.raises(ModelError) as caught:
        client.generate(PRO, "s", "u", None)
    assert caught.value.fallback_ok and caught.value.status is None


def test_successful_call_returns_result(monkeypatch):
    client = GeminiClient("p")
    seen = {}

    class Models:
        def generate_content(self, model, contents, config):
            seen.update(model=model, contents=contents)
            return response('{"ok":1}')

    monkeypatch.setattr(client, "_client", lambda loc: SimpleNamespace(models=Models()))
    r = client.generate(PRO, "s", "evidence", None)
    assert r.text == '{"ok":1}' and seen == {"model": PRO.model, "contents": "evidence"}


# ------------------------------------------------------------------ settings


def test_defaults_are_the_adr_decision(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("ASRE_MODEL_"):
            monkeypatch.delenv(k)
    s = ModelSettings.from_env()
    assert (s.location, s.hypothesis, s.hypothesis_fallback, s.critic) == (
        "global",
        "gemini-3.1-pro-preview",
        "gemini-3.8-flash",
        "gemini-3.8-flash",
    )


def test_every_model_setting_comes_from_environment(monkeypatch):
    env = {
        "ASRE_MODEL_LOCATION": "australia-southeast1",
        "ASRE_MODEL_HYPOTHESIS": "gemini-3.5-flash",
        "ASRE_MODEL_HYPOTHESIS_FALLBACK": "",
        "ASRE_MODEL_CRITIC": "gemini-3.5-flash",
        "ASRE_MODEL_HYPOTHESIS_THINKING": "high",
        "ASRE_MODEL_CRITIC_THINKING": "minimal",
        "ASRE_MODEL_HYPOTHESIS_TIMEOUT_S": "50",
        "ASRE_MODEL_FALLBACK_TIMEOUT_S": "40",
        "ASRE_MODEL_CRITIC_TIMEOUT_S": "20",
        "ASRE_MODEL_COOLDOWN_S": "60",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = ModelSettings.from_env()
    assert s == ModelSettings(
        "australia-southeast1", "gemini-3.5-flash", "", "gemini-3.5-flash", "high", "minimal", 50, 40, 20, 60
    )

    hypothesis, critic = build_models(s, "p", FakeGenerator())
    assert hypothesis.fallback is None  # empty string switches the fallback off (option C)
    assert hypothesis.primary.location == critic.primary.location == "australia-southeast1"


def test_build_models_wires_pro_with_flash_fallback():
    hypothesis, critic = build_models(ModelSettings(), "p", FakeGenerator())
    assert (hypothesis.primary.model, hypothesis.fallback.model) == (
        "gemini-3.1-pro-preview",
        "gemini-3.8-flash",
    )
    assert (hypothesis.primary.timeout_s, hypothesis.fallback.timeout_s) == (60, 45)
    assert critic.primary.model == "gemini-3.8-flash" and critic.fallback is None
    assert critic.primary.thinking == "low"


def test_analyse_settings_carry_model_settings(monkeypatch):
    for k, v in {
        "GOOGLE_CLOUD_PROJECT": "p",
        "ASRE_ANALYSE_AUDIENCE": "a",
        "ASRE_TASKS_INVOKER_SA": "sa@p",
        "ASRE_MODEL_HYPOTHESIS": "gemini-3.8-flash",
    }.items():
        monkeypatch.setenv(k, v)
    assert AnalyseSettings.from_env().models.hypothesis == "gemini-3.8-flash"


def test_credential_errors_do_not_fall_back(monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError

    client = GeminiClient("p")

    class Models:
        def generate_content(self, **kw):
            raise DefaultCredentialsError("no ADC")

    monkeypatch.setattr(client, "_client", lambda loc: SimpleNamespace(models=Models()))
    with pytest.raises(ModelError) as caught:
        client.generate(PRO, "s", "u", None)
    assert caught.value.fallback_ok is False
