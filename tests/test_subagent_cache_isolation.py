"""A child session must leave the parent's cache alive while it does its work."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

import apierr
import client_context
import direct
import validate
from test_native_context_reuse import FIRST, continued, request, run, setup


def headers(session, parent=None):
    result = {"x-proxi-client": "opencode", "x-session-affinity": "main",
              "x-session-id": "main", "x-opencode-session": "main",
              "x-opencode-session-id": session, "x-proxi-chat-title": session}
    if parent:
        result["x-opencode-parent-session-id"] = parent
    return result


@pytest.mark.parametrize("session_header", ["x-opencode-session-id", "x-proxi-session-id"])
def test_http_prefers_real_child_id_over_parent_affinity(relay, session_header):
    payload = {"model": "claude-opus-5-5", "messages": FIRST}
    for session, parent in (("main", None), ("child", "main"), ("child", "main"), ("main", None)):
        hdr = headers(session, parent)
        hdr[session_header] = hdr.pop("x-opencode-session-id")
        assert relay.json("/v1/chat/completions", payload, headers=hdr)[0] == 200
    main, child, following, resumed = relay.fake.seen
    assert main.conversation_key == resumed.conversation_key == "header:main"
    assert child.conversation_key == following.conversation_key == "header:child"
    assert child.parent_conversation_key == following.parent_conversation_key == main.conversation_key
    assert main.parent_conversation_key is None


def test_claude_agent_ids_are_stable_and_distinct_from_session_and_display_names():
    hdr = {"x-session-affinity": "main", "x-claude-code-agent-id": "agent-a"}
    first = validate.conversation_affinity({}, hdr)
    assert first[1] == "header:main" and first[0] != first[1]
    assert first == validate.conversation_affinity({}, {**hdr, "x-proxi-agent": "renamed"})
    assert first[0] != validate.conversation_affinity({}, {**hdr, "x-claude-code-agent-id": "agent-b"})[0]
    assert validate.conversation_affinity({}, {"x-session-affinity": "main", "x-proxi-agent": "explore"}) == (
        "header:main", None)


def test_blank_specific_id_does_not_hide_valid_legacy_affinity():
    assert validate.conversation_affinity({}, {"X-Proxi-Session-Id": "  ", "X-Session-Affinity": " main "}) == (
        "header:main", None)


def test_claude_metadata_parent_is_available_without_a_title_lookup(tmp_path):
    import json

    identity = {"session_id": "child", "parent_session_id": "main"}
    data = {"metadata": {"user_id": json.dumps(identity)}}
    info = client_context.describe(data, {"x-proxi-chat-title": "known title"},
                                   {"client_metadata_home": str(tmp_path)})
    assert validate.conversation_affinity(data, {}, info) == ("claude-metadata:child", "claude-metadata:main")


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
@pytest.mark.parametrize("minutes", [26, 40])
def test_parent_keeps_warming_during_long_child_and_resumes_its_own_cache(tmp_path, monkeypatch, mode, minutes):
    _, pool, runner = setup(tmp_path, transport="direct", session_mode=mode, project_context=False,
                            cookie_values=[], cache_keepwarm_seconds=240, cache_keepwarm_idle=1800,
                            conversation_idle_timeout=900, turn_timeout=3600)
    clock = SimpleNamespace(now=1000.0)
    real_sleep = asyncio.sleep
    sent, pings = [], []
    child_started, child_done = asyncio.Event(), asyncio.Event()

    async def sleep(_delay):
        await real_sleep(0)

    async def to_thread(fn, *args):
        return fn(*args)

    def ping(target, body):
        pings.append((clock.now, target, body["model"], body["openAICacheKey"]))
        target._last_warm_usage = {"promptTokensReadFromCache": 319000}
        return 0.1

    async def stream(_cfg, target, body, _params):
        sent.append((target, copy.deepcopy(body)))
        if body["model"] == "claude-sonnet-5-5":
            child_started.set()
            await child_done.wait()
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "_stream_body", stream)
    monkeypatch.setattr(direct, "_ping", ping)
    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    monkeypatch.setattr(direct.asyncio, "to_thread", to_thread)
    main = request(FIRST, system="MAIN_SYSTEM " * 120, key="header:main")
    child = request(FIRST, system="CHILD_SYSTEM " * 120, key="header:child")
    child.model = "claude-sonnet-5-5"
    child.entry = validate.parse_chat({"model": child.model, "messages": FIRST},
                                     {"strict_models": True}).entry
    child.tools[0]["function"]["name"] = "child_only_tool"
    child.parent_conversation_key = "header:main"

    async def scenario():
        first_holder, child_holder, resumed_holder = {}, {}, {}
        assert (await run(runner, main, first_holder))[-1]["type"] == "end"
        parent = first_holder["session"].session
        warm_body = copy.deepcopy(parent._warm_body)
        child_task = asyncio.create_task(run(runner, child, child_holder))
        await child_started.wait()
        try:
            worker = child_holder["session"].session
            assert worker is not parent and parent._cache_dependents == 1
            for elapsed in range(120, minutes * 60 + 1, 120):
                clock.now = 1000.0 + elapsed
                # Simulate other callers triggering pool eviction after the idle TTL.
                for key, value in pool._conversations.items():
                    if value is parent:
                        pool._last_used[key] = time.monotonic() - elapsed
                await pool._evict_idle()
                for _ in range(12):
                    await real_sleep(0)
                assert parent._warm_body == warm_body
                assert not parent._warm_task.done()
            parent_pings = [p for p in pings if p[1] is parent]
            assert len(parent_pings) >= minutes * 60 // 240
            assert all(b[0] - a[0] <= 240 for a, b in zip(parent_pings, parent_pings[1:]))
            assert all(p[2:] == ("claude-opus-5-5", warm_body["openAICacheKey"]) for p in parent_pings)
            assert any(p[1] is worker and p[2] == "claude-sonnet-5-5" for p in pings)
            child_done.set()
            assert (await child_task)[-1]["type"] == "end"
            assert parent._cache_dependents == 0
            resumed = request(continued(FIRST, "CHILD_REPORT"), system=main.system, key=main.conversation_key)
            assert (await run(runner, resumed, resumed_holder))[-1]["type"] == "end"
            assert resumed_holder["session"].session is parent
            assert sent[-1][1]["openAICacheKey"] == warm_body["openAICacheKey"]
            assert "child_only_tool" not in str(sent[-1][1])
            assert not pool._active and pool._slots._value == 8
            # Completion must not leave the ancestor warming forever.
            clock.now += 1801
            for _ in range(12):
                await real_sleep(0)
            assert parent._warm_task.done()
        finally:
            child_task.cancel()
            await asyncio.gather(child_task, return_exceptions=True)
            for target in {s for s, _ in sent}:
                await target.aclose()

    asyncio.run(scenario())


def test_failed_child_acquire_does_not_leak_parent_pin_or_capacity(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", max_conversations=1)

    async def scenario():
        lease = await pool.acquire("header:main")
        parent = lease.session
        await pool.release(lease)
        with pytest.raises(apierr.ApiError, match="pool is full"):
            await pool.acquire("header:child", parent_key="header:main")
        assert parent._cache_dependents == 0
        assert pool._slots._value == 8 and not pool._active

    asyncio.run(scenario())


def test_nested_child_pins_all_ancestors_and_duplicate_release_is_safe(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct")

    async def scenario():
        main = await pool.acquire("main")
        await pool.release(main)
        child = await pool.acquire("child", parent_key="main")
        await pool.release(child)
        grandchild = await pool.acquire("grandchild", parent_key="child")
        assert main.session._cache_dependents == child.session._cache_dependents == 1
        await pool.release(grandchild)
        await pool.release(grandchild)
        assert main.session._cache_dependents == child.session._cache_dependents == 0
        assert pool._slots._value == 8

    asyncio.run(scenario())


def test_cancelled_child_acquire_unpins_parent_and_restores_slot(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct")

    async def scenario():
        main = await pool.acquire("main")
        await pool.release(main)
        started = asyncio.Event()

        async def create(_backend=None):
            started.set()
            await asyncio.Event().wait()

        pool._new_thread_id = create
        child = asyncio.create_task(pool.acquire("child", parent_key="main"))
        await started.wait()
        assert main.session._cache_dependents == 1
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child
        assert main.session._cache_dependents == 0
        assert pool._slots._value == 8 and not pool._active
        assert set(pool._conversation_locks) == {"main"}

    asyncio.run(scenario())
