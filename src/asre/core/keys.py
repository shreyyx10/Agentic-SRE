"""The alert-group key.

Fixes MVP_Readiness_Review_v2 P1-4, where two customers with a service of the
same name shared one document and ``"a|b" + "c"`` collided with ``"a" + "b|c"``:

* ``customer_id`` is part of the key (and of the document path, see dedup.py);
* the key material is canonical JSON, so field boundaries cannot be forged;
* SHA-256, so the key passes routine security scans and FIPS environments.
"""

from __future__ import annotations

import hashlib
import json

from asre.core.models import AlertFamily, validate_customer_id, validate_identifier


def group_key(customer_id: str, service_name: str, family: AlertFamily | str, environment: str) -> str:
    validate_customer_id(customer_id)
    validate_identifier(service_name, "service_name")
    validate_identifier(environment, "environment")
    material = json.dumps(
        {"c": customer_id, "s": service_name, "t": str(family), "e": environment},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()
