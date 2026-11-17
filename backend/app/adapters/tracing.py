"""LangSmith tracing of agent runs: an observability view, never the system of record.

Off by default (`KLEANDBM_LANGSMITH_TRACING`). The Postgres records (runs, `run_messages`,
`source_evidence`, changesets) stay the audit trail; a trace is for dashboards, latency, cost and
failure triage, and for comparing eval runs across commits.

What a trace holds depends on `KLEANDBM_LANGSMITH_CONTENT`:
- `metadata` (the default): structure and numbers only. Each run keeps its name, timing, status,
  ids and tags, model, token usage, stop reason and tool names, while prompts, transcripts, tool
  arguments and results are dropped before anything leaves the process. Safe for tenant data.
- `full`: inputs and outputs too, with credentials redacted and personal data masked. Only for
  synthetic data such as the eval scenarios; prompts still contain document passages and source
  values, which is why it isn't the default.

Every trace carries the scope (tenant, project, model, conversation, agent, initiating user) as
metadata and the run id as its id, so a trace and its `agent_runs` row share one id. Global
`LANGSMITH_*` variables are ignored: tracing is switched off explicitly unless these settings
turn it on, so a stray environment variable can't start sending unhidden traces.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from app.adapters.llm import AssistantTurn, ChatModel, ToolSpec
from app.adapters.personal_data import mask_text
from app.domain.redaction import redact_credentials

log = logging.getLogger(__name__)

# Keys kept in metadata mode: numbers, statuses, names, never content.
SAFE_KEYS = frozenset({"usage_metadata", "stop_reason", "tool_names", "model", "message_count", "iterations", "stop",
                       "status", "tool", "ok", "is_error", "agent", "outcome"})


def _metadata_only(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"content": "hidden"}
    kept = {k: v for k, v in data.items() if k in SAFE_KEYS}
    return {**kept, "content": "hidden"} if len(kept) < len(data) else kept


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return mask_text(redact_credentials(value)[0])[0]
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _full(data: Any) -> Any:
    return _scrub(data) if isinstance(data, dict) else {"value": _scrub(data)}


@lru_cache(maxsize=4)
def _client(api_key: str, endpoint: str, content: str):
    from langsmith import Client

    hide = _full if content == "full" else _metadata_only
    return Client(api_url=endpoint, api_key=api_key, hide_inputs=hide, hide_outputs=hide,
                  omit_traced_runtime_info=True,
                  hide_metadata=lambda m: {k: v for k, v in (m or {}).items() if not k.startswith("LANGSMITH")})


def settings_enabled(settings) -> bool:
    if not settings.langsmith_tracing:
        return False
    if not settings.langsmith_api_key:
        log.warning("KLEANDBM_LANGSMITH_TRACING is on but KLEANDBM_LANGSMITH_API_KEY is empty; not tracing")
        return False
    return True


def run_metadata(run) -> dict[str, Any]:
    return {"tenant_id": str(run.tenant_id), "project_id": str(run.project_id),
            "conversation_id": str(run.conversation_id) if run.conversation_id else None,
            "model_id": str(getattr(run, "model_id", None)) if getattr(run, "model_id", None) else None,
            "agent": run.agent, "run_id": str(run.id),
            "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
            "initiated_by": str(run.initiated_by) if run.initiated_by else None}


@contextmanager
def agent_run(run, settings=None, client=None) -> Iterator[None]:
    """Trace one top-level agent run, or explicitly don't. `client` replaces the LangSmith client
    (tests); otherwise it comes from settings."""
    from langsmith import run_helpers as rh

    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    if client is None and not settings_enabled(settings):
        with rh.tracing_context(enabled=False):
            yield
        return
    client = client or _client(settings.langsmith_api_key, settings.langsmith_endpoint, settings.langsmith_content)
    metadata = {**run_metadata(run), **(settings.langsmith_metadata or {})}
    tags = [f"agent:{run.agent}", *(settings.langsmith_tags or [])]
    with rh.tracing_context(enabled=True, client=client, project_name=settings.langsmith_project,
                            tags=tags, metadata=metadata):
        with rh.trace(f"{run.agent} run", "chain", run_id=run.id, inputs={"agent": run.agent},
                      metadata=metadata, tags=tags) as root:
            yield
            root.end(outputs={"status": getattr(run.status, "value", run.status),
                              "iterations": run.iterations})


@contextmanager
def child_run(run, name: str | None = None) -> Iterator[None]:
    """Trace a child run (consultation, Validator round) inside its parent's trace. A no-op when
    the parent isn't traced."""
    from langsmith import run_helpers as rh

    if not rh.get_tracing_context().get("enabled"):
        yield
        return
    with rh.trace(name or f"{run.agent} run", "chain", run_id=run.id, inputs={"agent": run.agent},
                  metadata=run_metadata(run), tags=[f"agent:{run.agent}"]) as span:
        yield
        span.end(outputs={"status": getattr(run.status, "value", run.status), "iterations": run.iterations})


@contextmanager
def span(name: str, run_type: str = "chain", inputs: dict[str, Any] | None = None) -> Iterator[Any]:
    """A nested span (a tool call, a design search), or nothing when tracing is off."""
    from langsmith import run_helpers as rh

    if not rh.get_tracing_context().get("enabled"):
        yield None
        return
    with rh.trace(name, run_type, inputs=inputs or {}) as s:
        yield s


def _provider(model: ChatModel) -> str:
    name = type(model).__name__.lower()
    return "anthropic" if "anthropic" in name else "openai" if "openai" in name else "other"


def traced_complete(model: ChatModel, system: str, transcript: list[dict[str, Any]], tools: list[ToolSpec],
                    on_text=None, name: str | None = None) -> AssistantTurn:
    """`model.complete` as an LLM span with token usage, when tracing is on."""
    from langsmith import run_helpers as rh

    if not rh.get_tracing_context().get("enabled"):
        return model.complete(system, transcript, tools, on_text=on_text)
    inputs = {"model": model.model, "message_count": len(transcript), "tool_names": [t.name for t in tools],
              "system": system, "messages": [{k: v for k, v in e.items() if k != "raw"} for e in transcript]}
    meta = {"ls_provider": _provider(model), "ls_model_name": model.model}
    with rh.trace(name or f"{_provider(model)} chat", "llm", inputs=inputs, metadata=meta) as s:
        turn = model.complete(system, transcript, tools, on_text=on_text)
        u = turn.usage
        # Anthropic reports cached tokens apart from input_tokens; OpenAI includes them in it.
        prompt = u.input_tokens if _provider(model) == "openai" else u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
        s.end(outputs={
            "model": turn.model, "stop_reason": turn.stop_reason, "tool_names": [c.name for c in turn.tool_calls],
            "text": turn.text, "tool_calls": [{"name": c.name, "input": c.input} for c in turn.tool_calls],
            "usage_metadata": {"input_tokens": prompt, "output_tokens": u.output_tokens,
                               "total_tokens": prompt + u.output_tokens,
                               "input_token_details": {"cache_read": u.cache_read_tokens,
                                                       "cache_creation": u.cache_write_tokens}},
        })
    return turn
