from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.adapters.embeddings import Embedder, embedder_from_settings
from app.adapters.storage import ObjectStore, store_from_settings
from app.api.auth import TokenVerifier, verifier_from_settings
from app.api.document_routes import router as document_router
from app.api.modeling_routes import router as modeling_router
from app.api.routes import router
from app.config import get_settings
from app.domain.access import AccessDenied, NotFoundInScope
from app.domain.modeling import StaleUpdate
from app.domain.tenancy import InvalidOperation


def create_app(
    token_verifier: TokenVerifier | None = None,
    object_store: ObjectStore | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    app = FastAPI(title="KleanDBM API", version="0.1.0")
    settings = get_settings()

    # Tests inject their own adapters; otherwise they come from settings.
    app.state.object_store = object_store or store_from_settings(settings)
    app.state.embedder = embedder or embedder_from_settings(settings)

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
    return app


app = create_app()
