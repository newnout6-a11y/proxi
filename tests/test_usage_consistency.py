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


RETRY_USAGES = [
    {"input_tokens": 4, "cache_read": 341176, "cache_write": 1061, "output_tokens": 0},
    {"input_tokens": 4, "cache_read": 342237, "cache_write": None, "output_tokens": 0},
    {"input_tokens": 4, "cache_read": 342237, "cache_write": None, "output_tokens": 0},
    {"input_tokens": 4, "cache_read": 342237, "cache_write": 73, "output_tokens": 5456},
]
RETRY_COSTS = [1.471124, 1.36926, 1.36926, 2.07176]


def retry_events():
    events = []
    for usage, cost in zip(RETRY_USAGES, RETRY_COSTS):
        events.extend([{"type": "usage_start"}, {"type": "credits", "value": cost, "usage": usage}])
    return events


def assert_final_retry_usage(dialect, usage):
    cached = (usage["prompt_tokens_details"]["cached_tokens"] if dialect == "chat" else
              usage["input_tokens_details"]["cached_tokens"] if dialect == "responses" else
              usage["cache_read_input_tokens"])
    assert cached == 342237
    if dialect == "anthropic":
        assert usage["input_tokens"] == 4
        assert usage["cache_creation_input_tokens"] == 73
    else:
        assert input_tokens(dialect, usage) == 342314
        assert usage["total_tokens"] == 342314 + 5456
    assert usage["completion_tokens" if dialect == "chat" else "output_tokens"] == 5456


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("stream", [False, True])
def test_four_paid_attempts_report_one_context_and_all_credits(dialect, stream):
    writer = Writer() if stream else None
    adapter = adapters.for_dialect(dialect, parsed("system", "prompt"), writer)
    adapter.begin()
    for event in retry_events():
        adapter.feed(event)
    final = adapter.end("stop")
    assert_final_retry_usage(dialect, final["usage"])
    assert final["usage"]["credits_used"] == round(sum(RETRY_COSTS), 5)
    if stream:
        assert_final_retry_usage(dialect, stream_usages(dialect, writer.events)[-1])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_retry_does_not_inherit_missing_counters_or_previous_reasoning(dialect):
    adapter = adapters.for_dialect(dialect, parsed("", "new prompt"))
    adapter.feed({"type": "usage_start"})
    adapter.feed({"type": "thinking", "delta": "hidden reasoning " * 100})
    adapter.feed({"type": "credits", "value": 1, "usage": RETRY_USAGES[-1]})
    adapter.feed({"type": "usage_start"})
    adapter.feed({"type": "text", "delta": "done" * 8})
    adapter.feed({"type": "credits", "value": 2, "usage": {"input_tokens": 10, "output_tokens": 8}})
    usage = adapter.end("stop")["usage"]
    assert input_tokens(dialect, usage) == 10
    assert adapter.cache_read == adapter.cache_write == adapter.reasoning_tokens == 0
    assert usage["completion_tokens" if dialect == "chat" else "output_tokens"] == 8
    assert usage["credits_used"] == 3


@pytest.mark.parametrize("dialect", DIALECTS)
def test_final_attempt_without_telemetry_estimates_only_its_context_and_output(dialect):
    adapter = adapters.for_dialect(dialect, parsed("", "new prompt"))
    adapter.feed({"type": "usage_start"})
    adapter.feed({"type": "thinking", "delta": "old reasoning " * 100})
    adapter.feed({"type": "credits", "value": 1, "usage": RETRY_USAGES[-1]})
    adapter.feed({"type": "usage_start"})
    adapter.feed({"type": "text", "delta": "done" * 8})
    usage = adapter.end("stop")["usage"]
    assert input_tokens(dialect, usage) == len("\nnew prompt") // 4
    assert usage["completion_tokens" if dialect == "chat" else "output_tokens"] == 8
    assert adapter.cache_read == adapter.cache_write == 0
    assert usage["credits_used"] == 1


def test_real_direct_and_orchestrator_retries_keep_one_context_and_bill_every_attempt(tmp_path, monkeypatch):
    import asyncio
    import io

    import direct
    from test_native_context_reuse import FIRST, request, run, setup

    backend, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                               base="https://example.invalid", cache_keepwarm_seconds=0,
                               upstream_error_dir=str(tmp_path))
    backend.headers = lambda: {}
    paid, bodies = [], []
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: paid.append((key, cost)))

    def opener(req, **_kwargs):
        attempt = len(bodies)
        bodies.append(json.loads(req.data))
        usage = RETRY_USAGES[attempt]
        raw_usage = {"creditsUsed": RETRY_COSTS[attempt]}
        for source, dest in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                             ("cache_read", "promptTokensReadFromCache"),
                             ("cache_write", "promptTokensWrittenToCache")):
            if usage.get(source) is not None:
                raw_usage[dest] = usage[source]
        events = []
        if attempt == 3:
            events.append({"event": "content_block_delta", "type": "text", "partialText": "OK"})
        events.extend([{"event": "metadata", "usage": raw_usage}, {"event": "end_of_stream"}])
        return io.BytesIO(b"\n".join(json.dumps(event).encode() for event in events) + b"\n")

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    parsed_request = request(FIRST, system="CLIENT_SYSTEM " * 100)
    events = asyncio.run(run(runner, parsed_request))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert len(bodies) == len(paid) == len({key for key, _ in paid}) == 4
    assert sum(cost for _, cost in paid) == pytest.approx(sum(RETRY_COSTS))
    assert all("[RETRY]" not in json.dumps(body) for body in bodies[:3])
    assert "[RETRY]" in json.dumps(bodies[3])
    adapter = adapters.ChatAdapter(parsed_request)
    for event in events:
        adapter.feed(event)
    usage = adapter.end("stop")["usage"]
    assert_final_retry_usage("chat", usage)
    assert usage["credits_used"] == round(sum(RETRY_COSTS), 5)


def test_usage_boundary_does_not_prevent_account_failover():
    import queue
    from orchestrator import _FailoverOut

    events = queue.Queue()
    pending = _FailoverOut(events)
    pending.put({"type": "usage_start"})
    pending.put({"type": "credits", "value": 1})
    assert pending.started is False
    pending.put({"type": "text", "delta": "visible"})
    assert pending.started is True
