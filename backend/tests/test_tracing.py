"""LangSmith tracing: off unless configured, one trace per agent run with its child runs, model
calls and tool calls nested in it, token usage, the scope as metadata, and no content leaving the
process in metadata mode. A real langsmith Client posts to a fake HTTP session, so what's checked
is exactly what would have been sent."""

import json

import pytest
import requests
from langsmith import Client

from app.adapters import tracing
from app.config import get_settings
from tests.test_architect import FACT_SALES, _fix_seq, design_steps, proj  # noqa: F401
from tests.test_validator import fail_round, pass_round


class FakeSession(requests.Session):
    """Accepts every LangSmith API call and keeps the bodies."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        body = kw.get("data") or kw.get("json") or b"{}"
        self.sent.append((method, url, json.loads(body) if isinstance(body, (bytes, str)) else body))
        r = requests.Response()
        r.status_code, r._content, r.url = 200, b"{}", url
        r.headers["Content-Type"] = "application/json"
        return r

    def runs(self) -> dict[str, dict]:
        """Run id -> the run as created and then updated."""
        out: dict[str, dict] = {}
        for method, url, body in self.sent:
            if "/runs" in url and method in ("POST", "PATCH") and "id" in body:
                out.setdefault(body["id"], {}).update(body)
        return out


@pytest.fixture
def traced(monkeypatch):
    """Turn tracing on with a client that posts to a fake session. Returns the session."""
    def use(content: str):
        session = FakeSession()
        settings = get_settings()
        monkeypatch.setattr(settings, "langsmith_tracing", True)
        monkeypatch.setattr(settings, "langsmith_api_key", "test-key")
        monkeypatch.setattr(settings, "langsmith_content", content)
        monkeypatch.setattr(settings, "langsmith_metadata", {"experiment": "unit"})
        hide = tracing._full if content == "full" else tracing._metadata_only
        client = Client(api_url="http://langsmith.test", api_key="test-key", session=session,
                        auto_batch_tracing=False, omit_traced_runtime_info=True, hide_inputs=hide, hide_outputs=hide)
        monkeypatch.setattr(tracing, "_client", lambda *a: client)
        return session
    return use


def _architect_run_with_two_reviews(proj):
    no_grain = {**FACT_SALES, "grain": ""}
    steps = [*_fix_seq(proj, design_steps(proj, fact=no_grain)),
             {"tool_calls": [{"name": "put_table", "input": FACT_SALES}]},
             {"text": "Declared the order-line grain on fact_sales."}]
    proj.script_agents(steps, [], [*fail_round(), *pass_round()])
    return proj.run()[-1]


def test_an_agent_run_is_one_trace_with_its_children_and_no_content(proj, traced):
    session = traced("metadata")
    done = _architect_run_with_two_reviews(proj)
    runs = session.runs()
    root = runs[done["run"]["id"]]  # the trace id is the agent run id
    assert root["name"] == "architect run" and root["parent_run_id"] is None
    meta = root["extra"]["metadata"]
    assert meta["tenant_id"] and meta["project_id"] and meta["agent"] == "architect" and meta["experiment"] == "unit"
    assert all(r["trace_id"] == root["id"] for r in runs.values())

    validators = [r for r in runs.values() if r["name"] == "validator round"]
    names = {i: r["name"] for i, r in runs.items()}
    # LangGraph traces its nodes too: each Validator round sits under the Architect's review node.
    assert len(validators) == 2 and all(names[r["parent_run_id"]] == "review" for r in validators)
    llm = [r for r in runs.values() if r["run_type"] == "llm"]
    assert {r["name"] for r in llm} == {"architect model", "validator model"}
    usage = next(r for r in llm if r.get("outputs"))["outputs"]["usage_metadata"]
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 20  # the scripted model's usage
    tools = [r for r in runs.values() if r["run_type"] == "tool"]
    assert "put_table" in {r["name"] for r in tools} and "record_finding" in {r["name"] for r in tools}

    # Metadata mode: no prompt, transcript, tool argument or result left the process.
    sent = json.dumps([body for _, _, body in session.sent])
    for content in ("fact_sales", "order line", "<project_context>", "Declared the order-line grain", "system"):
        assert content not in sent.replace('"run_type": "system"', ""), content
    put = next(r for r in tools if r["name"] == "put_table")
    assert put["inputs"] == {"tool": "put_table", "content": "hidden"}
    assert next(r for r in llm if r.get("outputs"))["outputs"]["content"] == "hidden"


def test_full_mode_sends_content_with_credentials_and_personal_data_masked(proj, traced):
    session = traced("full")
    _architect_run_with_two_reviews(proj)
    sent = json.dumps([body for _, _, body in session.sent])
    assert "fact_sales" in sent and "<project_context>" in sent
    assert tracing._full({"q": "password=hunter2 for ana@example.com", "n": [1, "x"]}) == \
        {"q": "password=[redacted credential] for [masked email]", "n": [1, "x"]}


def test_tracing_stays_off_without_settings_even_with_langsmith_env(proj, monkeypatch):
    """A global LANGSMITH_TRACING variable must not start sending traces."""
    sent: list = []
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "should-not-be-used")
    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **k: sent.append(a) or pytest.fail("sent"))
    done = _architect_run_with_two_reviews(proj)
    assert done["status"] == "completed" and not sent


def test_tracing_on_without_a_key_is_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "")
    assert tracing.settings_enabled(settings) is False


def test_metadata_mode_keeps_numbers_and_names_only():
    assert tracing._metadata_only({"usage_metadata": {"input_tokens": 3}, "tool_names": ["a"], "text": "secret"}) == \
        {"usage_metadata": {"input_tokens": 3}, "tool_names": ["a"], "content": "hidden"}
    assert tracing._metadata_only({"iterations": 2}) == {"iterations": 2}
    assert tracing._metadata_only("raw") == {"content": "hidden"}
