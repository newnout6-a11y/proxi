"""count_tokens answers from the conversation's last real usage, and from a better ratio when it has none.

The relay answered len(text) // 4. Four saved Claude Desktop bodies of 779k to 829k characters were counted by upstream
at 290k to 311k tokens, so the estimate was 0.67 of the truth (a5, 2026-11-06): the context meter of Claude Desktop showed
256.5k when 487k were real, and nothing compacted on its own up to 492k real tokens."""
import copy
import json

import pytest

import adapters
from prompt_support import READ_FILE, client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture

# What a saved Desktop request measured: characters and the tokens upstream counted for them
DESKTOP = [(779_056, 290_240), (791_816, 294_945), (811_632, 303_766), (829_044, 310_975)]


def test_the_ratio_is_what_the_saved_desktop_requests_measured():
    for chars, real in DESKTOP:
        estimate, basis = adapters.count_input_tokens(chars)
        assert basis == "estimate" and abs(estimate / real - 1) < 0.02, (chars, real, estimate)
        assert chars // 4 / real < 0.7  # what the divisor 4 gave


def test_a_request_that_grew_is_the_last_real_count_plus_an_estimate_of_what_was_added():
    chars, real = DESKTOP[0]
    tokens, basis = adapters.count_input_tokens(chars + 2700, (chars, real))
    assert basis == "last-usage" and tokens == real + 1000
    assert adapters.count_input_tokens(chars, (chars, real)) == (real, "last-usage")


def test_a_request_that_shrank_is_scaled_by_the_same_ratio():
    chars, real = DESKTOP[0]
    tokens, basis = adapters.count_input_tokens(chars // 2, (chars, real))
    assert basis == "last-usage" and tokens == round(real / 2)
    assert adapters.count_input_tokens(0, (chars, real)) == (0, "last-usage")


@pytest.mark.parametrize("anchor", [None, (0, 5), (5, 0), (0, 0), (-1, 100)])
def test_an_anchor_that_says_nothing_is_not_used(anchor):
    assert adapters.count_input_tokens(2700, anchor) == (1000, "estimate")


def test_the_size_of_a_request_is_what_the_old_count_measured():
    parsed = type("P", (), {"system": "SYS", "prompt": "User: hi", "tools": []})()
    assert adapters.request_chars(parsed) == len("SYS\nUser: hi")
    parsed.tools = [{"type": "function", "function": {"name": "t"}}]
    text = "SYS\nUser: hi\n" + json.dumps(parsed.tools, ensure_ascii=False, separators=(",", ":"))
    assert adapters.request_chars(parsed) == len(text)
    assert adapters.request_chars(type("P", (), {})()) == 1


# --- through the relay ---------------------------------------------------------------------------------------------

def count(relay, body, headers=None):
    body = {"model": "claude-opus-5-5", "max_tokens": 64, **body}
    return relay.call("/v1/messages/count_tokens", body, {"x-api-key": "unused", **(headers or {})})


def usage(read, write, fresh, out=30):
    return {"event": "metadata", "usage": {"inputTokens": fresh, "promptTokensReadFromCache": read,
                                           "promptTokensWrittenToCache": write, "outputTokens": out,
                                           "creditsUsed": 0.1}}


TOOLS = [{"name": READ_FILE["name"], "description": READ_FILE["description"], "input_schema": READ_FILE["parameters"]}]
BIG = "the quick brown fox jumps over the lazy dog " * 400
SESSION = {"x-claude-code-session-id": "11111111-2222-3333-4444-555555555555"}


def messages(*turns):
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": text} for index, text in enumerate(turns)]


def test_without_a_conversation_the_answer_is_the_new_estimate_not_the_old_one(prompt_relay):
    relay = prompt_relay()
    body = {"system": "SYS", "messages": messages(BIG), "tools": TOOLS}
    status, answer, headers = count(relay, body)
    assert status == 200 and headers["x-proxi-token-count"] == "estimated"
    assert headers["x-proxi-token-count-basis"] == "estimate"
    parsed_chars = len("SYS") + 1 + len(("User: " + BIG).strip()) + 1 + len(json.dumps(
        [{"type": "function", "function": {"name": READ_FILE["name"], "description": READ_FILE["description"],
                                           "parameters": READ_FILE["parameters"]}}], separators=(",", ":")))
    assert answer["input_tokens"] == adapters.estimate_tokens(parsed_chars) > parsed_chars // 4 * 1.4
    assert relay.posts == []  # nothing went upstream


def test_after_a_real_request_the_conversation_is_counted_from_its_real_usage(prompt_relay):
    relay = prompt_relay()
    first = {"system": "SYS", "messages": messages(BIG), "tools": TOOLS}
    relay.scripts.append([{"event": "content_block_delta", "type": "text", "partialText": "ok"},
                          usage(read=250_000, write=8_000, fresh=120), {"event": "end_of_stream"}])
    status, _, _ = relay.call("/v1/messages", {"model": "claude-opus-5-5", "max_tokens": 64, **first}, SESSION)
    assert status == 200
    real = 250_000 + 8_000 + 120
    # the next question of the same conversation: the same messages plus an answer and a new question
    follow = {**first, "messages": messages(BIG, "ok", "And now a second question about the project?")}
    status, answer, headers = count(relay, follow, SESSION)
    assert headers["x-proxi-token-count-basis"] == "last-usage"
    added = len("Assistant: ok") + len("User: And now a second question about the project?") + 4
    assert abs(answer["input_tokens"] - (real + adapters.estimate_tokens(added))) <= 3
    assert answer["input_tokens"] > real  # the old estimate was a few hundred tokens for the same request
    # another conversation, another model, and no conversation at all do not borrow it
    for other_headers, other_model in (({"x-claude-code-session-id": "99999999-2222-3333-4444-555555555555"},
                                        "claude-opus-5-5"), (SESSION, "claude-sonnet-5-5"), ({}, "claude-opus-5-5")):
        _, _, reply_headers = count(relay, {**follow, "model": other_model}, other_headers)
        assert reply_headers["x-proxi-token-count-basis"] == "estimate"


def test_the_same_request_again_is_counted_exactly_as_upstream_counted_it(prompt_relay):
    relay = prompt_relay()
    body = {"system": "SYS", "messages": messages(BIG), "tools": TOOLS}
    relay.scripts.append([{"event": "content_block_delta", "type": "text", "partialText": "ok"},
                          usage(read=0, write=50_000, fresh=100), {"event": "end_of_stream"}])
    relay.call("/v1/messages", {"model": "claude-opus-5-5", "max_tokens": 64, **body}, SESSION)
    session = relay.runtime.pool._conversations["header:" + SESSION["x-claude-code-session-id"]]
    chars, tokens, model = session._count_anchor
    assert (tokens, model) == (50_100, "claude-opus-5-5")
    _, answer, headers = count(relay, body, SESSION)  # the very same request again: exactly the real count
    assert answer["input_tokens"] == 50_100 and headers["x-proxi-token-count-basis"] == "last-usage"


def test_a_conversation_that_was_never_counted_has_no_anchor(prompt_relay):
    relay = prompt_relay()
    pool = relay.runtime.pool
    assert pool.count_anchor("header:nobody", "claude-opus-5-5") is None
    assert pool.count_anchor(None, "claude-opus-5-5") is None
    assert pool.count_anchor("header:nobody", "claude-opus-5-5", isolated=True) is None
