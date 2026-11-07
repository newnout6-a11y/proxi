"""A relay that stops stops the commands its turns run, and the conversations that went idle leave the pool on their own.

A relay exec command runs in a thread that only its turn's cancel event stops (executor.run_command polls it). The relay
cancelled the turn's task at Ctrl+C but never set the event: the interpreter waits for that thread at exit, so the process
stayed alive until the command ended or command_timeout (300 s) killed it, and a second Ctrl+C did nothing."""
import asyncio
import json
import threading
import time
import urllib.request

import config as config_module
import proxy
from conftest import KEY, FakeCredits


class CommandRunner:
    """A turn whose step runs a command in a thread that stops only when the turn's cancel event is set."""

    def __init__(self):
        self.started, self.stopped = threading.Event(), threading.Event()

    async def run(self, parsed, out, holder):
        def command():
            self.started.set()
            if holder["cancel_event"].wait(30):
                self.stopped.set()

        try:
            await asyncio.get_running_loop().run_in_executor(None, command)
            out.put({"type": "end", "reason": "cancelled"})
        finally:
            out.put(None)


def test_stopping_the_relay_stops_a_command_a_turn_is_running(tmp_path):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({"base": "https://example.invalid", "page_uuid": "page-1", "ua": "ua", "cookie": "accessToken=abc123456789xyz",
                "xsrf": "q", "relay_api_key": KEY, "workspace_root": str(tmp_path), "host": "127.0.0.1", "port": 0,
                "cookie_values": ["abc123456789xyz"]})
    runtime = proxy.Runtime(cfg)
    runtime.credits, runtime.runner = FakeCredits(), CommandRunner()
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    body = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "run it"}]}).encode()
    request = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % server.server_address[1], data=body,
                                     headers={"authorization": "Bearer " + KEY, "content-type": "application/json"})

    def client():
        try:
            urllib.request.urlopen(request, timeout=30).read()
        except Exception:  # noqa: BLE001 - the answer to a request cut by a stop is not what this test is about
            pass

    threading.Thread(target=client, daemon=True).start()
    try:
        assert runtime.runner.started.wait(10)
        started = time.monotonic()
        runtime.close()
        assert runtime.runner.stopped.wait(5), "the command ran on after the relay stopped"
        assert time.monotonic() - started < 5
        assert runtime._turns == {}  # nothing is left registered once the turn has ended
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()


def test_a_conversation_past_its_idle_timeout_leaves_the_pool_without_another_request(tmp_path):
    """Only a request ran the eviction: a relay that went quiet kept every pooled conversation (its warm body, its signed
    history, the chunks of its last request) in memory until the next request of any conversation, hours later."""
    cfg = dict(config_module.DEFAULTS)
    cfg.update({"base": "https://example.invalid", "page_uuid": "page-1", "ua": "ua", "cookie": "accessToken=abc123456789xyz",
                "xsrf": "q", "relay_api_key": KEY, "workspace_root": str(tmp_path), "host": "127.0.0.1", "port": 0,
                "cookie_values": ["abc123456789xyz"], "conversation_idle_timeout": 1, "conversation_state": False})
    runtime = proxy.Runtime(cfg)

    async def visit():
        await runtime.pool.release(await runtime.pool.acquire("quiet"))
        return "quiet" in runtime.pool._conversations

    try:
        assert asyncio.run_coroutine_threadsafe(visit(), runtime.loop).result(10)
        deadline = time.monotonic() + 5
        while "quiet" in runtime.pool._conversations and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "quiet" not in runtime.pool._conversations
    finally:
        runtime.close()
