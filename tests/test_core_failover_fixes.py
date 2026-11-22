"""Focused regression tests for Wave 1 Core Proxy Watchdog, Direct, Session, Refresher,
AccountDispatcher, and Orchestrator accounting fixes."""
import asyncio
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import accounts
import apierr
import direct
import minara
import orchestrator
import session as session_mod
import stream_watchdog
import upstream_refresh
import validate


# ---------------------------------------------------------------------------
# 1. Watchdog / Direct / Session detach race & worker isolation (HIGH-2, #18-4)
# ---------------------------------------------------------------------------

def test_watchdog_abort_interrupts_worker_stuck_in_bounded_put(tmp_path, monkeypatch):
    """Per-attempt abort event must wake _bounded_put immediately instead of waiting 120s."""
    put_calls = []
    real_put = direct._bounded_put

    def tracking_put(loop, q, item, is_cancelled, max_wait=120.0):
        put_calls.append(is_cancelled)
        return real_put(loop, q, item, is_cancelled, max_wait=max_wait)

    monkeypatch.setattr(direct, "_bounded_put", tracking_put)

    delta_line = json.dumps({"event": "content_block_delta", "type": "text", "partialText": "x"}).encode()
    payload = b"\n".join([delta_line] * 1030) + b"\n"

    class SlowCloseResp:
        def __iter__(self):
            yield from payload.splitlines(keepends=True)

        def close(self):
            pass

    monkeypatch.setattr(direct.curl_pool, "retool_open", lambda *_a, **_kw: SlowCloseResp())
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path), "turn_timeout": 5}
    sess = session_mod.RelaySession(SimpleNamespace(cfg=cfg, headers=lambda: {}), "t-bp")

    async def scenario():
        raw = direct._stream_body(cfg, sess, {"model": "claude-opus-5-5"})
        await anext(raw)
        while not callable(getattr(sess, "_active_direct_abort", None)):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        sess.abort_active_turn()
        started = time.monotonic()
        try:
            async for _ in raw:
                pass
        except asyncio.CancelledError:
            pass
        assert time.monotonic() - started < 1.5
        assert sess._active_direct_abort is None

    asyncio.run(scenario())


def test_detached_direct_turn_cannot_clobber_next_turns_abort_or_socket(tmp_path, monkeypatch):
    """Turn 1 unwinding late must neither wipe Turn 2's _active_direct_abort nor close Turn 2's socket (#18-4)."""
    turn1_entered = threading.Event()
    turn1_release = threading.Event()
    turn2_entered = threading.Event()
    turn2_closed = threading.Event()
    turn2_resp_obj = []

    class Resp1:
        def __iter__(self):
            turn1_entered.set()
            turn1_release.wait(timeout=3)
            return iter(())

        def close(self):
            pass

    class Resp2:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            turn2_entered.set()
            yield json.dumps({"event": "content_block_delta", "type": "text", "partialText": "from-turn-2"}).encode() + b"\n"
            turn2_closed.wait(timeout=2)
            yield b'{"event": "end_of_stream"}\n'

        def close(self):
            self.closed = True

    attempt = {"n": 0}

    def fake_open(*_a, **_kw):
        attempt["n"] += 1
        if attempt["n"] == 1:
            return Resp1()
        r2 = Resp2()
        turn2_resp_obj.append(r2)
        return r2

    monkeypatch.setattr(direct.curl_pool, "retool_open", fake_open)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path), "turn_timeout": 5}
    sess = session_mod.RelaySession(SimpleNamespace(cfg=cfg, headers=lambda: {}), "t-race")

    async def scenario():
        gen1 = direct._stream_body(cfg, sess, {"model": "claude-opus-5-5"})
        assert (await anext(gen1))["type"] == "usage_start"
        task1 = asyncio.create_task(gen1.__anext__())
        assert await asyncio.to_thread(turn1_entered.wait, 2)
        abort1 = sess._active_direct_abort
        assert callable(abort1)
        sess.abort_active_turn()
        try:
            await task1
        except (StopAsyncIteration, asyncio.CancelledError):
            pass
        await gen1.aclose()

        # Start Turn 2 while Resp1's worker thread is STILL blocked on turn1_release!
        gen2 = direct._stream_body(cfg, sess, {"model": "claude-opus-5-5"})
        assert (await anext(gen2))["type"] == "usage_start"
        abort2 = sess._active_direct_abort
        assert callable(abort2) and abort2 is not abort1

        ev2 = await anext(gen2)
        assert ev2 == {"type": "text", "delta": "from-turn-2"}
        assert await asyncio.to_thread(turn2_entered.wait, 2)
        # Release Resp1's stalled worker while Turn 2 is actively streaming!
        turn1_release.set()
        await asyncio.sleep(0.05)

        assert sess._active_direct_abort is abort2
        assert not turn2_resp_obj[0].closed
        turn2_closed.set()
        rest = [ev async for ev in gen2]
        assert rest[-1] == {"type": "end", "reason": "stop"}
        assert sess._active_direct_abort is None

    try:
        asyncio.run(scenario())
    finally:
        turn1_release.set()
        turn2_closed.set()


def test_worker_retry_isolation_local_response_and_trace(tmp_path, monkeypatch):
    """Unwinding Attempt 1 worker must not close Attempt 2's live response or corrupt Attempt 2's trace (#18-4)."""
    attempt = {"n": 0}

    class RetryableResp1:
        def __iter__(self):
            yield b'{"event": "error", "statusCode": 500, "isRetryable": true, "error": {"message": "tmp"}}\n'
            yield b'{"event": "end_of_stream"}\n'

        def close(self):
            pass

    class OkResp2:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            yield json.dumps({"event": "content_block_delta", "type": "text", "partialText": "ok-2"}).encode() + b"\n"
            assert not self.closed, "Attempt 1 closed Attempt 2's live response!"
            yield b'{"event": "end_of_stream"}\n'

        def close(self):
            self.closed = True

    def opener(*_a, **_kw):
        attempt["n"] += 1
        return RetryableResp1() if attempt["n"] == 1 else OkResp2()

    monkeypatch.setattr(direct, "RETRYABLE_WAITS", (0.01,))
    monkeypatch.setattr(direct.curl_pool, "retool_open", opener)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path), "turn_timeout": 5}
    sess = session_mod.RelaySession(SimpleNamespace(cfg=cfg, headers=lambda: {}), "t-retry")
    events = asyncio.run(_collect(direct._stream_body(cfg, sess, {"model": "claude-opus-5-5"})))
    assert any(e == {"type": "text", "delta": "ok-2"} for e in events)
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_stream_watchdog_shutdown_closes_iterator_after_late_pending():
    """Even when pending unwinds slightly after close_grace, _shutdown must still invoke aclose()."""
    closed = {"done": False}
    release = asyncio.Event()

    async def slow_source():
        try:
            yield {"type": "usage_start"}
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                await release.wait()
                raise
        finally:
            closed["done"] = True

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.call_later(0.08, release.set)
        try:
            async for _ in stream_watchdog.watch(
                slow_source(), first_timeout=0.02, idle_timeout=0.02, close_grace=0.02,
                tool_timeout=None, thinking_timeout=None,
            ):
                pass
        except apierr.ApiError as exc:
            assert exc.status == 504
        await asyncio.sleep(0.15)
        assert closed["done"] is True

    asyncio.run(scenario())


def test_session_pool_quarantines_detached_direct_session_on_release_and_reacquire(monkeypatch):
    """Unsettled detached direct turn triggers _direct_detached in stream(); release() closes it and next acquire() isolates."""
    class DummyBackend:
        cfg = {"transport": "direct", "session_mode": "per_conversation", "thread_id": "t-det",
               "max_workers": 4, "upstream_timeout": 5,
               "stream_first_token_timeout": 0.02, "stream_idle_timeout": 0.02}

        async def create_thread(self):
            return "t-det"

    real_guard = stream_watchdog.guard
    monkeypatch.setattr(stream_watchdog, "guard",
                        lambda src, cfg, on_timeout=None, close_grace=0.01: real_guard(
                            src, cfg, on_timeout=on_timeout, close_grace=0.01))
    pool = session_mod.SessionPool(DummyBackend())
    release = threading.Event()

    async def scenario():
        lease1 = await pool.acquire("conv-det")
        sess1 = lease1.session
        sess1._native_history = "keep-me"

        async def detached_turn(*_a, **_kw):
            sess1._busy = True
            sess1._active_direct_abort = lambda: None
            yield {"type": "usage_start"}
            try:
                await asyncio.sleep(5)
            finally:
                # Delay unwind past close_grace (0.01s) so watch() detaches with _busy still True.
                while not release.is_set():
                    await asyncio.sleep(0.02)
                sess1._busy = False
                sess1._active_direct_abort = None

        sess1._stream_turn = detached_turn
        with pytest.raises(apierr.ApiError) as exc_info:
            async for _ in sess1.stream("p", {}):
                pass
        assert exc_info.value.status == 504
        assert sess1._direct_detached is True
        await pool.release(lease1)
        assert sess1._closed is True

        lease2 = await pool.acquire("conv-det")
        sess2 = lease2.session
        assert sess2 is not sess1
        assert sess2._closed is False
        assert sess2._native_history == "keep-me"
        assert sess2._direct_detached is False
        release.set()
        await asyncio.sleep(0.05)
        await pool.release(lease2)

    try:
        asyncio.run(scenario())
    finally:
        release.set()


# ---------------------------------------------------------------------------
# 2. stream_watchdog opens_tool_call with non-empty first arguments (#18-7)
# ---------------------------------------------------------------------------

def test_partial_tool_call_with_initial_arguments_uses_tool_budget():
    """OpenAI Chat / Minara first partial tool_call already carrying arguments must enter TOOL (600s) budget."""
    cfg = {"stream_first_token_timeout": 0.04, "stream_idle_timeout": 0.04, "stream_tool_timeout": 0.4}

    async def stream():
        yield {"type": "tool_call", "id": "c1", "name": "Write", "partial": True, "arguments": '{"path": "a.py"'}
        await asyncio.sleep(0.12)  # > idle (0.04s), < tool (0.4s)
        yield {"type": "tool_call", "id": "c1", "name": "Write", "arguments": '{"path": "a.py"}'}
        yield {"type": "end", "reason": "tool_calls"}

    async def run():
        return [ev async for ev in stream_watchdog.guard(stream(), cfg)]

    events = asyncio.run(run())
    assert events[-1] == {"type": "end", "reason": "tool_calls"}


# ---------------------------------------------------------------------------
# 3. Upstream Refresher bypasses MIN_INTERVAL on rejected same cookie (HIGH-3)
# ---------------------------------------------------------------------------

def test_refresher_bypasses_min_interval_when_rejected_cookie_unchanged():
    cfg = {
        "base": "https://x.invalid", "page_uuid": "p", "ua": "ua",
        "cookie": "accessToken=stale; xsrfToken=xsrf1", "xsrf": "xsrf1",
        "cookie_values": ["stale"], "upstream_timeout": 5,
    }
    calls = []

    def http_refresh():
        calls.append(cfg["cookie"])
        if len(calls) == 1:
            return []  # 200 without rotation -> cookie remains "stale"
        return ["accessToken=fresh-2; Path=/"]

    refresher = upstream_refresh.Refresher(cfg, http_refresh=http_refresh)
    assert refresher.refresh() is True  # keepalive succeeds without rotation (_last_result=True)
    # Within MIN_INTERVAL, Retool rejects the exact same unrotated cookie with 401/403:
    assert refresher.refresh(rejected_cookie="accessToken=stale; xsrfToken=xsrf1") is True
    assert len(calls) == 2
    assert "accessToken=fresh-2" in cfg["cookie"]
    # Another caller still holding the OLD rejected cookie after rotation adopts without extra HTTP call:
    assert refresher.refresh(rejected_cookie="accessToken=stale; xsrfToken=xsrf1") is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 4. AccountDispatcher concurrent remove_account safety & recheck (HIGH-6, #12-7)
# ---------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, name, upstream="retool", **extra):
        self.cfg = {"account_id": name, "upstream": upstream, **extra}
        self.unavailable = None
        self.refused_models = {}
        self.served_models = {}
        self.credits = None
        self.refresher = None


def test_account_dispatcher_handles_concurrent_remove_account_without_keyerror():
    b = _FakeBackend("b1", upstream="minara", bearer_token="t")
    def fake_get_balance(force_refresh=True):
        dispatcher.remove_account(b)
        return {"available": 10}

    manager = SimpleNamespace(
        get_balance=fake_get_balance,
        note_refusal=lambda: dispatcher.remove_account(b),
    )
    dispatcher = accounts.AccountDispatcher([b], {}, {b: manager})
    assert dispatcher.check_balance_now(b) is False

    b2 = _FakeBackend("b2", upstream="minara", bearer_token="t")
    mgr2 = SimpleNamespace(note_refusal=lambda: dispatcher.remove_account(b2),
                           get_balance=lambda force_refresh=True: {"available": 0})
    dispatcher.add_account(b2, mgr2)
    assert dispatcher.mark_minara_refusal(b2, apierr.ApiError(402, "insufficient_credits", "low")) is False
    assert dispatcher.mark_rate_limited(b) is None
    assert dispatcher.confirm_minara_token(b) is False
    assert dispatcher.refresh(b) is False

    # Also test remove_account racing mid-refresh_detailed() on a Retool backend:
    r = _FakeBackend("r-race", upstream="retool")
    r.refresher = SimpleNamespace(
        refresh_detailed=lambda force=True: (dispatcher.remove_account(r), False, "auth", "gone")[1:],
    )
    dispatcher.add_account(r, SimpleNamespace())
    assert dispatcher.refresh(r) is False


def test_recheck_exhausted_handles_mixed_route_aliases_and_universal_providers():
    retool_b = _FakeBackend("r1", upstream="retool")
    minara_b = _FakeBackend("m1", upstream="minara")
    univ_b = _FakeBackend("u1", upstream="openai", provider_id="custom-openai")
    dispatcher = accounts.AccountDispatcher(
        [retool_b, minara_b, univ_b], {},
        {b: object() for b in (retool_b, minara_b, univ_b)},
    )
    for b in (retool_b, minara_b, univ_b):
        dispatcher.state[b]["reasons"]["exhausted"] = {"available": 0}
    asked = []

    def check_now(backend):
        asked.append(backend)
        return True  # remains exhausted

    dispatcher.check_balance_now = check_now

    univ_entry = {"id": "custom/gpt", "upstream": "openai", "provider_id": "custom-openai"}
    assert dispatcher.recheck_exhausted(univ_entry) is False
    assert asked == [univ_b]

    asked.clear()
    mixed_alias = {
        "id": "smart-mix",
        "upstream": "retool",
        "route_targets": [
            {"id": "claude-opus-5-5", "upstream": "retool"},
            {"id": "minara/gpt-6", "upstream": "minara"},
        ],
    }
    assert dispatcher.recheck_exhausted(mixed_alias) is False
    assert set(asked) == {retool_b, minara_b}


# ---------------------------------------------------------------------------
# 5. SessionPool reserve race (HIGH-16) and peek() reserve retention (#12-12)
# ---------------------------------------------------------------------------

def test_next_backend_reserve_tolerates_concurrent_clear_of_unavailable():
    b1 = _FakeBackend("b1")
    b1.unavailable = {"reason": "exhausted", "available": 10.0}

    class FlakyBackend(_FakeBackend):
        def __init__(self):
            super().__init__("b2")
            self._calls = 0

        @property
        def unavailable(self):
            self._calls += 1
            if self._calls <= 2:
                return {"reason": "exhausted", "available": 5.0}
            return None

        @unavailable.setter
        def unavailable(self, value):
            pass

    flaky = FlakyBackend()
    pool = session_mod.SessionPool(SimpleNamespace(cfg={
        "session_mode": "per_conversation", "thread_id": "t", "max_workers": 4, "upstream_timeout": 5}))
    pool.backends = [b1, flaky]
    chosen = pool._next_backend(None)
    assert chosen in (b1, flaky)


def test_peek_keeps_non_route_session_on_exhausted_reserve_backend():
    cfg = {"session_mode": "per_conversation", "thread_id": "t", "max_workers": 4,
           "upstream_timeout": 5, "provider_resource_name": "__managed__"}
    b1 = _FakeBackend("b1", **cfg)
    b1.unavailable = {"reason": "exhausted", "available": 5.0}
    pool = session_mod.SessionPool(SimpleNamespace(cfg=cfg))
    pool.backends = [b1]
    sess = session_mod.RelaySession(b1, "t1")
    pool._conversations["conv-reserve"] = sess
    entry = {"id": "claude-opus-5-5", "provider": "anthropic", "upstream": "retool"}
    assert pool.peek("conv-reserve", model_entry=entry) is sess


# ---------------------------------------------------------------------------
# 6. Orchestrator HIGH-9, #18-9, #18-8, #18-5
# ---------------------------------------------------------------------------

def test_orchestrator_resets_sent_attempt_on_midstream_usage_start_and_bills_credits(tmp_path, monkeypatch):
    """HIGH-9: unsent retry after usage_start must not call finish_provider_attempt.
    #18-8 / #18-5: bound_entry drives breakdown/write_cost/minara_turn/switch, and credits call _client_key_recorder."""
    finished = []
    started_turns = []
    client_key_records = []

    class FakeAccountCredits:
        def start_provider_turn(self):
            started_turns.append(1)

        def record_provider_usage(self, attempt_id, turn_id, usage, final=False):
            finished.append(("usage", attempt_id, turn_id, dict(usage), final))

        def finish_provider_attempt(self, attempt_id, turn_id):
            finished.append(("finish", attempt_id, turn_id))

        def record_turn(self, tid, val):
            finished.append(("record_turn", tid, val))

    minara_cfg = {"upstream": "minara", "bearer_token": "tok", "account_id": "acc-m", "max_workers": 4,
                  "upstream_timeout": 5, "minara_models": ["anthropic/claude-opus-5-5"]}
    alias_entry = {
        "id": "smart-alias",
        "provider": "anthropic",
        "upstream": "retool",
        "route_targets": [
            {"id": "claude-opus-5-5", "provider": "anthropic", "upstream": "retool"},
            {"id": "minara/claude-opus-5-5", "provider": "anthropic", "upstream": "minara",
             "upstream_model": "anthropic/claude-opus-5-5", "alias_id": "smart-alias",
             "cache": "explicit", "pricing": {"prompt": 10.0, "completion": 30.0, "cache_write": 12.5}},
        ],
    }

    fake_sess = session_mod.RelaySession(SimpleNamespace(cfg=minara_cfg, headers=lambda: {}, credits=FakeAccountCredits()), "t-orch")
    bound_target = alias_entry["route_targets"][1]
    lease = session_mod.SessionLease(fake_sess)
    lease.bound_entry = bound_target

    class FakePool:
        backends = [fake_sess.backend]

        async def acquire(self, *_a, **_kw):
            return lease

        async def release(self, _l):
            pass

    async def scripted_stream(*_a, **_kw):
        fake_sess._provider_request_sent()
        yield {"type": "credits", "value": 0.5, "usage": {"input_tokens": 12, "output_tokens": 8}}
        yield {"type": "usage_start"}
        raise apierr.upstream_error("aborted before send")

    monkeypatch.setattr(fake_sess, "stream", scripted_stream)
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
           "context_max_bytes": 100, "max_exec_turns": 0, "cookie_values": [], "transport": "direct"}
    runner = orchestrator.TurnRunner(cfg, FakeAccountCredits(), FakePool())
    parsed = validate.parse_chat({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]},
                                 {"strict_models": True})
    parsed.model = "smart-alias"
    parsed.entry = alias_entry
    holder = {
        "_client_key_id": "ck-1",
        "_client_key_recorder": lambda kid, **kw: client_key_records.append((kid, kw)),
    }

    async def run_one():
        out = queue.Queue()
        await runner.run(parsed, out, holder)
        return [ev for ev in list(out.queue) if ev is not None]

    events = asyncio.run(run_one())
    assert events[-1]["type"] == "error"

    finish_ids = [item[1] for item in finished if item[0] == "finish"]
    assert len(finish_ids) == 1 and finish_ids[0].endswith(":1")
    assert client_key_records == [("ck-1", {"input_tokens": 12, "output_tokens": 8, "cost_usd": None})]


def test_orchestrator_uses_bound_entry_for_switch_to_messages_and_unpriced(tmp_path, monkeypatch):
    """#18-8: alias entry must not prevent bound Minara target from switching uncached channels or pricing."""
    switched = []
    monkeypatch.setattr(minara, "switch_to_messages",
                        lambda upstream, level, switched_flag=True, reason=None: switched.append((upstream, level, reason)))

    class FakeAccountCredits:
        def start_provider_turn(self):
            pass

        def record_provider_usage(self, *_a, **_kw):
            pass

        def finish_provider_attempt(self, *_a, **_kw):
            pass

    minara_cfg = {"upstream": "minara", "bearer_token": "tok", "account_id": "acc-m2", "max_workers": 4,
                  "upstream_timeout": 5, "minara_models": ["anthropic/claude-opus-5-5"]}
    alias_entry = {
        "id": "smart-opus",
        "provider": "anthropic",
        "upstream": "retool",
        "route_targets": [
            {"id": "claude-opus-5-5", "provider": "anthropic", "upstream": "retool"},
            {"id": "minara/claude-opus-5-5", "provider": "anthropic", "upstream": "minara",
             "upstream_model": "anthropic/claude-opus-5-5", "alias_id": "smart-opus",
             "cache": "explicit", "pricing": {"prompt": "invalid"}},
        ],
    }
    fake_sess = session_mod.RelaySession(SimpleNamespace(cfg=minara_cfg, headers=lambda: {}, credits=FakeAccountCredits()), "t-b8")
    bound_target = alias_entry["route_targets"][1]
    lease = session_mod.SessionLease(fake_sess)
    lease.bound_entry = bound_target

    class FakePool:
        backends = [fake_sess.backend]

        async def acquire(self, *_a, **_kw):
            return lease

        async def release(self, _l):
            pass

    async def scripted_stream(*_a, **_kw):
        yield {"type": "usage", "usage": {"input_tokens": 3000, "output_tokens": 10,
                                          "cache_write_reported": False, "channel": "bedrock", "channel_path": "chat"}}
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(fake_sess, "stream", scripted_stream)
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
           "context_max_bytes": 100, "max_exec_turns": 0, "cookie_values": [], "transport": "direct"}
    runner = orchestrator.TurnRunner(cfg, FakeAccountCredits(), FakePool())
    parsed = validate.parse_chat({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]},
                                 {"strict_models": True})
    parsed.model = "smart-opus"
    parsed.entry = alias_entry
    assert orchestrator.TurnRunner._unpriced(parsed, {"input_tokens": 5}, bound_target)[1].get("price_problem")

    async def run_one():
        out = queue.Queue()
        await runner.run(parsed, out, {})
        return [ev for ev in list(out.queue) if ev is not None]

    events = asyncio.run(run_one())
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert switched and switched[0][0] == "anthropic/claude-opus-5-5"


# ---------------------------------------------------------------------------
# 7. R4 openAICacheKey scoping & TTL _ASSIST_ONLY isolation (direct.py)
# ---------------------------------------------------------------------------

def test_retool_cache_key_isolates_conversations_and_credentials():
    head = "stable head " * 20
    tools = [{"name": "read", "description": "r", "params": {}}]
    s_a1 = SimpleNamespace(_native_conversation_key="conv-1", _credential_scope="cred-A")
    s_a2 = SimpleNamespace(_native_conversation_key="conv-2", _credential_scope="cred-A")
    s_b1 = SimpleNamespace(_native_conversation_key="conv-1", _credential_scope="cred-B")
    body_a1 = direct.build_body({}, {"model": "claude-opus-5-5"}, head, "hi", tools, "auto", 100, session=s_a1)
    body_a2 = direct.build_body({}, {"model": "claude-opus-5-5"}, head, "hi", tools, "auto", 100, session=s_a2)
    body_b1 = direct.build_body({}, {"model": "claude-opus-5-5"}, head, "hi", tools, "auto", 100, session=s_b1)
    assert body_a1["openAICacheKey"] != body_a2["openAICacheKey"]
    assert body_a1["openAICacheKey"] != body_b1["openAICacheKey"]
    # Explicit kwarg matching resumed history keeps identical cache key:
    body_resumed = direct.build_body(
        {}, {"model": "claude-opus-5-5"}, head, "", tools, "auto", 100,
        session=None, conversation_key="conv-1", credential_scope="cred-A",
    )
    assert body_resumed["openAICacheKey"] == body_a1["openAICacheKey"]


def test_assist_learning_is_account_scoped_and_expires_after_ttl(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(direct.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(direct, "_ASSIST_LEARNED", {})
    sess_a = SimpleNamespace(backend=SimpleNamespace(cfg={"account_id": "acc-A"}))
    sess_b = SimpleNamespace(backend=SimpleNamespace(cfg={"account_id": "acc-B"}))
    direct._remember_assist_only("claude-custom-x", sess_a)
    # Learned for Account A, never touches global _ASSIST_ONLY or Account B:
    assert "claude-custom-x" not in direct._ASSIST_ONLY
    assert direct._is_assist_only("claude-custom-x", sess_a) is True
    assert direct._is_assist_only("claude-custom-x", sess_b) is False
    # Expires cleanly after _ASSIST_TTL_SECONDS:
    clock["t"] += direct._ASSIST_TTL_SECONDS + 1.0
    assert direct._is_assist_only("claude-custom-x", sess_a) is False


async def _collect(agen):
    return [ev async for ev in agen]
