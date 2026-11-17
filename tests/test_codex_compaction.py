"""Codex's compaction reads the cache of its chat (v8, Codex run of 2026-11-07).

Codex compacts a conversation for a custom provider with the history of the chat and its summary prompt and NO tools; the cached
prefix starts with the tools, so the request shared nothing with the chat and wrote the whole history again: 363bb3d49ff4 wrote
31,943 tokens (1.65 credits) after a chat request that had read 24,311 and written 9,807, and Opus billed 52,527 uncached tokens
(4.80 credits, 0697b894535c). The relay now declares the tools the chat sent last, so the request is the next request of the chat
plus its instruction: the live replays read 34,118 of 34,121 tokens (gpt-6.1-sol, 0.13 credits) and 60,446 of 60,616 (Opus, 0.50).
A tool call must never reach Codex in reply to it: its summary is the last assistant message of its history.
"""
import io
import json
import urllib.error

import pytest

import admin
import compaction_tools
import config
import direct
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import SYSTEM, Chat, count, effort_fields, tail_after, thinking, OPENCODE
from test_compaction_tail_cache import flags
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text

# codex-rs/prompts/templates/compact/prompt.md, rust-v0.167.0
PROMPT = ("You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the "
          "task.\n\nInclude:\n- Current progress and key decisions made\n- Important context, constraints, or user preferences\n"
          "- What remains to be done (clear next steps)\n- Any critical data, examples, or references needed to continue\n\n"
          "Be concise, structured, and focused on helping the next LLM seamlessly continue the work.")
EXEC = {"originator": "codex_exec",
        "user-agent": "codex_exec/0.167.0 (Windows 10.0.26300; x86_64) xterm-256color (codex_exec; 0.167.0)"}
SUMMARY = [text("## Progress\n- the work is half done"), *END]
FINAL = [text("all done"), *END]
SESSION = "header:codex-chat"


def turn_metadata(kind, phase="mid_turn"):
    meta = {"session_id": "codex-chat", "thread_id": "codex-chat", "request_kind": kind, "turn_trigger": "exec"}
    if kind == "compaction":
        meta["compaction"] = {"trigger": "auto", "reason": "context_limit", "implementation": "responses", "phase": phase,
                              "strategy": "memento"}
    return json.dumps(meta, separators=(",", ":"))


def codex_chat(relay, model="claude-sonnet-5-5", session="codex-chat"):
    headers = {**EXEC, "session-id": session, "thread-id": session, "x-codex-turn-metadata": turn_metadata("turn")}
    return Chat(relay, "responses", effort="high", headers=headers, model=model)


def compact(chat, scripts=(SUMMARY,), *, messages=None, prompt=PROMPT, kind="compaction", phase="mid_turn", effort=None,
            model=None, headers=None, tools=()):
    """The request Codex sends to compact its chat: the history, then its prompt as a user message, and `tools` (none)."""
    relay, before = chat.relay, len(chat.relay.posts)
    relay.scripts.extend(scripts)
    body = client_request("responses", (chat.messages if messages is None else messages) + [
        {"role": "user", "content": prompt}], system=SYSTEM, tools=False, model=model or chat.model,
        **effort_fields("responses", effort or chat.effort))
    body["tools"], body["parallel_tool_calls"] = list(tools), False
    meta = {"x-codex-turn-metadata": turn_metadata(kind, phase)}
    status, payload, answer = relay.post("responses", body, {**chat.headers, **meta, **(headers or {})})
    assert status == 200, payload
    relay.settle(answer)
    return payload, relay.posts[before:]


def kinds(payload):
    return [item["type"] for item in payload["output"]]


# -- the compaction reads the chat's cache ------------------------------------------------------------------------------

def test_a_codex_compaction_declares_the_tools_of_its_chat_and_continues_its_signed_history(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    first, second = chat.work(2)
    mark = relay.mark()
    payload, (summary,) = compact(chat)

    assert kinds(payload)[-1] == "message" and "function_call" not in kinds(payload)
    # the tools, the head and the cache key the chat had: the provider reads what the chat cached
    assert second["tools"] and summary["tools"] == second["tools"]
    assert summary["openAICacheKey"] == second["openAICacheKey"] == first["openAICacheKey"]
    assert summary["messages"][0] == second["messages"][0]
    tail = tail_after(second, summary)
    assert [message["role"] for message in tail] == ["assistant", "tool", "user"]
    assert PROMPT[:50] in json.dumps(tail[-1]) and thinking(summary) == ["opaque-1", "opaque-2"]
    assert summary["reasoningEffort"] == second["reasoningEffort"]
    # nothing written for the instruction: its hint stays off, and the chat's own last hint is read
    assert "F" not in flags(summary)[-1]
    assert count(relay, mark, "native_history.resume") == 1
    for kind in ("native_history.reset", "native_history.cold", "cache.key_changed", "cache.head_changed",
                 "cache.tools_changed", "session.create"):
        assert count(relay, mark, kind) == 0, kind
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert (begin["client_task"], begin["tools"], begin["request_purpose"]) == ("compaction", len(second["tools"]), "client-tools")
    restored = relay.events(mark, "codex.compaction_tools_restored")
    assert len(restored) == 1 and restored[0]["data"]["tools"] == 1
    assert begin["compaction_phase"] == "mid_turn" and begin["compaction_tools"] == "restored"


def test_a_codex_compaction_after_a_final_answer_continues_the_chat_too(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    last = chat.ask(FINAL)
    echo = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "all done"}]}
    mark = relay.mark()
    payload, (summary,) = compact(chat, messages=chat.messages + [echo], phase="pre_turn")
    assert summary["tools"] == last["tools"] and summary["openAICacheKey"] == last["openAICacheKey"]
    assert [message["role"] for message in tail_after(last, summary)] == ["assistant", "user"]
    assert count(relay, mark, "native_history.resume") == 1 and count(relay, mark, "native_history.reset") == 0
    assert relay.events(mark, "turn.begin")[-1]["data"]["compaction_phase"] == "pre_turn"
    assert kinds(payload)[-1] == "message"


def test_the_compaction_runs_on_the_session_of_the_chat_and_not_on_a_text_session_of_its_own(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    pool = relay.runtime.pool
    assert SESSION in pool._conversations and (SESSION, "text") not in pool._conversations
    session = pool._conversations[SESSION]
    compact(chat)
    assert (SESSION, "text") not in pool._conversations and pool._conversations[SESSION] is session
    # a side request: it records no branch of its own and the chat's keepwarm stops after it (the client replaces its history)
    assert len(session._native_history.branches) == 1
    assert session._warm_blocked == "compacted"


def test_the_chat_goes_on_after_its_compaction_from_the_summary_like_before(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    compact(chat)
    # Codex replaces its history: the retained user message and the summary, then the next prompt
    chat.messages = [chat.messages[0], {"role": "user", "content": "SUMMARY: half done"},
                     {"role": "user", "content": "carry on"}]
    mark = relay.mark()
    body = chat.ask(tool_reply("c3", "opaque-3"))
    assert body["tools"] and count(relay, mark, "native_history.reset") == 1
    assert count(relay, mark, "session.create") == 0
    assert relay.runtime.pool._conversations[SESSION]._warm_blocked is None


# -- when the tools of the chat are not known, or the switch is off, it is served as before -------------------------------

def test_a_compaction_whose_chat_the_relay_never_saw_with_tools_is_served_as_codex_sent_it(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.messages = chat.messages + [{"role": "assistant", "content": "earlier"}]
    mark = relay.mark()
    _, (summary,) = compact(chat)
    assert "tools" not in summary and count(relay, mark, "codex.compaction_tools_unknown") == 1
    assert count(relay, mark, "codex.compaction_tools_restored") == 0
    assert (SESSION, "text") in relay.runtime.pool._conversations
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["client_task"] == "compaction" and begin["tools"] == 0 and "compaction_tools" not in begin


def test_the_tools_of_another_model_are_not_the_tools_of_the_compaction(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    mark = relay.mark()
    _, (summary,) = compact(chat, model="claude-opus-5-5")
    assert "tools" not in summary and count(relay, mark, "codex.compaction_tools_unknown") == 1


def test_with_the_switch_off_the_compaction_goes_out_as_codex_sent_it(prompt_relay):
    relay = prompt_relay(codex_compaction_cache=False)
    chat = codex_chat(relay)
    chat.work(2)
    mark = relay.mark()
    _, (summary,) = compact(chat)
    assert "tools" not in summary and (SESSION, "text") in relay.runtime.pool._conversations
    assert count(relay, mark, "codex.compaction_tools_restored") == 0 and count(relay, mark, "codex.compaction_tools_unknown") == 0


def test_the_switch_is_read_at_every_request(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    relay.runtime.cfg["codex_compaction_cache"] = False  # what the admin does for a live setting
    assert "tools" not in compact(chat)[1][0]
    relay.runtime.cfg["codex_compaction_cache"] = True
    assert compact(chat)[1][0]["tools"]


def test_only_a_compaction_codex_names_is_given_tools(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    # a request of Codex without tools that is not a compaction (a turn kind) is left alone
    _, (plain,) = compact(chat, kind="turn", prompt="Reply with one word.")
    assert "tools" not in plain
    # nor is the tool-less compaction of another client
    other = Chat(relay, "responses", effort="high", headers={**OPENCODE, "x-session-affinity": "oc"}, model="claude-sonnet-5-5")
    other.work(2)
    mark = relay.mark()
    _, (flattened,) = compact(other, headers={"x-session-affinity": "oc"}, kind="turn", prompt=PROMPT)
    assert "tools" not in flattened and count(relay, mark, "codex.compaction_tools_restored") == 0


def test_a_compaction_is_known_by_what_codex_says_whatever_its_prompt_is(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    first, second = chat.work(2)
    mark = relay.mark()
    _, (summary,) = compact(chat, prompt="Write the notes the next shift needs.")  # compact_prompt of the user's config
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["client_task"] == "compaction" and summary["tools"] == second["tools"]


# -- a tool call never reaches Codex ------------------------------------------------------------------------------------

def test_a_reply_that_calls_a_tool_is_dropped_and_the_request_asked_again_in_words(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    first, second = chat.work(2)
    mark = relay.mark()
    payload, posted = compact(chat, [tool_reply("c3", "opaque-3"), SUMMARY])

    assert len(posted) == 2 and kinds(payload)[-1] == "message"
    assert "function_call" not in kinds(payload) and "hit" not in json.dumps(payload["output"])
    assert "## Progress" in payload["output_text"] and "read_file" not in json.dumps(payload["output"])
    again = posted[1]
    assert again["tools"] == second["tools"] and again["openAICacheKey"] == second["openAICacheKey"]
    # the same prefix, then one user turn of words after the instruction: no cache mark, nothing stored
    assert tail_after(posted[0], again)[-1]["role"] == "user" and "Do not call any tool" in json.dumps(tail_after(posted[0], again))
    assert "F" not in flags(again)[-1] and again["rit"]
    events = relay.events(mark, "turn.compaction_tool_call")
    assert [event["data"]["called"] for event in events] == [["read_file"]] and events[0]["data"]["tools_kept"] is True
    assert count(relay, mark, "tool.call_dropped") == 0 and count(relay, mark, "tool.call") == 0


def test_a_second_call_sends_the_request_codex_made_and_the_summary_still_arrives(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    mark = relay.mark()
    payload, posted = compact(chat, [tool_reply("c3", "opaque-3"), tool_reply("c4", "opaque-4"), SUMMARY])
    assert len(posted) == 3 and kinds(payload)[-1] == "message" and "function_call" not in kinds(payload)
    assert "tools" not in posted[2] and "toolChoice" not in posted[2]
    # the history as text, which cannot be answered with a call: the text of the chat, then the instruction
    assert PROMPT[:50] in json.dumps(posted[2]["messages"][-1])
    assert [event["data"]["tools_kept"] for event in relay.events(mark, "turn.compaction_tool_call")] == [True, False]
    assert count(relay, mark, "turn.error") == 0 and count(relay, mark, "tool.call") == 0


def app_builder_refuses(relay, monkeypatch, model):
    """Retool answers a body of `model` that carries the App Builder marker (metadata.sandpackAgent) as it answers a model that
    is only on the Assist list: HTTP 400 "<model> is not available right now". Every POST is kept in relay.posts."""
    original = direct.urllib.request.urlopen

    def app_builder(request_, **kwargs):
        body = json.loads(request_.data)
        if "metadata" in body and body["model"] == model:
            relay.posts.append(body)
            raise urllib.error.HTTPError(request_.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps({
                "message": "%s is not available right now. Please try again later or select a different model." % model}).encode()))
        return original(request_, **kwargs)

    monkeypatch.setattr(direct.urllib.request, "urlopen", app_builder)


@pytest.mark.parametrize("model", ["gpt-5.2", "claude-haiku-4-5-20251001"])
def test_the_request_codex_made_goes_without_the_app_builder_marker_for_a_model_of_the_assist_list(prompt_relay, monkeypatch,
                                                                                                    model):
    """A model of the Assist list goes to Retool without the App Builder marker, which that route refuses. The two tries with the
    chat's tools went without it, but the third one, the request Codex made, is built anew, and it carried the marker: the
    compaction failed with "not available right now" after two paid tries."""
    monkeypatch.setattr(direct, "_ASSIST_ONLY", set(direct._ASSIST_SEEDS))
    relay = prompt_relay()
    app_builder_refuses(relay, monkeypatch, model)
    chat = codex_chat(relay, model=model)
    chat.work(2)
    mark = relay.mark()
    payload, posted = compact(chat, [tool_reply("c3", "opaque-3"), tool_reply("c4", "opaque-4"), SUMMARY])
    assert ["metadata" in body for body in posted] == [False, False, False]
    assert "tools" not in posted[2] and payload["output_text"].startswith("## Progress")
    assert count(relay, mark, "upstream.assist_retry") == 0


def test_a_compaction_that_finds_its_model_on_the_assist_list_sends_the_request_codex_made_without_the_marker_too(
        prompt_relay, monkeypatch):
    """A model the route refuses with the marker is asked again without it (the assist retry), and that retry starts the guarded
    compaction over: its third try was built with the marker again, and the 400 it got reached Codex."""
    monkeypatch.setattr(direct, "_ASSIST_ONLY", set(direct._ASSIST_SEEDS))
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    app_builder_refuses(relay, monkeypatch, chat.model)  # the route no longer serves the model with the marker
    mark = relay.mark()
    payload, posted = compact(chat, [tool_reply("c3", "opaque-3"), tool_reply("c4", "opaque-4"), SUMMARY])
    assert ["metadata" in body for body in posted] == [True, False, False, False]
    assert "tools" not in posted[3] and payload["output_text"].startswith("## Progress")
    assert count(relay, mark, "upstream.assist_retry") == 1


def test_a_streamed_compaction_gets_no_call_either(prompt_relay):
    """Codex streams: the held attempt must not leak a single event of its call to the SSE reply."""
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    relay.scripts.extend([tool_reply("c3", "opaque-3"), SUMMARY])
    body = client_request("responses", chat.messages + [{"role": "user", "content": PROMPT}], system=SYSTEM, tools=False,
                          model=chat.model, stream=True, **effort_fields("responses", chat.effort))
    body["tools"], body["parallel_tool_calls"] = [], False
    status, raw, answer = relay.post("responses", body, {**chat.headers, "x-codex-turn-metadata": turn_metadata("compaction")})
    assert status == 200 and isinstance(raw, str)
    relay.settle(answer)
    events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and line[5:].strip().startswith("{")]
    done = [event["item"]["type"] for event in events if event.get("type") == "response.output_item.done"]
    assert done[-1] == "message" and "function_call" not in done and events[-1]["type"] == "response.completed"
    assert "read_file" not in raw and "## Progress" in raw
    assert len(relay.posts) == 4  # the chat's two turns, the dropped attempt and the one asked again


def test_text_before_the_call_is_not_passed_on_either(prompt_relay):
    relay = prompt_relay()
    chat = codex_chat(relay)
    chat.work(2)
    thinking_then_text = [text("Let me check the file first."), *tool_reply("c3", "opaque-3")]
    payload, posted = compact(chat, [thinking_then_text, SUMMARY])
    assert len(posted) == 2 and "Let me check" not in json.dumps(payload["output"])
    assert payload["output_text"].startswith("## Progress")


def test_the_compaction_of_a_client_that_sends_its_own_tools_is_not_held(prompt_relay):
    """OpenCode's compaction carries the chat's tools itself: a call in its reply reaches it as before (not the guard's business)."""
    relay = prompt_relay()
    chat = Chat(relay, "responses", effort="high", headers={**OPENCODE, "x-session-affinity": "oc2"}, model="claude-sonnet-5-5")
    chat.work(2)
    mark = relay.mark()
    chat.relay.scripts.append(tool_reply("c3", "opaque-3"))
    body = client_request("responses", chat.messages + [{"role": "user", "content": "Summarize the conversation."}],
                          system=SYSTEM, model=chat.model, **effort_fields("responses", "high"))
    status, payload, answer = relay.post("responses", body, {**chat.headers, "x-proxi-task": "compaction"})
    assert status == 200 and "function_call" in kinds(payload)
    relay.settle(answer)
    assert count(relay, mark, "turn.compaction_tool_call") == 0 and count(relay, mark, "codex.compaction_tools_restored") == 0


# -- configuration and unit behaviour -----------------------------------------------------------------------------------

def test_the_default_serves_the_cache_and_a_wrong_value_stops_the_relay(tmp_path, monkeypatch):
    import apierr
    from test_client_adapt_config import load_with
    assert config.DEFAULTS["codex_compaction_cache"] is True
    with pytest.raises(apierr.ApiError, match="codex_compaction_cache must be a boolean"):
        load_with(tmp_path, monkeypatch, codex_compaction_cache="no")
    assert "codex_compaction_cache" in admin.LIVE_SETTINGS and "codex_compaction_cache" in admin._schema()


def test_chat_tools_are_kept_per_conversation_and_model_and_expire():
    clock = [1000.0]
    chats = compaction_tools.ChatTools(max_chats=2, ttl=100, clock=lambda: clock[0])
    tools = [{"type": "function", "name": "a"}]
    chats.remember("c1", "m", tools)
    got = chats.recall("c1", "m")
    assert got == tools and got is not tools  # a copy: the compaction may be parsed and edited
    assert chats.recall("c1", "other") is None and chats.recall("c2", "m") is None and chats.recall(None, "m") is None
    chats.remember("c1", "m", [])  # a request without tools never replaces the tools of the chat
    chats.remember("c1", "m", None)
    assert chats.recall("c1", "m") == tools
    chats.remember("c2", "m", tools)
    chats.remember("c3", "m", tools)  # the least recently used goes
    assert chats.recall("c1", "m") is None and len(chats) == 2
    clock[0] += 101
    assert chats.recall("c2", "m") is None and len(chats) == 1
