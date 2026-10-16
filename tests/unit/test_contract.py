import json

from asre.agent.contract import Outcome, response_schema, validate

KEYS = {"log:a1", "metric:mem", "deploy:d1"}


def good(**over):
    body = {
        "hypothesis": "cartservice v1.45.2 lowered the memory limit below its working set",
        "confidence": 0.8,
        "evidence": [
            {"claim": "OOMKilled at 09:00:12", "source": "logs", "citation": "log:a1"},
            {"claim": "limit lowered 64Mi->20Mi at 08:58", "source": "deployments", "citation": "deploy:d1"},
        ],
        "alternatives": [{"hypothesis": "traffic spike", "why_less_likely": "request rate flat"}],
        "next_steps": [
            {
                "priority": 2,
                "action": "compare limits",
                "expected_signal": "limit < p95 usage",
                "risk": "none",
            },
            {
                "priority": 1,
                "action": "roll back",
                "expected_signal": "restarts stop",
                "risk": "loses change",
            },
        ],
    }
    body.update(over)
    return body


def test_complete_output_sorted_steps():
    r = validate(json.dumps(good()), KEYS)
    assert r.outcome is Outcome.COMPLETE and [s.priority for s in r.output.next_steps] == [1, 2]


def test_uncited_claims_are_dropped():
    body = good(
        evidence=[*good()["evidence"], {"claim": "made up", "source": "metrics", "citation": "metric:zzz"}]
    )
    r = validate(body, KEYS)
    assert r.outcome is Outcome.COMPLETE and r.dropped_claims == 1 and len(r.output.evidence) == 2


def test_hypothesis_without_surviving_evidence_is_withdrawn():
    body = good(evidence=[{"claim": "x", "source": "logs", "citation": "log:nope"}])
    r = validate(body, KEYS)
    assert r.outcome is Outcome.INSUFFICIENT_EVIDENCE and r.output.hypothesis is None
    assert r.output.confidence == 0.0 and r.reason


def test_insufficient_evidence_is_a_success():
    r = validate(good(hypothesis=None, insufficient_evidence=True, missing=["deploy history"]), KEYS)
    assert r.outcome is Outcome.INSUFFICIENT_EVIDENCE and r.reason is None
    assert r.output.missing == ["deploy history"]


def test_missing_alternatives_is_degraded():
    assert validate(good(alternatives=[]), KEYS).outcome is Outcome.DEGRADED


def test_unparseable_and_off_schema_are_degraded():
    assert validate("not json", KEYS).outcome is Outcome.DEGRADED
    assert validate(good(confidence=7), KEYS).outcome is Outcome.DEGRADED
    assert validate(good(extra_field="x"), KEYS).outcome is Outcome.DEGRADED
    assert validate(None, KEYS).outcome is Outcome.DEGRADED


def test_schema_exposed_for_structured_output():
    assert "evidence" in response_schema()["properties"]
