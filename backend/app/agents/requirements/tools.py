"""Tools for the Business Requirements agent.

Every tool goes through the same scoped domain services as a person: the agent's scope is an
agent actor acting on behalf of the user who sent the message, so tenant/project isolation,
permissions, and the agent-authority guard in apply_changeset all apply. Tool inputs are
validated again here, because model output is untrusted.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.embeddings import Embedder
from app.adapters.llm import ToolSpec
from app.adapters.storage import ObjectStore
from app.adapters.vision import VisionError, prepare_image
from app.domain import documents, modeling
from app.domain.access import NotFoundInScope, ProjectScope
from app.domain.modeling import CreateOp, LinkOp, StaleUpdate, UpdateOp
from app.domain.records import (
    DefinitionKind,
    DefinitionProvenance,
    FeasibilityStatus,
    LinkType,
    OriginKind,
    QuestionKind,
    QuestionStatus,
    RecordType,
)
from app.domain.tenancy import InvalidOperation

READ_PAGE = 12  # passages per read_document call


from app.agents.runtime import ToolInputError  # noqa: E402  (shared with the other agents)


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _enum(values, *extra: str) -> dict:
    return {"type": "string", "enum": [v.value for v in values] + list(extra)}


def _opt(description: str) -> dict:
    # Strict tool use allows few union types per request (16 across all tools), so optional
    # text is a plain string where "" means none; handlers turn "" into null.
    return {"type": "string", "description": f"{description} Use an empty string if none."}


def _keep(description: str) -> dict:
    return {"type": "string", "description": f"{description} Empty string leaves it unchanged."}


def _none_if_empty(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


TOOL_SPECS = [
    ToolSpec("list_documents", "List the project's documents with their latest version, status and passage count.", _obj({})),
    ToolSpec(
        "read_document",
        f"Read passages of one document version in order, {READ_PAGE} at a time, with their locations. "
        "Use it for systematic review of a document.",
        # Strict tool schemas don't support numeric bounds; the handlers clamp values instead.
        _obj({"version_id": {"type": "string"}, "start": {"type": "integer", "description": "First passage ordinal, 0 or more"}}),
    ),
    ToolSpec(
        "view_image",
        "Look at an image document (report screenshot, table capture, dashboard) directly. Use it when "
        "its machine transcription is unclear or you need to check a figure, chart, axis or filter.",
        _obj({"version_id": {"type": "string"}}),
    ),
    ToolSpec(
        "search_documents",
        "Semantic search over the latest version of every project document. Returns passages with locations.",
        _obj({"query": {"type": "string"}, "limit": {"type": "integer", "description": "1 to 10 results"}}),
    ),
    ToolSpec(
        "get_registers",
        "Current requirements, business definitions, questions and decisions, with ids, codes, seq "
        "(needed to update) and statuses, plus links between them.",
        _obj({}),
    ),
    ToolSpec(
        "record_requirement",
        "Record one analytical requirement. Link the definitions it uses via uses_definition_ids.",
        _obj({
            "statement": {"type": "string", "description": "What the business needs to analyze, in one sentence"},
            "origin_kind": _enum(OriginKind),
            "origin_ref": {"type": "string", "description": "Document, version and location, or 'user message'"},
            "feasibility_status": _enum(FeasibilityStatus),
            "feasibility_notes": _opt("Why it is or isn't supported."),
            "is_proposed_alternative": {"type": "boolean"},
            "alternative_for_id": _opt("Id of the requirement this proposes an alternative to."),
            "uses_definition_ids": {"type": "array", "items": {"type": "string"}},
        }),
    ),
    ToolSpec(
        "record_definition",
        "Record one business term, metric or concept with its provenance. Unknown metric details stay null.",
        _obj({
            "term": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "kind": _enum(DefinitionKind),
            "definition": _opt("The meaning in plain language."),
            "business_scope": _opt("Whose meaning this is, e.g. 'Finance close reporting'."),
            "metric": _obj({
                "formula": _opt("Metric formula; for non-metrics or if unknown,"),
                "inclusions": _opt("What is included; if unknown,"),
                "exclusions": _opt("What is excluded; if unknown,"),
                "grain": _opt("Grain it applies at; if unknown,"),
                "time_basis": _opt("Time basis, e.g. original order month; if unknown,"),
                "aggregation": _opt("How it may be aggregated; if unknown,"),
            }),
            "provenance": _enum(DefinitionProvenance),
            "origin_ref": {"type": "string"},
            "conflicts_with_ids": {"type": "array", "items": {"type": "string"}},
        }),
    ),
    ToolSpec(
        "update_entry",
        "Change fields of an existing requirement, definition or question. Pass its current seq from "
        "get_registers; null fields are left unchanged.",
        _obj({
            "record_id": {"type": "string"},
            "expected_seq": {"type": "integer"},
            "statement": _keep("New requirement statement."),
            "definition": _keep("New definition text."),
            "business_scope": _keep("New business scope."),
            "origin_ref": _keep("New origin reference."),
            "feasibility_status": _enum(FeasibilityStatus, "unchanged"),
            "feasibility_notes": _keep("New feasibility notes."),
            "question_status": _enum(QuestionStatus, "unchanged"),
            "resolution": _keep("How the question was resolved, quoting the user."),
        }),
    ),
    ToolSpec(
        "raise_question",
        "Record a clarification, contradiction or missing-information question for the user, "
        "linked to the records it concerns.",
        _obj({
            "kind": {"type": "string", "enum": ["clarification", "contradiction", "missing_information"]},
            "description": {"type": "string"},
            "impact": _opt("What this affects."),
            "is_blocking": {"type": "boolean"},
            "proposed_action": _opt("What you suggest doing."),
            "concerns_ids": {"type": "array", "items": {"type": "string"}},
        }),
    ),
    ToolSpec(
        "link_records",
        "Link two register records. uses_definition: requirement -> definition; conflicts_with: "
        "definition <-> definition or requirement <-> requirement; alternative_for: proposed -> original "
        "requirement; concerns: question -> any record.",
        _obj({
            "from_id": {"type": "string"},
            "to_id": {"type": "string"},
            "link_type": {"type": "string", "enum": ["uses_definition", "conflicts_with", "alternative_for", "concerns"]},
        }),
    ),
]


@dataclass
class ToolContext:
    session: Session
    scope: ProjectScope  # agent actor on behalf of the user
    embedder: Embedder
    reason: str
    store: ObjectStore | None = None
    changeset_seqs: list[int] = field(default_factory=list)
    changed: list[dict[str, str]] = field(default_factory=list)  # {"id", "code", "type"}


def _uuid(value: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolInputError(f"{what} {value!r} is not a valid id; use ids from get_registers or list_documents") from None


def _write(ctx: ToolContext, ops: list) -> modeling.ChangesetResult:
    try:
        result = modeling.apply_changeset(ctx.session, ctx.scope, ops, reason=ctx.reason)
    except StaleUpdate as exc:
        raise ToolInputError(f"that record changed since you read it (now at seq {exc.current_seq}); call get_registers and retry") from exc
    except InvalidOperation as exc:
        raise ToolInputError(str(exc)) from exc
    ctx.changeset_seqs.append(result.seq)
    return result


def _note_change(ctx: ToolContext, record_id: uuid.UUID) -> dict[str, str]:
    view = modeling.get_record(ctx.session, ctx.scope, record_id)
    entry = {"id": str(view.id), "code": view.code or "", "type": str(view.type)}
    ctx.changed.append(entry)
    return entry


# --- Read tools -----------------------------------------------------------------------------


def list_documents(ctx: ToolContext, _: dict) -> dict:
    out = []
    for doc, version in documents.list_documents(ctx.session, ctx.scope):
        out.append({
            "document_id": str(doc.id), "title": doc.title, "version_id": str(version.id),
            "version_no": version.version_no, "filename": version.filename, "status": str(version.status),
            "passages": len(documents.list_passages(ctx.session, ctx.scope, version.id)),
            "notes": list(version.notes or []),
        })
    return {"documents": out, "summary": f"Listed {len(out)} document(s)"}


def read_document(ctx: ToolContext, args: dict) -> dict:
    version_id = _uuid(args["version_id"], "version_id")
    start = max(0, int(args.get("start") or 0))
    try:
        version = documents.get_version(ctx.session, ctx.scope, version_id)
        passages = documents.list_passages(ctx.session, ctx.scope, version_id)
    except NotFoundInScope:
        raise ToolInputError("no such document version in this project") from None
    page = passages[start : start + READ_PAGE]
    doc = documents.get_document(ctx.session, ctx.scope, version.document_id)
    return {
        "document": doc.title, "version_no": version.version_no, "total_passages": len(passages),
        "passages": [{"ordinal": p.ordinal, "location": p.location, "text": p.text} for p in page],
        "next_start": start + READ_PAGE if start + READ_PAGE < len(passages) else None,
        "summary": f"Read {doc.title} v{version.version_no} passages {start}-{start + len(page) - 1}",
    }


IMAGE_TYPES = {"image/png", "image/jpeg"}


def view_image(ctx: ToolContext, args: dict) -> dict:
    version_id = _uuid(args["version_id"], "version_id")
    if ctx.store is None:
        raise ToolInputError("viewing images isn't available in this run")
    try:
        version, data = documents.read_content(ctx.session, ctx.scope, ctx.store, version_id)
    except NotFoundInScope:
        raise ToolInputError("no such document version in this project") from None
    if version.content_type not in IMAGE_TYPES:
        raise ToolInputError("that document isn't an image; use read_document instead")
    try:
        image = prepare_image(data)
    except VisionError as exc:
        raise ToolInputError(str(exc)) from exc
    doc = documents.get_document(ctx.session, ctx.scope, version.document_id)
    # The agent loop attaches "__image__" to the tool result as an image the model can see.
    return {
        "__image__": {"media_type": image.media_type, "data": image.data_b64},
        "document": doc.title, "version_no": version.version_no,
        "summary": f"Viewed {doc.title} v{version.version_no}",
    }


def search_documents(ctx: ToolContext, args: dict) -> dict:
    hits = documents.search(ctx.session, ctx.scope, ctx.embedder, args["query"], limit=max(1, min(int(args.get("limit") or 6), 10)))
    return {
        "results": [{"document": h.document_title, "version_no": h.version_no, "location": h.location,
                     "score": h.score, "text": h.text} for h in hits],
        "summary": f"Searched documents for “{args['query'][:60]}” ({len(hits)} hits)",
    }


def get_registers(ctx: ToolContext, _: dict) -> dict:
    # The registers are project-level, so any model's snapshot holds them; use the first model
    # if one exists, otherwise read records directly.
    out: dict[str, list] = {"requirements": [], "definitions": [], "questions": [], "decisions": []}
    keys = {RecordType.REQUIREMENT: "requirements", RecordType.DEFINITION: "definitions",
            RecordType.QUESTION: "questions", RecordType.DECISION: "decisions"}
    seq = modeling.current_seq(ctx.session, ctx.scope)
    for rtype, key in keys.items():
        for v in modeling._latest(ctx.session, ctx.scope, rtype, as_of=seq):
            out[key].append({"id": str(v.id), "code": v.code, "seq": v.seq,
                             **{k: (str(val) if isinstance(val, uuid.UUID) else val) for k, val in v.data.items()}})
    links = modeling.list_live_links(ctx.session, ctx.scope)
    out["links"] = [{"from_id": str(l.from_id), "to_id": str(l.to_id), "type": str(l.link_type)} for l in links]
    counts = ", ".join(f"{len(out[k])} {k}" for k in keys.values())
    return {**out, "summary": f"Read registers ({counts})"}


# --- Write tools ----------------------------------------------------------------------------


def record_requirement(ctx: ToolContext, args: dict) -> dict:
    data = {
        "statement": args["statement"], "origin_kind": args["origin_kind"], "origin_ref": args["origin_ref"],
        "feasibility_status": args["feasibility_status"], "feasibility_notes": _none_if_empty(args.get("feasibility_notes")),
        "is_proposed_alternative": bool(args.get("is_proposed_alternative")),
    }
    ops: list = [CreateOp(type=RecordType.REQUIREMENT, ref="r", data=data)]
    for def_id in args.get("uses_definition_ids") or []:
        ops.append(LinkOp(from_id="$r", to_id=_uuid(def_id), link_type=LinkType.USES_DEFINITION))
    if _none_if_empty(args.get("alternative_for_id")):
        ops.append(LinkOp(from_id="$r", to_id=_uuid(args["alternative_for_id"]), link_type=LinkType.ALTERNATIVE_FOR))
    result = _write(ctx, ops)
    entry = _note_change(ctx, result.created["r"])
    return {**entry, "summary": f"Recorded {entry['code']}: {args['statement'][:80]}"}


def record_definition(ctx: ToolContext, args: dict) -> dict:
    metric = {k: _none_if_empty(v) for k, v in (args.get("metric") or {}).items()}
    if args["kind"] != "metric" and not any(metric.values()):
        metric = None  # unknown details stay null: explicitly unresolved, never invented
    data = {
        "term": args["term"], "aliases": args.get("aliases") or [], "kind": args["kind"],
        "definition": _none_if_empty(args.get("definition")),
        "business_scope": _none_if_empty(args.get("business_scope")),
        "metric": metric, "provenance": args["provenance"], "origin_ref": args["origin_ref"],
    }
    ops: list = [CreateOp(type=RecordType.DEFINITION, ref="d", data=data)]
    for other in args.get("conflicts_with_ids") or []:
        ops.append(LinkOp(from_id="$d", to_id=_uuid(other), link_type=LinkType.CONFLICTS_WITH))
    result = _write(ctx, ops)
    entry = _note_change(ctx, result.created["d"])
    scope = f" ({args['business_scope']})" if _none_if_empty(args.get("business_scope")) else ""
    return {**entry, "summary": f"Recorded {entry['code']}: {args['term']}{scope}"}


_UPDATE_FIELDS = ["statement", "definition", "business_scope", "origin_ref", "feasibility_status",
                  "feasibility_notes", "resolution"]


def update_entry(ctx: ToolContext, args: dict) -> dict:
    record_id = _uuid(args["record_id"], "record_id")
    changes = {k: args[k] for k in _UPDATE_FIELDS if _none_if_empty(args.get(k)) is not None and args.get(k) != "unchanged"}
    if args.get("question_status") not in (None, "", "unchanged"):
        changes["status"] = args["question_status"]
    if not changes:
        raise ToolInputError("no fields to change")
    _write(ctx, [UpdateOp(id=record_id, expected_seq=int(args["expected_seq"]), data=changes)])
    entry = _note_change(ctx, record_id)
    return {**entry, "summary": f"Updated {entry['code'] or entry['type']} ({', '.join(changes)})"}


def raise_question(ctx: ToolContext, args: dict) -> dict:
    data = {
        "kind": QuestionKind(args["kind"]).value, "description": args["description"],
        "impact": _none_if_empty(args.get("impact")),
        "is_blocking": bool(args.get("is_blocking")), "proposed_action": _none_if_empty(args.get("proposed_action")),
    }
    ops: list = [CreateOp(type=RecordType.QUESTION, ref="q", data=data)]
    for target in args.get("concerns_ids") or []:
        ops.append(LinkOp(from_id="$q", to_id=_uuid(target), link_type=LinkType.CONCERNS))
    result = _write(ctx, ops)
    entry = _note_change(ctx, result.created["q"])
    return {**entry, "summary": f"Raised {entry['code']}: {args['description'][:80]}"}


def link_records(ctx: ToolContext, args: dict) -> dict:
    _write(ctx, [LinkOp(from_id=_uuid(args["from_id"]), to_id=_uuid(args["to_id"]), link_type=LinkType(args["link_type"]))])
    return {"summary": f"Linked records ({args['link_type']})"}


HANDLERS: dict[str, Callable[[ToolContext, dict], dict]] = {
    "list_documents": list_documents,
    "read_document": read_document,
    "view_image": view_image,
    "search_documents": search_documents,
    "get_registers": get_registers,
    "record_requirement": record_requirement,
    "record_definition": record_definition,
    "update_entry": update_entry,
    "raise_question": raise_question,
    "link_records": link_records,
}
WRITE_TOOLS = {"record_requirement", "record_definition", "update_entry", "raise_question", "link_records"}
