"""Server-Sent Events writer with heartbeat and disconnect detection."""
import json
import select
import socket

HEARTBEAT_EVERY = 15.0


import time

class StreamWriter:
    def __init__(self, handler):
        self.handler = handler
        self.broken = False
        self.last_write = time.time()

    def start(self, headers=None):
        self.handler.send_response(200)
        self.handler.send_header("content-type", "text/event-stream; charset=utf-8")
        self.handler.send_header("cache-control", "no-cache, no-transform")
        self.handler.send_header("connection", "close")
        self.handler.send_header("x-accel-buffering", "no")
        for key, value in (headers or {}).items():
            self.handler.send_header(key, value)
        cors = getattr(self.handler, "_cors", None)
        if cors is not None:
            cors()
        self.handler.end_headers()

    def raw(self, text: str):
        if self.broken:
            return
        try:
            self.handler.wfile.write(text.encode("utf-8"))
            self.handler.wfile.flush()
            self.last_write = time.time()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.broken = True

    def event(self, name, payload):
        body = json.dumps(payload, ensure_ascii=False)
        self.raw(f"event: {name}\ndata: {body}\n\n" if name else f"data: {body}\n\n")

    def data(self, payload):
        self.raw("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n")

    def comment(self, text="keep-alive"):
        self.raw(f": {text}\n\n")

    def done(self):
        self.raw("data: [DONE]\n\n")


def client_gone(handler) -> bool:
    """Non-blocking check whether the peer closed the connection."""
    sock = getattr(handler, "connection", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True
