"""Shared fixtures.

Tests that touch the database need a real PostgreSQL. Point KLEANDBM_TEST_DATABASE_URL at a
throwaway database. Its tables are rebuilt through Alembic (downgrade, then upgrade) on every run.
Without that variable, database tests are skipped with a visible reason (run `pytest -rs`).
"""

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import TokenVerifier
from app.config import get_settings
from app.main import create_app
from app.persistence.db import get_session
from app.persistence import modeling as _modeling_tables  # noqa: F401  (registers tables)
from app.persistence.models import Base

ISSUER = "https://test-issuer.example/"
AUDIENCE = "https://api.kleandbm.test"
BACKEND_DIR = Path(__file__).resolve().parents[1]


# --- Tokens ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def verifier(signing_key) -> TokenVerifier:
    public_key = signing_key.public_key()
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, key_resolver=lambda _t: public_key)


@pytest.fixture(scope="session")
def make_token(signing_key):
    def _make(
        sub: str,
        *,
        iss: str = ISSUER,
        aud: str = AUDIENCE,
        expires_in: int = 300,
        key=None,
        algorithm: str = "RS256",
        **extra,
    ) -> str:
        now = int(time.time())
        claims = {"sub": sub, "iss": iss, "aud": aud, "iat": now, "exp": now + expires_in, **extra}
        return jwt.encode(claims, key or signing_key, algorithm=algorithm)

    return _make


# --- Database -------------------------------------------------------------------------------


def _alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    url = os.environ.get("KLEANDBM_TEST_DATABASE_URL") or get_settings().test_database_url
    if not url:
        pytest.skip("KLEANDBM_TEST_DATABASE_URL not set; database tests need PostgreSQL")
    engine = create_engine(url)
    # Rebuild through Alembic (down, then up) so every run also exercises the downgrade path.
    # Extensions such as pgvector are installed by an administrator and survive this.
    cfg = _alembic_config(url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session_factory(db_engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    yield factory
    # The app commits its own sessions, so clean up by truncating every table after each test.
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def client(db_session_factory, verifier) -> Iterator[TestClient]:
    app = create_app(token_verifier=verifier)

    def _session() -> Iterator[Session]:
        session = db_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        yield c


@pytest.fixture
def as_user(client, make_token):
    """Returns a helper that makes authenticated requests as a given subject."""

    class _User:
        def __init__(self, sub: str) -> None:
            self.headers = {"Authorization": f"Bearer {make_token(sub)}"}
            self.id = uuid.UUID(client.get("/me", headers=self.headers).json()["user_id"])

        def get(self, url, **kw):
            return client.get(url, headers=self.headers, **kw)

        def post(self, url, **kw):
            return client.post(url, headers=self.headers, **kw)

        def put(self, url, **kw):
            return client.put(url, headers=self.headers, **kw)

        def patch(self, url, **kw):
            return client.patch(url, headers=self.headers, **kw)

    return _User
