"""Write what an agent run's model saw and said to `run_messages`, as the run goes.

The shared `Loop` records every run's own conversation; the Architect's design search records
its generator and evaluator calls under the Architect run. Entries are the neutral transcript
(`app/adapters/llm.py`) made readable: thinking is extracted from the provider's raw content
(signatures and encrypted reasoning are dropped), images are noted but not stored, and every
string has credentials redacted. Ordinals are allocated per run from the table, so the loop and
a search running inside one of its tool calls share one sequence.
"""

import hashlib
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.redaction import redact_credentials
from app.persistence.conversations import AgentRun, RunMessage


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_credentials(value)[0]
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    return value


def _thinking(raw: Any) -> list[str]:
    """Readable reasoning from a provider's raw assistant content: Claude thinking blocks and
    OpenAI reasoning summaries. Redacted or encrypted reasoning is noted, not stored."""
    out: list[str] = []
    for block in raw if isinstance(raw, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking" and block.get("thinking"):
            out.append(block["thinking"])
        elif kind == "redacted_thinking":
            out.append("[thinking redacted by the provider]")
        elif kind == "reasoning":
            out += [s.get("text", "") for s in block.get("summary") or [] if isinstance(s, dict) and s.get("text")]
    return out


def content_of(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(role, content) to store for one neutral transcript entry."""
    role = entry["role"]
    if role == "assistant":
        return role, {"text": entry.get("text") or "", "thinking": _thinking(entry.get("raw")),
                      "tool_calls": entry.get("tool_calls") or []}
    if role == "tool":
        results = []
        for r in entry.get("results") or []:
            item = {"id": r.get("id"), "content": r.get("content"), "is_error": bool(r.get("is_error"))}
            if r.get("image"):
                item["image"] = "attached to the model's input, not stored here"
            results.append(item)
        return role, {"results": results}
    return role, {"text": entry.get("text") or ""}


class RunTranscript:
    """Appends entries to one run's transcript."""

    def __init__(self, session: Session, run: AgentRun) -> None:
        self.session, self.run = session, run

    def _next_ordinal(self) -> int:
        return (self.session.scalar(
            select(func.max(RunMessage.ordinal)).where(
                RunMessage.tenant_id == self.run.tenant_id, RunMessage.project_id == self.run.project_id,
                RunMessage.run_id == self.run.id)) or 0) + 1

    def _add(self, ordinal: int, role: str, content: dict[str, Any], segment: str | None) -> None:
        self.session.add(RunMessage(
            tenant_id=self.run.tenant_id, project_id=self.run.project_id, run_id=self.run.id,
            ordinal=ordinal, role=role, segment=segment, content=_redact(content)))

    def system(self, prompt: str, segment: str | None = None) -> None:
        """The system prompt, in full only the first time it appears in the run."""
        sha = hashlib.sha256(prompt.encode()).hexdigest()
        seen = self.session.scalar(select(func.count()).select_from(RunMessage).where(
            RunMessage.tenant_id == self.run.tenant_id, RunMessage.project_id == self.run.project_id,
            RunMessage.run_id == self.run.id, RunMessage.role == "system",
            RunMessage.content["sha256"].astext == sha))
        self._add(self._next_ordinal(), "system", {"sha256": sha} if seen else {"text": prompt, "sha256": sha}, segment)
        self.session.flush()

    def add(self, entries: list[dict[str, Any]], segment: str | None = None, **extra: Any) -> None:
        """Append neutral transcript entries; `extra` goes into the last entry's content
        (e.g. stop_reason and model for an assistant turn)."""
        ordinal = self._next_ordinal()
        for i, entry in enumerate(entries):
            role, content = content_of(entry)
            if i == len(entries) - 1:
                content |= {k: v for k, v in extra.items() if v is not None}
            self._add(ordinal + i, role, content, segment)
        self.session.flush()
