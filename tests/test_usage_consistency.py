"""Estimated input usage agrees across payloads and streaming lifecycles."""
import json
from types import SimpleNamespace

import pytest

import adapters


DIALECTS = ["chat", "responses", "anthropic"]


class Writer:
    def __init__(self):
        self.events = []

    def data(self, payload):
        self.events.append(("data", payload))

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        pass


def parsed(system, prompt):
    return SimpleNamespace(model="claude-opus-5-5", system=system, prompt=prompt,
                           show_thinking=True, wants_usage=True, tools=[])


def input_tokens(dialect, usage):
    return usage["prompt_tokens" if dialect == "chat" else "input_tokens"]


def stream_usages(dialect, events):
    if dialect == "chat":
        return [payload["usage"] for kind, payload in events
                if kind == "data" and payload.get("usage")]
    if dialect == "responses":
        return [payload["response"]["usage"] for kind, payload in events
                if kind == "response.completed"]
    return [payload["message"]["usage"] if kind == "message_start" else payload["usage"]
            for kind, payload in events if kind in ("message_start", "message_delta")]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("system,prompt,expected", [
    ("Follow the rules", "Inspect the source code", 10),
    ("", "Inspect the source code", 6),
    ("", "", 0),
])
def test_input_estimate_counts_complete_prompt_once_in_every_payload(dialect, system, prompt, expected):
    request = parsed(system, prompt)
    writer = Writer()
    streaming = adapters.for_dialect(dialect, request, writer)
    streaming.begin()
    streaming.feed({"type": "text", "delta": "answer"})
    final = streaming.end("stop")

    nonstreaming = adapters.for_dialect(dialect, request)
    nonstreaming.feed({"type": "text", "delta": "answer"})
    payload = nonstreaming.payload("stop")

    assert final["usage"] == payload["usage"]
    assert final["usage"]["estimated"] is True
    assert input_tokens(dialect, final["usage"]) == expected
    assert [input_tokens(dialect, usage) for usage in stream_usages(dialect, writer.events)] == (
        [expected, expected] if dialect == "anthropic" else [expected])


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("override,expected", [
    ("Follow the rules\nInspect the source code", 10),
    ("Other complete prompt", 5),
    ("", 0),
])
def test_explicit_complete_prompt_does_not_add_system_again(dialect, override, expected):
    adapter = adapters.for_dialect(dialect, parsed("Follow the rules", "Inspect the source code"))
    assert input_tokens(dialect, adapter.end("stop", override)["usage"]) == expected


@pytest.mark.parametrize("dialect", DIALECTS)
def test_minimal_parsed_without_prompt_remains_supported(dialect):
    request = parsed("Follow the rules", "")
    del request.prompt
    adapter = adapters.for_dialect(dialect, request)
    assert input_tokens(dialect, adapter.end("stop")["usage"]) == 4


@pytest.mark.parametrize("dialect,path,payload", [
    ("chat", "/v1/chat/completions", {
        "model": "claude-opus-5-5", "messages": [
            {"role": "system", "content": "Follow the rules"},
            {"role": "user", "content": "Inspect the source code"},
        ], "stream_options": {"include_usage": True},
    }),
    ("responses", "/v1/responses", {
        "model": "gpt-5.2", "instructions": "Follow the rules", "input": "Inspect the source code",
    }),
    ("anthropic", "/v1/messages", {
        "model": "claude-opus-5-5", "system": "Follow the rules", "max_tokens": 64,
        "messages": [{"role": "user", "content": "Inspect the source code"}],
    }),
])
def test_http_stream_and_nonstream_report_same_normalized_input(relay, dialect, path, payload):
    status, final = relay.json(path, payload)
    assert status == 200
    request = relay.fake.seen[-1]
    expected = len(request.system + "\n" + request.prompt) // 4
    assert input_tokens(dialect, final["usage"]) == expected

    status, body = relay.sse(path, dict(payload, stream=True))
    assert status == 200
    events = []
    event = "data"
    for line in body.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and line != "data: [DONE]":
            events.append((event, json.loads(line[6:])))
        elif not line:
            event = "data"
    estimates = stream_usages(dialect, events)
    assert len(estimates) == (2 if dialect == "anthropic" else 1)
    assert all(input_tokens(dialect, usage) == expected for usage in estimates)
