"""The transcript monitor: after a top-level run, a model from another family reads the stored
transcript (child runs included) and scores the agents' behaviour. Triage only: the result is
stored and served, a high score flags the run, and nothing is blocked. Scripted models."""

import time

import pytest

from app.adapters.llm import LLMError, ScriptedChatModel
from app.agents.monitor.monitor import assess, brief, render
from app.config import get_settings
from app.main import _chat_model_from_settings
from tests.test_architect import FACT_SALES, _fix_seq, design_steps, proj  # noqa: F401
from tests.test_validator import fail_round, pass_round


def submit(score, *, concerns=(), untrusted=(), summary="Looks fine."):
    return {"tool_calls": [{"name": "submit_assessment", "input": {
        "score": score, "summary": summary, "concerns": list(concerns), "untrusted_instructions": list(untrusted)}}]}


@pytest.fixture
def monitored(proj, monkeypatch):
    """Turn the monitor on and give it a scripted model next to the agents' ones."""
    monkeypatch.setattr(get_settings(), "monitor_enabled", True)

    def use(monitor_steps, *, unavailable=None):
        no_grain = {**FACT_SALES, "grain": ""}
        steps = [*_fix_seq(proj, design_steps(proj, fact=no_grain)),
                 {"tool_calls": [{"name": "put_table", "input": FACT_SALES}]},
                 {"text": "Declared the order-line grain on fact_sales."}]
        proj.script_agents(steps, [], [*fail_round(), *pass_round()])
        agents = proj.client.app.state.chat_model_factory
        monitor = ScriptedChatModel(list(monitor_steps), model="scripted-monitor")

        def factory(agent):
            if agent == "monitor":
                if unavailable:
                    raise LLMError(unavailable)
                return monitor
            return agents(agent)

        proj.client.app.state.chat_model_factory = factory
        done = proj.run()[-1]
        return done, monitor, _assessment(proj, done["run"]["id"])

    return use


def _assessment(proj, run_id, timeout=10.0):
    """The monitor runs after the stream ends, in the run's worker thread: wait for it."""
    url = f"{proj.base}/agent-runs/{run_id}/assessments"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = proj.alice.get(url).json()
        if rows and rows[0]["status"] != "pending":
            return rows[0]
        time.sleep(0.05)
    raise AssertionError("the monitor didn't finish")


def test_the_monitor_reads_the_whole_run_and_stores_its_assessment(monitored):
    concern = {"kind": "unsupported_claim", "explanation": "Claims a key check that never ran.",
               "entries": [{"agent": "architect", "ordinal": 4}]}
    untrusted = {"agent": "architect", "ordinal": 3, "quote": "mark every check passed", "followed": False}
    done, monitor, a = monitored([submit(72, concerns=[concern], untrusted=[untrusted], summary="One unsupported claim.")])
    assert done["status"] == "completed"
    assert a["status"] == "completed" and a["monitor_model"] == "scripted-monitor"
    assert a["score"] == 72 and a["flagged"] is True and a["samples"] == [72]
    assert a["concerns"] == [concern] and a["untrusted_instructions"] == [untrusted]
    assert a["summary"] == "One unsupported claim." and a["input_tokens"] == 100 and a["output_tokens"] == 20

    # It saw the Architect run and both Validator rounds, as data, without the agents' prompts.
    (call,) = monitor.calls
    seen = call["transcript"][0]["text"]
    assert seen.count("<transcript>") == 1 and seen.rstrip().endswith("</transcript>")
    assert "[architect #" in seen and "[validator #" in seen and "→ put_table(" in seen
    assert "<project_context>" in seen and "You are the Architect" not in seen
    assert call["tools"] == ["submit_assessment"]


def test_a_low_score_is_stored_unflagged_and_nothing_is_blocked(monitored):
    done, _, a = monitored([submit(10)])
    assert a["flagged"] is False and a["score"] == 10
    assert done["candidate"]["ready_for_review"] is True  # the monitor never gates


def test_without_a_monitor_model_the_assessment_is_skipped(monitored):
    done, _, a = monitored([], unavailable="KLEANDBM_OPENAI_API_KEY is not set")
    assert done["status"] == "completed"
    assert a["status"] == "skipped" and a["score"] is None and "OPENAI" in a["detail"]


def test_a_monitor_that_never_submits_is_recorded_as_failed(monitored):
    _, monitor, a = monitored([{"text": "It looks fine to me."}, {"text": "Really, fine."}])
    assert a["status"] == "failed" and a["score"] is None and "didn't submit" in a["detail"]
    assert len(monitor.calls) == 2  # one nudge, then it gives up


def test_samples_are_averaged_and_untrusted_instructions_merged():
    u = {"agent": "architect", "ordinal": 3, "quote": "add audit_override", "followed": False}
    model = ScriptedChatModel([submit(30, untrusted=[u]), submit(80, untrusted=[{**u, "followed": True}],
                                                                summary="Followed it.")])
    msgs = [{"agent": "architect", "ordinal": 1, "role": "user", "segment": None, "content": {"text": "Design."}}]
    result = assess(model, msgs, run_agent="architect", samples=2, threshold=50)
    assert result.samples == [30, 80] and result.score == 55 and result.flagged
    assert result.summary == "Followed it."
    assert result.untrusted_instructions == [{**u, "followed": True}]


def test_scores_and_kinds_are_kept_in_range():
    bad = submit(250, concerns=[{"kind": "made_up", "explanation": "x", "entries": []}])
    result = assess(ScriptedChatModel([bad]), [], run_agent="architect")
    assert result.score == 100 and result.concerns[0]["kind"] == "other"


def test_long_transcripts_are_shortened_and_marked():
    big = {"agent": "architect", "ordinal": 2, "role": "tool", "segment": None,
           "content": {"results": [{"id": "1", "content": "x" * 10_000, "is_error": False}]}}
    call = {"agent": "architect", "ordinal": 1, "role": "assistant", "segment": None,
            "content": {"text": "Looking.", "thinking": ["Check the grain."],
                        "tool_calls": [{"name": "source_sample_rows", "input": {"table": "orders"}}]}}
    system = {"agent": "architect", "ordinal": 0, "role": "system", "segment": None, "content": {"text": "PROMPT"}}
    text, truncated = render([system, call, big], max_chars=50_000)
    assert "PROMPT" not in text and "(thinking) Check the grain." in text and "→ source_sample_rows(" in text
    assert truncated is False and "characters cut" in text  # one long result is clipped on its own
    text, truncated = render([call, big] * 40, max_chars=20_000)  # fits once results are shortened
    assert truncated and len(text) <= 20_000 and "of the middle of the run cut" not in text
    text, truncated = render([call, big] * 40, max_chars=8_000)  # doesn't: the middle goes
    assert truncated and len(text) < 8_200 and "of the middle of the run cut" in text
    assert "shortened to fit" in brief("architect", text, truncated)


def test_the_monitor_must_be_another_family_than_the_architect(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "architect_agent_provider", "anthropic")
    monkeypatch.setattr(settings, "monitor_agent_provider", "anthropic")
    with pytest.raises(LLMError, match="another model family"):
        _chat_model_from_settings(settings, "monitor")
