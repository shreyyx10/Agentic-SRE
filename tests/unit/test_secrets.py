import base64

import pytest

from asre.core import config
from asre.core.secrets import SecretError, SecretReader

NAME = "projects/p/secrets/chat-webhook-url"


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return self.responses.pop(0)


def _ok(value: str) -> FakeResponse:
    return FakeResponse(200, {"payload": {"data": base64.b64encode(value.encode()).decode()}})


def test_reads_latest_version_and_caches_until_ttl():
    now = [0.0]
    session = FakeSession([_ok("v1"), _ok("v2")])
    reader = SecretReader({"chat-webhook-url": NAME}, lambda: session, ttl_s=60, clock=lambda: now[0])
    assert reader.get("chat-webhook-url") == "v1"
    assert reader.get("chat-webhook-url") == "v1"  # cached
    now[0] = 61
    assert reader.get("chat-webhook-url") == "v2"  # rotated value picked up after the TTL
    assert session.urls == [f"https://secretmanager.googleapis.com/v1/{NAME}/versions/latest:access"] * 2
    assert reader.available() == ["chat-webhook-url"]


def test_unknown_secret_is_refused_without_a_call():
    session = FakeSession([])
    with pytest.raises(SecretError, match="not configured"):
        SecretReader({}, lambda: session).get("anything")
    assert session.urls == []


@pytest.mark.parametrize(
    "response",
    [FakeResponse(404, {"error": "NOT_FOUND"}), FakeResponse(200, {"payload": {}}), FakeResponse(200, None)],
)
def test_errors_never_include_a_value(response):
    reader = SecretReader({"s": NAME}, lambda: FakeSession([response]))
    with pytest.raises(SecretError) as exc:
        reader.get("s")
    assert "payload" not in str(exc.value)


def test_secret_names_from_environment(monkeypatch):
    monkeypatch.setenv("ASRE_SECRET_CHAT_WEBHOOK_URL", NAME)
    monkeypatch.setenv("ASRE_SECRET_EMPTY", "")
    assert config.secret_names() == {"chat-webhook-url": NAME}
