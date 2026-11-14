"""What a restart must still find, and what a stored file must never do to a request (the findings of review 3).

R3-01 a compaction that is the first request after a restart, R3-03 blocks the transport does not expect,
R3-04/05 a request cancelled or repeated while the stored state is read, R3-06 which refusals forget a restored
branch, R3-09 the file of the text session. Same stand as test_conversation_state: a "process" is accounts, a pool
with a store, a runner and a scripted wire; a restart is a new process on the same (or a copied) directory.
"""
import asyncio
import copy
import json
import shutil
import threading
import time
import zlib

import pytest

import apierr
import conversation_state as cs
import journal
import native_history
from prompt_support import prompt_relay  # noqa: F401 - the fixture
from test_compaction_cache import Chat, count, thinking
from test_conversation_state import (BIASED, DONE, KEY, OPPOSITE, TWO, capture_of, converse, processes,  # noqa: F401
                                     refused, signature, signatures_in, split, state_file, stored, tool_scripts)
from test_native_context_reuse import FIRST, request
from test_native_history import append_result, parsed

INSTRUCTION = ("You MUST summarize the conversation above into a structured summary that will be given to another "
               "agent to resume the work.\n\nSynthetic template.")
ENDED = {"type": "end", "reason": "stop"}


def compaction_request(messages, effort="high"):
    asked = parsed(messages + [{"role": "user", "content": INSTRUCTION}])
    asked.effort = effort
    asked.client_task = "compaction"
    return asked


async def chat_rounds(process, rounds, effort="max", key=KEY):
    messages = copy.deepcopy(FIRST)
    for _ in range(rounds):
        asked = parsed(messages, key=key)
        asked.effort = effort
        events = await process.turn(asked)
        assert not any(event["type"] == "error" for event in events), events
        messages = append_result(messages, asked, events, "chat", value="RESULT")
    return messages


@pytest.fixture
def loads(monkeypatch):
    """The key of every read of a stored file."""
    calls, real = [], cs.ConversationStore.load

    def spy(self, key):
        calls.append(key)
        return real(self, key)

    monkeypatch.setattr(cs.ConversationStore, "load", spy)
    return calls


def slow_loads(monkeypatch, seconds, release=None):
    """Make every read of a stored file take `seconds` (or wait for `release`) on its executor thread."""
    calls, real = [], cs.ConversationStore.load

    def slow(self, key):
        calls.append(key)
        if release is not None:
            release.wait(5)
        else:
            time.sleep(seconds)
        return real(self, key)

    monkeypatch.setattr(cs.ConversationStore, "load", slow)
    return calls


def restart(tmp_path, processes, rounds=2, **options):
    """A conversation of `rounds` tool round trips, then its files in a new process: (messages, the new process)."""
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(rounds))
    messages = asyncio.run(chat_rounds(before, rounds, effort="high"))
    before.stop()
    return messages, processes(state, scripts=options.pop("scripts", [DONE]), **options)


# -- R3-01: the compaction that comes first ----------------------------------------------------------------------------
@pytest.mark.parametrize("restarted", [False, True])
def test_a_compaction_runs_on_the_effort_of_its_dialog_also_as_the_first_request_after_a_restart(
        tmp_path, processes, loads, restarted):
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    before = processes(state, scripts=tool_scripts(3) + [DONE])

    async def story():
        messages = await chat_rounds(before, 3)
        shutil.copytree(state, copied)
        return messages

    messages = asyncio.run(story())
    process = processes(copied, scripts=[DONE]) if restarted else before
    seq, reads = journal.JOURNAL.last_seq, len(loads)
    asyncio.run(process.turn(compaction_request(messages)))
    body = process.sent[0 if restarted else -1].body
    assert body["reasoningEffort"] == "max" and len(signatures_in(body)) == 3
    kinds = sorted(event["kind"] for event in stored(
        seq, "cache.effort_kept", "cache.compaction_restart", "native_history.resume", "native_history.reset",
        "native_history.cold", "conversation_state.restored"))
    # The branch of the chat is what the compaction continues: no restart of the history, no cold replay.
    assert kinds == (["cache.effort_kept", "conversation_state.restored", "native_history.resume"] if restarted else
                     ["cache.effort_kept", "native_history.resume"])
    assert len(loads) - reads == int(restarted)  # the file is read once: the check and the session share the read


def test_the_compaction_after_a_restart_over_http_keeps_the_effort_and_the_signatures(prompt_relay, tmp_path):
    state = tmp_path / "state" / "conversations"
    relay = prompt_relay(conversation_state_dir=str(state))
    chat = Chat(relay)
    chat.work()
    relay.server.shutdown()
    relay.server.server_close()
    relay.runtime.close()
    relay = prompt_relay(conversation_state_dir=str(state))
    chat.relay = relay
    mark = relay.mark()
    summary = chat.compact(effort="high")
    assert summary["reasoningEffort"] == "max" and thinking(summary) == ["opaque-1", "opaque-2"]
    assert count(relay, mark, "cache.effort_kept") == 1 and count(relay, mark, "native_history.resume") == 1
    assert count(relay, mark, "cache.compaction_restart") == count(relay, mark, "native_history.reset") == 0
    assert count(relay, mark, "native_history.cold") == 0


def test_peek_shows_a_stored_conversation_only_once_it_is_read_and_never_makes_a_session(tmp_path, processes, loads):
    messages, after = restart(tmp_path, processes)
    entry = parsed(messages).entry

    async def story():
        pool = after.pool
        assert pool.peek(KEY, model_entry=entry) is None  # nothing is read yet: peek never reads
        assert loads == []
        await pool.load_stored(KEY, model_entry=entry)
        view = pool.peek(KEY, model_entry=entry)
        assert isinstance(view, cs.StoredSession) and view.backend is after.backends[0]
        assert [branch.restored for branch in view._native_history.branches] == [True]
        assert KEY not in pool._conversations and not pool._conversation_locks
        # acquire's own choice, as for a live session: another account, no session for a standalone job
        assert pool.peek(KEY, model_entry=entry, exclude=(view.backend,)) is None
        assert pool.peek(KEY, model_entry=entry, isolated=True) is None
        assert pool.peek(KEY, model_entry=entry, context_scope="text") is None
        view.backend.unavailable = {"reason": "exhausted", "available": 0, "threshold": 20}
        assert pool.peek(KEY, model_entry=entry) is None
        view.backend.unavailable = None
        lease = await pool.acquire(KEY, model_entry=entry)
        assert pool.peek(KEY, model_entry=entry) is lease.session  # the live session, with the very same state
        assert [branch.restored for branch in lease.session._native_history.branches] == [True]
        assert not pool._reads
        await pool.release(lease)

    asyncio.run(story())
    assert loads == [KEY]


def test_a_stored_conversation_that_cannot_go_back_to_its_account_is_not_even_read_ahead(tmp_path, processes, loads):
    messages, after = restart(tmp_path, processes)
    entry = parsed(messages).entry
    after.backends[0].unavailable = {"reason": "auth_dead"}
    asyncio.run(after.pool.load_stored(KEY, model_entry=entry))
    assert loads == [] and after.pool.peek(KEY, model_entry=entry) is None and not after.pool._reads


def test_the_stored_state_is_read_ahead_without_stopping_the_event_loop(tmp_path, monkeypatch, processes):
    messages, after = restart(tmp_path, processes)
    slow_loads(monkeypatch, 0.4)  # a slow disk: the read takes this long on its own thread
    entry = parsed(messages).entry

    async def story():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        watcher = asyncio.create_task(ticker())
        started = time.monotonic()
        await after.pool.load_stored(KEY, model_entry=entry)
        took = time.monotonic() - started
        watcher.cancel()
        return ticks, took

    ticks, took = asyncio.run(story())
    assert took >= 0.35 and ticks >= 8  # a loop that waited for the disk would have ticked once, after it


def test_a_stalled_read_delays_a_compaction_by_the_restore_time_once_and_it_runs_as_the_client_asked(
        tmp_path, monkeypatch, processes):
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    before = processes(state, scripts=tool_scripts(3))

    async def story():
        messages = await chat_rounds(before, 3)
        shutil.copytree(state, copied)
        return messages

    messages = asyncio.run(story())
    after = processes(copied, scripts=[DONE])
    release = threading.Event()
    reads = slow_loads(monkeypatch, 0, release)
    monkeypatch.setattr(cs, "RESTORE_TIMEOUT", 0.3)
    seq = journal.JOURNAL.last_seq

    async def story():
        started = time.monotonic()
        events = await after.turn(compaction_request(messages))
        took = time.monotonic() - started
        release.set()  # the stuck read gives up its thread once the request has been answered
        return events, took

    events, took = asyncio.run(story())
    # The check waited for the read and the request after it did not wait again: the 0.3 s are the read's own.
    assert events[-1] == ENDED and took < 3
    assert after.sent[0].body["reasoningEffort"] == "high" and signatures_in(after.sent[0].body) == []
    assert reads == [KEY]
    assert [e["data"]["error"] for e in stored(seq, "conversation_state.restore_failed")] == ["TimeoutError"]
    assert [e["data"]["reason"] for e in stored(seq, "cache.compaction_restart")] == ["no_branch"]


def test_states_read_ahead_that_nobody_came_for_do_not_pile_up(tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    keys = ["header:dialog-%d" % number for number in range(3)]
    before = processes(state, scripts=tool_scripts(3))

    async def conversations():
        return [await chat_rounds(before, 1, key=key) for key in keys]

    all_messages = asyncio.run(conversations())
    before.stop()
    after = processes(state)
    entry = parsed(all_messages[0]).entry
    monkeypatch.setattr(cs, "RESTORE_READAHEAD", 2)

    async def story():
        for key in keys:
            await after.pool.load_stored(key, model_entry=entry)
        assert list(after.pool._reads) == keys[1:]  # the oldest made room
        monkeypatch.setattr(cs, "RESTORE_KEEP", -1)  # whatever is done by now has waited long enough
        await after.pool.load_stored(keys[0], model_entry=entry)
        assert list(after.pool._reads) == [keys[0]]  # the others were dropped before the new one started

    asyncio.run(story())


# -- R3-03: a file whose blocks are not what the transport expects ---------------------------------------------------
def rewrite(path, mutate):
    """The same file with its state changed by `mutate`: a whole zlib stream of JSON of the right shape."""
    head, payload = split(path.read_bytes())
    document = json.loads(zlib.decompress(payload))
    mutate(document)
    path.write_bytes(head + zlib.compress(json.dumps(document, separators=(",", ":")).encode()))


def newest(document):
    return document["branches"][-1]


MUTATIONS = {
    "assistant block without type": lambda doc: newest(doc).__setitem__("assistant", [{"x": 1}]),
    "assistant block of an unknown type": lambda doc: newest(doc).__setitem__(
        "assistant", [{"type": "audio", "data": "x"}]),
    "tool_call without toolCallId": lambda doc: newest(doc).__setitem__(
        "assistant", [{"type": "tool_call", "toolName": "read_file", "toolInput": {}}]),
    "tool_call with an id that is not text": lambda doc: newest(doc).__setitem__(
        "assistant", [{"type": "tool_call", "toolCallId": 7, "toolName": "read_file", "toolInput": {}}]),
    "tool_call whose input is not an object": lambda doc: newest(doc).__setitem__(
        "assistant", [{"type": "tool_call", "toolCallId": "c", "toolName": "read_file", "toolInput": [1]}]),
    "thinking without a signature": lambda doc: newest(doc).__setitem__(
        "assistant", [{"type": "thinking", "thinking": "", "signature": ""}]),
    "a block that is not an object": lambda doc: newest(doc).__setitem__("assistant", ["text"]),
    "assistant is not a list": lambda doc: newest(doc).__setitem__("assistant", {"type": "text"}),
    "client message without role": lambda doc: newest(doc)["client"].__setitem__(1, {"content": []}),
    "client message of a role it cannot have": lambda doc: newest(doc)["client"].__setitem__(
        0, {"role": "system", "content": "x"}),
    "echo without role": lambda doc: newest(doc).__setitem__("echo", {"content": []}),
    "echo that is a user message": lambda doc: newest(doc).__setitem__(
        "echo", {"role": "user", "content": [{"type": "text", "text": "x"}]}),
    "messages hold a message without content": lambda doc: newest(doc)["messages"].append({"role": "user"}),
    "messages hold a role nobody sends": lambda doc: newest(doc)["messages"].append({"role": "moderator", "content": "x"}),
    "content with a block that is a string": lambda doc: newest(doc)["messages"][1].__setitem__("content", ["x"]),
    "a tool message whose content is text": lambda doc: newest(doc)["messages"].append(
        {"role": "tool", "content": "x"}),
    "a tool message without a result": lambda doc: newest(doc)["messages"].append(
        {"role": "tool", "content": {"toolCallId": "c"}}),
    "an assistant message with a block of no type": lambda doc: newest(doc)["messages"].append(
        {"role": "assistant", "content": [{"text": "x"}]}),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_a_file_of_the_right_shape_with_blocks_the_transport_does_not_expect_is_dropped_not_replayed(
        tmp_path, processes, name):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    rewrite(state_file(state), MUTATIONS[name])
    after = processes(state, scripts=[DONE, DONE])
    seq = journal.JOURNAL.last_seq
    follow = messages + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "go on"}]

    async def story():
        return [(await after.turn(parsed(messages)))[-1], (await after.turn(parsed(follow)))[-1]]

    assert asyncio.run(story()) == [ENDED, ENDED]  # no request is lost to the file, none ends in an error
    assert [e["data"]["reason"] for e in stored(seq, "conversation_state.dropped")] == ["corrupt"]
    assert not stored(seq, "conversation_state.restored") and "SIG-" not in json.dumps(after.sent[0].body)
    # What the first request stored replaced the file, so the conversation goes on from its own state.
    assert [e["kind"] for e in stored(seq, "native_history.resume")] == ["native_history.resume"]
    assert signatures_in(after.sent[1].body) == []  # the DONE of the first turn has no thinking: nothing old came back


def test_a_branch_the_transport_cannot_resume_is_forgotten_and_the_request_goes_cold(tmp_path, monkeypatch, processes):
    """What the checks of the file did not stop (they are switched off here): the guard around resume."""
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    rewrite(state_file(state), MUTATIONS["assistant block without type"])
    monkeypatch.setattr(cs, "_reply_block", lambda block: True)  # the file passes decode()
    after = processes(state, scripts=[DONE])
    seq = journal.JOURNAL.last_seq
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1] == ENDED and not any(event["type"] == "error" for event in events)
    assert "SIG-" not in json.dumps(after.sent[0].body)
    dropped = stored(seq, "conversation_state.dropped")
    assert [(e["data"]["reason"], e["data"]["error"]) for e in dropped] == [("unusable", "KeyError")]
    assert [e["kind"] for e in stored(seq, "native_history.cold")] == ["native_history.cold"]
    # The stored copy follows: what the branch was is gone from the file, and what the turn made is in it.
    header, state_now = cs.decode(state_file(state).read_bytes(), 10 ** 9)
    assert header["branches"] == 1 and state_now.branches[0].assistant == [{"type": "text", "text": "done"}]
    live = after.pool._conversations[KEY]._native_history.branches
    assert [branch.restored for branch in live] == [False]


def test_a_failure_of_a_branch_the_relay_recorded_itself_is_still_raised():
    history = native_history.History()
    echo = {"role": "assistant", "content": []}
    history.record("scope", [], [], [{"x": 1}], echo)
    with pytest.raises(KeyError):
        cs.resume(None, history, "scope", [echo, {"role": "user", "content": []}])
    assert [branch.restored for branch in history.branches] == [False]


def test_a_file_of_another_stamp_is_dropped_unread_when_the_relay_starts(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    asyncio.run(converse(before, "chat", 2))
    before.stop()
    head, payload = split(state_file(state).read_bytes())
    stamp = cs.STAMP.encode()
    variants = ((head.replace(stamp, b"0" * len(stamp)), "another stamp"),
                (head.replace(b'"blocks":"%s",' % stamp, b""), "no stamp"))
    for other, text in variants:
        # The payload is garbage that would be `corrupt` if anyone read it: the verdict comes from the header alone.
        state_file(state).write_bytes(other + b"not a zlib stream")
        seq = journal.JOURNAL.last_seq
        cs.ConversationStore(state).close()
        assert [e["data"]["reason"] for e in stored(seq, "conversation_state.dropped")] == ["format"], text
        assert not state_file(state).exists(), text
        state_file(state).write_bytes(head + payload)


def test_the_stamp_follows_the_shapes_the_check_accepts(monkeypatch):
    assert len(cs.STAMP) == 12 and cs.STAMP == cs._blocks_stamp()
    changed = dict(cs._REPLY_BLOCKS, tool_call={**cs._REPLY_BLOCKS["tool_call"], "extra": str})
    monkeypatch.setattr(cs, "_REPLY_BLOCKS", changed)
    assert cs._blocks_stamp() != cs.STAMP
    monkeypatch.setattr(cs, "_REPLY_BLOCKS", dict(cs._REPLY_BLOCKS, **{"audio": {"data": str}}))
    assert cs._blocks_stamp() != cs.STAMP


def test_every_shape_the_transport_records_passes_the_check_and_comes_back_byte_for_byte(tmp_path, processes):
    """Thinking, redacted thinking, text, parallel calls, results as text, with an image, with an error flag."""
    import adapters
    from test_conversation_state import rich_reply
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"
    before = processes(state, scripts=[rich_reply(0), rich_reply(1), DONE])

    async def story():
        messages = copy.deepcopy(FIRST)
        for number in range(2):
            asked = parsed(messages, "anthropic")
            events = await before.turn(asked)
            assert not any(event["type"] == "error" for event in events), events
            adapter = adapters.AnthropicAdapter(asked, None)
            for event in events:
                adapter.feed(event)
            content = adapter.payload("tool_calls")["content"]
            calls = [block for block in content if block["type"] == "tool_use"]
            results = [{"type": "tool_result", "tool_use_id": call["id"], "is_error": number == 1,
                        "content": [{"type": "text", "text": "seen"}, image] if index == 0 else "plain text"}
                       for index, call in enumerate(calls)]
            messages = messages + [{"role": "assistant", "content": content}, {"role": "user", "content": results}]
        shutil.copytree(state, copied)
        await before.turn(parsed(messages, "anthropic"))
        return messages

    messages = asyncio.run(story())
    after = processes(copied, scripts=[DONE])
    seq = journal.JOURNAL.last_seq
    asyncio.run(after.turn(parsed(messages, "anthropic")))
    assert after.sent[0].data == before.sent[-1].data
    assert len(stored(seq, "conversation_state.restored")) == 1 and not stored(seq, "conversation_state.dropped")
    assert "RICH-SIG-0-" in json.dumps(after.sent[0].body) and "aGVsbG8=" in json.dumps(after.sent[0].body)


# -- R3-04 and R3-05: the window in which the stored state is read ------------------------------------------------------
def test_a_request_cancelled_while_its_state_is_read_leaves_nothing_behind_and_the_next_gets_the_state(
        tmp_path, monkeypatch, processes):
    messages, after = restart(tmp_path, processes, scripts=[DONE])
    reads = slow_loads(monkeypatch, 0.5)
    seq = journal.JOURNAL.last_seq

    async def story():
        entry = parsed(messages).entry
        task = asyncio.create_task(after.pool.acquire(KEY, model_entry=entry))
        await asyncio.sleep(0.1)
        task.cancel()  # the client went away (a timeout, a retry) while the file was being read
        with pytest.raises(asyncio.CancelledError):
            await task
        pool = after.pool
        assert KEY not in pool._conversations and KEY not in pool._conversation_locks
        assert KEY not in pool._conversation_refs and not pool._restoring and not pool._active
        assert pool._slots._value == pool.cfg["max_workers"]
        return await after.turn(parsed(messages))

    assert asyncio.run(story())[-1] == ENDED
    assert signatures_in(after.sent[0].body) == [signature(0), signature(1)]
    assert reads == [KEY] and state_file(tmp_path / "state").exists()  # one read, for both attempts
    assert len(stored(seq, "conversation_state.restored")) == 1
    assert not stored(seq, "native_history.cold") and not stored(seq, "native_history.reset")


@pytest.mark.parametrize("delay", [0.0, 0.3])
def test_two_requests_of_a_stored_conversation_at_once_both_continue_its_signed_history(
        tmp_path, monkeypatch, processes, delay):
    messages, after = restart(tmp_path, processes, scripts=[DONE, DONE])
    log, real_load, real_submit = [], cs.ConversationStore.load, cs.ConversationStore.submit

    def slow(self, key):
        log.append("read starts")
        time.sleep(delay)
        loaded = real_load(self, key)
        log.append("read ends")
        return loaded

    def submit(self, captured, urgent=False):
        log.append("stored")
        return real_submit(self, captured, urgent)

    monkeypatch.setattr(cs.ConversationStore, "load", slow)
    monkeypatch.setattr(cs.ConversationStore, "submit", submit)
    seq = journal.JOURNAL.last_seq

    async def story():
        return await asyncio.gather(after.turn(parsed(messages)), after.turn(parsed(messages)))

    answers = asyncio.run(story())
    assert [events[-1] for events in answers] == [ENDED, ENDED]
    assert [len(signatures_in(call.body)) for call in after.sent] == [2, 2]  # neither went cold
    # The file is read once, and nothing is stored over it before that: a request that comes while it is read
    # waits for the session (whose state it is) instead of starting from nothing and replacing the file.
    assert log.count("read starts") == 1 and log.index("stored") > log.index("read ends")
    kinds = [event["kind"] for event in stored(seq, "conversation_state.restored", "native_history.resume",
                                               "native_history.cold", "native_history.reset")]
    assert sorted(kinds) == ["conversation_state.restored", "native_history.resume", "native_history.resume"]


def test_a_request_waiting_for_a_stored_state_that_is_cancelled_leaves_the_pool_clean(tmp_path, monkeypatch, processes):
    messages, after = restart(tmp_path, processes)
    slow_loads(monkeypatch, 0.5)

    async def story():
        entry = parsed(messages).entry
        first = asyncio.create_task(after.pool.acquire(KEY, model_entry=entry))
        await asyncio.sleep(0.1)
        second = asyncio.create_task(after.pool.acquire(KEY, model_entry=entry))
        await asyncio.sleep(0.1)
        assert after.pool._conversation_refs[KEY] == 2  # the second one is at the gate
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        lease = await first
        assert [branch.restored for branch in lease.session._native_history.branches] == [True]
        assert after.pool._conversation_refs[KEY] == 1 and not after.pool._restoring
        await after.pool.release(lease)
        assert after.pool._conversation_refs[KEY] == 0
        assert after.pool._slots._value == after.pool.cfg["max_workers"]

    asyncio.run(story())


def test_a_stalled_read_ends_the_wait_of_every_request_that_came_for_the_state(tmp_path, monkeypatch, processes):
    messages, after = restart(tmp_path, processes, scripts=[DONE, DONE])
    release = threading.Event()
    slow_loads(monkeypatch, 0, release)
    monkeypatch.setattr(cs, "RESTORE_TIMEOUT", 0.3)
    seq = journal.JOURNAL.last_seq

    async def story():
        started = time.monotonic()
        answers = await asyncio.gather(after.turn(parsed(messages)), after.turn(parsed(messages)))
        release.set()
        return answers, time.monotonic() - started

    answers, took = asyncio.run(story())
    assert [events[-1] for events in answers] == [ENDED, ENDED] and took < 4
    assert [e["data"]["error"] for e in stored(seq, "conversation_state.restore_failed")] == ["TimeoutError"]
    assert all("SIG-" not in json.dumps(call.body) for call in after.sent)
    assert not after.pool._restoring and not after.pool._reads


def test_a_session_whose_state_is_still_being_read_is_never_stored(tmp_path, processes):
    messages, after = restart(tmp_path, processes)

    async def story():
        lease = await after.pool.acquire(KEY, model_entry=parsed(messages).entry)
        session = lease.session
        session._native_history.revision += 1  # it has moved since the stored copy
        after.pool._restoring[KEY] = asyncio.Event()  # ... but a read of its state is under way
        submitted = []
        after.store.submit = lambda captured, urgent=False: submitted.append(captured)
        after.pool._persist(KEY)
        assert submitted == []
        after.pool._restoring.clear()
        after.pool._persist(KEY)
        assert len(submitted) == 1
        await after.pool.release(lease)

    asyncio.run(story())


# -- R3-06: which refusals forget a restored branch -------------------------------------------------------------------
BLOCK_REFUSALS = [
    "messages.1.content.0: Invalid `signature` in `thinking` block",
    "Invalid signature in thinking block",
    "thinking or redacted_thinking blocks in the latest assistant message cannot be modified.",
    "Expected `thinking` or `redacted_thinking`, but found `text`. When `thinking` is enabled, a final `assistant` "
    "message must start with a thinking block",
    "messages.3: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_01",
    "messages.4.content.0: unexpected `tool_use_id` found in `tool_result` blocks",
    "messages.2.content.1: text content blocks must be non-empty",
]
OTHER_REFUSALS = [
    "prompt is too long: 240000 tokens > 200000 maximum",
    "Invalid request format",  # the route validator's: it names nothing, so nothing says the branch is at fault
    "{}",
    "",
    "max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens",
    "reasoningEffort 'xhigh' is not supported by this model",
    "Request blocked by the content filter",
    "tools.0.input_schema: JSON schema is invalid",
]


def refusal(text):
    return refused(400, json.dumps({"message": text}).encode() if text and text != "{}" else text.encode())


@pytest.mark.parametrize("text", BLOCK_REFUSALS + OTHER_REFUSALS)
def test_a_restored_branch_is_forgotten_only_when_the_refusal_names_its_blocks(tmp_path, processes, text):
    messages, after = restart(tmp_path, processes, scripts=[refusal(text)])
    forgotten = text in BLOCK_REFUSALS
    seq = journal.JOURNAL.last_seq
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1]["type"] == "error" and events[-1]["error"].status == 400
    assert [branch.restored for branch in after.pool._conversations[KEY]._native_history.branches] == (
        [] if forgotten else [True])
    assert len([e for e in stored(seq, "conversation_state.dropped") if e["data"]["reason"] == "rejected"]) == int(forgotten)


def chat_request(messages, effort="max"):
    asked = parsed(messages)
    asked.effort = effort
    return asked


def test_a_compaction_refused_for_another_reason_leaves_the_branch_of_the_chat_alone(tmp_path, processes, loads):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(3))
    messages = asyncio.run(chat_rounds(before, 3))
    before.stop()
    after = processes(state, scripts=[refusal("prompt is too long: 240000 tokens > 200000 maximum"), DONE])
    seq = journal.JOURNAL.last_seq

    async def story():
        summary = await after.turn(compaction_request(messages))  # resumes the chat's branch on its effort, refused
        assert summary[-1]["type"] == "error"
        assert [branch.restored for branch in after.pool._conversations[KEY]._native_history.branches] == [True]
        return await after.turn(chat_request(messages))  # the chat itself goes on from that branch

    assert asyncio.run(story())[-1] == ENDED
    assert after.sent[0].body["reasoningEffort"] == "max" and len(signatures_in(after.sent[0].body)) == 3
    assert signatures_in(after.sent[1].body) == [signature(n) for n in range(3)]
    assert not [e for e in stored(seq, "conversation_state.dropped") if e["data"]["reason"] == "rejected"]
    assert not stored(seq, "native_history.cold") and not stored(seq, "native_history.reset")
    assert loads.count(KEY) == 1


def test_a_compaction_refused_over_the_signatures_of_the_branch_does_forget_it(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(3))
    messages = asyncio.run(chat_rounds(before, 3))
    before.stop()
    after = processes(state, scripts=[refusal("messages.1.content.0: Invalid `signature` in `thinking` block"), DONE])

    async def story():
        summary = await after.turn(compaction_request(messages))
        assert summary[-1]["type"] == "error"
        assert after.pool._conversations[KEY]._native_history.branches == []
        return await after.turn(chat_request(messages))

    assert asyncio.run(story())[-1] == ENDED
    assert signatures_in(after.sent[1].body) == []  # the chat's next request takes the unsigned way


def test_blocks_refused_reads_the_message_of_the_error_and_nothing_else():
    assert cs.blocks_refused(apierr.ApiError(400, "invalid_request_error", "Invalid signature in thinking block"))
    assert not cs.blocks_refused(apierr.ApiError(400, "invalid_request_error", "prompt is too long"))
    assert not cs.blocks_refused(apierr.ApiError(400, "invalid_request_error", ""))
    assert cs.blocks_refused("a bare string about a thinking block") and not cs.blocks_refused(400)


# -- R3-09: the text session has a file of its own -----------------------------------------------------------------------
def test_a_plain_chat_goes_back_to_the_account_of_its_own_state_not_the_main_ones(tmp_path, processes):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of(KEY, account="acc-one"))
    store.submit(capture_of((KEY, "text"), account="acc-two"))
    assert store.flush(5)
    store.close()
    after = processes(state, accounts=TWO, priority=OPPOSITE, scripts=[DONE, DONE])
    seq = journal.JOURNAL.last_seq
    asyncio.run(after.turn(request(FIRST, tools=[], system="PLAIN_SYSTEM", key=KEY)))
    assert after.sent[0].account == "acc-two"  # not acc-one, where the file of the main session is
    assert len(stored(seq, "conversation_state.restored")) == 1 and not stored(seq, "conversation_state.skipped")


def test_the_main_session_still_goes_where_its_state_is_and_a_text_one_without_a_file_follows_it(tmp_path, processes):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of(KEY, account="acc-two"))
    assert store.flush(5)
    store.close()
    after = processes(state, accounts=TWO, priority=OPPOSITE, scripts=[DONE, DONE])
    asyncio.run(after.turn(parsed(FIRST)))  # a request with tools: the main session
    asyncio.run(after.turn(request(FIRST, tools=[], system="PLAIN_SYSTEM", key=KEY)))  # a plain chat: no file of its own
    assert [call.account for call in after.sent] == ["acc-two", "acc-two"]
    # only the text session's file: the main request still follows it (as a live conversation follows its text session)
    state = tmp_path / "state-text-only"
    store = cs.ConversationStore(state)
    store.submit(capture_of((KEY, "text"), account="acc-two"))
    assert store.flush(5)
    store.close()
    other = processes(state, accounts=TWO, priority=OPPOSITE, scripts=[DONE])
    asyncio.run(other.turn(parsed(FIRST)))
    assert other.sent[0].account == "acc-two"


# -- R3-02: the journal says when what was restored did not continue ------------------------------------------------------
def test_a_reset_says_whether_the_history_it_dropped_was_read_back_from_disk(tmp_path, processes):
    messages, after = restart(tmp_path, processes)
    seq = journal.JOURNAL.last_seq
    asyncio.run(after.turn(parsed(messages, system="ANOTHER_HEAD")))  # the head changed (a profile, a new relay): no match
    resets = stored(seq, "native_history.reset")
    assert [e["data"]["restored"] for e in resets] == [True]
    # and the same change in a conversation that never stopped is not blamed on a restart
    live = processes(tmp_path / "state-live", scripts=tool_scripts(2) + [DONE])

    async def story():
        history = await chat_rounds(live, 2, effort="high")
        await live.turn(parsed(history, system="ANOTHER_HEAD"))

    seq = journal.JOURNAL.last_seq
    asyncio.run(story())
    assert [e["data"]["restored"] for e in stored(seq, "native_history.reset")] == [False]
