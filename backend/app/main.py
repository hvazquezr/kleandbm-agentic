from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.auth import TokenVerifier, verifier_from_settings
from app.api.routes import router
from app.config import get_settings
from app.domain.access import AccessDenied, NotFoundInScope
from app.domain.tenancy import InvalidOperation


def create_app(token_verifier: TokenVerifier | None = None) -> FastAPI:
    app = FastAPI(title="KleanDBM API", version="0.1.0")

    if token_verifier is None:
        settings = get_settings()
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
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    app.include_router(router)
    return app


app = create_app()
