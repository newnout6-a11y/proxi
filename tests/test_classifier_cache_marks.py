"""The cache marks of a client's text blocks are kept where the client put them (item 3, a4 F9: auto-mode-check).

Claude Code's permission classifier sends the whole transcript as ONE user message of N text blocks, marks the last two
stable ones with cache_control and ends with a volatile tail (the pending action and the instruction). The relay joined the
blocks into one text and flagged its very end: a breakpoint that no later request can read. Every check wrote the whole
transcript to the cache again: 35 checks of one real Desktop session cost 107 credits (a5/a4, 2026-11-06), a replay of 292
captured bodies 359.6 credits against 186.2 with the marks kept.

What must not change is the text: the text sent upstream, parsed.prompt and the fingerprints of the native-history match are
the same bytes as before."""
import json
import random

import pytest

import direct
import validate
from prompt_support import prompt_relay  # noqa: F401 - a fixture

MARK = {"type": "ephemeral"}
CFG = {"strict_models": False, "model": "claude-opus-5-5", "show_thinking": True}
BUILD = {"direct_cache": True}
SYSTEM = [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.317.d83; cc_entrypoint=claude-desktop-3p;"},
          {"type": "text", "text": "You are a classifier. " * 40, "cache_control": MARK}]
TOOL = {"name": "x", "description": "d", "input_schema": {"type": "object", "properties": {}}}


def t(text, marked=False):
    return {"type": "text", "text": text, **({"cache_control": MARK} if marked else {})}


def built(messages, tools=None, system=SYSTEM):
    """(parsed, body posted to Retool) for an Anthropic request, as the relay builds it."""
    data = {"model": "claude-opus-5-5", "max_tokens": 1000, "system": system, "messages": messages}
    if tools:
        data["tools"] = tools
    parsed = validate.parse_anthropic(data, CFG)
    upstream_tools = [{"type": "function", "function": {"name": tool["name"], "description": "d",
                                                        "parameters": {"type": "object", "properties": {}}}}
                      for tool in tools] if tools else None
    body = direct.build_body(BUILD, {"model": parsed.model}, parsed.system, parsed.prompt, upstream_tools, None, 1000,
                             chunks=parsed.message_chunks)
    return parsed, body


def before(messages, tools=None):
    """The same request as the relay built it before: every message one text, one flag at the end of the last."""
    data = {"model": "claude-opus-5-5", "max_tokens": 1000, "system": SYSTEM, "messages": messages}
    parsed = validate.parse_anthropic(data, CFG)
    _, chunks = validate._render_chunks(messages, lead=0)
    return direct.build_body(BUILD, {"model": parsed.model}, parsed.system, parsed.prompt, None, None, 1000, chunks=chunks)


def user_blocks(body):
    return [block for message in body["messages"] if message["role"] == "user" for block in message["content"]]


def user_text(body):
    return "".join(block.get("text", "") for block in user_blocks(body))


def flags(body):
    """One letter per user block: F where upstream is asked for a breakpoint."""
    return "".join("F" if block.get("cache") else "-" for block in user_blocks(body))


def total_flags(body):
    return sum(1 for message in body["messages"] for block in message["content"] if block.get("cache"))


def sane(body):
    """No private key leaks, no block says nothing, and no message carries two flags."""
    assert '"sep"' not in json.dumps(body)
    for message in body["messages"]:
        assert sum(1 for block in message["content"] if block.get("cache")) <= 1, message
        for block in message["content"]:
            if block.get("type") == "text":
                assert block["text"].strip(), "an empty or blank text block went upstream: %r" % block["text"]


CLASSIFIER = [t("<transcript>"), t("entry one"), t("entry two", True), t("entry three", True), t("</transcript>"),
              t("Answer now.")]


def test_the_classifier_message_stays_blocks_and_the_breakpoint_is_the_clients_last_mark():
    parsed, body = built([{"role": "user", "content": CLASSIFIER}])
    blocks = user_blocks(body)
    assert len(blocks) == 6 and flags(body) == "---F--"  # not at the end of the volatile tail
    assert blocks[3]["text"].startswith("entry three")
    assert total_flags(body) == 2  # the system head and the one breakpoint of the transcript
    sane(body)
    assert user_text(body) == user_text(before([{"role": "user", "content": CLASSIFIER}]))
    assert user_text(body) == "User: <transcript>\nentry one\nentry two\nentry three\n</transcript>\nAnswer now."


def test_before_the_message_was_one_block_flagged_at_its_end():
    body = before([{"role": "user", "content": CLASSIFIER}])
    assert len(user_blocks(body)) == 1 and flags(body) == "F"


def test_the_text_the_model_gets_the_prompt_and_the_fingerprints_are_the_same_bytes_as_before():
    messages = [{"role": "user", "content": CLASSIFIER}]
    parsed, body = built(messages)
    assert user_text(body) == user_text(before(messages))
    assert parsed.prompt == "User: <transcript>\nentry one\nentry two\nentry three\n</transcript>\nAnswer now."
    old_chunk = validate._render_chunks(messages, lead=0)[1][0]
    assert parsed.prompt == old_chunk
    assert parsed.message_fingerprints == validate._message_fingerprints(messages, 0)
    assert isinstance(parsed.message_chunks[0], list) and isinstance(old_chunk, str)


CASES = {
    # name: (messages, tools, flags of the user blocks, user text)
    "a marked block that is empty hands its mark to the block before it":
        ([{"role": "user", "content": [t("alpha"), t("beta"), t("", True), t("tail")]}], None, "-F-",
         "User: alpha\nbeta\ntail"),
    "four marks in one message are one breakpoint, the last":
        ([{"role": "user", "content": [t("a", True), t("b", True), t("c", True), t("d", True), t("e")]}], None, "---F-",
         "User: a\nb\nc\nd\ne"),
    "trailing whitespace and unicode are the old join's":
        ([{"role": "user", "content": [t("привет \n"), t("мир ", True), t("end \n\n")]}], None, "-F-",
         "User: привет \n\nмир \nend"),
    "a block of only whitespace is joined to the block before it":
        ([{"role": "user", "content": [t("a"), t("b", True), t("  "), t("c")]}], None, "-F-", "User: a\nb\n  \nc"),
    "a marked block of only whitespace marks the block before it":
        ([{"role": "user", "content": [t("a"), t("  ", True), t("tail")]}], None, "F-", "User: a\n  \ntail"),
    "a blank first block still carries the role":
        ([{"role": "user", "content": [t(" "), t("x", True), t("tail")]}], None, "-F-", "User:  \nx\ntail"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_edge_cases(name):
    messages, tools, expected_flags, expected_text = CASES[name]
    parsed, body = built(messages, tools)
    assert flags(body) == expected_flags and user_text(body) == expected_text
    assert user_text(body) == user_text(before(messages))
    sane(body)


UNCHANGED = {
    "the only block carries the mark": [{"role": "user", "content": [t("only block", True)]}],
    "no mark at all": [{"role": "user", "content": [t("a"), t("b"), t("c")]}],
    "the mark is on the last block, where the flag goes anyway": [{"role": "user", "content": [t("a", True), t("b", True)]}],
    "whitespace after the last mark is stripped, so the mark ends the text":
        [{"role": "user", "content": [t("a"), t("b", True), t(" \n")]}],
    "an image next to the text": [{"role": "user", "content": [t("look"), {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}, t("end", True)]}],
}


@pytest.mark.parametrize("name", list(UNCHANGED))
def test_a_message_the_marks_do_not_help_stays_what_it_was(name):
    messages = UNCHANGED[name]
    parsed, body = built(messages)
    old = before(messages)
    if "image" in name:
        assert parsed.message_chunks[0][0]["type"] == "text"  # the image keeps its own handling
    else:
        assert all(isinstance(chunk, str) for chunk in parsed.message_chunks)
    assert user_text(body) == user_text(old)
    assert len(user_blocks(body)) == len(user_blocks(old)) and flags(body) == flags(old)
    sane(body)


def test_a_request_with_tools_is_untouched():
    messages = [{"role": "user", "content": [t("one"), t("two", True), t("tail")]}]
    parsed, body = built(messages, tools=[TOOL])
    assert all(isinstance(chunk, str) for chunk in parsed.message_chunks)
    assert len(user_blocks(body)) == 1 and flags(body) == "F"
    sane(body)


def test_a_tool_result_message_is_untouched():
    messages = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"},
                                             t("after", True)]}]
    parsed, body = built(messages, tools=[TOOL])
    assert all(isinstance(chunk, str) for chunk in parsed.message_chunks)
    sane(body)
    # and without tools in the request: a tool_result block is not a text block, the message stays one text
    parsed, body = built(messages)
    assert all(isinstance(chunk, str) for chunk in parsed.message_chunks)


def test_only_user_messages_of_text_blocks_are_kept_as_blocks():
    messages = [{"role": "user", "content": "one"},
                {"role": "assistant", "content": [t("a"), t("b", True), t("c")]},
                {"role": "user", "content": [t("x"), t("y", True), t("z")]}]
    parsed, body = built(messages)
    assert [isinstance(chunk, list) for chunk in parsed.message_chunks] == [False, False, True]
    sane(body)


def test_the_marks_of_a_growing_transcript_are_prefixes_of_the_next_request():
    """What makes the cache readable: the text up to the breakpoint of one request begins the next request."""
    def request(count):
        blocks = [t("[entry %02d] %s" % (index, "x" * 80)) for index in range(count)]
        blocks[count - 2]["cache_control"] = MARK
        blocks[count - 1]["cache_control"] = MARK
        return [{"role": "user", "content": blocks + [t("Pending action %d: rm -rf build" % count), t("Reply OK.")]}]

    previous = None
    for count in (12, 14, 16, 18):
        _, body = built(request(count))
        blocks = user_blocks(body)
        flagged = next(index for index, block in enumerate(blocks) if block.get("cache"))
        assert flagged == count - 1 and len(blocks) == count + 2  # the last transcript block, not the tail
        text = user_text(body)
        if previous is not None:
            assert text.startswith(previous), "the breakpoint of the last request is no prefix of this one"
        previous = "".join(block["text"] for block in blocks[:flagged + 1])
        sane(body)


def test_random_messages_keep_their_text_and_never_send_a_block_that_says_nothing():
    rng = random.Random(20261106)
    pool = ["x", "alpha ", "\n", " ", "", "привет\n", "tail text", "  indent", "a\n\n", "\t", "<b>", "z "]
    shapes = {"one text": 0, "blocks": 0}
    for _ in range(400):
        content = [t(rng.choice(pool), rng.random() < 0.35) for _ in range(rng.randint(2, 9))]
        messages = [{"role": "user", "content": content}]
        parsed, body = built(messages)
        old = before(messages)
        assert user_text(body) == user_text(old), content
        _, chunks = validate._render_chunks(messages, lead=0)
        assert parsed.prompt == chunks[0], content
        sane(body)
        shapes["blocks" if len(user_blocks(body)) > 1 else "one text"] += 1
        # a flag is only ever placed on a block that a client mark covers, or at the end as before
        if len(user_blocks(body)) > 1:
            assert flags(body).count("F") == 1 and not flags(body).endswith("F")
    assert shapes["blocks"] > 40 and shapes["one text"] > 40, shapes  # both paths were exercised


def test_the_separator_is_added_by_the_builder_and_never_sent():
    chunk = [{"type": "text", "text": "a", "sep": "\n"}, {"type": "text", "text": "b", "cache": True}]
    body = direct.build_body(BUILD, {"model": "m"}, "head", "", None, None, 100, chunks=[chunk])
    assert [block["text"] for block in user_blocks(body)] == ["a\n", "b"]
    assert "sep" not in json.dumps(body) and flags(body) == "-F"
    assert chunk[0]["text"] == "a" and chunk[0]["sep"] == "\n"  # the caller's chunk is not changed


def test_a_chunk_with_a_flag_of_its_own_gets_no_second_one_and_one_without_still_gets_the_usual():
    marked = [{"type": "text", "text": "a"}, {"type": "text", "text": "b", "cache": True}, {"type": "text", "text": "c"}]
    body = direct.build_body(BUILD, {"model": "m"}, "head", "", None, None, 100, chunks=[marked])
    assert flags(body) == "-F-"
    plain = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    body = direct.build_body(BUILD, {"model": "m"}, "head", "", None, None, 100, chunks=[plain])
    assert flags(body) == "-F"


# --- through the relay ---------------------------------------------------------------------------------------------

CLASSIFIER_SYSTEM = "You are a security monitor for autonomous AI coding agents.\n" + "stable policy\n" * 40


def classifier_request(count, tail="Pending action: rm -rf build"):
    blocks = [t("[entry %02d] %s" % (index, "x" * 60)) for index in range(count)]
    blocks[count - 2]["cache_control"] = MARK
    blocks[count - 1]["cache_control"] = MARK
    return {"model": "claude-opus-5-5", "max_tokens": 2112, "stop_sequences": ["</block>"],
            "system": [t("x-anthropic-billing-header: cc_version=2.1.317.d83; cc_entrypoint=cli;"),
                       t(CLASSIFIER_SYSTEM, True)],
            "messages": [{"role": "user", "content": blocks + [t(tail), t("Answer with one word.")]}]}


def test_through_the_relay_the_classifier_body_has_the_breakpoint_inside_the_transcript(prompt_relay):
    relay = prompt_relay()
    status, _, _ = relay.post("anthropic", classifier_request(10), {"user-agent": "claude-cli/2.1.317 (external, cli)"})
    assert status == 200
    body = relay.posts[0]
    blocks = user_blocks(body)
    assert flags(body).index("F") == 9 and flags(body).count("F") == 1 and len(blocks) == 12
    assert blocks[-2]["text"].startswith("Pending action") and not blocks[-1].get("cache")
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    sane(body)
    assert user_text(body) == user_text(before([classifier_request(10)["messages"][0]]))


def test_through_the_relay_the_next_check_starts_with_the_text_the_last_one_flagged(prompt_relay):
    relay = prompt_relay()
    headers = {"user-agent": "claude-cli/2.1.317 (external, cli)"}
    relay.post("anthropic", classifier_request(10, "Pending action: first"), headers)
    relay.post("anthropic", classifier_request(12, "Pending action: second"), headers)
    first, second = relay.posts[0], relay.posts[1]
    blocks = user_blocks(first)
    prefix = "".join(block["text"] for block in blocks[:flags(first).index("F") + 1])
    assert user_text(second).startswith(prefix)
    assert first["messages"][0] == second["messages"][0]  # the system head is the same too, so its breakpoint is read
