import pytest

from asre.agent.sanitise import luhn_valid, sanitise, verify


# Fake, secret-SHAPED fixtures. Each is assembled at runtime from fragments so that
# no complete credential-shaped literal exists in the repository: secret scanners
# (gitleaks in CI) stay clean without an allow-list that could hide a real leak.
def _fake(*parts: str) -> str:
    return "".join(parts)


SECRETS = [
    ("gcp_api_key", _fake("key=", "AIza", "SyA-1234567890", "abcdefghijklmnopqrstu")),
    ("aws_access_key", _fake("using ", "AKIA", "IOSFODNN7EXAMPLE", " for s3")),
    ("github_token", _fake("ghp", "_", "a" * 36)),
    ("slack_token", _fake("xox", "b-", "1234567890-abcdefghij")),
    ("jwt", _fake("ey", "JhbGciOiJIUzI1NiJ9", ".", "ey", "JzdWIiOiIxMjM0NTY3ODkwIn0", ".", "sig" * 12)),
    ("bearer", _fake("Authorization: ", "Bearer ", "abcdefghijklmnop", "1234567890")),
    ("secret_assignment", _fake("db pass", "word=", '"', "hunter2", "hunter2", '"', " retry")),
    ("email", "customer jane.doe@example.com placed order"),
    (
        "private_key",
        _fake("-----BEGIN ", "PRIVATE KEY-----\n", "MIIEvQIBADANBgkq", "\n-----END ", "PRIVATE KEY-----"),
    ),
]


@pytest.mark.parametrize("name,text", SECRETS)
def test_each_pattern_is_redacted_and_verified_clean(name, text):
    result = sanitise(text)
    assert result.hits[name] >= 1, result.text
    assert verify(result.text) == [], result.text


def test_pan_masked_only_when_luhn_valid():
    text = "charge card 4111 1111 1111 1111 order 1234567890123 ts 1790563493792"
    result = sanitise(text)
    assert "[REDACTED:pan]" in result.text and "4111" not in result.text
    assert "1234567890123" in result.text and "1790563493792" in result.text  # not Luhn-valid: kept
    assert result.hits["pan"] == 1 and verify(result.text) == []


def test_luhn():
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112") and not luhn_valid("123")


def test_verify_catches_what_sanitise_did_not_see():
    assert verify(_fake("leaked 5555555555554444 and ", "AKIA", "IOSFODNN7EXAMPLE")) == [
        "aws_access_key",
        "pan",
    ]


def test_clean_text_untouched():
    text = "OOMKilled: container cartservice exceeded memory limit 64Mi"
    result = sanitise(text)
    assert result.text == text and result.total == 0
