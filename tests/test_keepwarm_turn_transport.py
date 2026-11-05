"""The verdict on a turn over the real direct transport: a local HTTP server plays Retool, nothing leaves the machine."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import apierr
import direct

THINKING = [{"event": "content_block_start", "type": "thinking"},
            {"event": "content_block_delta", "type": "signature", "partialSignature": "opaque"},
            {"event": "content_block_end", "type": "thinking"}]
USAGE = {"event": "metadata", "usage": {"creditsUsed": 17.76, "promptTokensWrittenToCache": 190_000}}
END = {"event": "end_of_stream"}
TEXT = [{"event": "content_block_delta", "type": "text", "partialText": "done"}]
TOOL = [{"event": "content_block_start", "type": "tool_call", "toolCallId": "call-1", "toolName": "read_file"},
        {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path": "a"}'},
        {"event": "content_block_end", "type": "tool_call", "toolCallId": "call-1"}]

# name: (HTTP status, what the server writes, the verdict, the POSTs it takes)
CASES = {
    "an answer": (200, TEXT + [USAGE, END], "content", 1),
    "a tool call": (200, TOOL + [USAGE, END], "content", 1),
    "thinking and a signature only": (200, THINKING + [USAGE, END], "thinking_only", 2),
    "nothing at all": (200, [USAGE, END], "empty", 2),
    "a cut stream": (200, TEXT + [USAGE], "truncated", 1),
    "an error event": (200, [{"event": "error", "error": {"message": "boom"}, "statusCode": 500}], "error", 1),
    "a server error": (500, b"upstream exploded", "error", 1),
    "a rejected body": (400, b"Invalid request format", "rejected", 1),
}


def serve(status, lines):
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(status)
            self.send_header("Connection", "close")
            self.end_headers()
            if isinstance(lines, bytes):
                self.wfile.write(lines)
            else:
                for line in lines:
                    self.wfile.write(json.dumps(line).encode() + b"\n")
            self.wfile.flush()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, posts


@pytest.mark.parametrize("status,lines,outcome,posts", CASES.values(), ids=CASES.keys())
def test_what_upstream_sends_decides_whether_the_turn_is_warmed(monkeypatch, tmp_path, status, lines, outcome, posts):
    ensured = []
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *args: ensured.append(args))
    httpd, thread, seen = serve(status, lines)
    previous = {"model": "claude-opus-5-5", "openAICacheKey": "an-older-good-turn"}
    session = SimpleNamespace(_cancelled=False, thread_id="fixture", _warm_body=previous, _warm_at=5.0,
                              backend=SimpleNamespace(cfg={"base": "http://127.0.0.1:%d" % httpd.server_address[1]},
                                                      headers=lambda: {}))
    cfg = {"turn_timeout": 10, "upstream_error_dir": str(tmp_path)}

    async def turn():
        return [event async for event in direct.iter_events(
            cfg, session, {"model": "claude-opus-5-5"}, "continue", "A STABLE HEAD " * 20, None, "auto", 64)]

    try:
        try:
            asyncio.run(turn())
        except apierr.ApiError:
            pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
    assert len(seen) == posts, "the empty answers are retried once on the same body, and every retry is paid"
    assert session._turn_outcome == outcome
    if outcome == "content":
        assert session._warm_body is not previous and session._warm_body["openAICacheKey"] == seen[0]["openAICacheKey"]
        assert session._warm_at == session._turn_started_at > 5.0 and len(ensured) == 2
    else:
        assert session._warm_body is previous and session._warm_at == 5.0 and len(ensured) == 1
