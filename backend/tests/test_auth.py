import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.api.auth import InvalidToken, TokenVerifier
from tests.conftest import AUDIENCE, ISSUER


def test_valid_token_yields_identity_and_permissions(verifier, make_token):
    claims = verifier.verify(make_token("auth0|alice", permissions=["models:read"]))
    assert claims.issuer == ISSUER
    assert claims.subject == "auth0|alice"
    assert "models:read" in claims.permissions


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://some-other-api"},
        {"iss": "https://evil.example/"},
        {"expires_in": -3600},
    ],
    ids=["wrong-audience", "wrong-issuer", "expired"],
)
def test_rejects_bad_claims(verifier, make_token, overrides):
    with pytest.raises(InvalidToken):
        verifier.verify(make_token("auth0|alice", **overrides))


def test_rejects_token_signed_by_another_key(verifier, make_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        verifier.verify(make_token("auth0|alice", key=other))


def test_rejects_symmetric_algorithm_confusion(verifier, make_token):
    # An HS256 token must not be accepted when only RS256 is allowed.
    with pytest.raises(InvalidToken):
        verifier.verify(make_token("auth0|alice", key="shared-secret" * 4, algorithm="HS256"))


def test_rejects_garbage(verifier):
    with pytest.raises(InvalidToken):
        verifier.verify("not-a-jwt")


def test_required_permissions_enforced(signing_key, make_token):
    public_key = signing_key.public_key()
    strict = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        key_resolver=lambda _t: public_key,
        required_permissions=["kleandbm:use"],
    )
    with pytest.raises(InvalidToken):
        strict.verify(make_token("auth0|alice", permissions=["other"]))
    assert strict.verify(make_token("auth0|alice", permissions=["kleandbm:use"]))
    # Auth0 may also deliver granted scopes in the space-separated "scope" claim.
    assert strict.verify(make_token("auth0|alice", scope="openid kleandbm:use"))


def test_verifier_requires_issuer_and_audience():
    with pytest.raises(ValueError):
        TokenVerifier(issuer="", audience=AUDIENCE, key_resolver=lambda _t: None)
