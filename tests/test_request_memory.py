"""A finished request leaves nothing of its conversation for the garbage collector to find.

The adapter of a request holds its parsed request (every message of the conversation as text and as parsed JSON); the writer of a
stream and the handler hold each other. Left on the handler they were a cycle that only the garbage collector freed, a few turns
later: a conversation of 300k tokens held 10 MB per turn more than it used.
"""
import gc
import http.client
import json
import time
import weakref

import pytest

import adapters

CHAT = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi " * 2000}]}


@pytest.mark.parametrize("stream", [True, False])
def test_the_adapter_of_a_finished_request_is_freed_without_the_garbage_collector(relay, monkeypatch, stream):
    made = []
    real = adapters.for_dialect

    def spy(*args, **kwargs):
        adapter = real(*args, **kwargs)
        made.append(weakref.ref(adapter))
        return adapter

    monkeypatch.setattr(adapters, "for_dialect", spy)
    gc.collect()
    gc.disable()
    try:
        status, _ = relay.sse("/v1/chat/completions", dict(CHAT, stream=stream))
        assert status == 200 and len(made) == 1
        # the answer is complete before the handler thread has left _dispatch: wait for the thread, not for a time
        deadline = time.monotonic() + 5
        while made[0]() is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert made[0]() is None, "the adapter, and with it the whole request, is still alive"
    finally:
        gc.enable()


def test_a_kept_alive_connection_serves_the_requests_that_follow_a_finished_one(relay):
    host, port = relay.url.split("//")[1].split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        for stream in (False, False, True):
            connection.request("POST", "/v1/chat/completions", body=json.dumps(dict(CHAT, stream=stream)),
                               headers={"content-type": "application/json", "authorization": "Bearer " + relay.key})
            response = connection.getresponse()
            assert response.read() and response.status == 200
    finally:
        connection.close()
