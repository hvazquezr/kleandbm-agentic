"""Auth0 access-token validation.

The token is checked for signature, issuer, audience, expiry, and required permissions.
Passing these checks only identifies the caller. Tenant and project authorization happens
separately, against current memberships in the database.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import jwt

from app.config import Settings

KeyResolver = Callable[[str], object]


class InvalidToken(Exception):
    pass


# Auth0 only lets custom claims into access tokens under a namespace. An Auth0 Action adds the
# user's profile as `https://kleandbm.ai/name` and `.../email`; plain `name`/`email` are read too.
CLAIM_NAMESPACE = "https://kleandbm.ai/"


def _profile_claim(claims: dict, key: str) -> str | None:
    value = claims.get(CLAIM_NAMESPACE + key) or claims.get(key)
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class TokenClaims:
    issuer: str
    subject: str
    permissions: frozenset[str] = field(default_factory=frozenset)
    name: str | None = None
    email: str | None = None


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        key_resolver: KeyResolver,
        algorithms: list[str] | None = None,
        required_permissions: list[str] | None = None,
        leeway_seconds: int = 30,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("issuer and audience must both be configured")
        self._issuer = issuer
        self._audience = audience
        self._key_resolver = key_resolver
        self._algorithms = algorithms or ["RS256"]
        self._required = frozenset(required_permissions or [])
        self._leeway = leeway_seconds

    def verify(self, token: str) -> TokenClaims:
        try:
            key = self._key_resolver(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc

        permissions = frozenset(claims.get("permissions") or []) | frozenset(
            (claims.get("scope") or "").split()
        )
        missing = self._required - permissions
        if missing:
            raise InvalidToken(f"missing required permissions: {sorted(missing)}")

        return TokenClaims(
            issuer=claims["iss"],
            subject=claims["sub"],
            permissions=permissions,
            name=_profile_claim(claims, "name"),
            email=_profile_claim(claims, "email"),
        )


def jwks_key_resolver(jwks_url: str) -> KeyResolver:
    client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def resolve(token: str) -> object:
        return client.get_signing_key_from_jwt(token).key

    return resolve


def verifier_from_settings(settings: Settings) -> TokenVerifier:
    return TokenVerifier(
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
        key_resolver=jwks_key_resolver(settings.jwks_url),
        algorithms=settings.auth_algorithms,
        required_permissions=settings.auth_required_permissions,
    )
