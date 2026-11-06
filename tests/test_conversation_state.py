"""A conversation survives a relay restart: signed history, cache marks and account come back from disk.

A "process" here is what a relay run is for this feature: accounts, a pool with a store on a state directory,
a runner and a scripted wire (`direct.urllib.request.urlopen`). A restart is a new process on the same (or a
copied) state directory; the request after it is compared byte for byte with the one a process that never
stopped sends.
"""
import asyncio
import base64
import copy
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import conversation_state as cs
import adapters
import direct
import journal
import native_history
from orchestrator import TurnRunner
from session import SessionPool
from test_native_context_reuse import FIRST, Backend, run
from test_native_history import append_result, parsed, tool_reply
from test_review_fixes_pr19 import END, THINKING, response, text
from test_review_fixes_pr19 import signature as signature_event

KEY = "header:client-a"
DONE = [text("done"), *END]
TWO = ("acc-one", "acc-two")
BIASED = {"acc-one": 5, "acc-two": 0}  # a new conversation goes to acc-two
OPPOSITE = {"acc-one": 0, "acc-two": 5}  # the restarted pool would pick acc-one by itself


def signature(number):
    return "SIG-%d-%s" % (number, "x" * 48)


def tool_scripts(rounds):
    return [tool_reply("call-%d" % n, signature(n), path="file-%d" % n) for n in range(rounds)]


class Process:
    """Accounts, a pool with a store on `state`, a runner and a wire that records every request."""

    def __init__(self, tmp_path, monkeypatch, state, *, name="relay", accounts=("acc-one",), priority=None,
                 scripts=(), store=True, backend_options=None, **store_options):
        self.monkeypatch = monkeypatch
        self.sent, self.scripts = [], list(scripts)
        self.backends = []
        for index, account in enumerate(accounts):
            work = tmp_path / ("%s-%d" % (name, index))
            work.mkdir(exist_ok=True)
            options = dict(transport="direct", cache_keepwarm_seconds=0, base="https://fixture.invalid",
                           cookie_values=[], project_context=False, reasoning_effort="high",
                           account_id=account, priority=(priority or {}).get(account, 0),
                           upstream_error_dir=str(tmp_path / "upstream-diagnostics"))
            options.update(backend_options or {})
            backend = Backend(work, **options)
            backend.headers = lambda account=account: {"x-account": account}
            self.backends.append(backend)
        self.store = cs.ConversationStore(state, **store_options) if store else None
        self.pool = SessionPool(self.backends[0], store=self.store)
        self.pool.backends.extend(self.backends[1:])
        self.runner = TurnRunner(self.backends[0].cfg, SimpleNamespace(record_turn=lambda *_: None), self.pool)

    def wire(self, upstream_request, **_kw):
        self.sent.append(SimpleNamespace(account=upstream_request.get_header("X-account"),
                                         data=upstream_request.data, body=json.loads(upstream_request.data)))
        index = len(self.sent) - 1
        item = self.scripts[index] if index < len(self.scripts) else DONE
        if isinstance(item, Exception):
            raise item
        return response(*item)

    async def turn(self, client_request):
        """One client request through the runner, then wait until the writer has stored what it changed."""
        self.monkeypatch.setattr(direct.urllib.request, "urlopen", self.wire)
        events = await run(self.runner, client_request)
        if self.store is not None:
            assert self.store.flush(10)
        return events

    def stop(self):
        if self.store is not None:
            self.store.close()


@pytest.fixture
def processes(tmp_path, monkeypatch):
    made = []

    def make(state, **options):
        process = Process(tmp_path, monkeypatch, state, name="relay%d" % len(made), **options)
        made.append(process)
        return process

    yield make
    for process in made:
        process.stop()


async def converse(process, dialect, rounds, key=KEY, system="CLIENT_SYSTEM", first=FIRST, result="RESULT-%d"):
    """`rounds` tool round trips of a client; returns its messages, ready for the next request."""
    messages = copy.deepcopy(first)
    for number in range(rounds):
        client_request = parsed(messages, dialect, key=key, system=system)
        events = await process.turn(client_request)
        assert not any(event["type"] == "error" for event in events), events
        messages = append_result(messages, client_request, events, dialect, value=result % number)
    return messages


def stored(since, *kinds):
    """Journal events of the given kinds written after `since`."""
    return [event for event in journal.JOURNAL.query(since=since, limit=20000) if event["kind"] in kinds]


def signatures_in(body):
    return [block["signature"] for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if isinstance(block, dict) and block.get("type") == "thinking"]


def state_file(state, key=KEY):
    return state / (cs.key_digest(key) + cs.SUFFIX)


# -- the restart --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_the_next_turn_after_a_restart_is_byte_identical_to_one_without_a_restart(
        tmp_path, processes, dialect):
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    before = processes(state, scripts=tool_scripts(4) + [DONE])
    seq = journal.JOURNAL.last_seq

    async def never_stopped():
        messages = await converse(before, dialect, 4)
        shutil.copytree(state, copied)
        await before.turn(parsed(messages, dialect))
        return messages

    messages = asyncio.run(never_stopped())
    after = processes(copied, scripts=[DONE])
    asyncio.run(after.turn(parsed(messages, dialect)))
    bare = processes(tmp_path / "no-state", scripts=[DONE])  # a restart that remembers nothing
    asyncio.run(bare.turn(parsed(messages, dialect)))

    control, restarted = before.sent[-1], after.sent[0]
    assert restarted.data == control.data
    assert restarted.account == control.account == "acc-one"
    assert signatures_in(restarted.body) == [signature(n) for n in range(4)]
    assert restarted.body["rit"] == control.body["rit"]
    assert restarted.body["openAICacheKey"] == control.body["openAICacheKey"]
    # What the test is about: without the stored state the same request loses every signature and its prefix.
    assert signatures_in(bare.sent[0].body) == [] and bare.sent[0].data != control.data
    kinds = [event["kind"] for event in stored(seq, "conversation_state.restored", "native_history.resume",
                                               "native_history.cold", "native_history.reset")]
    assert kinds.count("conversation_state.restored") == 1
    assert kinds.count("native_history.cold") == 1  # the process that remembered nothing, and only it
    assert kinds.count("native_history.reset") == 0


def rich_reply(number):
    """A reply the way Claude 5.5 sends one in a real tool loop: reasoning, a redacted block, text, parallel calls."""
    return [*THINKING, signature_event("RICH-SIG-%d-%s" % (number, "y" * 40)),
            {"event": "content_block_end", "type": "thinking"},
            {"event": "content_block_start", "type": "redacted_thinking", "data": "REDACTED-%d==" % number,
             "responseItemId": "item-%d" % number},
            text("Reading two files, number %d. " % number),
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "pa-%d" % number,
             "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path": "a", "limit": "3"'},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": "}"},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": "pa-%d" % number},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": "pb-%d" % number,
             "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path": "Файл б", "limit": 2.5}'},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": "pb-%d" % number}, *END]


def with_results(messages, request, events, dialect):
    """The client's next messages: the assistant message as the relay produced it and a result for every call."""
    adapter = {"chat": adapters.ChatAdapter, "anthropic": adapters.AnthropicAdapter,
               "responses": adapters.ResponsesAdapter}[dialect](request, None)
    for event in events:
        adapter.feed(event)
    payload = adapter.payload("tool_calls")
    if dialect == "chat":
        assistant = payload["choices"][0]["message"]
        return messages + [assistant] + [
            {"role": "tool", "tool_call_id": call["id"], "content": "RESULT of " + call["id"]}
            for call in assistant["tool_calls"]]
    if dialect == "anthropic":
        calls = [block for block in payload["content"] if block["type"] == "tool_use"]
        return messages + [{"role": "assistant", "content": payload["content"]},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                                         "content": "RESULT of " + call["id"]} for call in calls]}]
    calls = [item for item in payload["output"] if item["type"] == "function_call"]
    return messages + payload["output"] + [{"type": "function_call_output", "call_id": call["call_id"],
                                            "output": "RESULT of " + call["call_id"]} for call in calls]


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_replies_with_parallel_calls_redacted_thinking_and_text_survive_a_restart_byte_for_byte(
        tmp_path, processes, dialect):
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    before = processes(state, scripts=[rich_reply(0), rich_reply(1), rich_reply(2), DONE])

    async def never_stopped():
        messages = copy.deepcopy(FIRST)
        for _ in range(3):
            request = parsed(messages, dialect)
            events = await before.turn(request)
            assert not any(event["type"] == "error" for event in events), events
            messages = with_results(messages, request, events, dialect)
        shutil.copytree(state, copied)
        await before.turn(parsed(messages, dialect))
        return messages

    messages = asyncio.run(never_stopped())
    after = processes(copied, scripts=[DONE])
    asyncio.run(after.turn(parsed(messages, dialect)))
    control, restarted = before.sent[-1], after.sent[0]
    assert restarted.data == control.data
    body = json.dumps(restarted.body)
    assert all("RICH-SIG-%d-" % n in body and "REDACTED-%d==" % n in body for n in range(3))
    assert body.count("RESULT of") == 6  # two calls answered in each of the three rounds
    assert "Файл б" in json.dumps(restarted.body, ensure_ascii=False)


@pytest.mark.parametrize("dialect", ["chat", "anthropic"])
def test_a_restart_before_any_turn_of_a_tool_loop_sends_what_the_running_relay_sends(tmp_path, processes, dialect):
    state = tmp_path / "state"
    rounds = 8
    before = processes(state, scripts=[rich_reply(n) if n % 2 else tool_reply("call-%d" % n, signature(n))
                                       for n in range(rounds)] + [DONE])
    asked = []  # the client's messages of every turn

    async def never_stopped():
        messages = copy.deepcopy(FIRST)
        for number in range(rounds + 1):
            if number:
                shutil.copytree(state, tmp_path / ("before-turn-%d" % number))  # the files a restart would find
            asked.append(copy.deepcopy(messages))
            request = parsed(messages, dialect)
            events = await before.turn(request)
            assert not any(event["type"] == "error" for event in events), events
            if number < rounds:
                messages = (with_results(messages, request, events, dialect) if number % 2 else
                            append_result(messages, request, events, dialect, value="RESULT-%d" % number))

    asyncio.run(never_stopped())
    for number in range(1, rounds + 1):
        after = processes(tmp_path / ("before-turn-%d" % number), scripts=[DONE])
        asyncio.run(after.turn(parsed(asked[number], dialect)))
        assert after.sent[0].data == before.sent[number].data, "restart before turn %d" % number
        assert signatures_in(after.sent[0].body), number


def test_several_restarts_in_a_row_keep_the_chain_of_signed_turns(tmp_path, processes):
    state = tmp_path / "state"
    messages = copy.deepcopy(FIRST)
    for round_number in range(4):
        process = processes(state, scripts=[tool_reply("call-%d" % round_number, signature(round_number))])

        async def one_turn(process=process):
            client_request = parsed(messages)
            events = await process.turn(client_request)
            return append_result(messages, client_request, events, "chat", value="RESULT-%d" % round_number)

        messages = asyncio.run(one_turn())
        process.stop()
        assert signatures_in(process.sent[0].body) == [signature(n) for n in range(round_number)]
    last = processes(state, scripts=[DONE])
    asyncio.run(last.turn(parsed(messages)))
    assert signatures_in(last.sent[0].body) == [signature(n) for n in range(4)]


def test_a_conversation_evicted_for_idleness_comes_back_with_its_signed_history(tmp_path, processes):
    process = processes(tmp_path / "state", scripts=tool_scripts(2) + [DONE])

    async def story():
        messages = await converse(process, "chat", 2)
        process.pool._last_used[KEY] -= 10_000
        await process.pool._evict_idle()
        assert KEY not in process.pool._conversations
        await process.turn(parsed(messages))

    asyncio.run(story())
    assert signatures_in(process.sent[-1].body) == [signature(0), signature(1)]


def test_a_restored_conversation_is_not_stored_again_until_it_moves(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[DONE])
    seq = journal.JOURNAL.last_seq
    first_write = state_file(state).stat().st_mtime_ns

    async def idle():
        lease = await after.pool.acquire(KEY, model_entry=parsed(messages).entry)
        await after.pool.release(lease)

    asyncio.run(idle())
    assert after.store.flush(5)
    assert not stored(seq, "conversation_state.saved") and state_file(state).stat().st_mtime_ns == first_write
    asyncio.run(after.turn(parsed(messages)))
    assert len(stored(seq, "conversation_state.saved")) == 1


# -- the account ------------------------------------------------------------------------------------------------
def test_a_conversation_returns_to_its_own_account_after_a_restart(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, accounts=TWO, priority=BIASED, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    assert {call.account for call in before.sent} == {"acc-two"}
    before.stop()
    # Without the stored binding the restarted pool chooses by priority, which now favours the other account.
    bare = processes(tmp_path / "no-state", accounts=TWO, priority=OPPOSITE, scripts=[DONE])
    asyncio.run(bare.turn(parsed(messages)))
    assert bare.sent[0].account == "acc-one"
    after = processes(state, accounts=TWO, priority=OPPOSITE, scripts=[DONE])
    asyncio.run(after.turn(parsed(messages)))
    assert after.sent[0].account == "acc-two"
    assert signatures_in(after.sent[0].body) == [signature(0), signature(1)]


UNAVAILABLE = {
    "exhausted": ("account_unavailable",
                  lambda b: setattr(b, "unavailable", {"reason": "exhausted", "available": 0, "threshold": 20})),
    "auth_dead": ("account_unavailable", lambda b: setattr(b, "unavailable", {"reason": "auth_dead"})),
    "rate_limited": ("account_unavailable",
                     lambda b: setattr(b, "unavailable", {"reason": "rate_limited", "until": 0, "until_at": 0})),
    "disabled": ("account_disabled", lambda b: b.cfg.update(enabled=False)),
    "no_route": ("no_route", lambda b: b.cfg.update(provider_resource_name="")),
    "removed": ("account_missing", None),
}


@pytest.mark.parametrize("why", list(UNAVAILABLE))
def test_an_account_that_cannot_serve_after_a_restart_takes_no_signature_to_another_account(
        tmp_path, processes, why):
    reason, make_unavailable = UNAVAILABLE[why]
    state = tmp_path / "state"
    before = processes(state, accounts=TWO, priority=BIASED, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    seq = journal.JOURNAL.last_seq
    after = processes(state, accounts=("acc-one",) if why == "removed" else TWO, priority=OPPOSITE, scripts=[DONE])
    if make_unavailable is not None:
        make_unavailable(after.backends[1])
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1] == {"type": "end", "reason": "stop"}
    sent = after.sent[0]
    assert sent.account == "acc-one"
    assert signatures_in(sent.body) == [] and "SIG-" not in json.dumps(sent.body)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.skipped")] == [reason]
    assert not stored(seq, "conversation_state.restored")
    # The turn on the new account stored its own state: nothing of the other account's remains in the file.
    assert after.store.peek(KEY).account == "acc-one"
    _, state_of_new_account = cs.decode(state_file(state).read_bytes(), 10 ** 9)
    assert "SIG-" not in json.dumps([branch.messages + branch.assistant for branch in state_of_new_account.branches])


def test_a_state_is_never_applied_to_a_session_of_another_account(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, accounts=TWO, priority=BIASED, scripts=tool_scripts(1))
    asyncio.run(converse(before, "chat", 1))
    before.stop()
    after = processes(state, accounts=TWO, scripts=[DONE])
    session = SimpleNamespace(backend=after.backends[0], _native_history=native_history.History(),
                              _cached_turn_ends=[], _last_cache_key=None)
    seq = journal.JOURNAL.last_seq
    assert asyncio.run(cs.restore(after.store, KEY, session)) is False
    assert session._native_history.branches == [] and session._cached_turn_ends == []
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.skipped")] == ["account_changed"]
    assert state_file(state).exists()


def test_a_failover_retry_never_carries_the_restored_state_to_the_next_account(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, accounts=TWO, priority=BIASED, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, accounts=TWO, priority=OPPOSITE, scripts=[DONE])

    async def story():
        # The request restores the conversation on acc-two, then that account is excluded (a failover retry).
        first = await after.pool.acquire(KEY, model_entry=parsed(messages).entry)
        assert first.backend.cfg["account_id"] == "acc-two" and first.session._native_history.branches
        await after.pool.release(first)
        moved = await after.pool.acquire(KEY, model_entry=parsed(messages).entry, exclude=(after.backends[1],))
        assert moved.backend.cfg["account_id"] == "acc-one"
        assert moved.session._native_history.branches == [] and moved.session._cached_turn_ends == []
        await after.pool.release(moved)

    asyncio.run(story())


# -- files that cannot be used --------------------------------------------------------------------------------------
def split(blob):
    end = blob.index(b"\n", len(cs.MAGIC))
    return blob[:end + 1], blob[end + 1:]


def with_payload(blob, payload):
    return split(blob)[0] + payload


CORRUPTIONS = {
    "truncated": (lambda blob: blob[:len(blob) // 2], "corrupt"),
    "empty": (lambda blob: b"", "corrupt"),
    "cut_inside_the_signature": (lambda blob: blob[:10], "corrupt"),
    "garbage": (lambda blob: bytes(range(256)) * 4, None),  # no signature of ours: not ours to judge or remove
    "foreign_version": (lambda blob: blob.replace(b'"format":1', b'"format":2', 1), "format"),
    "foreign_blocks": (lambda blob: blob.replace(cs.STAMP.encode(), b"f" * len(cs.STAMP), 1), "format"),
    "no_stamp": (lambda blob: blob.replace(b'"blocks":"' + cs.STAMP.encode() + b'",', b"", 1), "format"),
    "header_not_json": (lambda blob: cs.MAGIC + b"{not json\n" + split(blob)[1], "corrupt"),
    "payload_not_zlib": (lambda blob: with_payload(blob, b"this is not a zlib stream"), "corrupt"),
    "payload_wrong_shape": (lambda blob: with_payload(blob, zlib.compress(b'{"branches": "no"}')), "corrupt"),
    "payload_not_json": (lambda blob: with_payload(blob, zlib.compress(b"[1, 2")), "corrupt"),
    "trailing_bytes": (lambda blob: blob + b"junk", "corrupt"),
}


@pytest.mark.parametrize("damage", list(CORRUPTIONS))
def test_a_damaged_or_foreign_file_is_ignored_and_removed_and_the_conversation_goes_on(tmp_path, processes, damage):
    mutate, reason = CORRUPTIONS[damage]
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    path = state_file(state)
    damaged = mutate(path.read_bytes())
    assert damaged != path.read_bytes()
    path.write_bytes(damaged)
    seq = journal.JOURNAL.last_seq
    after = processes(state, scripts=[DONE])
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1] == {"type": "end", "reason": "stop"} and not any(e["type"] == "error" for e in events)
    assert "SIG-" not in json.dumps(after.sent[0].body)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == (
        [] if reason is None else [reason])
    assert not stored(seq, "conversation_state.restored")
    # The damaged bytes are gone: the turn that followed stored a good state under the same name.
    assert path.read_bytes() != damaged
    cs.decode(path.read_bytes(), 10 ** 9)


def test_a_file_that_belongs_to_another_conversation_is_not_applied(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(1) + tool_scripts(1))
    asyncio.run(converse(before, "chat", 1, key="header:a"))
    messages_b = asyncio.run(converse(before, "chat", 1, key="header:b"))
    before.stop()
    shutil.copyfile(state_file(state, "header:a"), state_file(state, "header:b"))
    seq = journal.JOURNAL.last_seq
    after = processes(state, scripts=[DONE])
    asyncio.run(after.turn(parsed(messages_b, key="header:b")))
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == ["key_mismatch"]
    assert "SIG-" not in json.dumps(after.sent[0].body)


def refused(status=400, detail=b'{"message": "Invalid signature in thinking block"}'):
    return urllib.error.HTTPError("https://fixture.invalid", status, "refused", {}, io.BytesIO(detail))


def test_a_restored_branch_that_upstream_refuses_is_forgotten_and_the_retry_goes_the_unsigned_way(
        tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[refused(), DONE])
    seq = journal.JOURNAL.last_seq

    async def story():
        first = await after.turn(parsed(messages))
        branches = after.pool._conversations[KEY]._native_history.branches
        assert first[-1]["type"] == "error" and branches == []  # not replayed again, nor kept
        header, _ = cs.decode(state_file(state).read_bytes(), 10 ** 9)
        assert header["branches"] == 0  # the stored copy follows: no refused branch is left on disk
        retry = await after.turn(parsed(messages))
        assert retry[-1] == {"type": "end", "reason": "stop"}
        assert [branch.restored for branch in branches] == []
        assert [branch.restored for branch in after.pool._conversations[KEY]._native_history.branches] == [False]

    asyncio.run(story())
    assert signatures_in(after.sent[0].body) == [signature(0), signature(1)]  # the refused attempt did replay them
    assert "SIG-" not in json.dumps(after.sent[1].body)  # the retry did not
    dropped = stored(seq, "conversation_state.dropped")
    assert [(e["data"]["reason"], e["data"]["upstream_status"]) for e in dropped if e["data"]["reason"] == "rejected"] \
        == [("rejected", 400)]
    assert [event["kind"] for event in stored(seq, "native_history.cold")] == ["native_history.cold"]


def test_forgetting_a_refused_branch_can_never_replace_the_refusal_the_client_gets(tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[refused()])
    monkeypatch.setattr(cs, "account_tag", lambda *_args: (_ for _ in ()).throw(RuntimeError("a bug in the hook")))
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1]["type"] == "error" and events[-1]["error"].status == 400  # the refusal, not an internal error


def test_a_branch_recorded_live_is_still_kept_when_upstream_refuses_a_request_built_from_it(tmp_path, processes):
    process = processes(tmp_path / "state", scripts=[*tool_scripts(2), refused()])
    seq = journal.JOURNAL.last_seq

    async def story():
        messages = await converse(process, "chat", 2)
        events = await process.turn(parsed(messages))
        assert events[-1]["type"] == "error"
        return process.pool._conversations[KEY]._native_history.branches

    branches = asyncio.run(story())
    assert len(branches) == 1 and not branches[0].restored
    assert not [e for e in stored(seq, "conversation_state.dropped") if e["data"]["reason"] == "rejected"]


SIGNATURE_REFUSAL = b'{"message": "messages.1.content.0: Invalid `signature` in `thinking` block"}'


@pytest.mark.parametrize("status,forgotten", [(400, True), (422, True), (408, False), (413, False), (429, False),
                                              (500, False), (503, False)])
def test_only_a_refusal_of_the_request_itself_discards_a_restored_branch(tmp_path, processes, status, forgotten):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    # Every status carries the same words about the blocks: here the status decides (the words, in
    # test_conversation_state_review).
    after = processes(state, scripts=[refused(status, SIGNATURE_REFUSAL)])
    seq = journal.JOURNAL.last_seq
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1]["type"] == "error"
    assert [branch.restored for branch in after.pool._conversations[KEY]._native_history.branches] == (
        [] if forgotten else [True])
    dropped = [e for e in stored(seq, "conversation_state.dropped") if e["data"]["reason"] == "rejected"]
    assert bool(dropped) is forgotten
    if forgotten:  # the status upstream sent, not the 400 the relay answers its client with for both
        assert dropped[0]["data"]["upstream_status"] == status


def test_a_process_that_dies_between_the_temporary_file_and_the_replace_leaves_the_previous_state(tmp_path):
    """A real process that exits at the worst moment (no cleanup, no finally): the file it leaves is whole."""
    state = tmp_path / "state"
    code = """
import os, sys
import conversation_state as cs
import native_history

def capture(label, pad):
    snapshot = native_history.Snapshot("scope", [], [{"role": "user", "content": label * pad}],
                                       [{"type": "thinking", "thinking": "", "signature": "S-" + label}],
                                       {"role": "assistant", "content": []})
    return cs.Capture("crash", cs.key_digest("crash"), "acc-one", (snapshot,), (0,), "key-" + label, 0, 1.0, (label,))

store = cs.ConversationStore(sys.argv[1])
store.submit(capture("A", 10))
assert store.flush(10)

def die(*_args):
    os._exit(7)  # the process is gone with the temporary file written and nothing replaced

cs.os.replace = die
store.submit(capture("B", 100000))
store.flush(10)
os._exit(3)  # not reached: the writer thread ended the process first
"""
    done = subprocess.run([sys.executable, "-c", code, str(state)], cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 7, done.stderr
    kept = state / (cs.key_digest("crash") + cs.SUFFIX)
    header, loaded = cs.decode(kept.read_bytes(), 10 ** 9)
    assert loaded.cache_key == "key-A" and loaded.branches[0].assistant[0]["signature"] == "S-A"  # the older, whole one
    assert [path.name for path in state.iterdir() if path.name.startswith(cs.TEMP_PREFIX)]  # what the death left
    seq = journal.JOURNAL.last_seq
    store = cs.ConversationStore(state)  # the next start: the leftover is nobody's write any more, the file is used
    assert [path.name for path in state.iterdir() if path.name.startswith(cs.TEMP_PREFIX)] == []
    assert store.peek("crash").account == "acc-one"
    assert store.load("crash").state.cache_key == "key-A"
    assert not stored(seq, "conversation_state.dropped")
    store.close()


def test_a_failed_replacement_leaves_the_previous_complete_file(tmp_path, monkeypatch):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of("steady"))
    assert store.flush(5)
    before = state_file(state, "steady").read_bytes()
    monkeypatch.setattr(cs.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError(5, "killed here")))
    store.submit(capture_of("steady", pad=500))
    assert store.flush(5)
    assert state_file(state, "steady").read_bytes() == before
    cs.decode(before, 10 ** 9)
    assert [path.name for path in state.iterdir() if path.name.startswith(cs.TEMP_PREFIX)] == []
    store.close()


def leftover(key, label="0" * 8):
    """The name of the temporary file a write of `key` leaves behind when its process dies."""
    return cs.TEMP_PREFIX + cs.key_digest(key) + "-" + label


def test_foreign_files_stay_and_the_leftovers_of_dead_writes_go_at_the_next_start(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    foreign = {"notes.txt": b"not ours", "something.state.bak": b"not ours either",
               "game.state": b"somebody else's save game",
               cs.key_digest("lookalike") + cs.SUFFIX: b"our kind of name, but not our signature",
               ".write-by-another-program": b"somebody else's temporary file"}
    for name, content in foreign.items():
        (state / name).write_bytes(content)
    old, fresh = state / leftover("a-dead-write", "0" * 8), state / leftover("a-recent-write", "f" * 8)
    old.write_bytes(b"half a write")
    fresh.write_bytes(b"a write that died a moment ago")
    stale = time.time() - 86400
    os.utime(old, (stale, stale))
    seq = journal.JOURNAL.last_seq
    cs.ConversationStore(state).close()
    # Any age: the relay that was writing it is gone. Nothing else is touched, and nothing is said about it.
    assert sorted(path.name for path in state.iterdir()) == sorted(foreign)
    assert not stored(seq, "conversation_state.dropped")
    assert all((state / name).read_bytes() == content for name, content in foreign.items())


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to remove a file that is open")
def test_a_temporary_file_that_is_still_open_is_left_to_its_writer(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    busy = state / leftover("being-written")
    with open(busy, "wb") as handle:
        handle.write(b"a write in progress")
        cs.ConversationStore(state).close()
        assert busy.exists()
    cs.ConversationStore(state).close()
    assert not busy.exists()


# -- limits ---------------------------------------------------------------------------------------------------------
def noise(size):
    """About `size` bytes that zlib cannot shrink."""
    return base64.b64encode(os.urandom(size)).decode()


def snapshot(label, pad=0):
    return native_history.Snapshot(
        json.dumps(["conv", {"model": "m"}, "head " + label, [], "auto"]),
        [{"role": "user", "content": [{"type": "text", "text": "ask " + label}]}],
        [{"role": "system", "content": [{"type": "text", "text": "head", "cache": True}]},
         {"role": "user", "content": [{"type": "text", "text": "ask " + label + noise(pad)}]}],
        [{"type": "thinking", "thinking": "", "signature": "S-" + label}, {"type": "text", "text": "ok"}],
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]})


def capture_of(key, *, branches=1, pad=0, account="acc-one"):
    items = tuple(snapshot("%s-%d" % (key, number), pad) for number in range(branches))
    return cs.Capture(key, cs.key_digest(key), account, items, (0, 3), "cache-key", 123, 1.5, ("mark", key))


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def files(state):
    return sorted(path.name for path in state.iterdir() if path.name.endswith(cs.SUFFIX))


def test_the_number_of_stored_conversations_is_bounded_and_the_oldest_go_first(tmp_path):
    clock, state = Clock(), tmp_path / "state"
    store = cs.ConversationStore(state, max_conversations=3, clock=clock)
    seq = journal.JOURNAL.last_seq
    for number in range(5):
        clock.now += 10
        assert store.submit(capture_of("conv-%d" % number)) and store.flush(5)
    assert [store.peek("conv-%d" % number) is not None for number in range(5)] == [False, False, True, True, True]
    assert files(state) == sorted(cs.key_digest("conv-%d" % number) + cs.SUFFIX for number in (2, 3, 4))
    dropped = stored(seq, "conversation_state.dropped")
    assert [event["data"]["reason"] for event in dropped] == ["limit_conversations"] * 2
    assert [event["conversation"] for event in dropped] == [journal.tag("conv-0"), journal.tag("conv-1")]
    store.close()


def test_the_total_size_is_bounded_and_the_state_just_written_always_stays(tmp_path):
    clock, state = Clock(), tmp_path / "state"
    store = cs.ConversationStore(state, max_file_bytes=5000, max_total_bytes=6000, clock=clock)
    seq = journal.JOURNAL.last_seq
    for number in range(4):
        clock.now += 10
        store.submit(capture_of("big-%d" % number, pad=2500))
        assert store.flush(5)
    total = sum(path.stat().st_size for path in state.iterdir())
    assert total <= 6000 and store.peek("big-3") is not None and store.peek("big-0") is None
    assert {event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")} == {"limit_bytes"}
    store.close()


def test_retention_removes_old_states_at_the_next_start_and_never_applies_one_in_between(tmp_path):
    clock, state = Clock(), tmp_path / "state"
    store = cs.ConversationStore(state, ttl=3600, clock=clock)
    store.submit(capture_of("old"))
    assert store.flush(5) and store.peek("old") is not None
    clock.now += 3601
    seq = journal.JOURNAL.last_seq
    assert store.peek("old") is None  # expired: never offered to a request
    assert store.load("old") is None  # even if a request got hold of it before the housekeeping
    assert files(state) == [] and [e["data"]["reason"] for e in stored(seq, "conversation_state.dropped")] == [
        "expired"]
    store.submit(capture_of("again"))
    assert store.flush(5)
    store.close()
    clock.now += 3601
    seq = journal.JOURNAL.last_seq
    cs.ConversationStore(state, ttl=3600, clock=clock).close()
    assert files(state) == []
    assert [e["data"]["reason"] for e in stored(seq, "conversation_state.dropped")] == ["expired"]


def test_a_state_too_big_for_a_file_loses_its_oldest_branches_first(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state, max_file_bytes=4500, max_total_bytes=100_000)
    seq = journal.JOURNAL.last_seq
    store.submit(capture_of("tall", branches=4, pad=2000))
    assert store.flush(5)
    saved = stored(seq, "conversation_state.saved")
    assert len(saved) == 1 and saved[0]["data"]["branches"] == 1 and saved[0]["data"]["trimmed"] == 3
    header, kept = cs.decode(state_file(state, "tall").read_bytes(), 10 ** 9)
    assert [branch.assistant[0]["signature"] for branch in kept.branches] == ["S-tall-3"]
    assert header["branches"] == 1 and state_file(state, "tall").stat().st_size <= 4500
    store.close()


def test_a_single_state_over_the_limit_is_not_stored_and_replaces_nothing_stale(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state, max_file_bytes=3000, max_total_bytes=100_000)
    store.submit(capture_of("grows", pad=100))
    assert store.flush(5) and store.peek("grows") is not None
    seq = journal.JOURNAL.last_seq
    store.submit(capture_of("grows", pad=6000))
    assert store.flush(5)
    failed = stored(seq, "conversation_state.save_failed")
    assert [event["data"]["reason"] for event in failed] == ["too_large"]
    # The older copy describes a turn the conversation has left behind: it cannot match again, so it goes.
    assert files(state) == [] and store.peek("grows") is None
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == ["too_large"]
    store.close()


def test_a_state_that_stays_too_large_is_said_once_and_never_retried_without_a_change(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state, max_file_bytes=3000, max_total_bytes=100_000)
    seq = journal.JOURNAL.last_seq
    for _ in range(4):  # four turns of a conversation that no longer fits
        store.submit(capture_of("huge", pad=6000))
        assert store.flush(5)
        assert not store.failed(cs.key_digest("huge"))  # nothing is waiting to be retried: only a change helps
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == ["too_large"]
    store.submit(capture_of("huge", pad=10))  # it fits again (a compaction): stored, and the report starts over
    assert store.flush(5) and files(state) == [cs.key_digest("huge") + cs.SUFFIX]
    store.submit(capture_of("huge", pad=6000))
    assert store.flush(5)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == [
        "too_large", "too_large"]
    store.close()


def test_a_transient_failure_stays_marked_for_a_retry_until_the_next_state_is_submitted(tmp_path, monkeypatch):
    store = cs.ConversationStore(tmp_path / "state", io_pause=0)  # no pause here: that is test_conversation_state_review
    key_id = cs.key_digest("flaky")
    monkeypatch.setattr(cs.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError(errno.EIO, "gone")))
    store.submit(capture_of("flaky"))
    assert store.flush(5) and store.failed(key_id)
    monkeypatch.undo()
    store.submit(capture_of("flaky"))
    assert store.flush(5) and not store.failed(key_id) and files(tmp_path / "state") == [key_id + cs.SUFFIX]
    store.close()


def test_a_state_that_would_expand_beyond_reason_is_refused_before_it_is_compressed(tmp_path):
    store = cs.ConversationStore(tmp_path / "state", max_file_bytes=1000, max_total_bytes=100_000)
    seq = journal.JOURNAL.last_seq
    # 200 KB of one letter fits a file of 1000 bytes once compressed, but it must not be inflated again on load.
    oversized = native_history.Snapshot("scope", [], [{"role": "user", "content": "a" * 200_000}], [], {})
    store.submit(cs.Capture("zeros", cs.key_digest("zeros"), "acc-one", (oversized,), (), "k", 0, 0.0, ("m",)))
    assert store.flush(5)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == ["too_large"]
    assert files(tmp_path / "state") == []
    store.close()
