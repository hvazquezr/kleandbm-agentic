"""Describe with AI: one model call that fills in the New table form from a description.

It writes nothing to the model or the registers. The user reviews the suggestion and saves it
through the usual changeset, so the table is the user's own edit. The call is still an agent run
(`agent="table_suggestion"`, in the model's Assistant conversation, with no message), so its
usage, transcript and trace are attributed to the initiating user like every other model call.
Its input is the user's description and the names, kinds and keys of the model's tables, never
documents, sources or agent replies."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import tracing
from app.adapters.llm import ChatModel, LLMError, ToolSpec
from app.adapters.tracing import traced_complete
from app.agents.runtime import record_usage
from app.agents.table_suggestion.prompt import SYSTEM_PROMPT
from app.agents.transcripts import RunTranscript
from app.domain import conversations, modeling
from app.domain.access import Permission
from app.domain.records import Additivity, AttributeRole, LogicalType, RecordType, ScdType, TableKind
from app.domain.redaction import redact_credentials
from app.domain.tenancy import InvalidOperation, ProjectScope
from app.persistence.conversations import AgentRun, Conversation, RunStatus

AGENT_NAME = "table_suggestion"
MAX_DESCRIPTION = 2000
MAX_CONTEXT_TABLES = 60
KEY_ROLES = (AttributeRole.SURROGATE_KEY, AttributeRole.NATURAL_KEY)


class SuggestedAttribute(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    logical_type: LogicalType
    role: AttributeRole
    is_nullable: bool
    additivity: Additivity | None = None
    description: str | None = Field(default=None, max_length=4000)


class SuggestedTable(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: TableKind
    grain: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=4000)
    scd_type: ScdType = ScdType.NONE


class TableSuggestion(BaseModel):
    run_id: uuid.UUID
    table: SuggestedTable
    attributes: list[SuggestedAttribute]
    notes: str | None = None


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    # Strings only: strict tools refuse an enum with a null type, so additivity uses "none".
    return {**schema, "type": [schema["type"], "null"]}


SUGGEST_SPEC = ToolSpec(
    name="suggest_table",
    description="Fill in the New table form: one table and its attributes, for the user to review.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "kind", "grain", "description", "scd_type", "attributes", "notes"],
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": [k.value for k in TableKind]},
            "grain": _nullable({"type": "string", "description": "For a fact: one row per what. Null otherwise."}),
            "description": _nullable({"type": "string"}),
            "scd_type": {"type": "string", "enum": [s.value for s in ScdType]},
            "attributes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "logical_type", "role", "is_nullable", "additivity", "description"],
                    "properties": {
                        "name": {"type": "string"},
                        "logical_type": {"type": "string", "enum": [t.value for t in LogicalType]},
                        "role": {"type": "string", "enum": [r.value for r in AttributeRole]},
                        "is_nullable": {"type": "boolean"},
                        "additivity": {"type": "string", "enum": [*(a.value for a in Additivity), "none"],
                                       "description": "For measures; \"none\" otherwise."},
                        "description": _nullable({"type": "string"}),
                    },
                },
            },
            "notes": _nullable({"type": "string", "description": "What you added or assumed, and what was unclear. Null if nothing."}),
        },
    },
)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def model_tables(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> list[dict[str, Any]]:
    """Names, kinds and key attributes of the model's tables: enough to follow its naming and
    point foreign keys at the right keys."""
    snapshot = modeling.get_snapshot(session, scope, model_id)
    keys: dict[str, list[str]] = {}
    for a in snapshot.records[RecordType.ATTRIBUTE]:
        if a.data.get("role") in KEY_ROLES:
            keys.setdefault(str(a.data["table_id"]), []).append(
                f"{_clip(a.data['name'], 80)} ({a.data['role']}, {a.data['logical_type']})")
    return [{"name": _clip(t.data["name"], 120), "kind": str(t.data["kind"]), "keys": keys.get(str(t.id), [])}
            for t in snapshot.records[RecordType.TABLE]][:MAX_CONTEXT_TABLES]


def brief(tables: list[dict[str, Any]], description: str) -> str:
    lines = [f"- {t['name']} ({t['kind']})" + (f": keys {', '.join(t['keys'])}" if t["keys"] else "") for t in tables]
    return ("<model_tables>\n" + ("\n".join(lines) if lines else "(none yet)") + "\n</model_tables>\n\n"
            f"<request>\n{description}\n</request>")


def parse(turn, existing: set[str]) -> tuple[SuggestedTable, list[SuggestedAttribute], str | None]:
    call = next((c for c in turn.tool_calls if c.name == SUGGEST_SPEC.name), None)
    if call is None:
        raise LLMError("the model didn't suggest a table")
    args = dict(call.input)
    try:
        table = SuggestedTable.model_validate({k: args.get(k) for k in ("name", "kind", "grain", "description", "scd_type")})
        attributes = [SuggestedAttribute.model_validate({**a, "additivity": None if a.get("additivity") in (None, "", "none")
                                                         else a["additivity"]}) for a in args.get("attributes") or []]
    except ValidationError as exc:
        raise LLMError(f"the model's suggestion wasn't a valid table: {exc.errors()[0]['msg']}") from exc
    if table.kind is not TableKind.FACT:
        table.grain = None
    for a in attributes:
        if a.role is not AttributeRole.MEASURE:
            a.additivity = None
    notes = (args.get("notes") or "").strip() or None
    if table.name.lower() in existing:
        clash = f"A table named {table.name} already exists in this model."
        notes = f"{notes} {clash}" if notes and clash not in notes else notes or clash
    return table, attributes, notes


def _assistant_conversation(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> Conversation:
    conversation = session.scalar(select(Conversation).where(
        Conversation.tenant_id == scope.tenant_id, Conversation.project_id == scope.project_id,
        Conversation.model_id == model_id, Conversation.agent == conversations.ROUTED,
    ).order_by(Conversation.created_at).limit(1))
    return conversation or conversations.create_conversation(
        session, scope, agent=conversations.ROUTED, model_id=model_id, title=None)


def suggest_table(
    session: Session, scope: ProjectScope, model_id: uuid.UUID, description: str, chat_model: ChatModel
) -> TableSuggestion:
    """One model call that suggests a table for the New table form. Writes nothing to the model;
    records the call as a run. Raises LLMError when the model fails or gives no valid table."""
    scope.require(Permission.PROJECT_EDIT)
    modeling.get_model(session, scope, model_id)
    text, _ = redact_credentials(description or "")
    text = text.strip()
    if not text:
        raise InvalidOperation("describe the table you want")
    if len(text) > MAX_DESCRIPTION:
        raise InvalidOperation(f"keep the description under {MAX_DESCRIPTION} characters")
    tables = model_tables(session, scope, model_id)
    conversation = _assistant_conversation(session, scope, model_id)
    run = AgentRun(
        tenant_id=scope.tenant_id, project_id=scope.project_id, conversation_id=conversation.id,
        agent=AGENT_NAME, llm_model=chat_model.model, status=RunStatus.RUNNING,
        initiated_by=conversations._user(scope), changeset_seqs=[],
    )
    session.add(run)
    session.flush()
    user_brief = brief(tables, text)
    recorder = RunTranscript(session, run)
    recorder.system(SYSTEM_PROMPT)
    recorder.add([{"role": "user", "text": user_brief}])
    session.commit()
    try:
        with tracing.agent_run(run):
            turn = traced_complete(chat_model, SYSTEM_PROMPT, [{"role": "user", "text": user_brief}],
                                   [SUGGEST_SPEC], name="table suggestion model")
        RunTranscript(session, run).add([turn.to_transcript()], stop_reason=turn.stop_reason, model=turn.model)
        record_usage(run, turn.usage, [])
        run.iterations = 1
        table, attributes, notes = parse(turn, {t["name"].lower() for t in tables})
    except LLMError as exc:
        run.status, run.status_detail, run.finished_at = RunStatus.FAILED, str(exc)[:2000], datetime.now(UTC)
        session.commit()
        raise
    run.status, run.finished_at = RunStatus.COMPLETED, datetime.now(UTC)
    session.commit()
    return TableSuggestion(run_id=run.id, table=table, attributes=attributes, notes=notes)
