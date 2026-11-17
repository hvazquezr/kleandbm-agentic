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

    # Reading images at upload (table captures, dashboards): "anthropic" (Claude vision),
    # "fake" (tests) or "none" (store images without reading them).
    image_description_provider: str = "anthropic"
    image_description_model: str = "claude-opus-5-5"
    image_description_effort: str = "medium"

    # Agents. The Business Requirements agent runs on Claude (spec §7); provider and model are
    # per-agent settings so they can change without code changes.
    anthropic_api_key: str = ""
    requirements_agent_provider: str = "anthropic"  # "anthropic" | "fake" (scripted, tests only)
    requirements_agent_model: str = "claude-opus-5-5"
    requirements_agent_effort: str = "high"  # low | medium | high | xhigh | max
    agent_max_iterations: int = 14  # model calls per run, a hard execution budget
    # The Architect investigates sources and designs a whole model, so it gets a larger budget.
    architect_agent_provider: str = "anthropic"  # "anthropic" | "fake"
    architect_agent_model: str = "claude-opus-5-5"
    architect_agent_effort: str = "high"
    architect_max_iterations: int = 40
    # ask_requirements: Business Requirements consultations per Architect run, and the model
    # calls each consultation may make. Bounded so the exchange never becomes its own loop.
    architect_max_consultations: int = 3
    consultation_max_iterations: int = 8
    # The Validator runs on a different model family than the Architect (spec §7): GPT-6 Astra
    # by default, "anthropic" with claude-sonnet-5-5 as the interim. "none" skips review, and
    # the model then stays Draft with validation unverified.
    validator_agent_provider: str = "openai"  # "openai" | "anthropic" | "fake" | "none"
    validator_agent_model: str = "gpt-6-astra"
    validator_agent_effort: str = "high"
    validator_max_iterations: int = 16  # model calls per review round
    validator_max_rounds: int = 3  # correction rounds per review episode (Architect run)
    validator_max_attempts_per_issue: int = 3  # carried across episodes
    # Selective Tree of Thought (spec §7): bounded, local beam search at recorded technical
    # decision points. Disable it for the single-candidate baseline.
    architect_tot_enabled: bool = True
    architect_max_decision_points: int = 2  # per Architect run
    tot_children: int = 3  # distinct alternatives per expanded node
    tot_beam: int = 2  # candidates kept per level
    tot_score_threshold: float = 2.5  # weighted 0-5 score an alternative needs to stay eligible
    tot_max_model_calls: int = 14  # generation + evaluation calls per decision point
    tot_max_tool_calls: int = 24  # source probes during evaluation per decision point
    tot_max_seconds: int = 900
    tot_max_output_tokens: int = 80000
    agent_max_tokens: int = 16000
    # The transcript monitor (app/agents/monitor): after each top-level run, a model from another
    # family than the Architect reads the stored transcript and scores how likely it is that an
    # agent misbehaved. Triage only; a score at or above the threshold flags the run for review.
    # "none" or a missing key records the assessment as skipped.
    monitor_enabled: bool = True
    monitor_agent_provider: str = "openai"  # "openai" | "anthropic" | "fake" | "none"
    monitor_agent_model: str = "gpt-6-astra"
    monitor_agent_effort: str = "medium"
    monitor_samples: int = 1  # assessments averaged per run
    monitor_flag_threshold: int = 50
    monitor_max_chars: int = 300_000  # rendered transcript cap; tool results are shortened first
    # LangGraph checkpoints live in Postgres but apart from the registers. Empty disables them.
    agent_checkpoints: bool = True

    # LangSmith tracing (app/adapters/tracing.py): an observability view, off by default.
    # "metadata" sends structure, timing, ids, models, token usage and tool names only; "full"
    # adds prompts and tool data (credentials redacted, personal data masked), for synthetic
    # eval data only. Global LANGSMITH_* variables are ignored.
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "kleandbm"
    langsmith_content: str = "metadata"  # metadata | full
    langsmith_tags: list[str] = []
    langsmith_metadata: dict[str, str] = {}  # added to every trace (the eval runner sets experiment ids)

    @field_validator("langsmith_content")
    @classmethod
    def _content_mode(cls, value: str) -> str:
        if value not in ("metadata", "full"):
            raise ValueError("langsmith_content must be 'metadata' or 'full'")
        return value

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
