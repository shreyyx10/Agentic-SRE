import pytest

from asre.core.keys import group_key
from asre.core.models import AlertFamily
from tests import properties as p


def test_p1_4_two_tenants_same_service_do_not_collide():
    assert p.tenants_do_not_collide(group_key)


def test_p1_4_separator_injection_does_not_collide():
    assert p.separator_injection_does_not_collide(group_key)


def test_key_is_sha256_hex_and_deterministic():
    k1 = group_key("acme-corp", "payment-service", AlertFamily.CONTAINER_FAILURE, "prod")
    k2 = group_key("acme-corp", "payment-service", "container_failure", "prod")
    assert k1 == k2 and len(k1) == 64 and int(k1, 16) >= 0


@pytest.mark.parametrize(
    "customer,service,env",
    [
        ("Acme", "svc", "prod"),
        ("acme-corp", "svc|x", "prod"),
        ("acme-corp", "svc", "prod/../x"),
        ("acme-corp", "", "prod"),
    ],
)
def test_invalid_identifiers_are_rejected(customer, service, env):
    with pytest.raises(ValueError):
        group_key(customer, service, "container_failure", env)
