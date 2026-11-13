from functools import lru_cache

from pydantic import field_validator
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

    # Document storage. "local" keeps files under local_storage_dir (development and tests);
    # "s3" uses the bucket below, with credentials from the standard AWS chain (env vars,
    # ~/.aws profile, or an instance role).
    storage_backend: str = "local"
    local_storage_dir: str = "../var/objects"
    s3_bucket: str = ""
    s3_prefix: str = "kleandbm/"
    s3_region: str = ""
    max_upload_bytes: int = 25 * 1024 * 1024

    # Embeddings for document retrieval. "fake" is deterministic and offline (tests and
    # development without a key); "openai" calls the OpenAI embeddings API.
    embedding_provider: str = "fake"
    embedding_model: str = "text-embedding-3-small"
    # Must match the vector column width in the migration (text-embedding-3-small: 1536).
    embedding_dimensions: int = 1536
    openai_api_key: str = ""

    # Key for encrypting stored secrets such as source passwords (a Fernet key:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").
    secrets_key: str = ""

    # Source access through MCP servers (P2b: MySQL only).
    mcp_dir: str = "../mcp"  # holds the mysql_source package
    # Hosts a source connection may point at. A connection makes the backend open a network
    # connection, so arbitrary hosts would allow probing internal networks (SSRF).
    source_allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    source_query_timeout_ms: int = 5000
    source_call_timeout_s: float = 20.0
    source_max_result_bytes: int = 200_000

    @field_validator("auth_issuer")
    @classmethod
    def _issuer_trailing_slash(cls, value: str) -> str:
        # Auth0's "iss" claim always ends with "/", and issuer validation is an exact match.
        return value if not value or value.endswith("/") else value + "/"

    @property
    def jwks_url(self) -> str:
        return self.auth_jwks_url or f"{self.auth_issuer.rstrip('/')}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
