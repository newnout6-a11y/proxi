"""A compaction is what Codex says it is: x-codex-turn-metadata names the request, whatever its prompt says (v8, rust-v0.167.0).

codex-rs core/src/responses_metadata.rs sets request_kind "compaction" and {trigger, reason, implementation, phase} on every
compaction request, in the header and in client_metadata. The relay knew a compaction by the opening of the template's text
("You are performing a CONTEXT CHECKPOINT COMPACTION."), so a compact_prompt of the user's own was an ordinary generation: it was
kept warm after the summary replaced its history, and its journal did not say what it was."""
import json

from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
import client_context
from test_compaction_cache import SYSTEM, count
from test_review_fixes_pr19 import END, text

EXEC = {"originator": "codex_exec",
        "user-agent": "codex_exec/0.167.0 (Windows 10.0.26300; x86_64) xterm-256color (codex_exec; 0.167.0)"}
SUMMARY = [text("## Progress\n- the work is half done"), *END]


def turn_metadata(kind, phase="mid_turn"):
    """x-codex-turn-metadata as codex-rs 0.167 writes it (the header is ASCII JSON; the tool inventory is not in it)."""
    meta = {"session_id": "label-chat", "thread_id": "label-chat", "request_kind": kind, "turn_trigger": "exec"}
    if kind == "compaction":
        meta["compaction"] = {"trigger": "auto", "reason": "context_limit", "implementation": "responses", "phase": phase,
                              "strategy": "memento"}
    return json.dumps(meta, separators=(",", ":"))


def test_codex_turn_metadata_names_a_compaction_and_junk_names_nothing():
    cfg = {"client_metadata_home": "."}
    headers = {"x-codex-turn-metadata": turn_metadata("compaction", "pre_turn")}
    info = client_context.describe({}, headers, cfg)
    assert info["codex_task"] == "compaction" and info["compaction_phase"] == "pre_turn" and info["compaction_trigger"] == "auto"
    assert client_context.client_task(None, info) == "compaction"
    for header in (turn_metadata("turn"), turn_metadata("memory"), "{", "[]", '"compaction"', "x" * 20000,
                   json.dumps({"request_kind": "compaction" + "x" * 20000})):
        info = client_context.describe({}, {"x-codex-turn-metadata": header}, cfg)
        assert "codex_task" not in info, header[:40]
    info = client_context.describe({}, {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction",
                                                                              "compaction": {"phase": "Bad Phase!"}})}, cfg)
    assert info["codex_task"] == "compaction" and "compaction_phase" not in info


def post(relay, kind, prompt, session="label-chat"):
    headers = {**EXEC, "session-id": session, "thread-id": session, "x-codex-turn-metadata": turn_metadata(kind)}
    relay.scripts.append(SUMMARY)
    status, payload, answer = relay.post("responses", client_request(
        "responses", [{"role": "user", "content": "the work so far"}, {"role": "user", "content": prompt}], system=SYSTEM,
        tools=False, model="claude-sonnet-5-5", reasoning={"effort": "high"}), headers)
    assert status == 200, payload
    relay.settle(answer)


def test_a_compaction_with_a_prompt_of_the_users_own_is_a_compaction_and_is_not_kept_warm(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    mark = relay.mark()
    post(relay, "compaction", "Write the notes the next shift needs.")
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert begin["client_task"] == "compaction" and begin["compaction_phase"] == "mid_turn"
    assert begin["request_purpose"] == "generation"
    session = relay.runtime.pool._conversations[("header:label-chat", "text")]
    assert session._warm_blocked == "compacted" and session._warm_body is None


def test_the_same_request_that_codex_calls_a_turn_is_an_ordinary_generation(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    mark = relay.mark()
    post(relay, "turn", "Write the notes the next shift needs.")
    begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert "client_task" not in begin and "compaction_phase" not in begin
    session = relay.runtime.pool._conversations[("header:label-chat", "text")]
    assert session._warm_blocked is None and session._warm_body is not None
    assert count(relay, mark, "codex.compaction_tools_unknown") == 0
