import io
import json

import pytest

from asre.core import logging as slog


@pytest.fixture(autouse=True)
def _reset():
    slog.clear()
    yield
    slog.clear()


def test_parse_cloud_trace_and_traceparent():
    assert slog.parse_trace_header("0123456789abcdef0123456789ABCDEF/42;o=1", None) == (
        "0123456789abcdef0123456789abcdef",
        "42",
    )
    assert slog.parse_trace_header(None, "00-0123456789abcdef0123456789abcdef-00f067aa0ba902b7-01") == (
        "0123456789abcdef0123456789abcdef",
        "00f067aa0ba902b7",
    )
    assert slog.parse_trace_header("garbage", "also-garbage") is None


def test_p2_1_emits_cloud_logging_field_names(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "asre-platform")
    out = io.StringIO()
    slog.bind_request(("a" * 32, "7"), customer_id="example-shop")
    slog.bind(group_key="k1")
    slog.Logger("t", stream=out).info("hello", event="alerts_received", severity="shadowed")
    entry = json.loads(out.getvalue())
    assert "time" in entry and entry["time"].endswith("Z") and "timestamp" not in entry
    assert entry["severity"] == "INFO" and entry["field_severity"] == "shadowed"
    assert entry["logging.googleapis.com/trace"] == f"projects/asre-platform/traces/{'a' * 32}"
    assert entry["logging.googleapis.com/spanId"] == "7"
    assert entry["customer_id"] == "example-shop" and entry["group_key"] == "k1"
    assert slog.current_trace_header() == f"{'a' * 32}/7"


def test_no_trace_without_request_context(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    out = io.StringIO()
    logger = slog.Logger("t", stream=out)
    logger.warning("w")
    logger.error("e")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logger.debug("d")
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x["severity"] for x in lines] == ["WARNING", "ERROR", "DEBUG"]
    assert all("logging.googleapis.com/trace" not in x for x in lines)
    assert slog.current_trace_header() is None
    slog.bind_request(("b" * 32, None))
    assert slog.current_trace_header() == "b" * 32
    assert isinstance(slog.get_logger("x"), slog.Logger)
