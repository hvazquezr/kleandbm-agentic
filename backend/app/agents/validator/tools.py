"""Tools for the Validator agent: read-only access to the candidate, registers, documents and
sources, plus record_finding and submit_review. Findings are kept on the candidate's review
round, never written to the model directly; they become CHECK records when the candidate is
applied. The Validator has no tool that edits the candidate or the registers."""

import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select

from app.adapters.llm import ToolSpec
from app.agents.architect import tools as at
from app.agents.requirements import tools as rt
from app.agents.requirements.tools import _enum, _none_if_empty, _obj, _opt
from app.agents.runtime import ToolInputError
from app.domain import design
from app.domain.records import CheckStatus, CheckType
from app.persistence.sources import SourceEvidence


@dataclass
class ValidatorContext(at.ArchitectContext):
    user_edited: set[str] = field(default_factory=set)
    findings: dict[str, dict[str, Any]] = field(default_factory=dict)  # by issue_key
    summary: str | None = None


_LIST = {"type": "array", "items": {"type": "string"}}

FINDING_SPEC = ToolSpec(
    "record_finding",
    "Record one validation finding (or replace the one with the same issue_key in this review).",
    _obj({
        "issue_key": {"type": "string", "description": "Stable key, e.g. grain:fact_sales_line"},
        "check_type": _enum(CheckType),
        "status": _enum(CheckStatus),
        "is_blocking": {"type": "boolean"},
        "summary": {"type": "string", "description": "What you found, in one or two sentences."},
        "elements": {**_LIST, "description": "Candidate elements: 'table', 'table.attribute' or a relationship label"},
        "requirement_ids": {**_LIST, "description": "Affected requirement ids from get_registers"},
        "evidence_ids": {**_LIST, "description": "Source evidence ids from your source tool calls (or the Architect's)"},
        "citations": {**_LIST, "description": "Document title, version and location"},
        "internal_checks": {**_LIST, "description": "Names of internal checks you confirmed"},
        "question_ids": {**_LIST, "description": "Open questions (ids from get_registers) this finding depends "
                                                 "on, so it is linked to them instead of raising a new question"},
        "impact": _opt("What goes wrong for the business if this isn't fixed."),
        "recommended_action": _opt("The smallest correction that would fix it."),
    }),
)

SUBMIT_SPEC = ToolSpec(
    "submit_review",
    "Finish the review. Call it exactly once, after recording your findings.",
    _obj({"summary": {"type": "string", "description": "The overall result in two or three sentences."}}),
)

GET_CANDIDATE_SPEC = ToolSpec(
    "get_candidate",
    "The design candidate under review: tables with full attribute details, relationships, requirement "
    "mappings, decision links, the Architect's internal check results, and which existing elements a "
    "person edited.",
    _obj({}),
)

READ_SPECS = [s for s in rt.TOOL_SPECS if s.name in ("get_registers", "list_documents", "read_document", "search_documents")]
TOOL_SPECS = [replace(s, strict=s.name in {"record_finding"})
              for s in [GET_CANDIDATE_SPEC, *READ_SPECS, *at.SOURCE_TOOL_SPECS, FINDING_SPEC, SUBMIT_SPEC]]


def get_candidate(ctx: ValidatorContext, _: dict) -> dict:
    c = ctx.candidate
    content = c.content
    tables = []
    for t in content["tables"]:
        tables.append({
            **{k: t.get(k) for k in ("name", "kind", "grain", "description", "scd_type", "source_ref")},
            "edited_by_person": design._key(t["name"]) in ctx.user_edited,
            "attributes": [{**{k: a.get(k) for k in ("name", "logical_type", "role", "is_nullable", "additivity",
                                                     "description", "source_ref")},
                            "edited_by_person": f"{design._key(t['name'])}.{design._key(a['name'])}" in ctx.user_edited}
                           for a in t["attributes"]],
        })
    rels = [{"label": design._rel_label(r), "cardinality": r["cardinality"], "description": r.get("description"),
             "edited_by_person": design._key(design._rel_label(r)) in ctx.user_edited}
            for r in content["relationships"]]
    return {"candidate_id": str(c.id), "base_seq": c.base_seq, "tables": tables, "relationships": rels,
            "mappings": content["mappings"], "decision_links": content["decision_links"],
            "internal_checks": c.internal_checks,
            "summary": f"Read candidate ({len(tables)} tables, {len(rels)} relationships)"}


def record_finding(ctx: ValidatorContext, args: dict) -> dict:
    key = " ".join(str(args.get("issue_key") or "").split())[:200]
    if not key:
        raise ToolInputError("issue_key is required")
    status = CheckStatus(args["status"])
    labels = design.element_labels(ctx.candidate.content)
    elements = [e for e in args.get("elements") or [] if str(e).strip()]
    unknown = [e for e in elements if design._key(e) not in labels]
    if unknown:
        raise ToolInputError(f"not in the candidate: {', '.join(unknown)}; use names from get_candidate")
    requirement_ids = {str(r.id) for r in ctx.requirements()}
    reqs = [str(r) for r in args.get("requirement_ids") or [] if str(r).strip()]
    if bad := [r for r in reqs if r not in requirement_ids]:
        raise ToolInputError(f"no such requirement: {', '.join(bad)}; use ids from get_registers")
    open_questions = {str(q.id) for q in ctx.open_questions()}
    questions = list(dict.fromkeys(str(q) for q in args.get("question_ids") or [] if str(q).strip()))
    if bad := [q for q in questions if q not in open_questions]:
        raise ToolInputError(f"not an open question: {', '.join(bad)}; use open question ids from get_registers")

    evidence: list[dict[str, Any]] = []
    for eid in args.get("evidence_ids") or []:
        try:
            ev_id = uuid.UUID(str(eid))
        except ValueError:
            raise ToolInputError(f"{eid!r} isn't an evidence id") from None
        row = ctx.session.scalar(select(SourceEvidence).where(
            SourceEvidence.tenant_id == ctx.scope.tenant_id, SourceEvidence.project_id == ctx.scope.project_id,
            SourceEvidence.id == ev_id))
        if row is None:
            raise ToolInputError(f"no source evidence {eid} in this project")
        evidence.append({"kind": "source", "evidence_id": str(row.id), "tool": row.tool, "is_error": row.is_error})
    evidence += [{"kind": "document", "citation": c.strip()} for c in args.get("citations") or [] if str(c).strip()]
    check_names = {c["name"] for c in ctx.candidate.internal_checks or []}
    for name in args.get("internal_checks") or []:
        if name not in check_names:
            raise ToolInputError(f"no internal check named {name}")
        evidence.append({"kind": "internal_check", "name": name})
    usable = [e for e in evidence if not e.get("is_error")]
    if status is CheckStatus.PASSED and not usable:
        # Missing evidence is unverified, never a pass (spec §6).
        raise ToolInputError("a passed finding needs evidence (evidence_ids, citations or internal_checks); "
                             "without it, record the finding as unverified")

    finding = {
        "issue_key": key, "check_type": CheckType(args["check_type"]).value, "status": status.value,
        "is_blocking": bool(args.get("is_blocking")), "summary": str(args["summary"]).strip()[:4000],
        "elements": elements, "requirement_ids": reqs, "evidence": evidence,
        "impact": _none_if_empty(args.get("impact")), "recommended_action": _none_if_empty(args.get("recommended_action")),
        "question_ids": questions,
        "edited_by_person": design.finding_targets_user_edits({"elements": elements}, ctx.user_edited),
    }
    ctx.findings[key] = finding
    return {"recorded": key, "status": status.value,
            "summary": f"Finding {key}: {status.value}" + (" (blocking)" if finding["is_blocking"] else "")}


def submit_review(ctx: ValidatorContext, args: dict) -> dict:
    if not ctx.findings:
        raise ToolInputError("record your findings (including what passed) before submitting")
    ctx.summary = str(args.get("summary") or "").strip()[:4000]
    counts = {s.value: sum(1 for f in ctx.findings.values() if f["status"] == s.value) for s in CheckStatus}
    return {"ok": True, "summary": f"Review submitted ({', '.join(f'{n} {k}' for k, n in counts.items())})"}


HANDLERS = {
    "get_candidate": get_candidate,
    "get_registers": rt.get_registers,
    "list_documents": rt.list_documents,
    "read_document": rt.read_document,
    "search_documents": rt.search_documents,
    **at.SOURCE_HANDLERS,
    "record_finding": record_finding,
    "submit_review": submit_review,
}
# Source calls are evidence and commit right away; findings live on the context until the round ends.
WRITE_TOOLS = set(at.SOURCE_HANDLERS) - {"list_sources"}
assert set(HANDLERS) == {s.name for s in TOOL_SPECS}
