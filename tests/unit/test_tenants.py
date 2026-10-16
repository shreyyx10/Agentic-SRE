import json

import pytest

from asre.core.models import AlertType
from asre.core.tenants import TenantRegistry, route
from tests.conftest import TENANT, make_alert


def test_rule_match_wins(tenants):
    a = route(make_alert(service_name="paymentservice"), tenants.get("example-shop"))
    assert (a.team, a.rule_id) == ("payments-squad", "payments")


def test_service_profile_owner_is_next(tenants):
    a = route(make_alert(service_name="checkoutservice"), tenants.get("example-shop"))
    assert (a.team, a.rule_id) == ("checkout-squad", "service_profile")
    assert a.profile.revenue_critical and a.profile.journey == "checkout"


def test_default_team_and_empty_profile(tenants):
    a = route(make_alert(service_name="currencyservice"), tenants.get("example-shop"))
    assert (a.team, a.rule_id, a.profile.journey) == ("platform", "default", "unknown")


@pytest.mark.parametrize(
    "pattern,alert_kwargs,matches",
    [
        ("*", {}, True),
        ("service:checkout*", {}, True),
        ("service:pay*", {}, False),
        ("checkout.service", {"service_name": "checkoutXservice"}, False),  # '.' is literal, not a regex dot
        ("type:oom_killed", {"alert_type": AlertType.OOM_KILLED}, True),
        ("name:Container*", {}, True),
        ("category:Container*", {}, True),
        ("bogus:anything", {}, False),
    ],
)
def test_patterns_are_anchored_globs(tenants, pattern, alert_kwargs, matches):
    base = tenants.get("example-shop").model_dump()
    tenant = type(tenants.get("example-shop")).model_validate(
        {**base, "routing_rules": [{"id": "r", "pattern": pattern, "team": "t"}], "services": {}}
    )
    assert (route(make_alert(**alert_kwargs), tenant).team == "t") is matches


def test_registry_rejects_duplicates_and_bad_ids():
    with pytest.raises(ValueError):
        TenantRegistry.from_json(json.dumps([TENANT, TENANT]))
    with pytest.raises(ValueError):
        TenantRegistry.from_json(json.dumps({**TENANT, "customer_id": "Bad_ID"}))


def test_registry_from_single_object_and_path(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps(TENANT))
    reg = TenantRegistry.from_path(f)
    assert len(reg) == 1 and reg.get("nope") is None
