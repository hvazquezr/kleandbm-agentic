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
  (design.readiness); acceptance is always a person's decision.
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
from app.agents.architect.tools import HANDLERS, OWN_TRANSACTION_TOOLS, TOOL_SPECS, WRITE_TOOLS, ArchitectContext
from app.agents.requirements.agent import _history
from app.agents.runtime import Emit, Loop, State, finish_run, last_text, record_usage, stop_outcome
from app.agents.validator.agent import review as validator_review
from app.domain import design, modeling, sources, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.modeling import CreateOp, LinkOp
from app.domain.records import CheckStatus, LinkType, RecordType
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus
from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.modeling import DataModel
from app.persistence.models import Project

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
) -> None:
    """Execute one Architect run. `requirements_model` drives the Business Requirements
    subgraph behind ask_requirements; `validator_model` drives the Validator review. Without a
    Validator the design is applied or proposed unreviewed and stays Draft. Never raises:
    outcomes are recorded on the run, the candidate and the assistant message."""
    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        conversation = session.get(Conversation, run.conversation_id)
        user_msg = session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation.id, ConversationMessage.run_id == run.id,
                ConversationMessage.role == MessageRole.USER))
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
        )
        emit({"type": "candidate", "candidate_id": str(candidate.id), "status": str(candidate.status)})

        transcript = _history(session, conversation.id, user_msg.ordinal)
        transcript.append({
            "role": "user",
            "text": f"<project_context>\n{_context_block(session, scope, model, ctx, empty_at_start)}\n"
                    f"</project_context>\n\n{user_msg.text}",
        })

        loop = Loop(session=session, run=run, chat_model=chat_model, system_prompt=SYSTEM_PROMPT,
                    tools=TOOL_SPECS, handlers=HANDLERS, write_tools=WRITE_TOOLS, ctx=ctx, emit=emit,
                    max_iterations=max_iterations, own_transaction=OWN_TRANSACTION_TOOLS)
        outcome: dict[str, Any] = {}
        episode: dict[str, Any] = {"corrections": 0, "skipped": None}

        def review(state: State) -> dict:
            if state.get("stop") in ("refusal", "truncated"):
                return {}
            session.refresh(candidate)
            candidate.internal_checks = design.internal_checks(candidate.content, ctx.requirements())
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
                                   skipped=episode["skipped"], corrections=episode["corrections"]))
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
            final = app.invoke(
                {"transcript": transcript, "iterations": 0, "stop": None},
                {"configurable": {"thread_id": str(run.id)}, "recursion_limit": 6 * max_iterations + 20},
            )
        except Exception as exc:
            session.rollback()
            if isinstance(exc, LLMError):
                detail = str(exc)
            else:
                traceback.print_exc()
                detail = f"the agent failed unexpectedly ({type(exc).__name__})"
            candidate.status = CandidateStatus.FAILED
            record_usage(run, loop.usage, ctx.changeset_seqs)
            finish_run(session, run, conversation, RunStatus.FAILED, detail, emit, loop.activity,
                       extra={"candidate": _candidate_event(candidate)})
            return
        finally:
            ctx.exits.close()  # close source sessions (MCP server processes)

        if outcome.get("applied_seq"):
            ctx.changeset_seqs.append(outcome["applied_seq"])
        record_usage(run, loop.usage, ctx.changeset_seqs)
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
                   extra={"candidate": _candidate_event(candidate)})


def _issue_prefix(issue_key: str) -> str:
    # Escalated questions start with their issue key, so a reworded finding on the same issue
    # is recognised as already escalated.
    return f"Validation finding {issue_key}:"


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


def _validation_note(candidate: DesignCandidate, skipped: str | None, corrections: int,
                     escalated: dict[str, list[str]]) -> str:
    if skipped:
        return f"_Not validated: {skipped}. The design is unverified until the Validator reviews it._"
    rounds = candidate.validation or []
    if not rounds:
        return ""
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
    return note


def _settle(session: Session, scope, ctx: ArchitectContext, candidate: DesignCandidate, *, refused: bool,
            skipped: str | None, corrections: int) -> dict[str, Any]:
    """Recompute checks, escalate what remains, then apply or propose the candidate."""
    session.refresh(candidate)
    candidate.internal_checks = design.internal_checks(candidate.content, ctx.requirements())
    if refused or not candidate.content["tables"]:
        design.finish_candidate(candidate, CandidateStatus.FAILED)
        session.commit()
        return {"note": None if refused else "_No design was produced, so nothing was applied._"}

    # Escalate before applying: a register change after the readiness event would reset it to Draft.
    escalated = _escalate(session, scope, ctx, candidate)
    session.commit()
    validation = _validation_note(candidate, skipped, corrections, escalated)
    ready, blockers = design.readiness(candidate)
    blocker_list = "\n".join(f"- {b}" for b in blockers)

    if design.model_is_empty(session, scope, candidate.model_id) and not design.is_stale(session, scope, candidate):
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
                "note": f"_Applied to the model as changeset {candidate.applied_seq}._ {validation}\n\n{state}"}
    design.finish_candidate(candidate, CandidateStatus.PROPOSED)
    session.commit()
    state = ("Once applied, nothing blocks review, so it becomes Ready for Review." if ready else
             f"Once applied, it stays a Draft because of:\n{blocker_list}")
    return {"note": "_The model already had content, so these changes are proposed: review the diff and apply "
                    f"them._ {validation}\n\n{state}"}


def _candidate_event(candidate: DesignCandidate) -> dict[str, Any]:
    ready, blockers = design.readiness(candidate)
    return {"id": str(candidate.id), "status": str(candidate.status), "applied_seq": candidate.applied_seq,
            "hard_failures": len(design.hard_failures(candidate.internal_checks or [])),
            "ready_for_review": ready and candidate.status is CandidateStatus.APPLIED, "blockers": len(blockers)}
