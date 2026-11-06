"""The reasoning text of a route that signs nothing reaches the client while the model is still reasoning (item 5, a3 F5).

direct._stream_body held every thinking event of an attempt until the answer began, so that a retried empty attempt could not
leak thinking or a signature. For GLM that meant a3's 9.1 s of silence and then the whole reasoning in one burst, upstream
having sent it continuously from 1.5 s to 10.6 s. Claude's thinking is signed and stays held; the other routes show their
reasoning at once, and a signature is held on every route.
"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import direct
from test_review_fixes_pr19 import END, THINKING, Response, signature, text
from upstream_attempts import AttemptBudget

GLM, CLAUDE, GPT = "zai-org/GLM-5.3", "claude-opus-5-5", "gpt-6.1-sol"
_OPEN = threading.Event()  # a gate that is always open
_OPEN.set()


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)


def reasoning(piece):
    return {"event": "content_block_delta", "type": "thinking", "partialThinking": piece}


class Gated(Response):
    """An answer in two parts: the lines of `first`, then, once the test opens the gate, the lines of `rest`."""

    def __init__(self, first, rest, gate):
        super().__init__(b"")
        self.first, self.rest, self.gate = first, rest, gate

    def __iter__(self):
        for event in self.first:
            yield (json.dumps(event) + "\n").encode()
        if self.gate.wait(10):
            for event in self.rest:
                yield (json.dumps(event) + "\n").encode()


def stream(tmp_path, monkeypatch, model, attempts):
    """The agen of direct._stream_body on `model`, one scripted answer (a callable) per POST."""
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag")}
    posts = []

    def opener(req, **_kw):
        posts.append(1)
        return attempts[min(len(posts), len(attempts)) - 1]()

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)
    return direct._stream_body(cfg, session, {"model": model})


async def pull(agen, seconds=3):
    return await asyncio.wait_for(agen.__anext__(), seconds)


async def rest_of(agen):
    return [event async for event in agen]


def kinds(events):
    return [event["type"] for event in events]


def deltas(events):
    return [event["delta"] for event in events if event["type"] == "thinking"]


FIRST = [*THINKING, reasoning("step one "), reasoning("step two ")]
REST = [{"event": "content_block_end", "type": "thinking"}, text("answer"), *END]


def test_a_route_without_signatures_shows_its_reasoning_before_the_answer_begins(tmp_path, monkeypatch):
    async def scenario():
        gate = threading.Event()
        agen = stream(tmp_path, monkeypatch, GLM, [lambda: Gated(FIRST, REST, gate)])
        seen = []
        try:
            while deltas(seen) != ["step one ", "step two "]:
                seen.append(await pull(agen))  # the gate is shut: the answer has not been sent
            assert not gate.is_set() and "text" not in kinds(seen)
            gate.set()
            return seen, await rest_of(agen)
        finally:
            gate.set()
            await agen.aclose()

    before, after = asyncio.run(scenario())
    assert kinds(before)[:2] == ["usage_start", "thinking_status"] and "thinking_start" in kinds(before)
    assert "text" in kinds(after) and kinds(after)[-1] == "end"


@pytest.mark.parametrize("model", [CLAUDE, "model-B"])
def test_signed_and_unknown_routes_still_hold_it_until_the_answer_begins(tmp_path, monkeypatch, model):
    async def scenario():
        gate = threading.Event()
        agen = stream(tmp_path, monkeypatch, model, [lambda: Gated(FIRST, REST, gate)])
        seen = [await pull(agen), await pull(agen)]
        try:
            following = asyncio.ensure_future(agen.__anext__())
            done, _ = await asyncio.wait({following}, timeout=0.5)
            assert not done  # only the status of the display is live
            gate.set()
            return seen, [await following] + await rest_of(agen)
        finally:
            gate.set()
            await agen.aclose()

    before, after = asyncio.run(scenario())
    assert kinds(before) == ["usage_start", "thinking_status"]
    # then the held events come out in their order, ahead of the text that released them
    assert deltas(after) == ["step one ", "step two "]
    assert kinds(after).index("thinking") < kinds(after).index("text")


@pytest.mark.parametrize("model,expected", [(GLM, ["first pass", "second pass"]), (CLAUDE, ["second pass"])])
def test_an_empty_attempt_costs_the_client_a_second_pass_of_unsigned_reasoning_and_signed_reasoning_never_repeats(
        tmp_path, monkeypatch, model, expected):
    empty = [*THINKING, reasoning("first pass"), {"event": "content_block_end", "type": "thinking"}, *END]
    answered = [*THINKING, reasoning("second pass"), {"event": "content_block_end", "type": "thinking"}, text("ok"), *END]
    agen = stream(tmp_path, monkeypatch, model, [lambda: Gated(empty, [], _OPEN), lambda: Gated(answered, [], _OPEN)])
    events = asyncio.run(rest_of(agen))
    assert deltas(events) == expected and {"type": "text", "delta": "ok"} in events


def test_a_signature_is_held_on_every_route_and_never_reaches_the_client_from_a_discarded_attempt(tmp_path, monkeypatch):
    empty = [*THINKING, reasoning("pass"), signature("sig-of-the-empty-attempt"), *END]
    answered = [text("ok"), *END]
    for model in (GLM, GPT, CLAUDE):
        agen = stream(tmp_path, monkeypatch, model, [lambda: Gated(empty, [], _OPEN), lambda: Gated(answered, [], _OPEN)])
        events = asyncio.run(rest_of(agen))
        assert "thinking_signature" not in kinds(events), model
        assert {"type": "text", "delta": "ok"} in events


@pytest.mark.parametrize("model,live", [
    (GLM, True), ("zai-org/GLM-5.3-Flash", True), (GPT, True), ("gemini-2.5-flash", True),
    (CLAUDE, False), ("claude-sonnet-5-5", False), ("model-B", False), (None, False)])
def test_which_routes_show_their_reasoning_live(model, live):
    assert direct._live_thinking(model) is live

