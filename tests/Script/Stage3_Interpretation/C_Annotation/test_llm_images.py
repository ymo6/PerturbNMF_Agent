"""Images in the LLM layer: claude_api content blocks, worst-case cost, session requests. No network."""

import base64
import hashlib
import json
from types import SimpleNamespace

import LLM_interface
from budget import claude_worst_case
from LLM_interface import claude_api, image_token_estimate
from pydantic import BaseModel
from session_llm import session_llm


class Answer(BaseModel):
    x: int


class FakeMessages:
    def __init__(self):
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=Answer(x=1), usage=None
        )


def api():
    h = claude_api(api_key="test-key", model="claude-sonnet-5")
    h.client = SimpleNamespace(messages=FakeMessages())
    return h


def test_no_images_sends_plain_string():
    h = api()
    assert h.get_json("sys", "hello", Answer).x == 1
    assert h.client.messages.calls[0]["messages"] == [
        {"role": "user", "content": "hello"}
    ]


def test_images_sent_before_text_in_order():
    h = api()
    h.get_json(
        "sys", "hello", Answer, images=[(b"one", "image/png"), (b"two", "image/jpeg")]
    )
    content = h.client.messages.calls[0]["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert base64.b64decode(content[0]["source"]["data"]) == b"one"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[2] == {"type": "text", "text": "hello"}


def test_worst_case_counts_image_tokens(monkeypatch):
    seen = []
    monkeypatch.setattr(
        LLM_interface, "claude_worst_case", lambda *a: seen.append(a) or 0.0
    )
    h = api()
    h.budget = SimpleNamespace(allows=lambda est, res: True)
    h.get_json("s", "p", Answer, images=[(b"x", "image/png")], images_px=[(750, 100)])
    h.get_json("s", "p", Answer, images=[(b"x", "image/png")])
    h.get_json("s", "p", Answer)
    assert seen[0][3] == 100
    assert seen[1][3] == 1568 * 1568 / 750
    assert seen[2][3] == 0
    assert claude_worst_case("claude-sonnet-5", 300, 0, 1000) > claude_worst_case(
        "claude-sonnet-5", 300, 0
    )
    assert image_token_estimate(None) == 0


def test_session_name_unchanged_without_images(tmp_path):
    h = session_llm(tmp_path / "Session_LLM", "step")
    h.key = "P1"
    assert h.get_json("sys", "prompt", Answer) is None
    old = f"P1__{hashlib.sha1(b'sysprompt').hexdigest()[:8]}"
    req = json.loads(
        (tmp_path / "Session_LLM" / "step" / f"{old}.request.json").read_text()
    )
    assert set(req) == {"key", "system", "prompt", "schema", "answer_file"}


def test_session_images_change_name_and_use_relative_paths(tmp_path):
    run = tmp_path / "run"
    crop = run / "Synthesis" / "Figures" / "P1" / "crops" / "fig_0123456789abcdef.png"
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b"a")
    h = session_llm(run / "Session_LLM", "step")
    h.key = "P1"
    h.get_json("sys", "prompt", Answer, images=[(b"a", "image/png", crop)])
    h.get_json("sys", "prompt", Answer, images=[(b"b", "image/png", crop)])
    reqs = sorted((run / "Session_LLM" / "step").glob("*.request.json"))
    assert len(reqs) == 2
    old = f"P1__{hashlib.sha1(b'sysprompt').hexdigest()[:8]}"
    assert old not in {r.name.split(".")[0] for r in reqs}
    req = json.loads(reqs[0].read_text())
    img = req["images"][0]
    assert img["path"] == "Synthesis/Figures/P1/crops/fig_0123456789abcdef.png"
    assert img["sha256"] in (
        hashlib.sha256(b"a").hexdigest(),
        hashlib.sha256(b"b").hexdigest(),
    )
    assert "open each image" in req["instructions"].lower()
    assert str(tmp_path) not in json.dumps(req["images"])
    # an image outside the run folder is listed by file name only
    other = tmp_path / "elsewhere.png"
    other.write_bytes(b"c")
    h.get_json("sys", "prompt", Answer, images=[(b"c", "image/png", other)])
    names = [
        json.loads(r.read_text())["images"][0]["path"]
        for r in (run / "Session_LLM" / "step").glob("*.request.json")
    ]
    assert "elsewhere.png" in names
