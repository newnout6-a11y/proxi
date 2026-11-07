"""The admin's long requests neither hold a max_workers slot nor turn a browser that leaves into a relay error."""
import json
import socket
import struct
import threading
import time
import urllib.request

import journal
import proxy
from conftest import FakeCredits, FakeRunner, KEY
import config as config_module
from test_admin import admin_relay  # noqa: F401 - a fixture


def one_slot_relay(tmp_path):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({"base": "https://example.invalid", "page_uuid": "page-1", "ua": "ua", "cookie": "accessToken=abc123456789xyz",
                "xsrf": "q", "relay_api_key": KEY, "workspace_root": str(tmp_path), "host": "127.0.0.1", "port": 0,
                "cookie_values": ["abc123456789xyz"], "max_workers": 1})
    runtime = proxy.Runtime(cfg)
    runtime.credits, runtime.runner = FakeCredits(), FakeRunner()
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(1))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return runtime, server


def test_an_open_journal_stream_does_not_hold_the_only_slot(tmp_path):
    runtime, server = one_slot_relay(tmp_path)
    port = server.server_address[1]
    stream = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        stream.sendall(("GET /admin/api/events/stream HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n\r\n"
                        % KEY).encode())
        assert stream.recv(64).startswith(b"HTTP/1.1 200")
        request = urllib.request.Request("http://127.0.0.1:%d/v1/models" % port, headers={"authorization": "Bearer " + KEY})
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=5) as response:  # it waited without a timeout before
            assert response.status == 200
        assert time.monotonic() - started < 2
    finally:
        stream.close()
        server.shutdown()
        server.server_close()
        runtime.close()


def test_a_browser_that_leaves_during_an_asset_download_is_no_relay_error(admin_relay):  # noqa: F811
    (admin_relay.build / "assets" / "big.js").write_bytes(b"x" * (32 * 1024 * 1024))
    mark = journal.JOURNAL.last_seq
    port = int(admin_relay.url.rsplit(":", 1)[1])
    client = socket.create_connection(("127.0.0.1", port), timeout=10)
    client.sendall(b"GET /admin/assets/big.js HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert client.recv(100).startswith(b"HTTP/1.1 200")
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    client.close()  # a reset in the middle of the body
    deadline = time.time() + 10
    events = []
    while time.time() < deadline:
        events = [event for event in journal.JOURNAL.query(since=mark, limit=200)
                  if event["kind"] in ("http.request", "http.exception") and "big.js" in json.dumps(event)]
        if events:
            break
        time.sleep(0.05)
    assert not [event for event in events if event["kind"] == "http.exception"]
    assert all(event["data"].get("status") != 500 for event in events)
