"""A keep-alive ping reads the cached prefix and asks for one word: it must not make the model write its reply again.

Retool's Claude route ignores maxCompletionTokens, so a ping that sent the last body again paid for the whole reply
again (467 credits against 194 expected over 1,041 pings of one load, 2026-11-06). The ping now adds one user message."""
import copy
import json
from types import SimpleNamespace

import pytest

import config
import direct
import native_history

TOOLS = [{"name": "read_file", "description": "", "params": {"path": {"type": "string", "required": True}},
          "strict": False}]
PROVIDER = {"model": "claude-opus-5-5", "reasoningEffort": "max"}


def sent_by_ping(monkeypatch, body):
    captured = []

    class Response:
        def __iter__(self):
            return iter([b'{"event":"metadata","usage":{"creditsUsed":0.05,"promptTokensReadFromCache":9000}}\n',
                         b'{"event":"end_of_stream"}\n'])

        def close(self):
            pass

    monkeypatch.setattr(direct.urllib.request, "urlopen",
                        lambda request, **_: captured.append(json.loads(request.data)) or Response())
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"base": "https://tenant.invalid"}, headers=lambda: {}),
                              thread_id="t", _cancelled=False)
    assert direct._ping(session, body) == 0.05
    return captured[0]


def history_body(tools=TOOLS):
    chunks = ["User: study the project and write a report", "Assistant: I read it", "User: now fix the loop"]
    return direct.build_body(dict(config.DEFAULTS), PROVIDER, "SYSTEM HEAD", "", tools, "auto", 32000, chunks)


def native_body():
    """What iter_events sends when the history continues natively: the last message is a tool result."""
    body = history_body()
    body["messages"] = native_history.cache_messages([
        body["messages"][0],
        {"role": "user", "content": [{"type": "text", "text": "fix the loop"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "opaque"},
                                          {"type": "tool_call", "toolCallId": "c1", "toolName": "read_file",
                                           "toolInput": {"path": "a.py"}}]},
        {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "for i in range(10): pass"}}], True, TOOLS)
    body["rit"] = direct.make_rit(body["messages"], body["tools"])
    return body


def flagged(messages):
    return [(index, position) for index, message in enumerate(messages) if isinstance(message["content"], list)
            for position, block in enumerate(message["content"]) if isinstance(block, dict) and block.get("cache")]


@pytest.mark.parametrize("make", [history_body, native_body, lambda: history_body(None)])
def test_the_ping_is_the_last_body_plus_one_user_message_that_asks_for_one_word(monkeypatch, make):
    body = make()
    before = copy.deepcopy(body)
    ping = sent_by_ping(monkeypatch, body)
    assert body == before  # the session's warm body is not touched: the next ping and the next turn start from it
    assert ping["messages"][:-1] == body["messages"]
    assert ping["messages"][-1] == {"role": "user", "content": [{"type": "text", "text": direct.KEEPWARM_NUDGE}]}
    assert "single word" in direct.KEEPWARM_NUDGE and "tool" in direct.KEEPWARM_NUDGE
    # what the model thinks before its OK is paid output: the nudge says it is a check to answer without thinking it over
    assert "without thinking" in direct.KEEPWARM_NUDGE and "Keep-alive" in direct.KEEPWARM_NUDGE
    assert {key: value for key, value in ping.items() if key not in ("messages", "rit", "maxCompletionTokens")} == {
        key: value for key, value in body.items() if key not in ("messages", "rit", "maxCompletionTokens")}
    assert ping["maxCompletionTokens"] == direct.MIN_OUTPUT_TOKENS


@pytest.mark.parametrize("make", [history_body, native_body])
def test_the_ping_reads_the_same_prefix_and_adds_no_cache_entry(monkeypatch, make):
    body = make()
    ping = sent_by_ping(monkeypatch, body)
    assert flagged(ping["messages"]) == flagged(body["messages"]) != []  # the same breakpoints, none on the new block
    assert "cache" not in ping["messages"][-1]["content"][0]
    assert ping["openAICacheKey"] == body["openAICacheKey"] and ping["reasoningEffort"] == "max"
    # never a lower effort and never another model: a ping is the turn's own request with one more message
    assert (ping["model"], ping["reasoningEffort"]) == (body["model"], body["reasoningEffort"])
    # the breakpoints stay within what the provider allows
    assert len(flagged(ping["messages"])) <= (3 if body.get("tools") else 4)


@pytest.mark.parametrize("make", [history_body, native_body, lambda: history_body(None)])
def test_the_integrity_hint_describes_the_messages_that_are_sent(monkeypatch, make):
    body = make()
    ping = sent_by_ping(monkeypatch, body)
    assert ping["rit"] == direct.make_rit(ping["messages"], ping.get("tools")) != body["rit"]


def test_after_a_tool_result_the_nudge_is_a_user_message_of_its_own(monkeypatch):
    ping = sent_by_ping(monkeypatch, native_body())
    roles = [message["role"] for message in ping["messages"]]
    assert roles[-3:] == ["assistant", "tool", "user"]
    assert ping["messages"][-2]["content"]["toolResult"] == "for i in range(10): pass"


def test_a_body_with_a_single_string_prompt_gets_the_message_after_it(monkeypatch):
    body = direct.build_body(dict(config.DEFAULTS), PROVIDER, "HEAD", "one prompt", None, "auto", None)
    ping = sent_by_ping(monkeypatch, body)
    assert [message["role"] for message in ping["messages"]] == ["system", "user", "user"]
    assert ping["messages"][1]["content"] == "one prompt"


def test_a_body_without_messages_still_pings(monkeypatch):
    ping = sent_by_ping(monkeypatch, {"model": "claude-opus-5-5"})
    assert [message["role"] for message in ping["messages"]] == ["user"]
    assert ping["rit"] == direct.make_rit(ping["messages"], None)
