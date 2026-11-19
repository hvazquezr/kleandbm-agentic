"""One conversation per model: merge each model's conversations into its Assistant conversation

Before the request router, a model had a Business Requirements conversation and an Architect
conversation. This moves every model's conversations into one Assistant conversation (the
oldest Assistant one, or else the model's oldest conversation, converted). Turns are
interleaved in the order they started; each turn keeps its messages in order. Runs move with
their messages, so transcripts, assessments and routing decisions stay attached. The emptied
conversations are deleted. Conversations without a model can't be routed and are left as
they are.

LangGraph checkpoint metadata keeps the old conversation id. Checkpoints are only for resuming
a run, and nothing reads them by conversation.

Revision ID: 0021_one_conversation_per_model
Revises: 0020_routing_overrides
Create Date: 2026-11-18 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0021_one_conversation_per_model'
down_revision: str | Sequence[str] | None = '0020_routing_overrides'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSISTANT_TITLE = "AI Assistant"


def merge_conversations(bind) -> int:
    """Merge every model's conversations into one Assistant conversation. Returns how many
    conversations were merged away. Safe to run again: a model with one Assistant conversation
    is left alone."""
    merged = 0
    models = bind.execute(sa.text(
        "SELECT DISTINCT tenant_id, project_id, model_id FROM conversations WHERE model_id IS NOT NULL")).all()
    for tenant_id, project_id, model_id in models:
        convs = bind.execute(sa.text(
            "SELECT id, agent FROM conversations WHERE tenant_id = :t AND project_id = :p AND model_id = :m "
            "ORDER BY (agent = 'assistant') DESC, created_at, id"),
            {"t": tenant_id, "p": project_id, "m": model_id}).all()
        target, target_agent = convs[0]
        others = [c for c, _ in convs[1:]]
        if target_agent == "assistant" and not others:
            continue
        if target_agent != "assistant":
            bind.execute(sa.text("UPDATE conversations SET agent = 'assistant', title = :title WHERE id = :id"),
                         {"title": ASSISTANT_TITLE, "id": target})
        rank = {c: i for i, (c, _) in enumerate(convs)}
        messages = bind.execute(sa.text(
            "SELECT m.id, m.conversation_id, m.ordinal, m.created_at, COALESCE(r.parent_run_id, r.id, m.id) AS turn "
            "FROM conversation_messages m LEFT JOIN agent_runs r ON r.id = m.run_id "
            "WHERE m.conversation_id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": [c for c, _ in convs]}).all()
        started: dict = {}
        for m in messages:
            started[m.turn] = min(started.get(m.turn, m.created_at), m.created_at)
        ordered = sorted(messages, key=lambda m: (started[m.turn], rank[m.conversation_id], m.ordinal))
        # Two passes, so the unique (conversation_id, ordinal) never collides mid-way.
        for i, m in enumerate(ordered, start=1):
            bind.execute(sa.text("UPDATE conversation_messages SET conversation_id = :c, ordinal = :o WHERE id = :id"),
                         {"c": target, "o": -i, "id": m.id})
        bind.execute(sa.text("UPDATE conversation_messages SET ordinal = -ordinal WHERE conversation_id = :c"),
                     {"c": target})
        if others:
            bind.execute(sa.text("UPDATE agent_runs SET conversation_id = :c WHERE conversation_id IN :ids")
                         .bindparams(sa.bindparam("ids", expanding=True)), {"c": target, "ids": others})
            bind.execute(sa.text("DELETE FROM conversations WHERE id IN :ids")
                         .bindparams(sa.bindparam("ids", expanding=True)), {"ids": others})
        merged += len(others)
    return merged


def upgrade() -> None:
    merge_conversations(op.get_bind())


def downgrade() -> None:
    # The merge isn't undone: which conversation each turn came from isn't kept, and the
    # schema is unchanged, so earlier revisions read the merged conversations as they are.
    pass
