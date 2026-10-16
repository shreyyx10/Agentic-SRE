"""Verification of Google-signed OIDC tokens on inbound pushes.

Cloud Run already rejects callers without ``run.invoker`` (there is no
``allUsers`` binding anywhere). This check is the second layer, and it does a
job IAM cannot: it binds the request to a *tenant*. A push for
``/pubsub/{customer_id}`` is accepted only if its token was minted for that
customer's push service account.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol


class AuthError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class TokenVerifier(Protocol):
    def verify(self, token: str, audience: str) -> Mapping[str, Any]: ...


class GoogleOidcVerifier:
    """Verifies signature, expiry, issuer and audience with Google's public keys."""

    def __init__(self) -> None:
        import google.auth.transport.requests
        import requests

        self._request = google.auth.transport.requests.Request(session=requests.Session())

    def verify(self, token: str, audience: str) -> Mapping[str, Any]:
        from google.oauth2 import id_token

        try:
            return id_token.verify_oauth2_token(token, self._request, audience=audience)
        except ValueError as exc:  # bad signature, expired, wrong audience or issuer
            raise AuthError(401, f"invalid token: {exc}") from exc


def bearer_token(headers: Mapping[str, str]) -> str:
    value = headers.get("Authorization") or headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError(401, "missing bearer token")
    return token.strip()


def authenticate(
    headers: Mapping[str, str], verifier: TokenVerifier, audience: str, expected_email: str
) -> Mapping[str, Any]:
    claims = verifier.verify(bearer_token(headers), audience)
    if not claims.get("email_verified", False):
        raise AuthError(403, "token email not verified")
    if str(claims.get("email", "")).lower() != expected_email.lower():
        raise AuthError(403, "token was not minted for this channel's service account")
    return claims


def insecure_local_mode_allowed() -> bool:
    """Local development only. Refused whenever the process runs on Cloud Run."""
    return os.getenv("ASRE_LOCAL_INSECURE") == "1" and not os.getenv("K_SERVICE")
