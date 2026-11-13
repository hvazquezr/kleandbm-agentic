"""Provider-neutral chat model interface for agents.

Agents work with a neutral transcript and neutral tool definitions; each provider adapter
translates to and from its own API. Provider-specific content that must be replayed
unchanged within a run (for example Claude's thinking blocks) travels in `AssistantTurn.raw`
and is only read by the adapter that produced it.

Transcript entries:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [ToolCall-dict...], "raw": provider data}
  {"role": "tool", "results": [{"id": str, "content": str, "is_error": bool,
                                "image": {"media_type": str, "data": base64 str} (optional)}]}
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema for the input. Strict-compatible: additionalProperties false, every property
    # required, optional values expressed as nullable types.
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | other provider values
    usage: Usage
    model: str
    raw: Any = None
    refusal_category: str | None = None

    def to_transcript(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "text": self.text,
            "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in self.tool_calls],
            "raw": self.raw,
        }


class LLMError(Exception):
    """The provider failed in a way the agent can't recover from within this run."""


class ChatModel(Protocol):
    model: str

    def complete(
        self,
        system: str,
        transcript: list[dict[str, Any]],
        tools: list[ToolSpec],
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantTurn: ...


# --- Anthropic ------------------------------------------------------------------------------


class AnthropicChatModel:
    """Claude via the official SDK: streaming, adaptive thinking (always on for Opus 5.5,
    controlled by effort), prompt caching, and server-side refusal fallbacks."""

    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int) -> None:
        import anthropic

        if not api_key:
            raise LLMError("KLEANDBM_ANTHROPIC_API_KEY is not set; add it to backend/.env")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        # strict: true guarantees schema-valid inputs. Forced tool_choice isn't available on
        # Opus 5.5, so the prompt steers tool use and tool_choice stays auto.
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema, "strict": True}
            for t in tools
        ]

    @staticmethod
    def _messages(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in transcript:
            if entry["role"] == "user":
                out.append({"role": "user", "content": entry["text"]})
            elif entry["role"] == "assistant":
                # Replay this provider's content unchanged (thinking blocks included) so
                # preserved thinking stays valid within the run.
                content = entry.get("raw") or [{"type": "text", "text": entry["text"] or "(no text)"}]
                out.append({"role": "assistant", "content": content})
            elif entry["role"] == "tool":
                out.append({"role": "user", "content": [AnthropicChatModel._tool_result(r) for r in entry["results"]]})
        return out

    @staticmethod
    def _tool_result(r: dict[str, Any]) -> dict[str, Any]:
        content: Any = r["content"]
        if r.get("image"):  # a tool that returns an image for the model to look at
            img = r["image"]
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}},
                {"type": "text", "text": r["content"]},
            ]
        return {"type": "tool_result", "tool_use_id": r["id"], "content": content, "is_error": bool(r.get("is_error"))}

    def complete(self, system, transcript, tools, on_text=None) -> AssistantTurn:
        a = self._anthropic
        try:
            with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=self._messages(transcript),
                tools=self._tools(tools),
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                # On a policy decline the API retries on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if on_text and event.type == "content_block_delta" and event.delta.type == "text_delta":
                        on_text(event.delta.text)
                message = stream.get_final_message()
        except a.AuthenticationError as exc:
            raise LLMError("the Anthropic API key was rejected") from exc
        except a.RateLimitError as exc:
            raise LLMError("rate limited by the Anthropic API; try again shortly") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMError("could not reach the Anthropic API") from exc

        text = "".join(b.text for b in message.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in message.content if b.type == "tool_use"]
        u = message.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        details = getattr(message, "stop_details", None)
        return AssistantTurn(
            text=text,
            tool_calls=calls,
            stop_reason=message.stop_reason or "end_turn",
            usage=usage,
            model=message.model,
            raw=[b.model_dump(mode="json", exclude_none=True) for b in message.content],
            refusal_category=getattr(details, "category", None) if message.stop_reason == "refusal" else None,
        )


# --- Scripted fake (tests) ------------------------------------------------------------------


@dataclass
class ScriptedChatModel:
    """Replays a fixed list of turns. Each step is either an AssistantTurn-like dict
    {"text": ..., "tool_calls": [{"name": ..., "input": {...}}]} or a callable that receives
    the transcript so far and returns such a dict (to react to tool results)."""

    steps: list[Any]
    model: str = "scripted-test-model"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, system, transcript, tools, on_text=None) -> AssistantTurn:
        self.calls.append({"system": system, "transcript": json.loads(json.dumps(transcript, default=str)),
                           "tools": [t.name for t in tools]})
        if not self.steps:
            step: dict[str, Any] = {"text": "(script finished)"}
        else:
            step = self.steps.pop(0)
            if callable(step):
                step = step(transcript)
        text = step.get("text", "")
        if on_text and text:
            for word in text.split(" "):
                on_text(word + " ")
        calls = [ToolCall(f"call_{len(self.calls)}_{i}", c["name"], c["input"]) for i, c in enumerate(step.get("tool_calls", []))]
        return AssistantTurn(
            text=text,
            tool_calls=calls,
            stop_reason=step.get("stop_reason", "tool_use" if calls else "end_turn"),
            usage=Usage(input_tokens=100, output_tokens=20),
            model=self.model,
        )
