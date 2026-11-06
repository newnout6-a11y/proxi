"""Offline shutdown checks: no live relay, credentials or external requests."""
import asyncio
import contextvars
import gc
import signal
import socket
import subprocess
import sys
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import warnings

import pytest

import client_context
import config
import direct
import journal
import proxy
from session import RelaySession


@pytest.mark.parametrize("closed", [False, True])
def test_bounded_put_does_not_create_coroutines_for_an_inactive_loop(closed):
    loop = asyncio.new_event_loop()
    if closed:
        loop.close()
    try:
        with warnings.catch_warnings(record=True) as reported:
            warnings.simplefilter("always", RuntimeWarning)
            try:
                assert direct._bounded_put(loop, asyncio.Queue(), "late frame", lambda: False,
                                           max_wait=0.01) is False
            finally:
                if not loop.is_closed():
                    loop.close()
                gc.collect()
            assert not [item for item in reported if issubclass(item.category, RuntimeWarning)]
    finally:
        if not loop.is_closed():
            loop.close()


@pytest.mark.asyncio
async def test_session_close_awaits_keepwarm_cleanup_in_its_own_context():
    session = RelaySession(SimpleNamespace(cfg={}), "fixture-thread")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def warm():
        token = client_context.bind({"request_id": "fixture-owner"})
        entered.set()
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0)
            client_context.reset(token)
            cleaned.set()

    task = session._warm_task = asyncio.create_task(warm(), context=contextvars.Context())
    try:
        await entered.wait()
        await session.aclose()
        assert task.done() and cleaned.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def runtime_config(tmp_path):
    return dict(config.DEFAULTS, base="https://fixture.invalid", page_uuid="fixture-page",
                thread_id="fixture-thread", cookie="accessToken=fixture", xsrf="fixture-xsrf",
                ua="fixture-agent", relay_api_key="fixture-relay-key", cookie_values=[],
                workspace_root=str(tmp_path), journal_path=str(tmp_path / "journal.jsonl"),
                command_log=str(tmp_path / "commands.log"), host="127.0.0.1", port=0)


def test_runtime_close_drains_tasks_and_does_not_wait_for_blocked_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL", journal.Journal())
    runtime = proxy.Runtime(runtime_config(tmp_path))
    entered, cleaned, release = threading.Event(), threading.Event(), threading.Event()

    async def seed():
        session = RelaySession(runtime.backend, "fixture-thread")
        runtime.pool._conversations["fixture-chat"] = session

        async def warm():
            token = client_context.bind({"request_id": "fixture-owner"})
            entered.set()
            try:
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(0)
                client_context.reset(token)
                cleaned.set()

        session._warm_task = asyncio.create_task(warm(), context=contextvars.Context())

    try:
        runtime.submit(seed()).result(3)
        assert entered.wait(3)
        worker_started = threading.Event()

        def blocked_headers():
            worker_started.set()
            return release.wait()

        runtime._io_executor.submit(blocked_headers)
        assert worker_started.wait(2)
        start = time.monotonic()
        runtime.close()
        assert time.monotonic() - start < 3
        assert cleaned.is_set()
        assert not runtime._loop_thread.is_alive()
        assert runtime.loop.is_closed()
        assert not asyncio.all_tasks(runtime.loop)
        runtime.close()  # repeated stop is harmless
        with pytest.raises(Exception) as error:
            runtime.submit(asyncio.sleep(0))
        assert getattr(error.value, "status", None) == 503
    finally:
        release.set()
        runtime.close()


def test_main_handles_repeated_stop_signals_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(journal, "JOURNAL", journal.Journal())
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    cfg = runtime_config(tmp_path)
    runtime = proxy.Runtime(cfg)
    monkeypatch.setattr(runtime.accounts, "start", lambda: None)
    monkeypatch.setattr(runtime.credits, "get_balance", lambda **_kw: {"available": 100})
    handlers = {}
    stopped = threading.Event()

    class Server:
        def __init__(self, *_args):
            pass

        def serve_forever(self):
            handlers[signal.SIGINT](signal.SIGINT, None)
            handlers[signal.SIGINT](signal.SIGINT, None)
            assert stopped.wait(2)

        def shutdown(self):
            stopped.set()

        def server_close(self):
            pass

    monkeypatch.setattr(proxy, "Runtime", lambda _cfg: runtime)
    monkeypatch.setattr(proxy, "BoundedServer", Server)
    monkeypatch.setattr(proxy, "startup_config", lambda _argv: cfg)
    monkeypatch.setattr(proxy.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    try:
        proxy.main([])
        assert capsys.readouterr().out.count("shutting down") == 1
        assert runtime.loop.is_closed()
    finally:
        runtime.close()


def test_server_shutdown_unblocks_a_full_connection_cap(monkeypatch):
    connections = threading.BoundedSemaphore(1)
    connections.acquire()
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.BaseHTTPRequestHandler, threading.BoundedSemaphore(1),
                                 connections)
    request, peer = socket.socketpair()
    worker = threading.Thread(target=server.process_request, args=(request, ("127.0.0.1", 0)), daemon=True)
    monkeypatch.setattr(proxy.ThreadingHTTPServer, "shutdown", lambda _self: None)
    try:
        worker.start()
        server.shutdown()
        worker.join(2)
        assert not worker.is_alive()
    finally:
        connections.release()
        request.close()
        peer.close()
        server.server_close()


def test_daemon_io_pool_is_bounded_and_cancels_queued_jobs():
    from io_workers import DaemonIOExecutor

    executor = DaemonIOExecutor(1)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        return release.wait(3)

    first = executor.submit(blocked)
    try:
        assert entered.wait(2)
        queued = executor.submit(lambda: pytest.fail("queued job must not start"))
        assert len(executor._threads) == 1 and executor._threads[0].daemon
        executor.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled()
        with pytest.raises(RuntimeError):
            executor.submit(lambda: None)
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert first.result() is True


def test_blocked_io_does_not_hold_the_interpreter_open():
    code = """
import threading
from io_workers import DaemonIOExecutor
entered = threading.Event()
def blocked():
    entered.set()
    threading.Event().wait(60)
pool = DaemonIOExecutor(1)
pool.submit(blocked)
assert entered.wait(2)
pool.shutdown(wait=False, cancel_futures=True)
print('clean exit')
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "clean exit"
    assert result.stderr == ""


def test_io_worker_propagates_errors_and_remains_available():
    from io_workers import DaemonIOExecutor

    with DaemonIOExecutor(1) as executor:
        def fail():
            raise ValueError("fixture failure")

        with pytest.raises(ValueError, match="fixture failure"):
            executor.submit(fail).result(2)
        assert executor.submit(lambda: 42).result(2) == 42


def test_cancelled_queue_handoff_never_lands_after_the_consumer_resumes():
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue(maxsize=1)
    attempted = threading.Event()
    put_nowait = queue.put_nowait

    def observe_put(item):
        if item == "late frame":
            attempted.set()
        return put_nowait(item)

    queue.put_nowait = observe_put
    cancelled = threading.Event()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    producer = None
    try:
        asyncio.run_coroutine_threadsafe(queue.put("resident"), loop).result(2)
        result = []
        producer = threading.Thread(target=lambda: result.append(
            direct._bounded_put(loop, queue, "late frame", cancelled.is_set)), daemon=True)
        producer.start()
        assert attempted.wait(2)
        cancelled.set()
        producer.join(2)
        assert not producer.is_alive() and result == [False]

        async def drain():
            assert await queue.get() == "resident"
            await asyncio.sleep(0.15)
            assert queue.empty()

        asyncio.run_coroutine_threadsafe(drain(), loop).result(2)
    finally:
        cancelled.set()
        if producer is not None:
            producer.join(2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()
