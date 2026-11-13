"""Live checks against the real Claude API. Opt-in (they cost money): pytest -m live_llm
Requires KLEANDBM_ANTHROPIC_API_KEY."""

import pytest

from app.adapters.llm import AnthropicChatModel, ToolSpec
from app.config import get_settings

pytestmark = pytest.mark.live_llm


@pytest.fixture
def model():
    s = get_settings()
    if not s.anthropic_api_key:
        pytest.skip("KLEANDBM_ANTHROPIC_API_KEY not set")
    return AnthropicChatModel(s.anthropic_api_key, s.requirements_agent_model, "low", 2000)


def test_tool_call_round_trip(model):
    tool = ToolSpec("record_term", "Record a business term.", {
        "type": "object", "properties": {"term": {"type": "string"}},
        "required": ["term"], "additionalProperties": False})
    streamed: list[str] = []
    transcript = [{"role": "user", "text": "Record the business term 'net revenue' using the record_term tool."}]
    turn = model.complete("You record business terms with tools.", transcript, [tool], on_text=streamed.append)
    assert turn.tool_calls and turn.tool_calls[0].name == "record_term"
    assert "net revenue" in turn.tool_calls[0].input["term"].lower()
    assert turn.usage.input_tokens > 0

    # The raw assistant content (thinking included) replays unchanged with the tool result.
    transcript += [turn.to_transcript(),
                   {"role": "tool", "results": [{"id": turn.tool_calls[0].id, "content": '{"code": "DEF-001"}', "is_error": False}]}]
    final = model.complete("You record business terms with tools.", transcript, [tool], on_text=streamed.append)
    assert final.stop_reason == "end_turn" and final.text


def test_agent_tool_definitions_are_accepted(model):
    """Send the agent's real tools: the API validates every schema on the request."""
    from app.agents.requirements.prompt import SYSTEM_PROMPT
    from app.agents.requirements.tools import TOOL_SPECS

    turn = model.complete(SYSTEM_PROMPT, [{"role": "user", "text": "List the project's documents."}], TOOL_SPECS)
    assert turn.stop_reason in {"tool_use", "end_turn"}
