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
metadata and the run id as its id, so a trace and its `agent_runs` row share one id. Agents
set a run's final status after its traced code returns, and LangSmith takes one update per run,
so an agent run's trace stays open until `record_outcome` ends it with that status (outputs
`status`, LangSmith's error flag with a fixed text for a failed run, and `outcome` feedback):
dashboards and alerts can then count failed and stopped runs. Global
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

# Agent runs traced in this process and not ended yet: run id -> the open RunTree, whether its
# code has finished (`closed`) and whether its final status has been set (`waiting`). LangSmith
# accepts one update per run, and agents set a run's final status after its traced code returns
# (the router just before), so a run is ended, with that status, once both have happened.
_open: dict[Any, dict[str, Any]] = {}
FAILED_ERROR = "agent run failed"
# Agent-run spans (top-level runs, router children, Validator rounds, consultations) are named
# "<something> run" and nothing else is: dashboards count agent runs with Run Name contains " run".
# Tags and metadata can't mark them, since nested runs inherit both.

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
    (tests); otherwise it comes from settings. The run is ended when its outcome is recorded."""
    from langsmith import run_helpers as rh
    from langsmith.run_trees import RunTree

    if getattr(run, "parent_run_id", None) and rh.get_tracing_context().get("enabled"):
        # An agent answering for the request router: a child of the router run's trace.
        with child_run(run):
            yield
        return
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
    tree = RunTree(name=f"{run.agent} run", run_type="chain", id=run.id, inputs={"agent": run.agent},
                   extra={"metadata": metadata}, tags=tags, ls_client=client,
                   session_name=settings.langsmith_project)
    with rh.tracing_context(enabled=True, client=client, project_name=settings.langsmith_project,
                            tags=tags, metadata=metadata, parent=tree):
        yield from _hold(run, tree)


@contextmanager
def child_run(run, name: str | None = None) -> Iterator[None]:
    """Trace a child run (consultation, Validator round) inside its parent's trace. A no-op when
    the parent isn't traced. The run is ended when its outcome is recorded."""
    from langsmith import run_helpers as rh

    parent = rh.get_current_run_tree() if rh.get_tracing_context().get("enabled") else None
    if parent is None:
        yield
        return
    tree = parent.create_child(name or f"{run.agent} run", "chain", run_id=run.id, inputs={"agent": run.agent},
                               extra={"metadata": run_metadata(run)}, tags=[f"agent:{run.agent}"])
    with rh.tracing_context(parent=tree):
        yield from _hold(run, tree)


def _hold(run, tree) -> Iterator[None]:
    tree.post()
    _open[run.id] = {"tree": tree, "closed": False, "waiting": False}
    try:
        yield
    finally:
        _closed(run)


def _final(run) -> bool:
    return getattr(run.status, "value", run.status) not in (None, "running")


def _closed(run) -> None:
    entry = _open.get(run.id)
    if entry is None:
        return
    entry["closed"] = True
    if entry["waiting"] or _final(run):
        _send_outcome(run)


def record_outcome(run) -> None:
    """End a traced run with its final status, once the agent has set it and its traced code has
    returned: outputs `status`, the error flag for a failed run (so LangSmith's error rate and
    error alerts count it), and `outcome` feedback (score 1 completed, 0 otherwise; value the
    status) for charts and alerts on stopped runs. No content: the error text is fixed. Never
    raises; a no-op for a run that wasn't traced. LangSmith takes one update per run, so this is
    the run's only end."""
    entry = _open.get(run.id)
    if entry is None:
        return
    if not entry["closed"]:
        entry["waiting"] = True  # ended when its traced code returns
        return
    _send_outcome(run)


def _send_outcome(run) -> None:
    entry = _open.pop(run.id, None)
    if entry is None:
        return
    tree = entry["tree"]
    status = getattr(run.status, "value", run.status)
    try:
        tree.end(outputs={"status": status, "iterations": run.iterations},
                 error=FAILED_ERROR if status == "failed" else None, end_time=run.finished_at)
        tree.patch()
        tree.ls_client.create_feedback(run.id, key="outcome", score=1 if status == "completed" else 0, value=status,
                                       trace_id=tree.trace_id)
    except Exception:  # tracing must never break a run
        log.warning("couldn't record the outcome of run %s on its trace", run.id, exc_info=True)


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
