"""R3-02 end to end: a conversation with a prompt profile, a prompts edit, a restart, and the next turn.

The stored conversation state (C1) holds a signed branch whose scope contains the head of the profile it was started under.
A restart that gives the conversation another head (the edited file) makes the branch useless and costs a full prefix write.
These tests run the restart through the stand of test_conversation_state (accounts, a pool with a store, a runner, a scripted
wire) and through the real relay over HTTP, and compare the next request with the one a relay that never stopped sends.
"""
import asyncio
import copy
import json
import shutil

import pytest

import journal
import prompt_policy as pp
from prompt_support import prompt_relay  # noqa: F401
from test_compaction_cache import OPENCODE, Chat, count, thinking
from test_conversation_state import DONE, processes, signature, signatures_in, stored, tool_scripts  # noqa: F401
from test_native_context_reuse import FIRST
from test_native_history import append_result, parsed, tool_reply


def write_prompts(path, rules, **extra):
    document = {"version": 1, "default_profile": "mine", "texts": {"rules": rules},
                "profiles": {"mine": {"head": "{{client_system}}\n\n{{text:rules}}"}}, "rules": [], **extra}
    path.write_text(json.dumps(document), encoding="utf-8")


def head_sent(sent):
    return "\n".join(block["text"] for block in sent.body["messages"][0]["content"])


def prompt_store(tmp_path, state, **cfg):
    settings = {"prompts_file": str(tmp_path / "prompts.json")}
    if state is not None:
        settings["conversation_state_dir"] = str(state)
    return pp.PromptStore({**settings, **cfg})


async def ask(process, store, messages, dialect):
    """One request of the client: the prompt store plans it first, as the HTTP handler does, then the runner serves it."""
    request = parsed(messages, dialect)
    request.request_purpose = "client-tools"
    request.prompt_plan = store.plan(pp.PlanInput.from_request(request, {}, {}, dialect))
    events = await process.turn(request)
    assert not any(event["type"] == "error" for event in events), events
    return request, events


async def work(process, store, dialect, rounds):
    """`rounds` tool round trips of a client; returns its messages, ready for the next request."""
    messages = copy.deepcopy(FIRST)
    for number in range(rounds):
        request, events = await ask(process, store, messages, dialect)
        messages = append_result(messages, request, events, dialect, value="RESULT-%d" % number)
    return messages


def started_dialog(tmp_path, monkeypatch, processes, dialect, rounds=3):
    """A relay on prompts V1 that ran `rounds` tool rounds, then the prompts became V2 and it stopped; returns what a restart
    needs: (process that never stopped, its store, the client's messages, state directory it left behind, journal mark)."""
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    write_prompts(tmp_path / "prompts.json", "RULES-V1")
    monkeypatch.setattr(pp, "RELOAD_INTERVAL", 0.0)
    before = processes(state, scripts=tool_scripts(rounds) + [DONE])
    store = prompt_store(tmp_path, state)
    seq = journal.JOURNAL.last_seq
    messages = asyncio.run(work(before, store, dialect, rounds))
    assert "RULES-V1" in head_sent(before.sent[-1])
    write_prompts(tmp_path / "prompts.json", "RULES-V2")           # the operator edits while the dialog is going on
    store.maybe_reload()
    assert store.snapshot().rev == 2
    store.close()                                                  # the relay stops: the runtime closes the store
    shutil.copytree(state, copied)
    return before, store, messages, copied, seq


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_the_turn_after_an_edit_and_a_restart_continues_the_signed_branch_on_the_head_it_began_with(
        tmp_path, monkeypatch, processes, dialect):
    before, store, messages, copied, seq = started_dialog(tmp_path, monkeypatch, processes, dialect)
    asyncio.run(ask(before, store, messages, dialect))             # what a relay that never stopped sends
    control = before.sent[-1]
    assert "RULES-V1" in head_sent(control) and "RULES-V2" not in head_sent(control)

    after = processes(copied, scripts=[DONE])
    restarted_store = prompt_store(tmp_path, copied)
    assert restarted_store.snapshot().document["texts"]["rules"] == "RULES-V2"        # the file is V2 now
    asyncio.run(ask(after, restarted_store, messages, dialect))
    restarted = after.sent[0]
    assert restarted.data == control.data                          # byte for byte what the relay that never stopped sent
    assert "RULES-V1" in head_sent(restarted) and "RULES-V2" not in head_sent(restarted)
    assert signatures_in(restarted.body) == [signature(n) for n in range(3)]
    assert restarted.body["rit"] == control.body["rit"] and restarted.body["openAICacheKey"] == control.body["openAICacheKey"]
    kinds = [event["kind"] for event in stored(seq, "conversation_state.restored", "native_history.resume",
                                               "native_history.cold", "native_history.reset", "prompt.pin_restored")]
    assert kinds.count("conversation_state.restored") == 1 and kinds.count("prompt.pin_restored") == 1
    assert kinds.count("native_history.reset") == 0 and kinds.count("native_history.cold") == 0


def test_without_the_pins_the_same_restart_loses_every_signature_which_is_what_the_pins_are_for(
        tmp_path, monkeypatch, processes):
    before, store, messages, copied, seq = started_dialog(tmp_path, monkeypatch, processes, "chat")
    asyncio.run(ask(before, store, messages, "chat"))
    control = before.sent[-1]
    after = processes(copied, scripts=[DONE])
    forgetful = prompt_store(tmp_path, None)                       # a store that keeps its pins in memory only
    asyncio.run(ask(after, forgetful, messages, "chat"))
    restarted = after.sent[0]
    assert "RULES-V2" in head_sent(restarted) and signatures_in(restarted.body) == []
    assert restarted.data != control.data
    assert stored(seq, "native_history.cold") and not stored(seq, "prompt.pin_restored")


def test_a_new_conversation_after_the_restart_gets_the_edited_prompts_while_the_old_one_keeps_its_own(
        tmp_path, monkeypatch, processes):
    before, store, messages, copied, seq = started_dialog(tmp_path, monkeypatch, processes, "chat")
    after = processes(copied, scripts=[DONE, DONE])
    restarted_store = prompt_store(tmp_path, copied)

    async def both():
        await ask(after, restarted_store, messages, "chat")
        request = parsed(copy.deepcopy(FIRST), "chat", key="header:another-client")
        request.request_purpose = "client-tools"
        request.prompt_plan = restarted_store.plan(pp.PlanInput.from_request(request, {}, {}, "chat"))
        await after.turn(request)

    asyncio.run(both())
    old, new = after.sent
    assert "RULES-V1" in head_sent(old) and "RULES-V2" in head_sent(new)
    assert not restarted_store.plan(pp.PlanInput.from_request(parsed(messages, "chat"), {}, {}, "chat")).legacy


def test_applying_to_all_after_the_restart_gives_the_dialog_the_edited_head_and_costs_its_signatures(
        tmp_path, monkeypatch, processes):
    before, store, messages, copied, seq = started_dialog(tmp_path, monkeypatch, processes, "chat")
    after = processes(copied, scripts=[DONE])
    restarted_store = prompt_store(tmp_path, copied)
    assert restarted_store.unpin(None) == 1                         # the admin's "apply to all", before the dialog came back
    asyncio.run(ask(after, restarted_store, messages, "chat"))
    restarted = after.sent[0]
    assert "RULES-V2" in head_sent(restarted) and signatures_in(restarted.body) == []


# -- the whole relay over HTTP: the scenario of the reviewer ----------------------------------------------------------------


def write_dialog_prompts(tmp_path, rules):
    write_prompts(tmp_path / "prompts.json", rules)


def stop(relay, *, close_prompts):
    """What stops a relay: the server, then the runtime. `close_prompts` closes the prompt store first, as the runtime does
    when it is wired to (a test that wants a relay that was killed stubs PromptStore.close out instead)."""
    if close_prompts:
        relay.runtime.prompts.close()
    relay.server.shutdown()
    relay.server.server_close()
    relay.runtime.close()


@pytest.mark.parametrize("how", ["no_restart", "stopped_after_closing_the_prompts", "killed_without_closing_anything"])
def test_edit_prompts_then_restart_over_http(prompt_relay, tmp_path, monkeypatch, how):
    state = tmp_path / "state" / "conversations"
    write_dialog_prompts(tmp_path, "RULES-V1")
    if how == "killed_without_closing_anything":
        # Whatever the runtime does when it stops, a killed relay does none of it: what the second request wrote is all there is.
        monkeypatch.setattr(pp.PromptStore, "close", lambda self: None)
    relay = prompt_relay(conversation_state_dir=str(state))
    chat = Chat(relay)
    bodies = chat.work(2)
    assert "RULES-V1" in json.dumps(bodies[0]["messages"][0])
    write_dialog_prompts(tmp_path, "RULES-V2")                      # the operator edits the prompts while the dialog is running
    if how == "no_restart":
        monkeypatch.setattr(pp, "RELOAD_INTERVAL", 0.0)
    else:
        stop(relay, close_prompts=how == "stopped_after_closing_the_prompts")
        relay = prompt_relay(conversation_state_dir=str(state))
        chat.relay = relay
    mark = relay.mark()
    body = chat.ask(tool_reply("c3", "opaque-3"))
    head = json.dumps(body["messages"][0])
    assert "RULES-V1" in head and "RULES-V2" not in head
    assert thinking(body) == ["opaque-1", "opaque-2"]
    assert count(relay, mark, "native_history.resume") == 1
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "native_history.cold") == 0
    assert count(relay, mark, "cache.head_changed") == 0
    # a dialog that starts now is on the edited prompts
    other = Chat(relay, headers={**OPENCODE, "x-session-affinity": "ses_new"})
    fresh = other.ask(tool_reply("n1", "opaque-n1"))
    assert "RULES-V2" in json.dumps(fresh["messages"][0])
    relay.runtime.prompts.unpin(None)
    again = chat.ask(tool_reply("c4", "opaque-4"))                  # apply to all: the old dialog moves, once
    assert "RULES-V2" in json.dumps(again["messages"][0])
    assert relay.runtime.prompts.pins()[0]["rev"] == relay.runtime.prompts.snapshot().rev


def test_the_pins_of_a_relay_that_was_killed_at_the_second_request_are_those_that_request_found_written(
        prompt_relay, tmp_path):
    state = tmp_path / "state" / "conversations"
    write_dialog_prompts(tmp_path, "RULES-V1")
    relay = prompt_relay(conversation_state_dir=str(state))
    chat = Chat(relay)
    chat.work(2)
    stored_pins = json.loads((state / pp.PINS_DIR / pp.PINS_FILE).read_text(encoding="utf-8"))
    assert len(stored_pins["pins"]) == 1                            # written by the look the second request made
    assert not any(("RULES-V1" in path.read_text(encoding="utf-8")) for path in (state / pp.PINS_DIR).glob("*.json"))
    assert len(list((state / pp.PINS_DIR / pp.REVISIONS_DIR).glob("*.json"))) == 1


def test_a_relay_without_a_state_directory_leaves_no_pins_behind(prompt_relay, tmp_path):
    write_dialog_prompts(tmp_path, "RULES-V1")
    relay = prompt_relay()
    Chat(relay).work(2)
    relay.runtime.prompts.close()
    assert not list(tmp_path.rglob(pp.PINS_DIR))
