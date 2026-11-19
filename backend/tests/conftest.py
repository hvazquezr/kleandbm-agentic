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

from app.adapters.embeddings import FakeEmbedder
from app.adapters.storage import LocalObjectStore
from app.adapters.vision import FakeImageDescriber
from app.api.auth import TokenVerifier
from app.config import get_settings
from app.main import create_app
from app.persistence.db import get_session
from app.persistence import conversations as _conversation_tables, design as _design_tables, documents as _document_tables, modeling as _modeling_tables, search as _search_tables, sources as _source_tables  # noqa: F401  (registers tables)
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
def object_store(tmp_path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def client(db_session_factory, verifier, object_store) -> Iterator[TestClient]:
    from langgraph.checkpoint.memory import InMemorySaver

    app = create_app(
        token_verifier=verifier, object_store=object_store, embedder=FakeEmbedder(1536),
        session_factory=db_session_factory, agent_checkpointer=InMemorySaver(),
        image_describer=FakeImageDescriber(),
    )

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

        def delete(self, url, **kw):
            return client.delete(url, headers=self.headers, **kw)

    return _User


@pytest.fixture(autouse=True)
def _no_monitor_by_default(monkeypatch):
    """Many tests hand every agent the same scripted model; the transcript monitor would read
    from it after the run. Monitor tests turn it back on (tests/test_monitor.py)."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "monitor_enabled", False)


@pytest.fixture(autouse=True)
def _no_tracing_by_default(monkeypatch):
    """Tests never send traces, whatever backend/.env says: a developer's LangSmith key there
    would otherwise trace every scripted test run to their workspace. Tracing tests turn it on
    with a fake HTTP session (tests/test_tracing.py)."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "langsmith_tracing", False)
    monkeypatch.setattr(get_settings(), "langsmith_api_key", "")


@pytest.fixture
def single_agent_conversation(db_session_factory):
    """Create a conversation with one agent, as the user `sub`, for tests that exercise one agent
    on its own. The API refuses these (one Assistant conversation per model); the domain makes
    one only when asked with `single_agent=True`."""
    from sqlalchemy import select

    from app.domain import conversations, tenancy
    from app.domain.access import Actor, ActorKind
    from app.persistence.models import User

    def _make(base: str, sub: str, *, model_id: str | None = None, agent: str = "business_requirements") -> dict:
        _, tenant, _, project = base.strip("/").split("/")[:4]
        with db_session_factory() as session:
            user = session.scalar(select(User).where(User.auth_subject == sub))
            scope = tenancy.resolve_project_scope(session, Actor(kind=ActorKind.USER, id=user.id),
                                                  uuid.UUID(tenant), uuid.UUID(project))
            c = conversations.create_conversation(session, scope, agent=agent, title=None, single_agent=True,
                                                  model_id=uuid.UUID(model_id) if model_id else None)
            session.commit()
            return {"id": str(c.id), "model_id": model_id, "agent": c.agent, "title": c.title}

    return _make
