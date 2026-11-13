"""Design candidates: the Architect's workspace, its internal checks, and applying it.

A candidate is a complete proposed model (tables with attributes, relationships) plus the
requirement mappings and decision links that go with it. It starts as a copy of the live
model at a pinned watermark (`base_seq`), so existing elements keep their identity: applying
the candidate updates them in place (with `expected_seq` from the base) instead of
recreating them. Everything is applied as one attributed changeset.

Workspace content (JSON):
  tables:        [{name, kind, grain, description, scd_type, source_ref, record_id, base_seq,
                   attributes: [{name, logical_type, role, is_nullable, additivity, description,
                                 source_ref, record_id, base_seq}]}]
  relationships: [{from_table, from_attribute, to_table, to_attribute, cardinality, role_name,
                   description, record_id, base_seq}]
  mappings:      [{requirement_id, elements: ["table" | "table.attribute"], note}]
  decision_links:[{decision_id, elements: [...]}]
  removed:       [{record_id, base_seq, type, label}]   existing elements the candidate drops

Validation (P4c) lives beside the content: `validation` holds the Validator's review rounds,
and `issue_attempts` counts correction attempts per issue key across episodes. The last
round's findings are written as CHECK records in the same changeset as the candidate, and
the model becomes Ready for Review only when nothing blocks it (`readiness`).
"""

import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain import modeling
from app.domain.access import AccessDenied, ActorKind, NotFoundInScope, Permission, ProjectScope
from app.domain.modeling import CreateOp, DeleteOp, LinkOp, UpdateOp
from app.domain.records import (
    Additivity,
    AttributeRole,
    Cardinality,
    CheckStatus,
    DecisionStatus,
    FeasibilityStatus,
    LinkType,
    LogicalType,
    ModelState,
    QuestionStatus,
    RecordType,
    ScdType,
    TableKind,
)
from app.domain.tenancy import InvalidOperation
from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.models import User
from app.persistence.modeling import Changeset
from app.persistence.search import DecisionPoint

TABLE_FIELDS = ("name", "kind", "grain", "description", "scd_type")
ATTRIBUTE_FIELDS = ("name", "logical_type", "role", "is_nullable", "additivity", "description", "ordinal")
RELATIONSHIP_FIELDS = ("cardinality", "role_name", "description")


def _user(scope: ProjectScope) -> uuid.UUID:
    return scope.actor.on_behalf_of or scope.actor.id


def _key(name: str) -> str:
    return name.strip().lower()


# --- Lifecycle ------------------------------------------------------------------------------


def start_candidate(
    session: Session, scope: ProjectScope, model_id: uuid.UUID, run_id: uuid.UUID | None = None
) -> DesignCandidate:
    """A new workspace seeded from the model as it is now."""
    scope.require(Permission.PROJECT_EDIT)
    snapshot = modeling.get_snapshot(session, scope, model_id)
    previous = session.scalar(
        select(DesignCandidate).where(DesignCandidate.project_id == scope.project_id,
                                      DesignCandidate.model_id == model_id)
        .order_by(DesignCandidate.created_at.desc()).limit(1))
    candidate = DesignCandidate(
        tenant_id=scope.tenant_id, project_id=scope.project_id, model_id=model_id, run_id=run_id,
        status=CandidateStatus.BUILDING, base_seq=snapshot.as_of_seq, content=_seed(snapshot),
        internal_checks=[], validation=[], created_by=_user(scope),
        # Per-issue correction allowances carry across review episodes (spec §7).
        issue_attempts=dict(previous.issue_attempts or {}) if previous else {},
    )
    if previous is not None:
        # A search that ended without a selection keeps blocking readiness until a person
        # decides it; a new run can't sidestep it by designing around it.
        resolutions = search_resolutions(session, scope, previous)
        carried = [s for s in previous.content.get("searches", [])
                   if s.get("status") != "selected" and s.get("decision_point_id") not in resolutions]
        if carried:
            candidate.content = {**candidate.content, "searches": copy.deepcopy(carried)}
    session.add(candidate)
    session.flush()
    return candidate


def _seed(snapshot: modeling.Snapshot) -> dict[str, Any]:
    tables = {t.id: t for t in snapshot.records[RecordType.TABLE]}
    attrs = {a.id: a for a in snapshot.records[RecordType.ATTRIBUTE]}
    content: dict[str, Any] = {"tables": [], "relationships": [], "mappings": [], "decision_links": [], "removed": []}
    for t in sorted(tables.values(), key=lambda v: v.created_seq):
        own = sorted((a for a in attrs.values() if a.data["table_id"] == t.id), key=lambda a: a.data.get("ordinal", 0))
        content["tables"].append({
            **{f: _plain(t.data.get(f)) for f in TABLE_FIELDS}, "source_ref": None,
            "record_id": str(t.id), "base_seq": t.seq,
            "attributes": [{**{f: _plain(a.data.get(f)) for f in ATTRIBUTE_FIELDS if f != "ordinal"},
                            "source_ref": None, "record_id": str(a.id), "base_seq": a.seq} for a in own],
        })
    for r in snapshot.records[RecordType.RELATIONSHIP]:
        d = r.data
        ft, fa, tt, ta = (tables.get(d["from_table_id"]), attrs.get(d["from_attribute_id"]),
                          tables.get(d["to_table_id"]), attrs.get(d["to_attribute_id"]))
        if not (ft and fa and tt and ta):
            continue
        content["relationships"].append({
            "from_table": ft.data["name"], "from_attribute": fa.data["name"],
            "to_table": tt.data["name"], "to_attribute": ta.data["name"],
            **{f: _plain(d.get(f)) for f in RELATIONSHIP_FIELDS}, "record_id": str(r.id), "base_seq": r.seq,
        })
    labels = {**{i: t.data["name"] for i, t in tables.items()},
              **{i: f"{tables[a.data['table_id']].data['name']}.{a.data['name']}" for i, a in attrs.items()
                 if a.data["table_id"] in tables}}
    by_req: dict[str, list[str]] = {}
    for link in snapshot.links:
        if link.link_type is LinkType.MAPS_TO and link.to_id in labels:
            by_req.setdefault(str(link.from_id), []).append(labels[link.to_id])
    content["mappings"] = [{"requirement_id": rid, "elements": els, "note": None} for rid, els in by_req.items()]
    return content


def _plain(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def get_candidate(session: Session, scope: ProjectScope, candidate_id: uuid.UUID) -> DesignCandidate:
    scope.require(Permission.PROJECT_READ)
    candidate = session.scalar(select(DesignCandidate).where(
        DesignCandidate.tenant_id == scope.tenant_id, DesignCandidate.project_id == scope.project_id,
        DesignCandidate.id == candidate_id))
    if candidate is None:
        raise NotFoundInScope("design candidate")
    return candidate


def list_candidates(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> list[DesignCandidate]:
    modeling.get_model(session, scope, model_id)
    return list(session.scalars(
        select(DesignCandidate)
        .where(DesignCandidate.tenant_id == scope.tenant_id, DesignCandidate.project_id == scope.project_id,
               DesignCandidate.model_id == model_id)
        .order_by(DesignCandidate.created_at.desc())
    ))


def _editable(candidate: DesignCandidate) -> None:
    if candidate.status is not CandidateStatus.BUILDING:
        raise InvalidOperation(f"the candidate is {candidate.status}, not being built")


def _save(candidate: DesignCandidate, content: dict[str, Any], requirements: list[modeling.RecordView] | None = None) -> None:
    candidate.content = content  # reassign so the JSONB change is tracked
    if requirements is not None:
        candidate.internal_checks = internal_checks(content, requirements)


def finish_candidate(candidate: DesignCandidate, status: CandidateStatus, summary: str | None = None) -> None:
    candidate.status = status
    if summary is not None:
        candidate.summary = summary[:8000]


def discard_candidate(session: Session, scope: ProjectScope, candidate_id: uuid.UUID) -> DesignCandidate:
    scope.require(Permission.PROJECT_EDIT)
    candidate = get_candidate(session, scope, candidate_id)
    if candidate.status is CandidateStatus.APPLIED:
        raise InvalidOperation("an applied candidate can't be discarded; edit the model instead")
    candidate.status = CandidateStatus.DISCARDED
    session.flush()
    return candidate


# --- Workspace edits ------------------------------------------------------------------------


def _find_table(content: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((t for t in content["tables"] if _key(t["name"]) == _key(name)), None)


def _find_attr(table: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((a for a in table["attributes"] if _key(a["name"]) == _key(name)), None)


def _remove_existing(content: dict[str, Any], element: dict[str, Any], rtype: RecordType, label: str) -> None:
    if element.get("record_id"):
        content["removed"].append({"record_id": element["record_id"], "base_seq": element["base_seq"],
                                   "type": str(rtype), "label": label})


def put_table(candidate: DesignCandidate, table: dict[str, Any], requirements: list[modeling.RecordView]) -> dict[str, Any]:
    """Create or replace one table, with its full attribute list. Existing attributes keep their
    identity when their name is kept; attributes left out are dropped."""
    _editable(candidate)
    content = copy.deepcopy(candidate.content)
    name = table["name"].strip()
    if not name:
        raise InvalidOperation("a table needs a name")
    kind = TableKind(table["kind"])
    attrs_in = table.get("attributes") or []
    seen: set[str] = set()
    for a in attrs_in:
        if not a["name"].strip():
            raise InvalidOperation(f"{name}: every attribute needs a name")
        if _key(a["name"]) in seen:
            raise InvalidOperation(f"{name}: attribute {a['name']!r} appears twice")
        seen.add(_key(a["name"]))

    existing = _find_table(content, table.get("previous_name") or name)
    if existing is not None and table.get("previous_name") and _find_table(content, name) not in (None, existing):
        raise InvalidOperation(f"there is already a table named {name}")
    attributes = []
    for a in attrs_in:
        prior = _find_attr(existing, a["name"]) if existing else None
        additivity = a.get("additivity")
        attributes.append({
            "name": a["name"].strip(),
            "logical_type": LogicalType(a["logical_type"]).value,
            "role": AttributeRole(a.get("role") or "descriptive").value,
            "is_nullable": bool(a.get("is_nullable", True)),
            "additivity": Additivity(additivity).value if additivity else None,
            "description": a.get("description") or None,
            "source_ref": a.get("source_ref") or None,
            "record_id": prior.get("record_id") if prior else None,
            "base_seq": prior.get("base_seq") if prior else None,
        })
    entry = {
        "name": name, "kind": kind.value, "grain": table.get("grain") or None,
        "description": table.get("description") or None,
        "scd_type": ScdType(table.get("scd_type") or "none").value,
        "source_ref": table.get("source_ref") or None,
        "record_id": existing.get("record_id") if existing else None,
        "base_seq": existing.get("base_seq") if existing else None,
        "attributes": attributes,
    }
    if existing is None:
        content["tables"].append(entry)
    else:
        kept = {_key(a["name"]) for a in attributes}
        for old in existing["attributes"]:
            if _key(old["name"]) not in kept:
                _remove_existing(content, old, RecordType.ATTRIBUTE, f"{existing['name']}.{old['name']}")
                _drop_references(content, existing["name"], old["name"])
        if _key(existing["name"]) != _key(name):
            _rename_references(content, existing["name"], name)
        content["tables"][content["tables"].index(existing)] = entry
    _save(candidate, content, requirements)
    return entry


def _drop_references(content: dict[str, Any], table: str, attribute: str | None) -> None:
    """Relationships and mappings that pointed at a removed table or attribute go with it."""
    def touches(r):
        ends = [(r["from_table"], r["from_attribute"]), (r["to_table"], r["to_attribute"])]
        return any(_key(t) == _key(table) and (attribute is None or _key(a) == _key(attribute)) for t, a in ends)

    for r in [r for r in content["relationships"] if touches(r)]:
        _remove_existing(content, r, RecordType.RELATIONSHIP, _rel_label(r))
        content["relationships"].remove(r)
    target = _key(table) if attribute is None else f"{_key(table)}.{_key(attribute)}"
    for group in ("mappings", "decision_links"):
        for m in content[group]:
            m["elements"] = [e for e in m["elements"]
                             if not (_key(e) == target or (attribute is None and _key(e).startswith(target + ".")))]


def _rename_references(content: dict[str, Any], old: str, new: str) -> None:
    for r in content["relationships"]:
        for end in ("from_table", "to_table"):
            if _key(r[end]) == _key(old):
                r[end] = new
    for group in ("mappings", "decision_links"):
        for m in content[group]:
            m["elements"] = [new + e[len(old):] if _key(e) == _key(old) or _key(e).startswith(_key(old) + ".") else e
                             for e in m["elements"]]


def remove_table(candidate: DesignCandidate, name: str, requirements: list[modeling.RecordView]) -> None:
    _editable(candidate)
    content = copy.deepcopy(candidate.content)
    table = _find_table(content, name)
    if table is None:
        raise InvalidOperation(f"no table named {name} in the candidate")
    _remove_existing(content, table, RecordType.TABLE, table["name"])  # its attributes go with it on apply
    _drop_references(content, table["name"], None)
    content["tables"].remove(table)
    _save(candidate, content, requirements)


def _rel_label(r: dict[str, Any]) -> str:
    role = f" as {r['role_name']}" if r.get("role_name") else ""
    return f"{r['from_table']}.{r['from_attribute']} -> {r['to_table']}.{r['to_attribute']}{role}"


def put_relationships(
    candidate: DesignCandidate, relationships: list[dict[str, Any]], requirements: list[modeling.RecordView]
) -> list[str]:
    """Add or update relationships, matched by their endpoints and role name."""
    _editable(candidate)
    content = copy.deepcopy(candidate.content)
    out = []
    for rel in relationships:
        for end in ("from", "to"):
            table = _find_table(content, rel[f"{end}_table"])
            if table is None:
                raise InvalidOperation(f"no table named {rel[f'{end}_table']} in the candidate")
            attr = _find_attr(table, rel[f"{end}_attribute"])
            if attr is None:
                raise InvalidOperation(f"{table['name']} has no attribute {rel[f'{end}_attribute']}")
            rel[f"{end}_table"], rel[f"{end}_attribute"] = table["name"], attr["name"]
        entry = {
            "from_table": rel["from_table"], "from_attribute": rel["from_attribute"],
            "to_table": rel["to_table"], "to_attribute": rel["to_attribute"],
            "cardinality": Cardinality(rel.get("cardinality") or "many_to_one").value,
            "role_name": rel.get("role_name") or None, "description": rel.get("description") or None,
        }
        match = next((r for r in content["relationships"] if _rel_label(r).lower() == _rel_label(entry).lower()), None)
        if match is None:
            content["relationships"].append({**entry, "record_id": None, "base_seq": None})
        else:
            match.update(entry)
        out.append(_rel_label(entry))
    _save(candidate, content, requirements)
    return out


def remove_relationship(candidate: DesignCandidate, label: str, requirements: list[modeling.RecordView]) -> None:
    _editable(candidate)
    content = copy.deepcopy(candidate.content)
    match = next((r for r in content["relationships"] if _rel_label(r).lower() == label.strip().lower()), None)
    if match is None:
        raise InvalidOperation(f"no relationship {label!r}; use the label shown by get_candidate")
    _remove_existing(content, match, RecordType.RELATIONSHIP, label)
    content["relationships"].remove(match)
    _save(candidate, content, requirements)


def _check_element(content: dict[str, Any], element: str) -> str:
    table_name, _, attr_name = element.partition(".")
    table = _find_table(content, table_name)
    if table is None:
        raise InvalidOperation(f"no table named {table_name} in the candidate")
    if not attr_name:
        return table["name"]
    attr = _find_attr(table, attr_name)
    if attr is None:
        raise InvalidOperation(f"{table['name']} has no attribute {attr_name}")
    return f"{table['name']}.{attr['name']}"


def map_requirement(
    candidate: DesignCandidate, requirement_id: uuid.UUID, elements: list[str], note: str | None,
    requirements: list[modeling.RecordView],
) -> list[str]:
    """Set which candidate elements satisfy a requirement (replacing any earlier mapping)."""
    _editable(candidate)
    if not any(r.id == requirement_id for r in requirements):
        raise InvalidOperation("no such requirement; use ids from get_registers")
    content = copy.deepcopy(candidate.content)
    resolved = [_check_element(content, e) for e in elements]
    content["mappings"] = [m for m in content["mappings"] if m["requirement_id"] != str(requirement_id)]
    if resolved:
        content["mappings"].append({"requirement_id": str(requirement_id), "elements": resolved, "note": note or None})
    _save(candidate, content, requirements)
    return resolved


def link_decision(candidate: DesignCandidate, decision_id: uuid.UUID, elements: list[str]) -> list[str]:
    _editable(candidate)
    content = copy.deepcopy(candidate.content)
    resolved = [_check_element(content, e) for e in elements]
    if resolved:
        content["decision_links"].append({"decision_id": str(decision_id), "elements": resolved})
    _save(candidate, content)
    return resolved


# --- Internal checks ------------------------------------------------------------------------


def _check(check_type: str, name: str, status: str, hard: bool, summary: str, elements: list[str] | None = None) -> dict:
    return {"check_type": check_type, "name": name, "status": status, "hard": hard, "summary": summary,
            "elements": elements or []}


def internal_checks(content: dict[str, Any], requirements: list[modeling.RecordView]) -> list[dict[str, Any]]:
    """The Architect's structural checks. Missing evidence is unverified, never a pass."""
    checks: list[dict[str, Any]] = []
    tables = content["tables"]
    facts = [t for t in tables if t["kind"] == "fact"]

    if not tables:
        return [_check("coverage", "has_tables", "failed", True, "The candidate has no tables yet.")]

    no_grain = [t["name"] for t in facts if not (t.get("grain") or "").strip()]
    checks.append(_check("grain", "fact_grain_declared", "failed" if no_grain else "passed", True,
                         f"Facts without a declared grain: {', '.join(no_grain)}" if no_grain
                         else "Every fact declares its grain.", no_grain))

    empty = [t["name"] for t in tables if not t["attributes"]]
    checks.append(_check("conventions", "tables_have_attributes", "failed" if empty else "passed", True,
                         f"Tables without attributes: {', '.join(empty)}" if empty else "Every table has attributes.", empty))

    keyless = [t["name"] for t in tables if t["kind"] == "dimension"
               and not any(a["role"] in ("surrogate_key", "natural_key") for a in t["attributes"])]
    checks.append(_check("keys_and_joins", "dimension_keys", "failed" if keyless else "passed", True,
                         f"Dimensions without a surrogate or natural key: {', '.join(keyless)}" if keyless
                         else "Every dimension has a key.", keyless))

    misplaced = [f"{t['name']}.{a['name']}" for t in tables for a in t["attributes"]
                 if a["role"] == "measure" and t["kind"] != "fact"]
    no_additivity = [f"{t['name']}.{a['name']}" for t in facts for a in t["attributes"]
                     if a["role"] == "measure" and not a.get("additivity")]
    non_numeric = [f"{t['name']}.{a['name']}" for t in facts for a in t["attributes"]
                   if a["role"] == "measure" and a["logical_type"] not in ("integer", "decimal")]
    problems = []
    if misplaced:
        problems.append(f"measures outside facts: {', '.join(misplaced)}")
    if no_additivity:
        problems.append(f"measures without additivity: {', '.join(no_additivity)}")
    checks.append(_check("conventions", "measures", "failed" if problems else "passed", True,
                         "; ".join(problems).capitalize() if problems else "Measures sit in facts and declare additivity.",
                         misplaced + no_additivity))
    if non_numeric:
        checks.append(_check("conventions", "numeric_measures", "failed", False,
                             f"Measures with a non-numeric type: {', '.join(non_numeric)}", non_numeric))

    names = {_key(t["name"]): t for t in tables}
    dangling, not_fk = [], []
    for r in content["relationships"]:
        ft, tt = names.get(_key(r["from_table"])), names.get(_key(r["to_table"]))
        fa = _find_attr(ft, r["from_attribute"]) if ft else None
        ta = _find_attr(tt, r["to_attribute"]) if tt else None
        if not (fa and ta):
            dangling.append(_rel_label(r))
        elif r["cardinality"] == "many_to_one" and fa["role"] != "foreign_key":
            not_fk.append(f"{ft['name']}.{fa['name']}")
    checks.append(_check("keys_and_joins", "relationship_endpoints", "failed" if dangling else "passed", True,
                         f"Relationships with missing endpoints: {', '.join(dangling)}" if dangling
                         else "Every relationship joins existing attributes.", dangling))
    if not_fk:
        checks.append(_check("keys_and_joins", "foreign_key_roles", "failed", False,
                             f"Many-to-one joins from attributes not marked as foreign keys: {', '.join(not_fk)}", not_fk))

    joined = {_key(r["from_table"]) for r in content["relationships"]} | {_key(r["to_table"]) for r in content["relationships"]}
    isolated = [t["name"] for t in facts if _key(t["name"]) not in joined]
    checks.append(_check("keys_and_joins", "facts_join_dimensions", "failed" if isolated else "passed", False,
                         f"Facts not joined to any dimension: {', '.join(isolated)}" if isolated
                         else "Every fact joins at least one dimension.", isolated))

    mapped = {m["requirement_id"] for m in content["mappings"] if m["elements"]}
    in_scope = [r for r in requirements
                if r.data.get("confirmation_status") != "rejected"
                and r.data.get("feasibility_status") != FeasibilityStatus.UNSUPPORTED]
    unmapped = [r.code or str(r.id) for r in in_scope if str(r.id) not in mapped]
    checks.append(_check("coverage", "requirements_mapped",
                         "failed" if unmapped else ("passed" if in_scope else "unverified"), False,
                         f"Requirements not mapped to any element: {', '.join(unmapped)}" if unmapped
                         else ("Every in-scope requirement maps to model elements." if in_scope
                               else "There are no requirements to check coverage against."), unmapped))

    unsourced = [t["name"] for t in tables if not t.get("source_ref")]
    checks.append(_check("assumptions", "source_evidence", "unverified" if unsourced else "passed", False,
                         f"Tables without cited source evidence: {', '.join(unsourced)}" if unsourced
                         else "Every table cites source evidence.", unsourced))
    return checks


def hard_failures(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in checks if c["hard"] and c["status"] == "failed"]


# --- Diff and apply -------------------------------------------------------------------------


def is_stale(session: Session, scope: ProjectScope, candidate: DesignCandidate) -> bool:
    """Whether the model's elements changed after the candidate's base watermark."""
    return bool(session.scalar(
        select(func.count()).select_from(Changeset).where(
            Changeset.project_id == scope.project_id, Changeset.model_id == candidate.model_id,
            Changeset.seq > candidate.base_seq)
    ))


def _table_data(t: dict[str, Any]) -> dict[str, Any]:
    return {f: t.get(f) for f in TABLE_FIELDS}


def _attr_data(a: dict[str, Any], ordinal: int) -> dict[str, Any]:
    return {**{f: a.get(f) for f in ATTRIBUTE_FIELDS if f != "ordinal"}, "ordinal": ordinal}


def _changed(current: dict[str, Any], proposed: dict[str, Any]) -> bool:
    return any(_plain(current.get(k)) != v for k, v in proposed.items())


def plan_changes(session: Session, scope: ProjectScope, candidate: DesignCandidate) -> tuple[list, dict[str, list[str]]]:
    """The changeset operations that turn the live model into the candidate, plus a readable
    diff ({"added": [...], "changed": [...], "removed": [...]})."""
    content = candidate.content
    current = {v.id: v for rtype in (RecordType.TABLE, RecordType.ATTRIBUTE, RecordType.RELATIONSHIP)
               for v in modeling.get_snapshot(session, scope, candidate.model_id).records[rtype]}
    ops: list = []
    diff: dict[str, list[str]] = {"added": [], "changed": [], "removed": []}

    removed_tables = {r["record_id"] for r in content["removed"] if r["type"] == "table"}
    for r in content["removed"]:
        rid = uuid.UUID(r["record_id"])
        view = current.get(rid)
        if view is None:
            continue  # already gone with its table, or deleted meanwhile (staleness catches that)
        if r["type"] == "attribute" and str(view.data.get("table_id")) in removed_tables:
            continue  # deleted together with its table
        ops.append(DeleteOp(id=rid, expected_seq=r["base_seq"]))
        diff["removed"].append(f"{r['type']} {r['label']}")

    ids: dict[str, uuid.UUID | str] = {}  # "table" / "table.attr" (lowercase) -> record id or $ref
    for ti, t in enumerate(content["tables"]):
        data = _table_data(t)
        if t.get("record_id"):
            rid = uuid.UUID(t["record_id"])
            ids[_key(t["name"])] = rid
            if rid in current and _changed(current[rid].data, data):
                ops.append(UpdateOp(id=rid, expected_seq=t["base_seq"], data=data))
                diff["changed"].append(f"table {t['name']}")
        else:
            ops.append(CreateOp(type=RecordType.TABLE, ref=f"t{ti}", data=data))
            ids[_key(t["name"])] = f"$t{ti}"
            diff["added"].append(f"{t['kind']} {t['name']}")
        for ai, a in enumerate(t["attributes"]):
            adata = {**_attr_data(a, ai), "table_id": ids[_key(t["name"])]}
            label = f"{_key(t['name'])}.{_key(a['name'])}"
            if a.get("record_id"):
                rid = uuid.UUID(a["record_id"])
                ids[label] = rid
                adata.pop("ordinal")  # keep the stored position; only content changes count
                if rid in current and _changed(current[rid].data, adata):
                    ops.append(UpdateOp(id=rid, expected_seq=a["base_seq"], data=adata))
                    diff["changed"].append(f"attribute {t['name']}.{a['name']}")
            else:
                ops.append(CreateOp(type=RecordType.ATTRIBUTE, ref=f"t{ti}a{ai}", data=adata))
                ids[label] = f"$t{ti}a{ai}"
                if t.get("record_id"):
                    diff["added"].append(f"attribute {t['name']}.{a['name']}")

    for ri, r in enumerate(content["relationships"]):
        data = {
            "from_table_id": ids[_key(r["from_table"])], "from_attribute_id": ids[f"{_key(r['from_table'])}.{_key(r['from_attribute'])}"],
            "to_table_id": ids[_key(r["to_table"])], "to_attribute_id": ids[f"{_key(r['to_table'])}.{_key(r['to_attribute'])}"],
            **{f: r.get(f) for f in RELATIONSHIP_FIELDS},
        }
        if r.get("record_id"):
            rid = uuid.UUID(r["record_id"])
            if rid in current and _changed(current[rid].data, data):
                ops.append(UpdateOp(id=rid, expected_seq=r["base_seq"], data=data))
                diff["changed"].append(f"relationship {_rel_label(r)}")
        else:
            ops.append(CreateOp(type=RecordType.RELATIONSHIP, ref=f"r{ri}", data=data))
            diff["added"].append(f"relationship {_rel_label(r)}")
        ids[_key(_rel_label(r))] = uuid.UUID(r["record_id"]) if r.get("record_id") else f"$r{ri}"

    existing_links = {(l.from_id, l.to_id, l.link_type) for l in modeling.list_live_links(session, scope)}
    for group, link_type, owner in (("mappings", LinkType.MAPS_TO, "requirement_id"),
                                    ("decision_links", LinkType.AFFECTS, "decision_id")):
        for m in content[group]:
            source = uuid.UUID(m[owner])
            for element in m["elements"]:
                target = ids.get(_key(element))
                if target is None or (source, target, link_type) in existing_links:
                    continue
                ops.append(LinkOp(from_id=source, to_id=target, link_type=link_type))
                existing_links.add((source, target, link_type))

    # The last review's findings replace the model's previous validation checks.
    findings = latest_findings(candidate)
    if findings is not None:
        for old in modeling.get_snapshot(session, scope, candidate.model_id).records[RecordType.CHECK]:
            ops.append(DeleteOp(id=old.id, expected_seq=old.seq))
        requirement_ids = {str(r.id) for r in modeling._latest(session, scope, RecordType.REQUIREMENT,
                                                                 as_of=modeling.current_seq(session, scope))}
        for fi, f in enumerate(findings):
            ops.append(CreateOp(type=RecordType.CHECK, ref=f"c{fi}", data=_check_data(candidate, f)))
            targets = [ids.get(_key(e)) for e in f.get("elements") or []]
            targets += [uuid.UUID(r) for r in f.get("requirement_ids") or [] if r in requirement_ids]
            targets += [uuid.UUID(q) for q in f.get("question_ids") or []]  # the open question it rests on
            for target in dict.fromkeys(t for t in targets if t is not None):
                ops.append(LinkOp(from_id=f"$c{fi}", to_id=target, link_type=LinkType.CHECKS))
        failed = sum(1 for f in findings if f["status"] != CheckStatus.PASSED.value)
        diff["added"].append(f"{len(findings)} validation checks ({failed} not passed)")
    return ops, diff


def _check_data(candidate: DesignCandidate, f: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_type": f["check_type"], "status": f["status"], "summary": f["summary"],
        "evidence": f.get("evidence") or [], "impact": f.get("impact"),
        "recommended_action": f.get("recommended_action"), "based_on_seq": candidate.base_seq,
        "issue_key": f["issue_key"], "is_blocking": bool(f.get("is_blocking")),
        "attempts": int((candidate.issue_attempts or {}).get(f["issue_key"], 0)), "candidate_id": candidate.id,
    }


# --- Validation -----------------------------------------------------------------------------


def latest_findings(candidate: DesignCandidate) -> list[dict[str, Any]] | None:
    """Findings of the last completed review round, or None if the Validator hasn't reviewed it."""
    done = [r for r in candidate.validation or [] if r.get("status") == "completed"]
    return done[-1]["findings"] if done else None


def search_resolutions(session: Session, scope: ProjectScope, candidate: DesignCandidate) -> dict[str, dict[str, Any]]:
    """How people resolved the candidate's unselected searches, by decision point id."""
    ids = [uuid.UUID(s["decision_point_id"]) for s in candidate.content.get("searches", [])
           if s.get("status") != "selected" and s.get("decision_point_id")]
    if not ids:
        return {}
    points = session.scalars(select(DecisionPoint).where(
        DecisionPoint.tenant_id == scope.tenant_id, DecisionPoint.project_id == scope.project_id,
        DecisionPoint.id.in_(ids), DecisionPoint.resolution.is_not(None)))
    return {str(dp.id): dp.resolution for dp in points}


def candidate_readiness(session: Session, scope: ProjectScope, candidate: DesignCandidate) -> tuple[bool, list[str]]:
    return readiness(candidate, search_resolutions(session, scope, candidate))


def readiness(candidate: DesignCandidate, resolutions: dict[str, dict[str, Any]] | None = None) -> tuple[bool, list[str]]:
    """Whether applying the candidate may mark the model Ready for Review, and what blocks it:
    failed hard internal checks, a decision-point search that ended without a selection and
    that no person has accepted, blocking findings that failed or couldn't be verified, or no
    completed review at all (an unreviewed design is unverified, never a pass)."""
    blockers = [f"{c['name']}: {c['summary']}" for c in hard_failures(candidate.internal_checks or [])]
    for s in candidate.content.get("searches", []):
        if s.get("status") == "selected":
            continue
        res = (resolutions or {}).get(s.get("decision_point_id") or "")
        if res and res.get("outcome") == "accepted":
            continue
        if res:
            blockers.append(f"decision point: {s['question']} (rejected by {res.get('user_name')}; "
                            "ask the Architect to redesign it)")
        else:
            blockers.append(f"decision point: {s['question']} (the search ended "
                            f"{str(s['status']).replace('_', ' ')}; a person must accept or reject the design)")
    findings = latest_findings(candidate)
    if findings is None:
        blockers.append("validation: the Validator hasn't reviewed this design")
    else:
        blockers += [f"{f['issue_key']}: {f['summary']}" for f in findings
                     if f.get("is_blocking") and f["status"] != CheckStatus.PASSED.value]
    return not blockers, blockers


def user_edited_elements(session: Session, scope: ProjectScope, candidate: DesignCandidate) -> set[str]:
    """Labels ("table", "table.attribute", relationship label) of existing elements whose
    current revision was written by a person. Findings on these become questions to the user,
    never automatic corrections (spec §7)."""
    labels: dict[int, list[str]] = {}
    for t in candidate.content["tables"]:
        if t.get("record_id"):
            labels.setdefault(t["base_seq"], []).append(_key(t["name"]))
        for a in t["attributes"]:
            if a.get("record_id"):
                labels.setdefault(a["base_seq"], []).append(f"{_key(t['name'])}.{_key(a['name'])}")
    for r in candidate.content["relationships"]:
        if r.get("record_id"):
            labels.setdefault(r["base_seq"], []).append(_key(_rel_label(r)))
    if not labels:
        return set()
    by_people = set(session.scalars(select(Changeset.seq).where(
        Changeset.project_id == scope.project_id, Changeset.seq.in_(list(labels)),
        Changeset.actor_kind == ActorKind.USER)))
    return {label for seq in by_people for label in labels[seq]}


def finding_targets_user_edits(finding: dict[str, Any], user_edited: set[str]) -> bool:
    """A finding concerns a person's edit if it names a user-edited element."""
    return any(_key(e) in user_edited for e in finding.get("elements") or [])


def element_labels(content: dict[str, Any]) -> set[str]:
    """Every label a finding may name: "table", "table.attribute", relationship labels."""
    out = set()
    for t in content["tables"]:
        out.add(_key(t["name"]))
        out.update(f"{_key(t['name'])}.{_key(a['name'])}" for a in t["attributes"])
    out.update(_key(_rel_label(r)) for r in content["relationships"])
    return out


def element_record_ids(content: dict[str, Any]) -> dict[str, uuid.UUID]:
    """Labels of elements that already exist in the model, with their record ids."""
    out: dict[str, uuid.UUID] = {}
    for t in content["tables"]:
        if t.get("record_id"):
            out[_key(t["name"])] = uuid.UUID(t["record_id"])
        for a in t["attributes"]:
            if a.get("record_id"):
                out[f"{_key(t['name'])}.{_key(a['name'])}"] = uuid.UUID(a["record_id"])
    for r in content["relationships"]:
        if r.get("record_id"):
            out[_key(_rel_label(r))] = uuid.UUID(r["record_id"])
    return out


def apply_candidate(session: Session, scope: ProjectScope, candidate_id: uuid.UUID) -> DesignCandidate:
    """Write the candidate to the model as one changeset. Refuses a stale candidate."""
    scope.require(Permission.PROJECT_EDIT)
    candidate = get_candidate(session, scope, candidate_id)
    if candidate.status not in (CandidateStatus.BUILDING, CandidateStatus.PROPOSED):
        raise InvalidOperation(f"the candidate is {candidate.status} and can't be applied")
    if is_stale(session, scope, candidate):
        candidate.status = CandidateStatus.STALE
        session.flush()
        raise InvalidOperation("the model changed after this candidate was designed; ask the Architect for a new one")
    ops, _ = plan_changes(session, scope, candidate)
    if ops:
        result = modeling.apply_changeset(
            session, scope, ops, model_id=candidate.model_id,
            reason=f"Applied design candidate {candidate.id}" + (f" (Architect run {candidate.run_id})" if candidate.run_id else ""),
        )
        candidate.applied_seq = result.seq
    candidate.status = CandidateStatus.APPLIED
    candidate.applied_by = _user(scope)
    candidate.applied_at = datetime.now(UTC)
    ready, _ = candidate_readiness(session, scope, candidate)
    if ready and candidate.applied_seq:
        # Readiness, never acceptance: only a person with approval authority accepts.
        modeling.set_state(session, scope, candidate.model_id, ModelState.READY_FOR_REVIEW,
                           note=f"Validated design candidate {candidate.id}")
    session.flush()
    return candidate


def model_is_empty(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> bool:
    snapshot = modeling.get_snapshot(session, scope, model_id)
    return not snapshot.records[RecordType.TABLE]


# --- Decision points decided by a person ----------------------------------------------------


def get_decision_point(session: Session, scope: ProjectScope, decision_point_id: uuid.UUID) -> DecisionPoint:
    scope.require(Permission.PROJECT_READ)
    dp = session.scalar(select(DecisionPoint).where(
        DecisionPoint.tenant_id == scope.tenant_id, DecisionPoint.project_id == scope.project_id,
        DecisionPoint.id == decision_point_id))
    if dp is None:
        raise NotFoundInScope("decision point")
    return dp


def resolve_decision_point(
    session: Session, scope: ProjectScope, decision_point_id: uuid.UUID, outcome: str, note: str | None = None
) -> DecisionPoint:
    """A person decides a search that ended without a selection (spec §7): `accepted` keeps
    the design as it stands and clears the readiness blocker; `rejected` keeps the model in
    Draft until the Architect redesigns it. Recorded three ways, all naming the person: one
    register changeset by them (the decision confirmed or rejected, the question resolved), and
    the resolution on the decision point. If nothing else blocks the model's applied design,
    accepting makes it Ready for Review (never Accepted)."""
    scope.require(Permission.PROJECT_EDIT)
    if scope.actor.kind is not ActorKind.USER:
        raise AccessDenied("only a person can decide a decision point")
    if outcome not in ("accepted", "rejected"):
        raise InvalidOperation("the outcome is accepted or rejected")
    dp = get_decision_point(session, scope, decision_point_id)
    if str(dp.status) not in ("partial", "no_candidate"):
        raise InvalidOperation(f"this search ended {dp.status}; there is nothing to decide")
    if dp.resolution is not None:
        raise InvalidOperation(f"this decision point was already {dp.resolution['outcome']} "
                               f"by {dp.resolution.get('user_name')}")
    user = session.get(User, scope.actor.id)
    name = (user.display_name or user.email) if user else None
    name = name or "a project member"
    verb = "Accepted" if outcome == "accepted" else "Rejected"
    stamp = f"{verb} by {name}" + (f": {note.strip()}" if note and note.strip() else ".")
    ops: list = []
    if dp.decision_id:
        decision = _live_record(session, scope, dp.decision_id)
        if decision is not None:
            rationale = f"{decision.data.get('rationale') or ''}\n\n{stamp}".strip()[:8000]
            ops.append(UpdateOp(id=decision.id, expected_seq=decision.seq, data={
                "status": (DecisionStatus.CONFIRMED if outcome == "accepted" else DecisionStatus.REJECTED).value,
                "rationale": rationale}))
    if dp.question_id:
        question = _live_record(session, scope, dp.question_id)
        if question is not None:
            ops.append(UpdateOp(id=question.id, expected_seq=question.seq, data={
                "status": QuestionStatus.RESOLVED.value, "resolution": stamp[:8000]}))
    seq = None
    if ops:
        result = modeling.apply_changeset(session, scope, ops,
                                          reason=f"Decision point {verb.lower()} by {name}")
        seq = result.seq
    dp.resolved_by = scope.actor.id
    dp.resolved_at = datetime.now(UTC)
    dp.resolution = {"outcome": outcome, "note": (note or "").strip() or None, "user_name": name, "seq": seq}
    session.flush()
    if outcome == "accepted":
        _ready_after_decision(session, scope, dp, name)
    return dp


def _live_record(session: Session, scope: ProjectScope, record_id: uuid.UUID):
    try:
        return modeling.get_record(session, scope, record_id)
    except NotFoundInScope:
        return None


def _ready_after_decision(session: Session, scope: ProjectScope, dp: DecisionPoint, name: str) -> None:
    """Mark the model Ready for Review if its applied design now has no blockers and the model
    hasn't changed since it was applied."""
    applied = session.scalar(select(DesignCandidate).where(
        DesignCandidate.project_id == scope.project_id, DesignCandidate.model_id == dp.model_id,
        DesignCandidate.status == CandidateStatus.APPLIED).order_by(DesignCandidate.applied_at.desc()).limit(1))
    if applied is None or not applied.applied_seq:
        return
    if str(dp.id) not in {s.get("decision_point_id") for s in applied.content.get("searches", [])}:
        return
    changed = session.scalar(select(func.count()).select_from(Changeset).where(
        Changeset.project_id == scope.project_id, Changeset.model_id == dp.model_id,
        Changeset.seq > applied.applied_seq))
    ready, _ = candidate_readiness(session, scope, applied)
    if ready and not changed:
        modeling.set_state(session, scope, dp.model_id, ModelState.READY_FOR_REVIEW,
                           note=f"Decision point accepted by {name}; design candidate {applied.id} has no other blockers")
