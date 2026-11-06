"""The result of a tool carries a cache hint where it arrives, so the next request reads it (item 16, a3: OpenCode's dialog).

A tool result never had a hint: native_history.cache_messages skipped the messages whose content is a dict, which is how
tool results are stored. A new result was billed as plain input when it arrived and written to the cache one request later,
when the newest hint stood after it: 43,703 tokens of input (c6f288ce) and then a 43,918-token write (26b9fe10), and in the
Opus dialog plain input was 14.9 of 224.7 credits (6.6 %). With the hint where the result arrives it is written once and read
afterwards. Upstream was never seen to take a hint inside a tool result, so a body that carries one and is refused with a 400
or 422 is sent again without it, once, and the hints stop for the rest of the run."""
import copy
import io
import json
import urllib.error

import pytest

import direct
import native_history
from prompt_support import prompt_relay, strip_cache  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import Chat, SUMMARY, tail_after
from test_native_history import tool_reply

TOOLS = [{"name": "read"}]
RESULT = "file contents " * 20


def dialog(results=1, text=RESULT):
    messages = [{"role": "system", "content": [{"type": "text", "text": "head", "cache": True}]},
                {"role": "user", "content": [{"type": "text", "text": "task"}]}]
    for number in range(results):
        messages += [{"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c%d" % number,
                                                        "toolName": "read", "toolInput": {}}]},
                     {"role": "tool", "content": {"toolCallId": "c%d" % number, "toolResult": text}}]
    return messages


def hinted(messages):
    """The positions of the tool results of a body that carry a hint, and of the other blocks that do, as (kind, index)."""
    found = []
    for index, message in enumerate(messages):
        content = message["content"]
        if isinstance(content, dict):
            result = content["toolResult"]
            if isinstance(result, list) and any(block.get("cache") for block in result):
                found.append(("result", index))
        else:
            found += [("block", index) for block in content if block.get("cache")]
    return found


# --- the builder ---------------------------------------------------------------------------------------------------

def test_by_default_a_tool_result_is_left_as_it_was_and_the_hint_is_before_it():
    placed = native_history.cache_messages(dialog(), True, TOOLS)
    assert placed[3]["content"]["toolResult"] == RESULT  # still a string
    assert hinted(placed) == [("block", 0), ("block", 2)]  # the head and the call, the newest block


def test_with_tool_results_the_newest_hint_is_on_the_result_and_the_result_becomes_one_text_block():
    placed = native_history.cache_messages(dialog(), True, TOOLS, tool_results=True)
    assert placed[3]["content"]["toolResult"] == [{"type": "text", "text": RESULT, "cache": True}]
    assert hinted(placed) == [("block", 0), ("result", 3)]
    assert placed[3]["content"]["toolCallId"] == "c0"  # nothing else of the message changes


def test_the_next_request_keeps_the_last_hint_where_it_was_and_the_shape_of_the_older_result():
    first = native_history.cache_messages(dialog(), True, TOOLS, tool_results=True)
    second_input = first + [{"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c1", "toolName": "read",
                                                               "toolInput": {}}]},
                            {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "second " * 30}}]
    second = native_history.cache_messages(second_input, True, TOOLS, tool_results=True)
    assert hinted(second) == [("block", 0), ("result", 3), ("result", 5)]  # the head, the last request's, the new one
    assert second[3]["content"]["toolResult"] == [{"type": "text", "text": RESULT, "cache": True}]  # the same bytes
    assert second[5]["content"]["toolResult"][0]["text"] == "second " * 30
    third = native_history.cache_messages(second + dialog(1)[2:], True, TOOLS, tool_results=True)
    assert len(hinted(third)) <= 3  # head, the previous breakpoint, the newest: within what the provider allows with tools


def test_a_result_that_is_a_list_takes_the_hint_on_its_last_text_block_and_one_of_images_takes_none():
    image = {"type": "image", "imageData": "AAAA", "mimeType": "image/png"}
    mixed = dialog()
    mixed[3]["content"]["toolResult"] = [{"type": "text", "text": "first part"}, image, {"type": "text", "text": "last part"}]
    placed = native_history.cache_messages(mixed, True, TOOLS, tool_results=True)
    assert [bool(block.get("cache")) for block in placed[3]["content"]["toolResult"]] == [False, False, True]
    only_image = dialog()
    only_image[3]["content"]["toolResult"] = [image]
    placed = native_history.cache_messages(only_image, True, TOOLS, tool_results=True)
    assert placed[3]["content"]["toolResult"] == [image] and hinted(placed) == [("block", 0), ("block", 2)]


@pytest.mark.parametrize("text", ["", "   \n"])
def test_a_result_with_no_text_takes_no_hint(text):
    placed = native_history.cache_messages(dialog(text=text), True, TOOLS, tool_results=True)
    assert placed[3]["content"]["toolResult"] == text and hinted(placed) == [("block", 0), ("block", 2)]


def test_without_the_flag_the_hints_an_earlier_body_put_on_a_result_are_taken_off():
    with_hint = native_history.cache_messages(dialog(), True, TOOLS, tool_results=True)
    cleared = native_history.cache_messages(with_hint, True, TOOLS)
    assert hinted(cleared) == [("block", 0), ("block", 2)]
    assert cleared[3]["content"]["toolResult"] == [{"type": "text", "text": RESULT}]  # a list still, a hint no more


def test_a_compaction_leaves_the_last_result_unmarked():
    placed = native_history.cache_messages(dialog(2), True, TOOLS, mark_tail=False, tool_results=True)
    assert ("result", 5) not in hinted(placed)  # the last message is the tail
    assert hinted(placed) == [("block", 0), ("block", 4)]  # the newest hint stands before it, as it would without results


def test_the_input_is_not_changed():
    original = dialog()
    before = copy.deepcopy(original)
    native_history.cache_messages(original, True, TOOLS, tool_results=True)
    assert original == before


# --- which models -----------------------------------------------------------------------------------------------------

def test_only_the_anthropic_route_gets_a_hint_on_a_result():
    assert direct._hints_tool_results("claude-sonnet-5-5") and direct._hints_tool_results("claude-opus-5-5")
    assert not direct._hints_tool_results("gpt-6.1-sol") and not direct._hints_tool_results("gemini-2.5-pro")
    assert not direct._hints_tool_results("zai-org/GLM-5.3") and not direct._hints_tool_results("no-such-model")
    assert not direct._hints_tool_results(None)
    direct._TOOL_RESULT_HINTS["refused"] = True
    assert not direct._hints_tool_results("claude-sonnet-5-5")


# --- through the relay ------------------------------------------------------------------------------------------------

def result_messages(body):
    return [message for message in body["messages"] if message["role"] == "tool"]


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_the_result_of_a_tool_is_hinted_where_it_arrives_and_read_by_the_next_request(prompt_relay, dialect):
    relay = prompt_relay()
    chat = Chat(relay, dialect=dialect, effort="max")
    first, second, third = chat.work(3)
    assert result_messages(first) == []
    assert [index for kind, index in hinted(second["messages"]) if kind == "result"] == [
        index for index, message in enumerate(second["messages"]) if message["role"] == "tool"]
    kinds = [kind for kind, _ in hinted(third["messages"])]
    assert kinds.count("result") == 2 and len(kinds) <= 3  # the last request's result and the new one, and the head
    # the older result is the same bytes as in the request that sent it first, hint aside: the prefix is the one cached
    assert tail_after(second, third)
    for body in (second, third):
        assert len(hinted(body["messages"])) <= 3


def test_a_model_of_another_provider_is_left_as_it_was(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, effort="max", model="gpt-6.1-sol")
    first, second, third = chat.work(3)
    for body in (second, third):
        assert result_messages(body), "the dialog must be native for this test to say anything"
        assert all(isinstance(message["content"]["toolResult"], str) for message in result_messages(body))
        assert not any(kind == "result" for kind, _ in hinted(body["messages"]))


def test_a_compaction_reads_the_last_result_of_the_chat_and_does_not_hint_its_own_instruction(prompt_relay):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(2)
    summary = chat.compact(effort="max")
    kinds = hinted(summary["messages"])
    assert ("result", [i for i, m in enumerate(summary["messages"]) if m["role"] == "tool"][-1]) in kinds
    assert all(index != len(summary["messages"]) - 1 for _, index in kinds)


# --- the safety net --------------------------------------------------------------------------------------------------

def refusing(relay, monkeypatch, status=400, message="Invalid request format", only_hinted=True):
    """Retool refuses every body that carries a hint on a tool result (or every body, only_hinted=False); nothing else.

    Returns the bodies it refused."""
    original = direct.urllib.request.urlopen
    refused = []

    def picky(request_, **kwargs):
        body = json.loads(request_.data)
        if not only_hinted or direct._has_tool_result_hint(body["messages"]):
            relay.posts.append(body)
            refused.append(body)
            raise urllib.error.HTTPError(request_.full_url, status, "refused", {},
                                         io.BytesIO(message.encode() if message in ("", "{}")
                                                    else json.dumps({"message": message}).encode()))
        return original(request_, **kwargs)

    monkeypatch.setattr(direct.urllib.request, "urlopen", picky)
    return refused


@pytest.mark.parametrize("status", [400, 422])
def test_a_body_upstream_refuses_for_its_hint_is_sent_again_without_it_and_the_hints_stop(prompt_relay, monkeypatch, status):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(1)
    refused = refusing(relay, monkeypatch, status)
    mark = relay.mark()
    second = chat.ask(tool_reply("c2", "opaque-2"))  # the request that carries the first tool result
    assert len(refused) == 1 and direct._has_tool_result_hint(refused[0]["messages"])
    assert not direct._has_tool_result_hint(second["messages"])  # what was answered
    assert second["messages"][-1]["content"]["toolResult"] == "file contents"  # the result as the client sent it
    assert direct._TOOL_RESULT_HINTS["refused"] is True
    events = relay.events(mark, "upstream.tool_result_hint_refused")
    assert len(events) == 1 and events[0]["level"] == "warn" and events[0]["data"]["status"] == status
    # the dialog goes on, and no body carries a hint again
    third = chat.ask(tool_reply("c3", "opaque-3"))
    assert not any(kind == "result" for kind, _ in hinted(third["messages"]))
    assert len(refused) == 1


@pytest.mark.parametrize("message", [
    "messages.3.content.0.tool_result.content.0.text.cache_control: Extra inputs are not permitted",
    "Invalid request format: messages[3].content.toolResult[0].cache is not allowed", "", "{}"])
def test_a_refusal_that_names_the_cache_or_nothing_is_asked_again_without_the_hint(prompt_relay, monkeypatch, message):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(1)
    refused = refusing(relay, monkeypatch, message=message)
    second = chat.ask(tool_reply("c2", "opaque-2"))
    assert len(refused) == 1 and not direct._has_tool_result_hint(second["messages"])
    assert direct._TOOL_RESULT_HINTS["refused"] is True


@pytest.mark.parametrize("message", [
    "prompt is too long: 240000 tokens > 200000 maximum", "tools.0.input_schema: JSON schema is invalid",
    "Request blocked by the content filter", "max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens",
    "Invalid signature in thinking block", "messages.3: `tool_use` ids were found without `tool_result` blocks"])
def test_a_refusal_that_names_another_reason_is_not_asked_again(prompt_relay, monkeypatch, message):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(1)
    refused = refusing(relay, monkeypatch, message=message)
    with pytest.raises(AssertionError):
        chat.ask(tool_reply("c2", "opaque-2"))  # Chat.ask asserts status 200
    assert len(refused) == 1  # one try: the refusal is not about the hint
    assert direct._TOOL_RESULT_HINTS["refused"] is False


def test_a_refusal_that_comes_back_is_not_taken_for_the_hints(prompt_relay, monkeypatch):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(1)
    refused = refusing(relay, monkeypatch, only_hinted=False)  # refuses everything, hinted or not
    with pytest.raises(AssertionError):
        chat.ask(tool_reply("c2", "opaque-2"))
    assert len(refused) == 2  # the hinted body, and once more without the hint, which fails the same way
    assert direct._has_tool_result_hint(refused[0]["messages"]) and not direct._has_tool_result_hint(refused[1]["messages"])
    assert direct._TOOL_RESULT_HINTS["refused"] is False  # the retry was not answered: the hint was not the cause


def test_a_server_error_is_not_retried_without_the_hint(prompt_relay, monkeypatch):
    relay = prompt_relay()
    chat = Chat(relay, effort="max")
    chat.work(1)
    refused = refusing(relay, monkeypatch, status=500)
    with pytest.raises(AssertionError):
        chat.ask(tool_reply("c2", "opaque-2"))
    assert direct._TOOL_RESULT_HINTS["refused"] is False
    assert refused and all(direct._has_tool_result_hint(body["messages"]) for body in refused)


def test_a_model_without_hints_never_meets_the_retry(prompt_relay, monkeypatch):
    relay = prompt_relay()
    chat = Chat(relay, effort="max", model="gpt-6.1-sol")
    chat.work(2)
    refused = refusing(relay, monkeypatch)
    chat.ask(tool_reply("c3", "opaque-3"))
    assert refused == [] and direct._TOOL_RESULT_HINTS["refused"] is False
