from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables prefixed with KLEANDBM_."""

    model_config = SettingsConfigDict(env_prefix="KLEANDBM_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://kleandbm:kleandbm@localhost:5432/kleandbm"
    # Throwaway database for pytest only. Its schema is dropped and rebuilt on each run.
    test_database_url: str = ""

    # Auth0 access-token validation. The issuer is the tenant URL with a trailing slash,
    # e.g. "https://example.us.auth0.com/". The JWKS URL defaults to the issuer's well-known path.
    auth_issuer: str = ""
    auth_audience: str = ""
    auth_jwks_url: str = ""
    auth_algorithms: list[str] = ["RS256"]
    # Token permissions (Auth0 RBAC "permissions" claim) every API call must carry.
    # Empty means only the application-level tenant/project checks apply.
    auth_required_permissions: list[str] = []

    @property
    def jwks_url(self) -> str:
        return self.auth_jwks_url or f"{self.auth_issuer.rstrip('/')}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
