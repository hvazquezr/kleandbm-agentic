"""Reading images (vision transcription at upload), the view_image agent tool, and
reprocessing documents after settings change."""

import base64
import io
import json

import pytest
from PIL import Image

from app.adapters.embeddings import FakeEmbedder
from app.adapters.llm import AnthropicChatModel, ScriptedChatModel
from app.adapters.vision import MAX_LONG_EDGE, FakeImageDescriber, prepare_image


def png(width=200, height=120, color=(240, 240, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def proj(as_user, client):
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]

    class P:
        pass

    p = P()
    p.alice, p.client = alice, client
    p.base = f"/tenants/{tenant}/projects/{project}"

    def upload(name, data):
        return alice.post(f"{p.base}/documents", files={"file": (name, data, "application/octet-stream")}).json()["latest"]

    p.upload = upload
    return p


TRANSCRIPT = (
    "## Overview\nDashboard: Sales performance Q3 2026.\n"
    "## Charts and widgets\n- KPI tile **Net revenue** 937,602 (USD)\n"
    "## Filters and controls\n- Channel: All"
)


def test_prepare_image_downscales_and_validates():
    big = prepare_image(png(5000, 300))
    img = Image.open(io.BytesIO(base64.b64decode(big.data_b64)))
    assert max(img.size) <= MAX_LONG_EDGE and big.media_type == "image/png"
    with pytest.raises(Exception):
        prepare_image(b"\x89PNG not really")


def test_images_are_transcribed_at_upload_and_searchable(proj):
    proj.client.app.state.image_describer = FakeImageDescriber(TRANSCRIPT)
    v = proj.upload("Sales-Dashboard.png", png())
    assert v["status"] == "ready" and v["embedding_model"] == "fake-hash-v1"
    assert any("fake-vision" in n for n in v["notes"])  # says which model read it

    passages = proj.alice.get(f"{proj.base}/document-versions/{v['id']}/passages").json()
    assert passages[0]["location"]["image"] == "transcription"
    assert passages[0]["text"].startswith("[Machine transcription of an image")

    hits = proj.alice.post(f"{proj.base}/search", json={"query": "net revenue KPI tile dashboard"}).json()
    assert hits and hits[0]["filename"] == "Sales-Dashboard.png"


def test_images_are_stored_when_reading_is_off_or_fails(proj):
    proj.client.app.state.image_describer = None
    off = proj.upload("chart.png", png())
    assert off["status"] == "stored_only" and "not configured" in off["notes"][0]

    proj.client.app.state.image_describer = FakeImageDescriber()
    broken = proj.upload("broken.png", b"\x89PNG\r\n\x1a\nnot an image")
    assert broken["status"] == "stored_only" and "couldn't be read" in broken["notes"][0]
    # The original is always kept and downloadable.
    assert proj.alice.get(f"{proj.base}/document-versions/{broken['id']}/content").status_code == 200


def test_reprocess_after_turning_on_image_reading_and_changing_embeddings(proj):
    proj.client.app.state.image_describer = None
    image = proj.upload("table-capture.png", png())
    text = proj.upload("notes.txt", b"Returns are net of original discounts.")
    assert image["status"] == "stored_only" and text["embedding_model"] == "fake-hash-v1"

    class NewModel(FakeEmbedder):
        def __init__(self):
            super().__init__(1536)
            self.model = "fake-hash-v2"

    proj.client.app.state.image_describer = FakeImageDescriber(TRANSCRIPT)
    proj.client.app.state.embedder = NewModel()
    # Vectors from the old model aren't comparable, so the text doc drops out of search ...
    assert proj.alice.post(f"{proj.base}/search", json={"query": "returns discounts"}).json() == []

    for v in (image, text):
        redone = proj.alice.post(f"{proj.base}/document-versions/{v['id']}/reprocess").json()
        assert redone["status"] == "ready" and redone["embedding_model"] == "fake-hash-v2"
    # ... and comes back after reprocessing, alongside the newly read image.
    names = {h["filename"] for h in proj.alice.post(f"{proj.base}/search", json={"query": "returns discounts net revenue"}).json()}
    assert names == {"notes.txt", "table-capture.png"}


def test_agent_can_look_at_an_image(proj):
    proj.client.app.state.image_describer = None
    image = proj.upload("dashboard.png", png())
    text = proj.upload("notes.txt", b"plain text")
    conv = proj.alice.post(f"{proj.base}/conversations", json={}).json()
    model = ScriptedChatModel([
        {"tool_calls": [{"name": "view_image", "input": {"version_id": image["id"]}}]},
        {"tool_calls": [{"name": "view_image", "input": {"version_id": text["id"]}}]},
        {"text": "The dashboard shows one KPI."},
    ])
    proj.client.app.state.chat_model_factory = lambda *_: model
    resp = proj.alice.post(f"{proj.base}/conversations/{conv['id']}/messages", json={"text": "Look at the dashboard"})
    assert resp.status_code == 200

    tool_turns = [t for t in model.calls[2]["transcript"] if t["role"] == "tool"]
    first, second = tool_turns[0]["results"][0], tool_turns[1]["results"][0]
    assert first["image"]["media_type"] == "image/png" and not first["is_error"]
    assert "__image__" not in first["content"]  # the image travels as an image, not as text
    assert second["is_error"] and "isn't an image" in second["content"]


def test_anthropic_adapter_sends_tool_images_as_image_blocks():
    result = AnthropicChatModel._tool_result(
        {"id": "t1", "content": json.dumps({"document": "x"}), "is_error": False,
         "image": {"media_type": "image/png", "data": "AAAA"}}
    )
    assert result["content"][0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert result["content"][1]["type"] == "text"
    plain = AnthropicChatModel._tool_result({"id": "t2", "content": "ok", "is_error": False})
    assert plain["content"] == "ok"


def test_anthropic_message_building_handles_tool_results():
    # Offline guard for the request shape the live API receives (a static-method bug here
    # was only caught live).
    msgs = AnthropicChatModel._messages([
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [], "raw": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "tool", "results": [{"id": "t1", "content": "ok", "is_error": False,
                                      "image": {"media_type": "image/png", "data": "AAAA"}}]},
    ])
    assert msgs[2]["content"][0]["content"][0]["type"] == "image"
