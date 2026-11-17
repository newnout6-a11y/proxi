"""An image named by URL is fetched through the system proxy when one applies, and otherwise only from the address the
guard checked: a name that resolves to a public address for the check and to an inside one for the connect gets nothing."""
import http.server
import socket
import threading
import urllib.request

import pytest

import apierr
import validate

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class Recorder(http.server.BaseHTTPRequestHandler):
    """Answers every GET with a small PNG and remembers the request line (an absolute URL when it acts as a proxy)."""
    seen = None

    def do_GET(self):
        self.seen.append(self.requestline)
        self.send_response(200)
        self.send_header("content-type", "image/png")
        self.send_header("content-length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    seen = []
    handler = type("Handler", (Recorder,), {"seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def no_network(monkeypatch):
    """Only 127.0.0.1 can be reached: no test may leave the machine, whatever the code under test does."""
    real_connect = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and address[0] != "127.0.0.1":
            raise OSError("no network in tests: %s" % (address,))
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def resolving(monkeypatch, *answers):
    """socket.getaddrinfo answers with these addresses in turn (the last one from then on)."""
    answers = list(answers)

    def getaddrinfo(host, port=None, *args, **kwargs):
        if host == "127.0.0.1":  # the local proxy: an address, not a name
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port or 0))]
        address = answers.pop(0) if len(answers) > 1 else answers[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_a_name_that_rebinds_to_an_inside_address_gets_no_request_through(monkeypatch, local_server, no_network):
    """The guard resolved the name and the connection resolved it again: answering 8.8.8.8 to the check and 127.0.0.1 to
    the connect delivered the GET, with its path and query, to a service inside before the peer check refused the answer
    (audit of 2026-11-07)."""
    port, seen = local_server
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    resolving(monkeypatch, "8.8.8.8", "127.0.0.1")
    with pytest.raises(apierr.ApiError, match="disallowed destination host"):
        validate._fetch_remote("http://rebind.example:%d/admin/delete?all=1" % port, "image")
    assert seen == []


def test_with_a_system_proxy_the_image_comes_through_the_proxy(monkeypatch, local_server, no_network):
    """With a proxy set in Windows (v2rayN, Clash) every image URL was refused: the peer check looked at the socket, which
    led to the proxy on 127.0.0.1."""
    port, seen = local_server
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:%d" % port})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    resolving(monkeypatch, "8.8.8.8")  # the image host, as the guard sees it before the request
    mime, data = validate._fetch_remote("http://images.example/cat.png", "image")
    assert mime == "image/png" and data
    assert seen == ["GET http://images.example/cat.png HTTP/1.1"]


def test_through_a_proxy_an_image_host_off_the_public_internet_is_still_refused(monkeypatch, local_server, no_network):
    port, seen = local_server
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:%d" % port})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    resolving(monkeypatch, "10.0.0.5")
    with pytest.raises(apierr.ApiError, match="disallowed destination host"):
        validate._fetch_remote("http://intranet.example/secret.png", "image")
    assert seen == []
