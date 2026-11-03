"""A regular text conversation keeps its own warm cache between HTTP turns."""
import asyncio
from types import SimpleNamespace

import pytest

import direct
from test_native_context_reuse import FIRST, request, run, setup
from test_service_requests import capture_direct


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
@pytest.mark.parametrize("local_exec", [False, True])
def test_plain_chat_keeps_warming_after_release_through_a_pause_over_the_ttl(tmp_path, monkeypatch, mode, local_exec):
    _, pool, runner = setup(tmp_path, transport="direct", session_mode=mode, project_context=False,
                            exec_policy="allowlist" if local_exec else "off", cookie_values=[],
                            cache_keepwarm_seconds=240, cache_keepwarm_idle=1800)
    clock = SimpleNamespace(now=1000.0)
    sleeps = asyncio.sleep
    pings = []
    sent = []

    async def stream(_cfg, target, body, _params):
        sent.append((target, body))
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    async def sleep(delay):
        clock.now += delay
        if clock.now >= 1725:
            raise asyncio.CancelledError()
        await sleeps(0)

    def ping(target, body):
        pings.append((clock.now, target, body))
        return 0

    monkeypatch.setattr(direct, "_stream_body", stream)
    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    monkeypatch.setattr(direct, "_ping", ping)
    parsed = request(FIRST, tools=[], system="PLAIN_CHAT_SYSTEM")

    async def scenario():
        holder = {}
        events = await run(runner, parsed, holder)
        assert events[-1] == {"type": "end", "reason": "stop"}
        target = holder["session"].session
        task = target._warm_task
        assert holder["session"].released and not pool._active
        assert task is not None and not task.cancelling() and not task.done()
        await task
        assert len(pings) == 3
        assert all(b[0] - a[0] < 241 for a, b in zip(pings, pings[1:]))
        assert all(item[1] is target for item in pings)
        second = {}
        await run(runner, parsed, second)
        assert second["session"].session is target
        assert sent[0][1]["openAICacheKey"] == sent[1][1]["openAICacheKey"]
        await target.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
@pytest.mark.parametrize("native_first", [False, True])
def test_native_and_text_contexts_keep_account_affinity_in_both_creation_orders(tmp_path, mode, native_first):
    from test_native_context_reuse import Backend

    _, pool, _ = setup(tmp_path, transport="direct", session_mode=mode, project_context=False)
    pool.backends.append(Backend(tmp_path, provider_resource_name="other-account"))

    async def scenario():
        async def take(native):
            return await pool.acquire("chat", **({} if native else {"context_scope": "text"}))
        first = await take(native_first)
        second = await take(not native_first)
        assert first.backend is second.backend
        assert first.session is not second.session
        assert len(pool._active) == 2
        await pool.release(first)
        await pool.release(second)
        assert pool._slots._value == 8
        again = await take(native_first)
        assert again.session is first.session
        await pool.release(again)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
def test_known_service_request_never_leaves_an_idle_keepwarm_task(tmp_path, monkeypatch, mode):
    from test_service_requests import CLASSIFIER

    _, pool, runner = setup(tmp_path, transport="direct", session_mode=mode, project_context=False,
                            cookie_values=[], cache_keepwarm_seconds=240)
    tasks = set()
    ensure = direct._ensure_keepwarm

    def track(target, cfg):
        ensure(target, cfg)
        if target._warm_task:
            tasks.add(target._warm_task)

    async def stream(_cfg, _target, _body, _params):
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "_ensure_keepwarm", track)
    monkeypatch.setattr(direct, "_stream_body", stream)
    monkeypatch.setattr(direct, "_ping", lambda *_args: pytest.fail("service left an idle ping"))
    parsed = request(FIRST, tools=[], system="SDK wrapper\n" + CLASSIFIER)

    async def scenario():
        events = await run(runner, parsed)
        assert events[-1] == {"type": "end", "reason": "stop"}
        assert tasks and all(task.cancelling() or task.done() for task in tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(result is None or isinstance(result, asyncio.CancelledError) for result in results)
        assert not pool._conversations and not pool._active and pool._shared is None

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
def test_persistent_text_session_cannot_replace_a_native_head_or_inherit_tools(tmp_path, monkeypatch, mode):
    _, pool, runner = setup(tmp_path, transport="direct", session_mode=mode,
                            project_context=False, cookie_values=[])
    sent = capture_direct(monkeypatch)
    native = request(FIRST, system="NATIVE_SYSTEM")
    text = request(FIRST, tools=[], system="PLAIN_SYSTEM")

    async def scenario():
        for parsed in (native, text, native, text):
            events = await run(runner, parsed)
            assert events[-1] == {"type": "end", "reason": "stop"}
        assert sent[0][0] is sent[2][0]
        assert sent[1][0] is sent[3][0]
        assert sent[0][0] is not sent[1][0]
        assert sent[0][1] == sent[2][1]
        assert sent[1][1] == sent[3][1]
        assert not sent[1][1].get("tools") and "NATIVE_SYSTEM" not in str(sent[1][1])
        assert not pool._active

    asyncio.run(scenario())
