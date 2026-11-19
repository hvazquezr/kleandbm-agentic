"""The Architect agent as an explicit LangGraph workflow.

    START -> model --(tool calls)--> tools --> model ... --> review --+--> finalize -> END
                ^                                                    |
                +------------------------ correct <------------------+

- One run designs one candidate for the conversation's model (spec §4 phase 1). The model
  investigates the linked sources and edits the candidate workspace through tools, and can
  consult the Business Requirements subgraph (ask_requirements).
- review runs the Validator subgraph (a different model family) on the candidate. If it finds
  correctable failures, correct hands them back to the model as a targeted message and the
  loop revises the candidate; review then revalidates. Limits (spec §7): at most
  `max_rounds` correction rounds per run (episode), at most `max_attempts` per issue key,
  carried across runs. Findings on person-edited elements are never corrected: they become
  questions to the person, and the candidate tools refuse to change those elements.
- finalize recomputes the internal checks, escalates remaining failures to the Questions &
  Conflicts register, and settles the candidate: applied automatically as one changeset
  (with the findings as CHECK records) when the model is still empty, otherwise `proposed`
  for the user to review as a diff. Ready for Review only when nothing blocks it
  (design.readiness); a search that ended without a selection raises a decision_point question
  and blocks readiness until a person accepts the design. Acceptance is always a person's decision.
"""

import traceback
import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.embeddings import Embedder
from app.adapters.llm import ChatModel, LLMError
from app.adapters.sources import SourceConnector
from app.adapters.storage import ObjectStore
from app.agents.architect.prompt import SYSTEM_PROMPT
from app.agents.architect import search
from app.agents.architect.tools import (
    BASELINE_TOOL_SPECS,
    HANDLERS,
    OWN_TRANSACTION_TOOLS,
    TOOL_SPECS,
    WRITE_TOOLS,
    ArchitectContext,
    _open_meaning_issues,
)
from app.agents.requirements.agent import _history
from app.adapters import tracing
from app.agents.runtime import (
    Emit, Loop, State, finish_run, last_text, record_usage, run_config, stop_outcome, turn_message,
)
from app.agents.validator.agent import review as validator_review
from app.domain import design, modeling, sources, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.modeling import CreateOp, LinkOp, UpdateOp
from app.domain.records import CheckStatus, FeasibilityStatus, LinkType, RecordType
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus
from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.modeling import Changeset, DataModel
from app.persistence.models import Project
from app.persistence.search import DecisionPoint, SearchNode

AGENT_NAME = "architect"


def _context_block(session: Session, scope, model: DataModel, ctx: ArchitectContext, empty: bool) -> str:
    project = session.get(Project, scope.project_id)
    lines = [f"Project: {project.name}" + (f" — {project.description}" if project.description else ""),
             f"Data model: {model.name}" + (f" — {model.description}" if model.description else "")]
    lines.append("The model is empty; your candidate will be applied when you finish." if empty else
                 "The model already has content; your candidate starts as a copy of it and will be shown to the "
                 "user as a diff to apply.")
    linked = [sources.get_connection(session, scope, cid) for cid in ctx.source_ids]
    if linked:
        lines.append("Linked sources: " + ", ".join(f"{c.name} ({c.database}, source_id {c.id})" for c in linked))
    else:
        lines.append("Linked sources: none. Design from the registers only and say the design is unverified against data.")
    seq = modeling.current_seq(session, scope)
    counts = {name: len(modeling._latest(session, scope, rtype, as_of=seq))
              for name, rtype in [("requirements", RecordType.REQUIREMENT), ("definitions", RecordType.DEFINITION),
                                  ("questions", RecordType.QUESTION), ("decisions", RecordType.DECISION)]}
    lines.append("Registers: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return "\n".join(lines)


def run_design(
    session_factory: sessionmaker[Session],
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    chat_model: ChatModel,
    embedder: Embedder,
    max_iterations: int,
    emit: Emit,
    connector: SourceConnector | None,
    box: sources.SecretBox | None,
    checkpointer: Any = None,
    store: ObjectStore | None = None,
    requirements_model: ChatModel | None = None,
    max_consultations: int = 3,
    consultation_max_iterations: int = 8,
    validator_model: ChatModel | None = None,
    validator_unavailable: str | None = None,
    validator_max_iterations: int = 16,
    max_rounds: int = 3,
    max_attempts: int = 3,
    tot: search.Settings | None = None,
    max_decision_points: int = 2,
) -> None:
    """Execute one Architect run. `requirements_model` drives the Business Requirements
    subgraph behind ask_requirements; `validator_model` drives the Validator review. Without a
    Validator the design is applied or proposed unreviewed and stays Draft. `tot` enables the
    bounded search at decision points (None: the single-candidate baseline). Never raises:
    outcomes are recorded on the run, the candidate and the assistant message."""
    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        conversation = session.get(Conversation, run.conversation_id)
        user_msg = turn_message(session, run)
        agent = Actor(kind=ActorKind.AGENT, id=run.id, on_behalf_of=run.initiated_by)
        try:
            scope = tenancy.resolve_project_scope(session, agent, tenant_id, project_id)
            model = modeling.get_model(session, scope, conversation.model_id)
        except Exception:  # membership revoked, or the model was deleted
            finish_run(session, run, None, RunStatus.FAILED, "you no longer have access to this model", emit, [])
            return

        # A new design supersedes earlier unapplied candidates for this model.
        for old in session.scalars(select(DesignCandidate).where(
                DesignCandidate.project_id == scope.project_id, DesignCandidate.model_id == model.id,
                DesignCandidate.status.in_([CandidateStatus.BUILDING, CandidateStatus.PROPOSED]))):
            old.status = CandidateStatus.DISCARDED
        candidate = design.start_candidate(session, scope, model.id, run.id)
        empty_at_start = design.model_is_empty(session, scope, model.id)
        session.commit()

        ctx = ArchitectContext(
            session=session, scope=scope, embedder=embedder, store=store,
            reason=f"Architect run {run.id}", model_id=model.id, candidate=candidate,
            connector=connector, box=box,
            source_ids=[c.id for c in sources.model_sources(session, scope, model.id) if not c.disabled_at],
            run=run, requirements_model=requirements_model, emit=emit, max_consultations=max_consultations,
            consultation_max_iterations=consultation_max_iterations,
            chat_model=chat_model, tot_enabled=tot is not None, tot_settings=tot,
            max_decision_points=max_decision_points,
        )
        emit({"type": "candidate", "candidate_id": str(candidate.id), "status": str(candidate.status)})

        transcript = _history(session, conversation.id, user_msg.ordinal)
        transcript.append({
            "role": "user",
            "text": f"<project_context>\n{_context_block(session, scope, model, ctx, empty_at_start)}\n"
                    f"</project_context>\n\n{user_msg.text}",
        })

        loop = Loop(session=session, run=run, chat_model=chat_model, system_prompt=SYSTEM_PROMPT,
                    tools=TOOL_SPECS if tot is not None else BASELINE_TOOL_SPECS, handlers=HANDLERS,
                    write_tools=WRITE_TOOLS, ctx=ctx, emit=emit,
                    max_iterations=max_iterations, own_transaction=OWN_TRANSACTION_TOOLS, history=len(transcript) - 1)
        outcome: dict[str, Any] = {}
        episode: dict[str, Any] = {"corrections": 0, "skipped": None}

        def review(state: State) -> dict:
            if state.get("stop") in ("refusal", "truncated"):
                return {}
            session.refresh(candidate)
            ctx.recheck(final=True)
            if not candidate.content["tables"]:
                session.commit()
                return {}
            if validator_model is None:
                episode["skipped"] = validator_unavailable or "no Validator model is configured"
                session.commit()
                return {}
            rounds = candidate.validation or []
            previous = design.latest_findings(candidate)
            correction = last_text(state["transcript"]) if episode["corrections"] else None
            session.commit()
            emit({"type": "review", "status": "started", "round": len(rounds)})
            rnd = validator_review(
                session, parent=run, candidate=candidate, chat_model=validator_model, embedder=embedder,
                store=store, connector=connector, box=box, source_ids=ctx.source_ids, round_no=len(rounds),
                previous=previous, correction=correction, max_iterations=validator_max_iterations, emit=emit)
            session.refresh(candidate)
            candidate.validation = [*(candidate.validation or []), rnd]
            session.commit()
            counts = {s.value: sum(1 for f in rnd["findings"] if f["status"] == s.value) for s in CheckStatus}
            emit({"type": "review", "status": rnd["status"], "round": rnd["round"], "counts": counts,
                  "run_id": rnd["run_id"]})
            return {}

        def _correctable() -> list[dict[str, Any]]:
            rounds = candidate.validation or []
            if not rounds or rounds[-1]["status"] != "completed":
                return []
            attempts = candidate.issue_attempts or {}
            return [f for f in rounds[-1]["findings"]
                    if f["status"] == CheckStatus.FAILED.value and not f.get("edited_by_person")
                    and attempts.get(f["issue_key"], 0) < max_attempts]

        def after_review(state: State) -> str:
            if state.get("stop") in ("refusal", "truncated") or episode["corrections"] >= max_rounds:
                return "finalize"
            if state["iterations"] >= max_iterations - 1:  # no budget left to correct
                return "finalize"
            return "correct" if _correctable() else "finalize"

        def correct(state: State) -> dict:
            fixes = _correctable()
            attempts = dict(candidate.issue_attempts or {})
            for f in fixes:
                attempts[f["issue_key"]] = attempts.get(f["issue_key"], 0) + 1
            candidate.issue_attempts = attempts
            episode["corrections"] += 1
            last = candidate.validation[-1]
            findings = last["findings"]
            ctx.protected = {design._key(e) for f in findings if f.get("edited_by_person") for e in f["elements"]}
            session.commit()
            lines = [f"<validator_findings round=\"{last['round']}\">",
                     f"The Validator reviewed the candidate. Correction round {episode['corrections']} of {max_rounds}. "
                     "Fix these findings in the candidate with the candidate tools, record a decision for any "
                     "design choice you change, then reply in two or three sentences with what you changed "
                     "(the Validator will check it). If a finding is wrong, don't change the design: say why, "
                     "citing evidence.", "", "To correct:"]
            for f in fixes:
                lines.append(f"- {f['issue_key']} [{f['check_type']}{', blocking' if f['is_blocking'] else ''}; "
                             f"attempt {attempts[f['issue_key']]} of {max_attempts}]: {f['summary']}"
                             + (f" Elements: {', '.join(f['elements'])}." if f["elements"] else "")
                             + (f" Recommended: {f['recommended_action']}" if f.get("recommended_action") else ""))
            fix_keys = {f["issue_key"] for f in fixes}
            others = [f for f in findings if f["status"] != CheckStatus.PASSED.value and f["issue_key"] not in fix_keys]
            if others:
                lines += ["", "Not yours to correct now (they go to the user as questions; don't change these):"]
                lines += [f"- {f['issue_key']} [{f['status']}]"
                          + (" (a person edited these elements)" if f.get("edited_by_person") else "")
                          + f": {f['summary']}" for f in others]
            lines.append("</validator_findings>")
            return {"transcript": [*state["transcript"], {"role": "user", "text": "\n".join(lines)}], "stop": None}

        def finalize(state: State) -> dict:
            outcome.update(_settle(session, scope, ctx, candidate, refused=state.get("stop") == "refusal",
                                   skipped=episode["skipped"], corrections=episode["corrections"],
                                   activity=loop.activity))
            return {}

        graph = StateGraph(State)
        loop.add_to(graph, after_loop="review")
        graph.add_node("review", review)
        graph.add_node("correct", correct)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("review", after_review, ["correct", "finalize"])
        graph.add_edge("correct", "model")
        graph.add_edge("finalize", END)
        app = graph.compile(checkpointer=checkpointer)

        try:
            with tracing.agent_run(run):
                final = app.invoke(
                    {"transcript": transcript, "iterations": 0, "stop": None},
                    run_config(run, 6 * max_iterations + 20),
                )
        except Exception as exc:
            session.rollback()
            if isinstance(exc, LLMError):
                detail = str(exc)
            else:
                traceback.print_exc()
                detail = f"the agent failed unexpectedly ({type(exc).__name__})"
            candidate.status = CandidateStatus.FAILED
            record_usage(run, _total_usage(loop, ctx), ctx.changeset_seqs)
            finish_run(session, run, conversation, RunStatus.FAILED, detail, emit, loop.activity,
                       extra={"candidate": _candidate_event(session, scope, candidate)})
            return
        finally:
            ctx.exits.close()  # close source sessions (MCP server processes)

        if outcome.get("applied_seq"):
            ctx.changeset_seqs.append(outcome["applied_seq"])
        record_usage(run, _total_usage(loop, ctx), ctx.changeset_seqs)
        text = last_text(final["transcript"])
        note = outcome.get("note")
        stop = final.get("stop")
        status, detail = RunStatus.COMPLETED, None
        if stop:
            detail, text = stop_outcome(stop, text)
            status = RunStatus.STOPPED
        if note:
            text = f"{text}\n\n{note}".strip()
        finish_run(session, run, conversation, status, detail, emit, loop.activity, text=text,
                   extra={"candidate": _candidate_event(session, scope, candidate)})


def _issue_prefix(issue_key: str) -> str:
    # Escalated questions start with their issue key, so a reworded finding on the same issue
    # is recognised as already escalated.
    return f"Validation finding {issue_key}:"


def _total_usage(loop: Loop, ctx: ArchitectContext):
    """The run's own model calls plus those inside its decision-point searches."""
    from app.adapters.llm import Usage

    total = Usage()
    total.add(loop.usage)
    total.add(ctx.extra_usage)
    return total


def _escalate(session: Session, scope, ctx: ArchitectContext, candidate: DesignCandidate) -> dict[str, list[str]]:
    """Make sure each finding that remains failed, or blocking and unverified, has an open
    question, so a person decides (spec §7: preserve them and escalate with impact and next
    action). A finding that depends on open questions (its question_ids) is linked to them; a
    finding whose issue key was escalated before is linked to that question; otherwise a new
    question is raised. The question ids are written back onto the finding, so its CHECK record
    links to them. Returns {"raised": codes, "linked": codes}."""
    out: dict[str, list[str]] = {"raised": [], "linked": []}
    rounds = [dict(r) for r in candidate.validation or []]
    done = [r for r in rounds if r.get("status") == "completed"]
    if not done:
        return out
    findings = [dict(f) for f in done[-1]["findings"]]
    open_questions = {str(q.id): q for q in ctx.open_questions()}
    existing = design.element_record_ids(candidate.content)
    attempts = candidate.issue_attempts or {}
    for f in findings:
        if not (f["status"] == CheckStatus.FAILED.value
                or (f["status"] == CheckStatus.UNVERIFIED.value and f.get("is_blocking"))):
            continue
        linked = [q for q in f.get("question_ids") or [] if q in open_questions]
        if not linked:
            linked = [qid for qid, q in open_questions.items()
                      if str(q.data.get("description") or "").startswith(_issue_prefix(f["issue_key"]))]
        if linked:
            f["question_ids"] = linked
            out["linked"] += [open_questions[q].code for q in linked if open_questions[q].code not in out["linked"]]
            continue
        if f.get("edited_by_person"):
            text = (f"{_issue_prefix(f['issue_key'])} you edited {', '.join(f['elements'])}, and the Validator found: "
                    f"{f['summary']} Should the Architect change it, or is your version intended?")
        elif f["status"] == CheckStatus.FAILED.value:
            tried = attempts.get(f["issue_key"], 0)
            after = f" after {tried} correction attempt{'s' if tried != 1 else ''}" if tried else ""
            text = f"{_issue_prefix(f['issue_key'])} still failing{after}. {f['summary']}"
        else:
            text = f"{_issue_prefix(f['issue_key'])} couldn't be verified. {f['summary']}"
        ops: list = [CreateOp(type=RecordType.QUESTION, ref="q", data={
            "kind": "validation_finding", "description": text[:4000], "impact": f.get("impact"),
            "is_blocking": bool(f.get("is_blocking")), "proposed_action": f.get("recommended_action")})]
        targets = [uuid.UUID(r) for r in f.get("requirement_ids") or []]
        targets += [existing[design._key(e)] for e in f.get("elements") or [] if design._key(e) in existing]
        for target in dict.fromkeys(targets):
            ops.append(LinkOp(from_id="$q", to_id=target, link_type=LinkType.CONCERNS))
        result = modeling.apply_changeset(session, scope, ops, reason=f"{ctx.reason}: escalated validation finding")
        ctx.changeset_seqs.append(result.seq)
        question = modeling.get_record(session, scope, result.created["q"])
        f["question_ids"] = [str(question.id)]
        open_questions[str(question.id)] = question  # a second finding on the same key links to it
        out["raised"].append(question.code)
    done[-1]["findings"] = findings
    candidate.validation = rounds  # reassign so the JSON column is saved
    return out


def _design_key(table: dict[str, Any]) -> str:
    import json

    def plain(v):
        if isinstance(v, dict):
            return {k: plain(x) for k, x in v.items() if k not in ("record_id", "base_seq")}
        if isinstance(v, list):
            return [plain(x) for x in v]
        return v
    return json.dumps(plain(table), sort_keys=True, default=str)


def _raise_decision_questions(session: Session, scope, ctx: ArchitectContext, candidate: DesignCandidate) -> list[str]:
    """A search that ended without a selection is a person's decision (spec §7): raise one
    blocking decision_point question per such search, saying what was tried, why each
    alternative wasn't chosen and what the Architect did afterwards, linked to the decision
    record and the affected elements. Its readiness blocker clears only when a person accepts
    the design (design.resolve_decision_point), never when the Architect decides around it."""
    raised: list[str] = []
    entries = [dict(e) for e in candidate.content.get("searches", [])]
    existing = design.element_record_ids(candidate.content)
    final_tables = {design._key(t["name"]): t for t in candidate.content.get("tables", [])}
    for entry in entries:
        if entry.get("status") == "selected" or entry.get("question_id"):
            continue
        dp = session.get(DecisionPoint, uuid.UUID(entry["decision_point_id"]))
        if dp is None or dp.project_id != scope.project_id or dp.resolution is not None:
            continue
        nodes = list(session.scalars(select(SearchNode).where(SearchNode.decision_point_id == dp.id)
                                     .order_by(SearchNode.depth, SearchNode.created_at)))
        after = next((n for n in nodes if n.id == dp.selected_node_id), None) or next(
            (n for n in nodes if n.depth == 0), None)
        tried = [f"- {n.choice.strip().rstrip('.')}: {n.status}" + (f" ({n.reason})" if n.reason else "")
                 for n in nodes if n.depth > 0 and str(n.status) != "duplicate"]
        scoped = {design._key(e.split(".")[0]) for e in dp.scope.get("elements", [])}
        changes: list[str] = []
        if after is not None:
            searched = {design._key(t["name"]): t for t in after.content.get("tables", [])}
            searched_added = set(searched) - {design._key(t["name"]) for n in nodes if n.depth == 0
                                              for t in n.content.get("tables", [])}
            for key in sorted(scoped | searched_added | (set(final_tables) - set(searched))):
                before, now = searched.get(key), final_tables.get(key)
                if before is None and now is not None:
                    changes.append(f"added {now['name']}")
                elif before is not None and now is None:
                    changes.append(f"removed {before['name']}")
                elif before is not None and now is not None and _design_key(before) != _design_key(now):
                    changes.append(f"changed {now['name']}")
        decision = modeling.get_record(session, scope, dp.decision_id) if dp.decision_id else None
        outcome = ("only a partial design that doesn't pass every hard check" if str(dp.status) == "partial"
                   else "no alternative that qualified, so the design was left unchanged")
        afterwards = (f"After the search, the Architect {', '.join(changes)}." if changes
                      else "The Architect made no further changes to these tables after the search.")
        text = (f"Decision point{f' {decision.code}' if decision and decision.code else ''}: {dp.question} "
                f"The bounded search ({str(dp.trigger).replace('_', ' ')}) found {outcome} "
                f"({dp.stop_reason}).\nAlternatives tried:\n" + "\n".join(tried or ["- none"]) + f"\n{afterwards}")
        ops: list = [CreateOp(type=RecordType.QUESTION, ref="q", data={
            "kind": "decision_point", "description": text[:8000], "is_blocking": True,
            "impact": "The model stays Draft until a person accepts or rejects this design.",
            "proposed_action": "Accept the design as it stands, or reject it so the Architect redesigns it."})]
        targets = [dp.decision_id] if dp.decision_id else []
        targets += [uuid.UUID(r) for r in dp.scope.get("requirement_ids", [])]
        targets += [existing[k] for k in scoped if k in existing]
        for target in dict.fromkeys(targets):
            ops.append(LinkOp(from_id="$q", to_id=target, link_type=LinkType.CONCERNS))
        result = modeling.apply_changeset(session, scope, ops, reason=f"{ctx.reason}: decision point needs a person")
        ctx.changeset_seqs.append(result.seq)
        question = modeling.get_record(session, scope, result.created["q"])
        dp.question_id = question.id
        entry["question_id"] = str(question.id)
        raised.append(question.code or "")
        ctx.changed.append({"id": str(question.id), "code": question.code or "", "type": "question"})
    if raised:
        candidate.content = {**candidate.content, "searches": entries}
    return raised


def _withdraw_unsettled_support(session: Session, scope, ctx: ArchitectContext,
                                candidate: DesignCandidate) -> list[str]:
    """Deterministic, after escalation: full support set during the run can't outlive a blocking
    question raised later (a Validator finding escalated at finalize, say). A requirement this
    design maps, marked fully_supported by an agent, goes back to partially_supported when an open
    blocking question now concerns it, a definition it uses, or an element it maps to, or when it's
    in an unresolved conflict. A person's own feasibility call is left alone. Returns the codes."""
    existing = design.element_record_ids(candidate.content)
    mapped = {m["requirement_id"]: m["elements"] for m in candidate.content.get("mappings", [])}
    requirements = [r for r in ctx.requirements() if str(r.id) in mapped
                    and r.data.get("feasibility_status") == FeasibilityStatus.FULLY_SUPPORTED.value]
    if not requirements:
        return []
    by_people = set(session.scalars(select(Changeset.seq).where(
        Changeset.project_id == scope.project_id, Changeset.seq.in_([r.seq for r in requirements]),
        Changeset.actor_kind == ActorKind.USER)))
    links = modeling.list_live_links(session, scope)
    blocking = [q for q in ctx.open_questions() if q.data.get("is_blocking")]
    withdrawn: list[str] = []
    for req in requirements:
        if req.seq in by_people:
            continue
        issues = _open_meaning_issues(ctx, req.id)
        keys = {design._key(e) for e in mapped[str(req.id)]}
        elements = {existing[k] for k in keys | {k.split(".")[0] for k in keys} if k in existing}
        issues += [f"{q.code} is open and blocking about a mapped element" for q in blocking
                   if any(l.link_type is LinkType.CONCERNS and l.from_id == q.id and l.to_id in elements
                          for l in links)
                   and f"{q.code} is open and blocking" not in issues]
        if not issues:
            continue
        notes = (req.data.get("feasibility_notes") or "").strip()
        note = ("Full support withdrawn when the design was finalized, because the meaning is still open: "
                f"{'; '.join(dict.fromkeys(issues))}. A person settles it.")
        result = modeling.apply_changeset(session, scope, [UpdateOp(id=req.id, expected_seq=req.seq, data={
            "feasibility_status": FeasibilityStatus.PARTIALLY_SUPPORTED.value,
            "feasibility_notes": f"{notes}\n\n{note}".strip()[:4000]})],
            reason=f"{ctx.reason}: full support withdrawn while questions are open")
        ctx.changeset_seqs.append(result.seq)
        ctx.changed.append({"id": str(req.id), "code": req.code or "", "type": "requirement"})
        withdrawn.append(req.code or str(req.id))
    return withdrawn


def _validation_note(candidate: DesignCandidate, skipped: str | None, corrections: int,
                     escalated: dict[str, list[str]]) -> str:
    if skipped:
        return f"_Not validated: {skipped}. The design is unverified until the Validator reviews it._" + _search_note(escalated)
    rounds = candidate.validation or []
    if not rounds:
        return _search_note(escalated).strip()
    last = rounds[-1]
    if last["status"] != "completed":
        return f"_The Validator's review didn't complete ({last.get('detail')}); the design is unverified._"
    counts = {s.value: sum(1 for f in last["findings"] if f["status"] == s.value) for s in CheckStatus}
    note = (f"_Validator ({last['model']}): {counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['unverified']} unverified")
    if corrections:
        note += f" after {corrections} correction round{'s' if corrections > 1 else ''}"
    note += "._"
    if escalated["raised"]:
        note += f" _Raised {', '.join(escalated['raised'])} for what remains open._"
    if escalated["linked"]:
        note += f" _Open findings are linked to {', '.join(escalated['linked'])}, already open._"
    return note + _search_note(escalated)


def _search_note(escalated: dict[str, list[str]]) -> str:
    note = ""
    if escalated.get("withdrawn"):
        note = (f" _{', '.join(escalated['withdrawn'])} went back to partially supported: a blocking question "
                "or conflict about it is still open._")
    if not escalated.get("searches"):
        return note
    return note + (f" _A decision-point search ended without a selected design, so {', '.join(escalated['searches'])} "
            "asks you to accept or reject the design as it stands._")


def _listed(items: list[str], limit: int = 12) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _changes_note(diff: dict[str, list[str]], activity: list[dict[str, Any]], *, proposed: bool) -> str:
    """What the whole run changed, from the candidate's diff and the register entries its tools
    wrote. Deterministic, so the reply covers the run even when the model's last turn only
    describes its corrections after a Validator round."""
    lines = []
    rank = {"attribute": 1, "relationship": 2}  # tables first: they're the headline change
    for key, label in (("added", "Added"), ("changed", "Changed"), ("removed", "Removed")):
        if diff.get(key):
            items = sorted(diff[key], key=lambda d: rank.get(d.split(" ", 1)[0], 0))
            lines.append(f"- {label} in the model{' (once applied)' if proposed else ''}: {_listed(items)}")
    by_action: dict[str, list[str]] = {}
    for item in activity:
        record = item.get("record") if item.get("ok") else None
        if record and record.get("code"):
            codes = by_action.setdefault(record.get("action") or "changed", [])
            if record["code"] not in codes:
                codes.append(record["code"])
    registers = [f"{action} {_listed(codes)}" for action, codes in by_action.items()]
    if registers:
        lines.append(f"- Registers: {'; '.join(registers)}")
    if not lines:
        return "**What this run changed:** nothing in the model or the registers."
    return "**What this run changed:**\n" + "\n".join(lines)


def _settle(session: Session, scope, ctx: ArchitectContext, candidate: DesignCandidate, *, refused: bool,
            skipped: str | None, corrections: int, activity: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Recompute checks, escalate what remains, then apply or propose the candidate."""
    session.refresh(candidate)
    ctx.recheck(final=True)
    if refused or not candidate.content["tables"]:
        design.finish_candidate(candidate, CandidateStatus.FAILED)
        session.commit()
        return {"note": None if refused else "_No design was produced, so nothing was applied._"}

    # Escalate before applying: a register change after the readiness event would reset it to Draft.
    escalated = _escalate(session, scope, ctx, candidate)
    escalated["searches"] = _raise_decision_questions(session, scope, ctx, candidate)
    escalated["withdrawn"] = _withdraw_unsettled_support(session, scope, ctx, candidate)
    # The watermark the review and escalations describe; register changes after it make the
    # review out of date (design.review_outdated).
    candidate.proposed_seq = modeling.current_seq(session, scope)
    session.commit()
    validation = _validation_note(candidate, skipped, corrections, escalated)
    ready, blockers = design.candidate_readiness(session, scope, candidate)
    blocker_list = "\n".join(f"- {b}" for b in blockers)
    _, diff = design.plan_changes(session, scope, candidate)  # against the live model, before any apply
    empty = design.model_is_empty(session, scope, candidate.model_id)
    changes = _changes_note(diff, activity or [], proposed=not empty)

    if empty and not design.is_stale(session, scope, candidate):
        try:
            design.apply_candidate(session, scope, candidate.id)
        except Exception as exc:  # e.g. a payload the model got wrong; keep it for review instead
            session.rollback()
            session.refresh(candidate)
            design.finish_candidate(candidate, CandidateStatus.PROPOSED)
            session.commit()
            return {"note": f"_The design couldn't be applied automatically ({exc}); it is saved for review._"
                            f" {validation}"}
        session.commit()
        state = ("Nothing blocks review, so the model is **Ready for Review**; accepting it is your decision."
                 if ready else f"The model stays a **Draft** because of:\n{blocker_list}")
        return {"applied_seq": candidate.applied_seq,
                "note": f"{changes}\n\n_Applied to the model as changeset {candidate.applied_seq}._ {validation}\n\n{state}"}
    design.finish_candidate(candidate, CandidateStatus.PROPOSED)
    session.commit()
    state = ("Once applied, nothing blocks review, so it becomes Ready for Review." if ready else
             f"Once applied, it stays a Draft because of:\n{blocker_list}")
    return {"note": f"{changes}\n\n**Not applied yet.** The model already had content, so these changes are a "
                    f"proposal: open **Review** to check the diff and apply or discard it. {validation}\n\n{state}"}


def _candidate_event(session: Session, scope, candidate: DesignCandidate) -> dict[str, Any]:
    ready, blockers = design.candidate_readiness(session, scope, candidate)
    return {"id": str(candidate.id), "status": str(candidate.status), "applied_seq": candidate.applied_seq,
            "hard_failures": len(design.hard_failures(candidate.internal_checks or [])),
            "ready_for_review": ready and candidate.status is CandidateStatus.APPLIED, "blockers": len(blockers)}
