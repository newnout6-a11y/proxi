"""Stored conversations: failing disks, a real Runtime, configuration, privacy, keepwarm, branches, the format."""
import asyncio
import copy
import errno
import http.client
import json
import os
import queue
import shutil
import stat
import threading
import time
from types import SimpleNamespace

import pytest

import apierr
import config
import conversation_state as cs
import direct
import journal
import native_history
import proxy
from test_conversation_state import (BIASED, DONE, KEY, OPPOSITE, TWO, capture_of, converse, files,  # noqa: F401
                                     processes, signature, signatures_in, state_file, stored, tool_scripts)
from test_docs_consistency import load_with
from test_native_context_reuse import FIRST, continued, request
from test_native_history import TOOLS, append_result, parsed, tool_reply
from test_review_fixes_pr19 import response

SETTINGS = ("conversation_state", "conversation_state_dir", "conversation_state_max_conversations",
            "conversation_state_ttl", "conversation_state_max_file_bytes", "conversation_state_max_total_bytes")


def leftovers(state):
    return sorted(path.name for path in state.iterdir() if path.name.startswith(cs.TEMP_PREFIX))


# -- a disk that fails never reaches a request -----------------------------------------------------------------------
def test_a_failing_write_never_breaks_the_turn_and_the_next_change_or_the_stop_retries_it(
        tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    process = processes(state, scripts=tool_scripts(2))
    real, broken = os.replace, {"on": True}

    def replace(source, target):
        if broken["on"]:
            raise OSError(errno.EIO, "the disk is gone")
        return real(source, target)

    monkeypatch.setattr(cs.os, "replace", replace)
    seq = journal.JOURNAL.last_seq

    async def story():
        messages = await converse(process, "chat", 1)
        assert files(state) == [] and leftovers(state) == []
        failed = stored(seq, "conversation_state.save_failed")
        assert [event["data"]["reason"] for event in failed] == ["io_error"]
        assert failed[0]["data"]["errno"] == errno.EIO and process.store.failed(cs.key_digest(KEY))
        # The disk is back: the stop (persist_pending) stores what the failed write could not, without any new turn.
        broken["on"] = False
        process.pool.persist_pending()
        assert process.store.flush(5)
        assert len(files(state)) == 1 and not process.store.failed(cs.key_digest(KEY))
        return messages

    asyncio.run(story())


def test_a_scanner_holding_the_file_for_a_moment_does_not_lose_the_state(tmp_path, monkeypatch):
    real, refusals = os.replace, [PermissionError(13, "in use"), PermissionError(13, "in use")]

    def replace(source, target):
        if refusals:
            raise refusals.pop(0)
        return real(source, target)

    monkeypatch.setattr(cs.os, "replace", replace)
    monkeypatch.setattr(cs.time, "sleep", lambda _seconds: None)
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    seq = journal.JOURNAL.last_seq
    store.submit(capture_of("busy"))
    assert store.flush(5) and files(state) == [cs.key_digest("busy") + cs.SUFFIX]
    assert not stored(seq, "conversation_state.save_failed") and leftovers(state) == []
    refusals.extend(PermissionError(13, "in use") for _ in range(cs.REPLACE_ATTEMPTS))
    store.submit(capture_of("busy", pad=10))
    assert store.flush(5)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == ["io_error"]
    assert leftovers(state) == []
    store.close()


def test_no_room_for_even_a_temporary_file_is_reported_and_leaves_nothing_behind(tmp_path, monkeypatch):
    real = os.open

    def full(path, *args, **kwargs):
        if os.path.basename(path).startswith(cs.TEMP_PREFIX):
            raise OSError(errno.ENOSPC, "no space left on device")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(cs.os, "open", full)
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    seq = journal.JOURNAL.last_seq
    store.submit(capture_of("full"))
    assert store.flush(5)
    failed = stored(seq, "conversation_state.save_failed")
    assert [event["data"]["errno"] for event in failed] == [errno.ENOSPC] and files(state) == []
    store.close()


def test_a_state_that_cannot_be_serialized_is_reported_not_raised(tmp_path, monkeypatch):
    store = cs.ConversationStore(tmp_path / "state")
    seq = journal.JOURNAL.last_seq
    loop = {"role": "user", "content": []}
    loop["content"].append(loop)  # a cycle
    for number, message in enumerate(({"role": "user", "content": {1, 2}}, loop)):
        snapshot = native_history.Snapshot("scope", [], [message], [], {})
        store.submit(cs.Capture("bad-%d" % number, cs.key_digest("bad-%d" % number), "acc-one", (snapshot,),
                                (), "k", 0, 0.0, ("m", number)))
        assert store.flush(5)

    def too_deep(_value):
        raise RecursionError("maximum recursion depth exceeded")

    with monkeypatch.context() as patch:
        patch.setattr(cs, "_dump", too_deep)
        store.submit(capture_of("deep"))
        assert store.flush(5)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == [
        "unserializable"] * 3
    assert files(tmp_path / "state") == []
    store.submit(capture_of("fine"))  # the writer is still alive
    assert store.flush(5) and len(files(tmp_path / "state")) == 1
    store.close()


def test_an_unexpected_error_in_the_writer_is_reported_and_the_writer_goes_on(tmp_path, monkeypatch):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    seq = journal.JOURNAL.last_seq
    real, calls = cs.ConversationStore._write, []

    def write(self, key_id, blob):
        calls.append(key_id)
        if len(calls) == 1:
            raise RuntimeError("a bug")
        return real(self, key_id, blob)

    monkeypatch.setattr(cs.ConversationStore, "_write", write)
    store.submit(capture_of("first"))
    store.submit(capture_of("second"))
    assert store.flush(5)
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == ["internal"]
    assert files(state) == [cs.key_digest("second") + cs.SUFFIX]
    store.close()


def test_a_failure_while_capturing_a_session_is_journaled_and_the_turn_is_untouched(tmp_path, monkeypatch, processes):
    process = processes(tmp_path / "state", scripts=tool_scripts(1))

    def boom(*_args):
        raise RuntimeError("a bug in capture")

    monkeypatch.setattr(cs, "capture", boom)
    seq = journal.JOURNAL.last_seq
    asyncio.run(converse(process, "chat", 1))
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.save_failed")] == ["capture"]
    assert files(tmp_path / "state") == [] and not process.pool._active


def test_a_bug_in_the_stored_binding_or_state_never_reaches_the_request(tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[DONE])

    def boom(*_args):
        raise RuntimeError("a bug while looking the conversation up")

    monkeypatch.setattr(after.store, "peek", boom)
    seq = journal.JOURNAL.last_seq
    events = asyncio.run(after.turn(parsed(messages)))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert "SIG-" not in json.dumps(after.sent[0].body)  # it took the way it always did: no account, no state
    assert {event["data"]["error"] for event in stored(seq, "conversation_state.restore_failed")} == {"RuntimeError"}


def test_a_directory_that_cannot_be_created_switches_the_store_off_without_failing_the_start(tmp_path, capsys):
    blocked = tmp_path / "in-the-way"
    blocked.write_text("a file where the directory should be")
    seq = journal.JOURNAL.last_seq
    assert cs.open_store({"conversation_state_dir": str(blocked / "conversations")}) is None
    assert [event["level"] for event in stored(seq, "conversation_state.disabled")] == ["warn"]
    assert "[state] conversation state is off" in capsys.readouterr().out


def test_a_store_that_fails_to_start_for_any_reason_never_keeps_the_relay_from_starting(
        tmp_path, monkeypatch, isolated_journal):
    def broken(self):
        raise RuntimeError("a bug while reading the directory")

    monkeypatch.setattr(cs.ConversationStore, "scan", broken)
    cfg = runtime_config(tmp_path, conversation_state_dir=str(tmp_path / "state"))
    runtime = proxy.Runtime(cfg)
    try:
        assert runtime.pool._store is None
        assert [event["level"] for event in stored(0, "conversation_state.disabled")] == ["warn"]
    finally:
        runtime.close()


# -- switched off, or nowhere to write --------------------------------------------------------------------------------
class Wire:
    def __init__(self, scripts):
        self.sent, self.raw, self.scripts = [], [], list(scripts)

    def __call__(self, upstream_request, **_kw):
        self.sent.append(json.loads(upstream_request.data))
        self.raw.append(upstream_request.data)
        index = len(self.sent) - 1
        return response(*(self.scripts[index] if index < len(self.scripts) else DONE))


def runtime_config(tmp_path, **extra):
    return dict(config.DEFAULTS, base="https://fixture.invalid", page_uuid="fixture-page", account_id="acc-runtime",
                thread_id="fixture-thread", cookie="accessToken=fixture", xsrf="fixture-xsrf", ua="fixture-agent",
                relay_api_key="fixture-relay-key", cookie_values=[], workspace_root=str(tmp_path),
                provider_resource_name="test-provider", cache_keepwarm_seconds=0, project_context=False,
                host="127.0.0.1", port=0, **extra)


def run_turn(runtime, client_request):
    out = queue.Queue()
    runtime.submit(runtime.runner.run(client_request, out, {})).result(20)
    return [event for event in list(out.queue) if event is not None]


def converse_on(runtime, rounds):
    messages = copy.deepcopy(FIRST)
    for number in range(rounds):
        client_request = parsed(messages)
        events = run_turn(runtime, client_request)
        assert not any(event["type"] == "error" for event in events), events
        messages = append_result(messages, client_request, events, "chat", value="RESULT-%d" % number)
    return messages


@pytest.fixture
def isolated_journal(monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL", journal.Journal())


def test_a_real_runtime_restarts_into_the_conversation_it_stopped_with(tmp_path, monkeypatch, isolated_journal):
    state = tmp_path / "state" / "conversations"
    cfg = runtime_config(tmp_path, conversation_state_dir=str(state))
    wire = Wire(tool_scripts(2) + [DONE])
    monkeypatch.setattr(direct.urllib.request, "urlopen", wire)
    first = proxy.Runtime(dict(cfg))
    try:
        messages = converse_on(first, 2)
    finally:
        first.close()  # a normal stop: the writer finishes what it was handed
    assert len(files(state)) == 1
    second = proxy.Runtime(dict(cfg))
    try:
        assert second.pool._store is not None and second.pool._store.peek(KEY) is not None
        run_turn(second, parsed(messages))
    finally:
        second.close()
    assert signatures_in(wire.sent[-1]) == [signature(0), signature(1)]
    assert len(stored(0, "conversation_state.restored")) == 1


@pytest.mark.parametrize("how", ["switched_off", "no_directory"])
def test_when_it_is_off_or_has_no_directory_no_file_and_no_folder_appear(tmp_path, monkeypatch, isolated_journal, how):
    extra = {"conversation_state": False, "conversation_state_dir": str(tmp_path / "state" / "conversations")} \
        if how == "switched_off" else {}
    wire = Wire(tool_scripts(2) + [DONE])
    monkeypatch.setattr(direct.urllib.request, "urlopen", wire)
    runtime = proxy.Runtime(runtime_config(tmp_path, **extra))
    try:
        messages = converse_on(runtime, 2)
        run_turn(runtime, parsed(messages))
        assert runtime.pool._store is None
    finally:
        runtime.close()
    assert list(tmp_path.rglob("*" + cs.SUFFIX)) == [] and not (tmp_path / "state").exists()
    assert not (tmp_path / "conversations").exists()
    assert signatures_in(wire.sent[-1]) == [signature(0), signature(1)]  # the live conversation still resumes
    assert not stored(0, "conversation_state.saved")


def test_a_normal_stop_waits_for_the_state_being_written(tmp_path, monkeypatch, isolated_journal):
    state = tmp_path / "state"
    monkeypatch.setattr(direct.urllib.request, "urlopen", Wire(tool_scripts(1)))
    runtime = proxy.Runtime(runtime_config(tmp_path, conversation_state_dir=str(state)))
    gate, real = threading.Event(), cs.ConversationStore._write

    def slow(self, key_id, blob):
        gate.wait(5)
        return real(self, key_id, blob)

    monkeypatch.setattr(cs.ConversationStore, "_write", slow)
    try:
        run_turn(runtime, parsed(FIRST))
        assert files(state) == []  # the writer is still holding it
        threading.Timer(0.3, gate.set).start()
    finally:
        runtime.close()
        gate.set()
    assert len(files(state)) == 1


def test_a_stop_cannot_wait_forever_for_a_stuck_writer(tmp_path, monkeypatch):
    store = cs.ConversationStore(tmp_path / "state")
    gate = threading.Event()
    monkeypatch.setattr(cs.ConversationStore, "_write", lambda self, key_id, blob: gate.wait(5))
    store.submit(capture_of("stuck"))
    started = time.monotonic()
    assert store.close(timeout=0.2) is False and time.monotonic() - started < 2
    assert store.submit(capture_of("late")) is False  # nothing is accepted after the stop began
    gate.set()


def test_the_runtime_stop_retries_a_conversation_whose_write_failed(tmp_path, monkeypatch, isolated_journal):
    state = tmp_path / "state"
    monkeypatch.setattr(direct.urllib.request, "urlopen", Wire(tool_scripts(1)))
    real, broken = os.replace, {"on": True}

    def replace(source, target):
        if broken["on"]:
            raise OSError(errno.EIO, "the disk is gone")
        return real(source, target)

    monkeypatch.setattr(cs.os, "replace", replace)
    runtime = proxy.Runtime(runtime_config(tmp_path, conversation_state_dir=str(state)))
    try:
        run_turn(runtime, parsed(FIRST))
        assert runtime.pool._store.flush(5) and files(state) == []
        broken["on"] = False
    finally:
        runtime.close()
    assert len(files(state)) == 1


def test_startup_keeps_the_stored_conversations_in_the_state_directory(tmp_path, monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")

    def write(**extra):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua",
                                    "cookie": "accessToken=fake-token", "xsrf": "fake-xsrf",
                                    "workspace_root": str(tmp_path), "provider_resource_name": "__managed__",
                                    **extra}), encoding="utf-8")
        return str(path)

    state = tmp_path / "state"
    cfg = proxy.startup_config(["--config", write(), "--state-dir", str(state)])
    assert cfg["conversation_state_dir"] == str(state / "conversations")
    # --state-dir moves every state file together, whatever the config says; without it the config's own wins.
    cfg = proxy.startup_config(["--config", write(conversation_state_dir="elsewhere"), "--state-dir", str(state)])
    assert cfg["conversation_state_dir"] == str(state / "conversations")
    cfg = proxy.startup_config(["--config", write(conversation_state_dir="elsewhere")])
    assert cfg["conversation_state_dir"] == "elsewhere"
    cfg = proxy.startup_config(["--config", write()])
    assert cfg["conversation_state_dir"] == os.path.join(os.path.abspath(proxy.HERE), "conversations")


# -- configuration ----------------------------------------------------------------------------------------------------
def test_the_settings_have_the_documented_defaults(tmp_path, monkeypatch):
    loaded = load_with(tmp_path, monkeypatch)
    assert [loaded[key] for key in SETTINGS] == [True, "", 200, 24 * 3600, 32 * 1024 * 1024, 512 * 1024 * 1024]
    assert all(loaded[key] == config.DEFAULTS[key] for key in SETTINGS)
    chosen = load_with(tmp_path, monkeypatch, conversation_state=False, conversation_state_dir="x",
                       conversation_state_max_conversations="7", conversation_state_ttl=60,
                       conversation_state_max_file_bytes=1000, conversation_state_max_total_bytes=1000)
    assert [chosen[key] for key in SETTINGS] == [False, "x", 7, 60, 1000, 1000]


@pytest.mark.parametrize("settings,message", [
    ({"conversation_state": "false"}, "boolean"),
    ({"conversation_state": 1}, "boolean"),
    ({"conversation_state_dir": 7}, "string"),
    ({"conversation_state_ttl": 0}, "positive"),
    ({"conversation_state_max_conversations": -3}, "positive"),
    ({"conversation_state_max_file_bytes": 0}, "positive"),
    ({"conversation_state_max_total_bytes": 0}, "positive"),
    ({"conversation_state_ttl": "soon"}, "must be an integer"),
    ({"conversation_state_max_file_bytes": 2000, "conversation_state_max_total_bytes": 1000}, "must not exceed"),
])
def test_invalid_settings_are_refused_at_start(tmp_path, monkeypatch, settings, message):
    with pytest.raises(apierr.ApiError, match=message):
        load_with(tmp_path, monkeypatch, **settings)


# -- privacy ----------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="permission bits are POSIX")
def test_stored_files_and_their_directory_are_private(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of("private"))
    assert store.flush(5)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file(state, "private").stat().st_mode) == 0o600
    store.close()


def test_the_journal_and_the_console_get_tags_and_counts_never_the_conversation(tmp_path, processes, capsys):
    secrets = ("SECRET-USER-TEXT", "SECRET-SYSTEM-PROMPT", "SECRET-TOOL-RESULT", "SECRET-TOOL-PATH", "SECRET-SIG")
    state = tmp_path / "state"
    scripts = [tool_reply("call-%d" % n, "SECRET-SIG-%d" % n, path="SECRET-TOOL-PATH") for n in range(2)]
    first = [{"role": "user", "content": "Inspect SECRET-USER-TEXT."}]
    before = processes(state, scripts=scripts)
    seq = journal.JOURNAL.last_seq
    messages = asyncio.run(converse(before, "chat", 2, system="SECRET-SYSTEM-PROMPT", first=first,
                                    result="SECRET-TOOL-RESULT-%d"))
    before.stop()
    # The content really is in the stored state (compressed), so the check below is not vacuous.
    _, kept = cs.decode(state_file(state).read_bytes(), 10 ** 9)
    assert all(secret in json.dumps([b.messages + b.assistant + b.client + [b.scope] for b in kept.branches])
               for secret in secrets)
    after = processes(state, scripts=[DONE])
    asyncio.run(after.turn(parsed(messages, system="SECRET-SYSTEM-PROMPT")))
    events = [event for event in journal.JOURNAL.query(since=seq, limit=20000) if event["category"] == "conversation_state"]
    assert {event["kind"] for event in events} >= {"conversation_state.saved", "conversation_state.restored",
                                                   "conversation_state.scan"}
    assert not any(secret in json.dumps(events, ensure_ascii=False) for secret in secrets)
    console = capsys.readouterr().out
    assert "[state] conversation restored" in console
    assert not any(secret in line for line in console.splitlines() if "[state]" in line for secret in secrets)


# -- keepwarm and the cache key ---------------------------------------------------------------------------------------
def test_a_restored_conversation_is_not_warmed_until_a_real_request_arrives(tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[DONE], backend_options={"cache_keepwarm_seconds": 240,
                                                              "cache_keepwarm_idle": 900})
    ensured, pings = [], []
    real_ensure = direct._ensure_keepwarm

    def spy(session, cfg):
        ensured.append(session)
        return real_ensure(session, cfg)

    monkeypatch.setattr(direct, "_ensure_keepwarm", spy)
    monkeypatch.setattr(direct, "_ping", lambda *args: pings.append(args) or 0.0)

    async def story():
        entry = parsed(messages).entry
        lease = await after.pool.acquire(KEY, model_entry=entry)
        session = lease.session
        assert session._native_history.branches and session._warm_body is None and session._warm_task is None
        # Nothing of the keepwarm verdict (the outcome of the last turn) comes back from a file either.
        assert getattr(session, "_turn_outcome", None) is None
        # A subagent's request touches the keepwarm of its ancestors; a restored parent has no body to warm yet.
        child = await after.pool.acquire(KEY + ":agent", model_entry=entry, parent_key=KEY)
        await after.pool.release(child)
        await after.pool.release(lease)
        await asyncio.sleep(0.3)
        assert not ensured and not pings and session._warm_task is None
        await after.turn(parsed(messages))
        assert ensured and session._warm_task is not None  # the real request starts the scheduler as always
        await session.aclose()

    asyncio.run(story())


def test_the_time_of_the_last_cache_touch_comes_back_as_it_was_and_never_as_the_time_of_the_restore(
        tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    touched = before.pool._conversations[KEY]._warm_at  # wall clock: when the request that last touched the cache began
    assert 0 < touched <= time.time()
    before.stop()
    after = processes(state)

    async def story():
        # Whatever the clock says at the moment of the restore, the cache was last touched when it was.
        with monkeypatch.context() as patch:
            patch.setattr(time, "time", lambda: touched + 10_000)
            lease = await after.pool.acquire(KEY, model_entry=parsed(messages).entry)
        assert lease.session._warm_at == touched
        await after.pool.release(lease)

    asyncio.run(story())
    # A conversation whose cache was never touched (0) is stored and restored as exactly that: an expired one.
    store = cs.ConversationStore(tmp_path / "never")
    store.submit(capture_of("untouched"))
    assert store.flush(5)
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"account_id": "acc-one"}),
                              _native_history=native_history.History(), _warm_at=123.0)
    assert asyncio.run(cs.restore(store, "untouched", session)) is True
    assert session._warm_at == 1.5  # capture_of stores a warm_at of 1.5: the stored value, not 123 and not now
    zero = cs.Capture("zero", cs.key_digest("zero"), "acc-one", capture_of("zero").branches, (), "k", 0, 0.0, ("m",))
    store.submit(zero)
    assert store.flush(5)
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"account_id": "acc-one"}),
                              _native_history=native_history.History(), _warm_at=999.0)
    assert asyncio.run(cs.restore(store, "zero", session)) is True and session._warm_at == 0.0
    store.close()


def test_a_disk_that_stalls_costs_the_conversation_its_state_never_the_request_its_answer(
        tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[DONE])
    release, real = threading.Event(), cs.ConversationStore.load

    def stalled(self, key):
        release.wait(5)
        return real(self, key)

    monkeypatch.setattr(cs.ConversationStore, "load", stalled)
    monkeypatch.setattr(cs, "RESTORE_TIMEOUT", 0.2)
    seq = journal.JOURNAL.last_seq

    async def story():
        started = time.monotonic()
        events = await after.turn(parsed(messages))
        release.set()  # the stuck read gives up its thread once the turn has been answered
        return events, time.monotonic() - started

    events, took = asyncio.run(story())
    assert events[-1] == {"type": "end", "reason": "stop"} and took < 4
    assert [event["data"]["error"] for event in stored(seq, "conversation_state.restore_failed")] == ["TimeoutError"]
    assert "SIG-" not in json.dumps(after.sent[0].body)


def test_the_restored_cache_key_lets_the_journal_explain_a_changed_prefix(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, scripts=tool_scripts(2))
    messages = asyncio.run(converse(before, "chat", 2))
    before.stop()
    after = processes(state, scripts=[DONE])
    seq = journal.JOURNAL.last_seq
    asyncio.run(after.turn(parsed(messages, system="A NEW SYSTEM PROMPT")))
    changed = stored(seq, "cache.key_changed")
    assert len(changed) == 1
    assert changed[0]["data"]["old"] == before.sent[-1].body["openAICacheKey"]
    assert changed[0]["data"]["new"] == after.sent[0].body["openAICacheKey"] != changed[0]["data"]["old"]
    assert [event["data"]["reason"] for event in stored(seq, "native_history.reset")] == ["prefix_or_settings_mismatch"]


# -- other kinds of conversation ------------------------------------------------------------------------------------
def marked(call):
    """Indexes of the messages that carry a cache breakpoint."""
    return [index for index, message in enumerate(call.body["messages"]) for block in message.get("content", [])
            if isinstance(block, dict) and block.get("cache")]


def test_a_plain_chat_restarts_on_its_account_and_sends_the_same_cache_marks(tmp_path, processes):
    state, copied = tmp_path / "state", tmp_path / "state-at-restart"

    def chat(messages):
        return request(messages, tools=[], system="PLAIN_SYSTEM", key=KEY)

    steps = [copy.deepcopy(FIRST)]
    for label in ("question 2", "question 3", "question 4", "question 5", "question 6"):
        steps.append(continued(steps[-1], label))
    before = processes(state, accounts=TWO, priority=BIASED)

    async def never_stopped():
        for messages in steps[:5]:
            await before.turn(chat(messages))
        shutil.copytree(state, copied)
        await before.turn(chat(steps[5]))

    asyncio.run(never_stopped())
    assert {call.account for call in before.sent} == {"acc-two"}
    after = processes(copied, accounts=TWO, priority=OPPOSITE)
    asyncio.run(after.turn(chat(steps[5])))
    bare = processes(tmp_path / "no-state", accounts=TWO, priority=OPPOSITE)  # a restart that remembers nothing
    asyncio.run(bare.turn(chat(steps[5])))
    control = before.sent[-1]
    assert after.sent[0].account == "acc-two" and bare.sent[0].account == "acc-one"
    assert after.sent[0].data == control.data
    assert after.store.peek((KEY, "text")) is not None and after.store.peek(KEY) is None
    # The breakpoint of the previous request stays where it was written: the stored marks are what keep it there.
    assert marked(after.sent[0]) == marked(control) != marked(bare.sent[0])


def test_a_restart_before_any_turn_of_a_plain_chat_sends_what_the_running_relay_sends(tmp_path, processes):
    state = tmp_path / "state"

    def chat(messages):
        return request(messages, tools=[], system="PLAIN_SYSTEM", key=KEY)

    steps = [copy.deepcopy(FIRST)]
    for number in range(2, 16):
        steps.append(continued(steps[-1], "question %d" % number))
    before = processes(state, accounts=TWO, priority=BIASED)

    async def never_stopped():
        for number, messages in enumerate(steps):
            if number:
                shutil.copytree(state, tmp_path / ("before-turn-%d" % number))  # the files a restart would find
            await before.turn(chat(messages))

    asyncio.run(never_stopped())
    for number in range(1, len(steps)):
        after = processes(tmp_path / ("before-turn-%d" % number), accounts=TWO, priority=OPPOSITE)
        asyncio.run(after.turn(chat(steps[number])))
        assert after.sent[0].account == "acc-two", number
        assert after.sent[0].data == before.sent[number].data, "restart before turn %d" % number


def test_both_parallel_branches_of_a_conversation_survive_a_restart(tmp_path, monkeypatch, processes):
    state = tmp_path / "state"
    process = processes(state, scripts=[tool_reply("child", "child-sig"), tool_reply("main", "main-sig")])
    real = direct._stream_body

    async def story():
        started, release = asyncio.Event(), asyncio.Event()
        first = True

        async def gated(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                started.set()
                await release.wait()
            async for event in real(*args, **kwargs):
                yield event

        monkeypatch.setattr(direct, "_stream_body", gated)
        main_request, child_request = parsed(FIRST), parsed(FIRST)
        task = asyncio.create_task(process.turn(main_request))
        await asyncio.wait_for(started.wait(), 2)
        child = await process.turn(child_request)  # a second request of the same conversation: a child session
        release.set()
        main = await task
        return main_request, child_request, main, child

    main_request, child_request, main, child = asyncio.run(story())
    process.stop()
    header, kept = cs.decode(state_file(state).read_bytes(), 10 ** 9)
    assert header["branches"] == 2
    after = processes(state, scripts=[])
    follow_child = parsed(append_result(copy.deepcopy(FIRST), child_request, child, "chat"))
    follow_main = parsed(append_result(copy.deepcopy(FIRST), main_request, main, "chat"))
    asyncio.run(after.turn(follow_child))
    asyncio.run(after.turn(follow_main))
    assert "child-sig" in json.dumps(after.sent[0].body) and "main-sig" not in json.dumps(after.sent[0].body)
    assert "main-sig" in json.dumps(after.sent[1].body) and "child-sig" not in json.dumps(after.sent[1].body)


# -- the store and its format -----------------------------------------------------------------------------------------
def test_the_stored_state_round_trips_to_exactly_the_bytes_upstream_would_get(tmp_path):
    odd = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "ab\ud83dcd=="},
        {"type": "text", "text": "Привет, мир 🌍 \u2028 \"quoted\" \\ back"},
        {"type": "tool_call", "toolCallId": "c", "toolName": "t",
         "toolInput": {"z": 1, "a": [1.5, 2, None, True, 1e100, -0.0, 10 ** 30], "n": {"deep": "x", "b": False}}}]},
        {"role": "tool", "content": {"toolCallId": "c", "toolResult": "line1\nline2\ttab"}, "isError": True}]
    snapshot = native_history.Snapshot(json.dumps(["k", {"b": 1, "a": 2}, "head", [], "auto"]), copy.deepcopy(odd),
                                       copy.deepcopy(odd), copy.deepcopy(odd[0]["content"]), copy.deepcopy(odd[0]))
    captured = cs.Capture("k", cs.key_digest("k"), "acc-one", (snapshot,), (0, 4, 9), "cache", 372_000, 12.25, ("m",))
    blob, header, kept = cs.encode(captured, 100.0, 10 ** 6, 10 ** 7)
    assert kept == 1 and header["signatures"] == 2
    _, state = cs.decode(blob, 10 ** 7)
    restored = state.branches[0]
    dumped = lambda value: json.dumps(value, ensure_ascii=True)  # noqa: E731 - what the request body uses
    assert [dumped(restored.messages), dumped(restored.client), dumped(restored.assistant), dumped(restored.echo),
            restored.scope] == [dumped(odd), dumped(odd), dumped(odd[0]["content"]), dumped(odd[0]), snapshot.scope]
    assert restored.client == odd and (state.turn_ends, state.cache_key, state.prefix_tokens, state.warm_at) == (
        [0, 4, 9], "cache", 372_000, 12.25)


def test_conversation_keys_round_trip_and_name_distinct_files():
    keys = ["header:a", ("header:a", "text"), (None, "text"), ("a", ("b", None)), "agent:0123",
            "header:" + "ключ-🌍-" * 40, "header:" + "\ud83d"]
    for key in keys:
        assert cs.key_value(json.loads(json.dumps(cs.key_json(key)))) == key
    assert len({cs.key_digest(key) for key in keys}) == len(keys)
    assert cs.key_digest("header:a") == cs.key_digest("header:a") and len(cs.key_digest("x")) == 32
    with pytest.raises(TypeError):
        cs.key_digest(("a", 7))


def test_a_giant_conversation_key_is_not_stored_instead_of_breaking_the_header_line(tmp_path):
    history = native_history.History()
    history.record("scope", [], [], [{"type": "text", "text": "x"}],
                   {"role": "assistant", "content": [{"type": "text", "text": "x"}]})
    session = SimpleNamespace(backend=SimpleNamespace(cfg={"account_id": "acc-one"}), _native_history=history,
                              _cached_turn_ends=[0], _last_cache_key="k")
    assert cs.capture("header:" + "k" * 100, session) is not None
    assert cs.capture("header:" + "k" * 5000, session) is None
    # The limit counts the characters of the key, in whatever script: the JSON escapes of the header line are
    # at most 12 bytes a character, far inside the 64 KiB the line may have.
    assert cs.capture("header:" + "ключ" * 400, session) is not None
    assert cs.capture("header:" + "🌍" * 2000, session) is not None
    assert cs.capture("header:" + "ключ" * 600, session) is None
    assert cs.capture(("header:" + "k" * 1500, "text" * 200), session) is None  # the parts of a scoped key add up
    assert cs.key_chars(("ab", ("c", None))) == 3 and cs.key_chars(None) == 0
    store = cs.ConversationStore(tmp_path / "state")
    assert store.peek("header:" + "k" * 5000) is None  # asking about one is harmless
    # The longest key that is stored really fits the header line, the way it is written.
    key = "header:" + "🌍" * (cs.MAX_KEY_CHARS - len("header:"))
    store.submit(cs.capture(key, session))
    assert store.flush(5) and store.peek(key) is not None
    assert store.load(key) is not None
    store.close()


def test_a_newer_state_replaces_a_waiting_one_so_the_writer_never_falls_behind(tmp_path, monkeypatch):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    gate, written, real = threading.Event(), [], cs.ConversationStore._write

    def slow(self, key_id, blob):
        if not written:
            gate.wait(5)
        written.append(key_id)
        return real(self, key_id, blob)

    monkeypatch.setattr(cs.ConversationStore, "_write", slow)
    store.submit(capture_of("busy"))
    deadline = time.monotonic() + 5
    while not store._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    for number in range(5):
        store.submit(capture_of("same", pad=number))  # five states of one conversation while the writer is busy
    gate.set()
    assert store.flush(5)
    assert written == [cs.key_digest("busy"), cs.key_digest("same")]  # one write for the five
    _, kept = cs.decode(state_file(state, "same").read_bytes(), 10 ** 9)
    assert len(json.dumps(kept.branches[0].messages)) > 0
    store.close()


def test_peek_names_the_account_and_a_tag_without_reading_the_file(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of(("conv", "text"), account="acc-two"))
    assert store.flush(5)
    meta = store.peek(("conv", "text"))
    assert (meta.account, meta.tag, meta.branches, meta.signatures) == ("acc-two", journal.tag(("conv", "text")), 1, 1)
    state_file(state, ("conv", "text")).write_bytes(b"gone bad")
    assert store.peek(("conv", "text")) == meta  # peek is the index: only a load notices the damage
    assert store.load(("conv", "text")) is None and store.peek(("conv", "text")) is None
    assert store.peek("never-stored") is None and store.peek(("a", 7)) is None
    store.close()


def test_history_counts_the_snapshots_it_records():
    history = native_history.History()
    assert history.revision == 0
    parent = None
    for expected in (1, 2, 3):
        history.record("scope", [], [], [], {}, parent)
        parent = history.branches[-1]
        assert history.revision == expected


def test_a_file_removed_behind_the_stores_back_is_noticed_by_the_next_load(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of("vanishing"))
    assert store.flush(5)
    state_file(state, "vanishing").unlink()
    assert store.peek("vanishing") is not None  # the index has not heard yet
    assert store.load("vanishing") is None and store.peek("vanishing") is None
    store.close()


def test_a_file_another_relay_rewrote_with_another_account_is_learned_not_deleted(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of("shared", account="acc-one"))
    assert store.flush(5)
    other = cs.ConversationStore(state)  # a second relay on the same directory rewrites the conversation
    other.submit(capture_of("shared", account="acc-two"))
    assert other.flush(5)
    seq = journal.JOURNAL.last_seq
    assert store.load("shared") is None  # this relay's index still says acc-one: it must not use the file
    assert store.peek("shared").account == "acc-two" and state_file(state, "shared").exists()
    assert not stored(seq, "conversation_state.dropped")
    assert store.load("shared").state.cache_key == "cache-key"  # and the next request can judge it
    store.close()
    other.close()


def test_reading_and_replacing_the_same_file_from_two_threads_never_shows_a_damaged_state(tmp_path):
    state = tmp_path / "state"
    store = cs.ConversationStore(state)
    store.submit(capture_of("busy"))
    assert store.flush(5)
    seq = journal.JOURNAL.last_seq
    stop, problems, loads = threading.Event(), [], [0]

    def reader():
        while not stop.is_set():
            try:
                loaded = store.load("busy")
                if loaded is not None:
                    loads[0] += 1
                    assert loaded.state.branches and loaded.header["branches"] == 1
            except Exception as exc:  # noqa: BLE001 - the test reports whatever went wrong
                problems.append(repr(exc))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for number in range(60):
            store.submit(capture_of("busy", pad=number))
            if number % 3 == 0:
                assert store.flush(5)
        assert store.flush(5)
    finally:
        stop.set()
        thread.join(5)
    assert problems == [] and loads[0] > 0
    assert not stored(seq, "conversation_state.dropped") and not stored(seq, "conversation_state.save_failed")
    store.close()


# -- modes that keep nothing, and the jobs that follow a conversation -----------------------------------------------
class Served:
    """A real Runtime behind a real HTTP server (chat completions), with the scripted wire."""

    def __init__(self, tmp_path, monkeypatch, state, wire):
        monkeypatch.setattr(direct.urllib.request, "urlopen", wire)
        self.runtime = proxy.Runtime(runtime_config(tmp_path, conversation_state_dir=str(state)))
        self.server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(self.runtime),
                                          threading.BoundedSemaphore(4))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def chat(self, messages, key="e2e-1"):
        body = {"model": "claude-opus-5-5", "stream": False, "tools": TOOLS, "messages":
                [{"role": "system", "content": "CLIENT_SYSTEM"}] + messages}
        # Not urllib: the relay's own upstream call is that module's urlopen, which the test has replaced.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        try:
            connection.request("POST", "/v1/chat/completions", json.dumps(body).encode(), {
                "content-type": "application/json", "authorization": "Bearer fixture-relay-key",
                "x-session-affinity": key})
            answer = connection.getresponse()
            payload = answer.read().decode()
            assert answer.status == 200, payload
            return json.loads(payload)
        finally:
            connection.close()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()


def test_over_http_a_restart_of_the_whole_relay_keeps_the_conversation_byte_for_byte(
        tmp_path, monkeypatch, isolated_journal):
    state, copied = tmp_path / "state" / "conversations", tmp_path / "state-at-restart"
    first_wire = Wire(tool_scripts(3) + [DONE])
    served = Served(tmp_path, monkeypatch, state, first_wire)
    try:
        messages = copy.deepcopy(FIRST)
        for number in range(3):
            answer = served.chat(messages)
            message = answer["choices"][0]["message"]
            assert answer["choices"][0]["finish_reason"] == "tool_calls", answer
            messages = messages + [message, {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"],
                                             "content": "RESULT-%d" % number}]
        assert served.runtime.pool._store.flush(10)
        shutil.copytree(state, copied)
        served.chat(messages)  # the relay that never stopped
        control = first_wire.raw[-1]
    finally:
        served.stop()
    second_wire = Wire([DONE])
    again = Served(tmp_path, monkeypatch, copied, second_wire)
    try:
        answer = again.chat(messages)
        assert answer["choices"][0]["finish_reason"] == "stop"
    finally:
        again.stop()
    assert second_wire.raw[0] == control
    assert signatures_in(second_wire.sent[0]) == [signature(n) for n in range(3)]
    assert [event["data"]["account"] for event in stored(0, "conversation_state.restored")] == [
        journal.tag("acc-runtime")]


# -- modes that keep nothing, and the jobs that follow a conversation -----------------------------------------------
@pytest.mark.parametrize("mode", ["shared", "per_request"])
def test_modes_without_a_conversation_to_name_store_nothing(tmp_path, processes, mode):
    state = tmp_path / "state"
    process = processes(state, scripts=tool_scripts(2), backend_options={"session_mode": mode})

    def chat(messages):
        return request(messages, tools=[], system="PLAIN_SYSTEM", key=KEY)

    async def story():
        await converse(process, "chat", 2)
        await process.turn(chat(FIRST))
        await process.turn(chat(continued(FIRST, "again")))

    asyncio.run(story())
    assert files(state) == [] and process.store.flush(5)


def test_the_shared_session_keeps_choosing_its_own_account_with_a_store_attached(tmp_path, processes):
    process = processes(tmp_path / "state", accounts=TWO, priority=BIASED,
                        backend_options={"session_mode": "shared", "thread_id": "legacy"})

    async def story():
        entry = parsed(FIRST).entry
        # Before the shared session exists, shared mode puts a standalone job on the pool's own account,
        # not wherever the priorities point: a store that knows nothing about the conversation changes nothing.
        lease = await process.pool.acquire("any", isolated=True, model_entry=entry)
        assert lease.backend is process.backends[0]
        await process.pool.release(lease)

    asyncio.run(story())


def test_a_standalone_job_of_a_restored_conversation_goes_where_the_conversation_goes(tmp_path, processes):
    state = tmp_path / "state"
    before = processes(state, accounts=TWO, priority=BIASED, scripts=tool_scripts(1))
    asyncio.run(converse(before, "chat", 1))
    before.stop()
    after = processes(state, accounts=TWO, priority=OPPOSITE)

    async def story():
        entry = parsed(FIRST).entry
        job = await after.pool.acquire(KEY, isolated=True, model_entry=entry)
        assert job.backend is after.backends[1]  # acc-two: the conversation's own account, as for a live one
        await after.pool.release(job)
        other = await after.pool.acquire("header:someone-else", isolated=True, model_entry=entry)
        assert other.backend is after.backends[0]  # a conversation with nothing stored is placed as always
        await after.pool.release(other)

    asyncio.run(story())
