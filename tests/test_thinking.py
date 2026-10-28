import adapters


class Writer:
    def __init__(self):
        self.events = []

    def data(self, payload):
        self.events.append(("data", payload))

    def event(self, name, payload):
        self.events.append((name, payload))

    def done(self):
        self.events.append(("done", None))


def parsed(model="claude-opus-5-5", show=True):
    p = type("P", (), {})()
    p.model, p.system, p.wants_usage, p.show_thinking, p.effort = model, "", False, show, None
    return p


def test_chat_streams_reasoning_content():
    writer = Writer()
    adapter = adapters.for_dialect("chat", parsed(), writer)
    adapter.feed({"type": "thinking", "delta": "думаю"})
    adapter.feed({"type": "text", "delta": "ответ"})
    chunks = [p for kind, p in writer.events if kind == "data"]
    assert chunks[0]["choices"][0]["delta"] == {"reasoning_content": "думаю"}
    assert chunks[1]["choices"][0]["delta"] == {"content": "ответ"}


def test_chat_non_stream_includes_reasoning():
    adapter = adapters.for_dialect("chat", parsed())
    adapter.feed({"type": "thinking", "delta": "думаю"})
    adapter.feed({"type": "text", "delta": "ответ"})
    payload = adapter.payload("stop", "")
    assert payload["choices"][0]["message"]["reasoning_content"] == "думаю"
    assert payload["choices"][0]["message"]["content"] == "ответ"


def test_anthropic_opens_thinking_block_first():
    writer = Writer()
    adapter = adapters.for_dialect("anthropic", parsed(), writer)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "хм"})
    adapter.feed({"type": "text", "delta": "да"})
    kinds = [k for k, _ in writer.events]
    assert "content_block_start" in kinds
    starts = [p["content_block"]["type"] for k, p in writer.events if k == "content_block_start"]
    assert starts == ["thinking", "text"]
    deltas = [p["delta"] for k, p in writer.events if k == "content_block_delta"]
    assert deltas[0] == {"type": "thinking_delta", "thinking": "хм"}
    assert deltas[1] == {"type": "text_delta", "text": "да"}


def test_thinking_hidden_when_disabled():
    writer = Writer()
    adapter = adapters.for_dialect("chat", parsed(show=False), writer)
    adapter.feed({"type": "thinking", "delta": "думаю"})
    adapter.feed({"type": "text", "delta": "ответ"})
    assert all("reasoning_content" not in p["choices"][0]["delta"] for k, p in writer.events if k == "data")


def test_responses_emits_reasoning_events():
    writer = Writer()
    adapter = adapters.for_dialect("responses", parsed("gpt-6-sol"), writer)
    adapter.begin()
    adapter.feed({"type": "thinking", "delta": "хм"})
    assert any(k == "response.reasoning_summary_text.delta" for k, _ in writer.events)
