"""Migration 0021: each model's conversations merge into one Assistant conversation, turns
interleaved in the order they started; conversations without a model are left alone."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0021_one_conversation_per_model.py"


def _merge(session):
    spec = importlib.util.spec_from_file_location("merge_0021", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    merged = module.merge_conversations(session.connection())
    session.commit()
    return merged


def _setup(as_user):
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = alice.post(f"{base}/models", json={"name": "Retail"}).json()["id"]
    return alice, base, model


def _turn(session, conv_id, agent, at, text, *, child=None):
    """A user message and its reply, at `at`; `child` makes the reply come from a child run."""
    conv = session.get(Conversation, uuid.UUID(conv_id))
    ordinal = (session.scalar(select(ConversationMessage.ordinal).where(ConversationMessage.conversation_id == conv.id)
                              .order_by(ConversationMessage.ordinal.desc()).limit(1)) or 0) + 1
    run = AgentRun(tenant_id=conv.tenant_id, project_id=conv.project_id, conversation_id=conv.id, agent=agent,
                   llm_model="scripted", status=RunStatus.COMPLETED, initiated_by=conv.created_by, changeset_seqs=[])
    session.add(run)
    session.flush()
    answer = run
    if child:
        answer = AgentRun(tenant_id=conv.tenant_id, project_id=conv.project_id, conversation_id=conv.id, agent=child,
                          parent_run_id=run.id, llm_model="scripted", status=RunStatus.COMPLETED,
                          initiated_by=conv.created_by, changeset_seqs=[])
        session.add(answer)
        session.flush()
    for i, (role, body, owner) in enumerate([(MessageRole.USER, text, run), (MessageRole.ASSISTANT, f"re: {text}", answer)]):
        session.add(ConversationMessage(
            tenant_id=conv.tenant_id, project_id=conv.project_id, conversation_id=conv.id, ordinal=ordinal + i,
            role=role, text=body, attachments=[], activity=[], run_id=owner.id, author_id=conv.created_by,
            created_at=at + timedelta(seconds=i)))
    session.commit()


def test_a_models_conversations_merge_into_its_assistant_in_turn_order(as_user, db_session_factory, single_agent_conversation):
    alice, base, model = _setup(as_user)
    reqs = single_agent_conversation(base, "auth0|alice", model_id=model)
    arch = single_agent_conversation(base, "auth0|alice", model_id=model, agent="architect")
    assistant = alice.post(f"{base}/conversations", json={"model_id": model}).json()
    loose = single_agent_conversation(base, "auth0|alice")  # no model: can't be routed
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    with db_session_factory() as s:
        _turn(s, reqs["id"], "business_requirements", t0, "define net sales")
        _turn(s, arch["id"], "architect", t0 + timedelta(minutes=5), "draft the model")
        _turn(s, reqs["id"], "business_requirements", t0 + timedelta(minutes=10), "exclude freight")
        _turn(s, assistant["id"], "router", t0 + timedelta(minutes=15), "add vendors", child="business_requirements")
        _turn(s, loose["id"], "business_requirements", t0, "project-level note")

        assert _merge(s) == 2
        convs = list(s.scalars(select(Conversation).where(Conversation.model_id == uuid.UUID(model))))
        assert [(str(c.id), c.agent) for c in convs] == [(assistant["id"], "assistant")]
        texts = [(m.ordinal, m.text) for m in s.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == convs[0].id).order_by(ConversationMessage.ordinal))]
        assert texts == [(1, "define net sales"), (2, "re: define net sales"), (3, "draft the model"),
                         (4, "re: draft the model"), (5, "exclude freight"), (6, "re: exclude freight"),
                         (7, "add vendors"), (8, "re: add vendors")]
        runs = s.scalars(select(AgentRun.conversation_id).where(AgentRun.project_id == convs[0].project_id,
                                                                 AgentRun.conversation_id != uuid.UUID(loose["id"])))
        assert set(runs) == {convs[0].id}
        assert s.get(Conversation, uuid.UUID(loose["id"])).agent == "business_requirements"
        assert _merge(s) == 0  # running again changes nothing

    # The API serves the merged thread with each reply's agent.
    msgs = alice.get(f"{base}/conversations/{assistant['id']}/messages").json()
    assert [m["agent"] for m in msgs if m["role"] == "assistant"] == ["business_requirements", "architect",
                                                                     "business_requirements", "business_requirements"]


def test_without_an_assistant_the_oldest_conversation_becomes_it(as_user, db_session_factory, single_agent_conversation):
    _, base, model = _setup(as_user)
    first = single_agent_conversation(base, "auth0|alice", model_id=model)
    second = single_agent_conversation(base, "auth0|alice", model_id=model, agent="architect")
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    with db_session_factory() as s:
        _turn(s, second["id"], "architect", t0, "draft")
        _turn(s, first["id"], "business_requirements", t0 + timedelta(minutes=1), "define")
        assert _merge(s) == 1
        [conv] = s.scalars(select(Conversation).where(Conversation.model_id == uuid.UUID(model)))
        assert str(conv.id) == first["id"] and conv.agent == "assistant" and conv.title == "AI Assistant"
        assert [m.text for m in s.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conv.id).order_by(ConversationMessage.ordinal))] == [
            "draft", "re: draft", "define", "re: define"]
