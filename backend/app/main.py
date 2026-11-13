from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.adapters.embeddings import Embedder, embedder_from_settings
from app.adapters.llm import AnthropicChatModel, ChatModel, LLMError, ScriptedChatModel
from app.adapters.sources import SourceConnector, StdioMySqlConnector
from app.adapters.storage import ObjectStore, store_from_settings
from app.adapters.vision import ImageDescriber, describer_from_settings
from app.api.auth import TokenVerifier, verifier_from_settings
from app.api.conversation_routes import router as conversation_router
from app.api.design_routes import router as design_router
from app.api.document_routes import router as document_router
from app.api.source_routes import router as source_router
from app.api.modeling_routes import router as modeling_router
from app.api.routes import router
from app.config import get_settings
from app.persistence.db import _session_factory
from app.domain.access import AccessDenied, NotFoundInScope
from app.domain.modeling import StaleUpdate
from app.domain.tenancy import InvalidOperation


def _chat_model_from_settings(settings, agent: str = "business_requirements") -> ChatModel:
    """The chat model for one agent, from that agent's provider, model and effort settings."""
    prefix = {"business_requirements": "requirements_agent", "architect": "architect_agent"}[agent]
    provider = getattr(settings, f"{prefix}_provider")
    if provider == "anthropic":
        return AnthropicChatModel(
            settings.anthropic_api_key, getattr(settings, f"{prefix}_model"),
            getattr(settings, f"{prefix}_effort"), settings.agent_max_tokens,
        )
    if provider == "fake":
        return ScriptedChatModel([{"text": f"The fake model is configured; set KLEANDBM_{prefix.upper()}_PROVIDER=anthropic."}])
    raise LLMError(f"unknown agent provider {provider!r}")


class _LazyCheckpointer:
    """Opens the Postgres checkpointer on first use, so importing the app never touches the
    database. Checkpoint tables are created by LangGraph and kept apart from the registers."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self._saver = None
        self._pool = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool, self._saver = None, None

    def resolve(self):
        if not self._settings.agent_checkpoints:
            return None
        if self._saver is None:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            url = self._settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            pool = ConnectionPool(url, max_size=5, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
            self._pool = pool
            saver = PostgresSaver(pool)
            saver.setup()
            self._saver = saver
        return self._saver


def create_app(
    token_verifier: TokenVerifier | None = None,
    object_store: ObjectStore | None = None,
    embedder: Embedder | None = None,
    source_connector: SourceConnector | None = None,
    chat_model_factory=None,
    session_factory=None,
    agent_checkpointer=None,
    image_describer: ImageDescriber | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        holder = app.state.agent_checkpointer
        if hasattr(holder, "close"):
            holder.close()

    app = FastAPI(title="KleanDBM API", version="0.1.0", lifespan=lifespan)
    settings = get_settings()

    # Tests inject their own adapters; otherwise they come from settings.
    app.state.object_store = object_store or store_from_settings(settings)
    app.state.embedder = embedder or embedder_from_settings(settings)
    app.state.image_describer = image_describer or describer_from_settings(settings)
    # Agents run in worker threads with their own sessions, so they need a factory, not a session.
    app.state.session_factory = session_factory or _session_factory()
    # Called with the agent name ("business_requirements", "architect").
    app.state.chat_model_factory = chat_model_factory or (lambda agent: _chat_model_from_settings(settings, agent))
    app.state.agent_checkpointer = agent_checkpointer if agent_checkpointer is not None else _LazyCheckpointer(settings)
    app.state.source_connector = source_connector or StdioMySqlConnector(
        settings.mcp_dir, settings.source_query_timeout_ms, settings.source_call_timeout_s,
        settings.source_max_result_bytes,
    )

    if token_verifier is None:
        if settings.auth_issuer and settings.auth_audience:
            token_verifier = verifier_from_settings(settings)
    # Left as None when auth isn't configured: authenticated routes then return 503.
    app.state.token_verifier = token_verifier

    @app.exception_handler(NotFoundInScope)
    def _not_found(_: Request, __: NotFoundInScope) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(AccessDenied)
    def _forbidden(_: Request, exc: AccessDenied) -> JSONResponse:
        return JSONResponse(
            {"detail": "forbidden", "required_permission": str(exc.args[0])},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.exception_handler(InvalidOperation)
    def _invalid(_: Request, exc: InvalidOperation) -> JSONResponse:
        return JSONResponse(
            {"error": "invalid_operation", "detail": str(exc)},
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(StaleUpdate)
    def _stale(_: Request, exc: StaleUpdate) -> JSONResponse:
        return JSONResponse(
            {
                "error": "stale_update",
                "detail": str(exc),
                "record_id": str(exc.record_id),
                "expected_seq": exc.expected_seq,
                "current_seq": exc.current_seq,
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    app.include_router(router)
    app.include_router(modeling_router)
    app.include_router(document_router)
    app.include_router(source_router)
    app.include_router(conversation_router)
    app.include_router(design_router)
    return app


app = create_app()
