import asyncio
import hashlib
import io
import json
import time
from types import SimpleNamespace
import urllib.error

import pytest

import direct
import apierr
from scripts import replay_upstream
from upstream_attempts import AttemptBudget
from upstream_diagnostics import AttemptTrace, WireTrace


def _trace_names(count, minute=12):
    return [f"20261105-{minute:02d}00{i:02d}-abc-attempt1-{i:012x}" for i in range(count)]


def test_trace_directory_keeps_only_the_newest_folders_and_leaves_other_entries(tmp_path):
    cfg = {"upstream_trace_dir": str(tmp_path), "trace_max_folders": 3}
    (tmp_path / "notes").mkdir()
    (tmp_path / "README.txt").write_text("kept")
    names = _trace_names(6)
    for name in names:
        trace = WireTrace(cfg, name, b"{}")
        trace.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[-3:] + ["notes", "README.txt"])


def test_client_traces_use_the_same_cap(tmp_path):
    cfg = {"client_trace_dir": str(tmp_path), "trace_max_folders": 2}
    names = [name.replace("-attempt1-", "-client-") for name in _trace_names(4)]
    for name in names:
        WireTrace(cfg, name, b"{}", root_key="client_trace_dir").close()
    assert sorted(p.name for p in tmp_path.iterdir()) == names[-2:]


def test_zero_keeps_every_trace_and_the_new_folder_is_never_pruned(tmp_path):
    cfg = {"upstream_trace_dir": str(tmp_path), "trace_max_folders": 0}
    for name in _trace_names(5):
        WireTrace(cfg, name, b"{}").close()
    assert len(list(tmp_path.iterdir())) == 5
    late = _trace_names(1, minute=0)[0]  # sorts before everything (clock moved back)
    WireTrace(dict(cfg, trace_max_folders=2), late, b"{}").close()
    assert late in {p.name for p in tmp_path.iterdir()}


def test_default_cap_bounds_an_enabled_trace_directory(tmp_path):
    cfg = {"upstream_trace_dir": str(tmp_path)}
    for name in [f"20261105-1{i // 3600:01d}{(i // 60) % 60:02d}{i % 60:02d}-abc-attempt1-{i:012x}" for i in range(205)]:
        WireTrace(cfg, name, b"{}").close()
    assert len(list(tmp_path.iterdir())) == 200


def test_opt_in_trace_preserves_exact_request_response_and_parser_event(tmp_path, monkeypatch):
    cfg = {"base": "https://fixture.invalid", "upstream_trace_dir": str(tmp_path / "wire"),
           "upstream_error_dir": str(tmp_path / "summaries")}
    payload = b'{ "model": "claude-opus-5-5", "messages": [{"role":"user","content":"original task"}] }\n'
    body = json.loads(payload)
    raw = b'  {"event":"content_block_delta","type":"text","partialText":"hello"}\r\n\n{"event":"end_of_stream"}\n'
    sent = []

    def opener(req, **kw):
        sent.append(req.data)
        response = io.BytesIO(raw)
        response.status = 200
        response.headers = {"x-request-id": "provider123", "set-cookie": "secret-cookie", "authorization": "secret-auth"}
        return response

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {
        "cookie": "secret-cookie", "x-xsrf-token": "secret-csrf", "authorization": "secret-auth"}),
        _cancelled=False, _request_tag="trace-request")

    async def run():
        return [e async for e in direct._stream_body(cfg, session, body, payload=payload)]

    events = asyncio.run(run())
    assert {"type": "text", "delta": "hello"} in events
    assert sent == [payload]
    folder = next((tmp_path / "wire").iterdir())
    assert (folder / "request-body.json").read_bytes() == payload
    assert (folder / "response-body.ndjson").read_bytes() == raw
    timeline = [json.loads(x) for x in (folder / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["seq"] for x in timeline] == list(range(1, len(timeline) + 1))
    assert all(timeline[i]["elapsed_ms"] <= timeline[i+1]["elapsed_ms"] for i in range(len(timeline)-1))
    assert any(x["stage"] == "parser_event" and x["event"] == {"type": "text", "delta": "hello"} for x in timeline)
    assert timeline[-1]["stage"] == "wire_end"
    assert timeline[-1]["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert timeline[-1]["response_bytes"] == len(raw)
    captured = "".join(p.read_text(encoding="utf-8") for p in folder.iterdir())
    assert all(secret not in captured for secret in ("secret-cookie", "secret-csrf", "secret-auth"))


def test_raw_trace_disabled_by_default(tmp_path):
    budget = AttemptBudget(); budget.used = 1
    trace = AttemptTrace({"upstream_error_dir": str(tmp_path)}, budget, "req", "model", b"{}")
    trace.wire.raw(b"private generated text")
    trace.finish("empty")
    assert trace.wire.folder is None
    assert {p.suffix for p in tmp_path.iterdir()} == {".json"}
    assert all("private generated text" not in p.read_text() for p in tmp_path.iterdir())


def test_raw_trace_disk_failure_does_not_mask_upstream_result(tmp_path, monkeypatch):
    blocked = tmp_path / "file"; blocked.write_text("blocking file")
    budget = AttemptBudget(); budget.used = 1
    trace = AttemptTrace({"upstream_trace_dir": str(blocked / "wire"), "upstream_error_dir": str(tmp_path / "summaries")},
                         budget, "req", "model", b"{}")
    trace.wire.raw(b"raw")
    result = trace.finish("empty")
    assert result["outcome"] == "empty"
    assert result["wire_trace_saved"] is False


def test_invalid_trace_path_is_reported_without_affecting_attempt(tmp_path):
    budget = AttemptBudget(); budget.used = 1
    trace = AttemptTrace({"upstream_trace_dir": 42, "upstream_error_dir": str(tmp_path)},
                         budget, "req", "model", b"{}")
    result = trace.finish("empty")
    assert result["outcome"] == "empty" and result["wire_trace_saved"] is False


def test_replay_payload_mismatch_never_sends_request(monkeypatch):
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not POST"))
    async def run():
        async for _ in direct._stream_body({}, None, {"model": "original"}, payload=b'{"model":"changed"}'):
            pass
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(run())


def test_exact_replay_uses_at_most_five_identical_posts(tmp_path, monkeypatch):
    payload = b'{ "model": "claude-opus-5-5", "messages": [{"role": "user", "content": "task"}], "tools": [] }\n'
    source = tmp_path / "source.json"
    source.write_bytes(payload)
    sent = []

    def opener(req, **_kw):
        sent.append(req.data)
        resp = io.BytesIO(b'{"event":"metadata","usage":{"outputTokens":0,"creditsUsed":0.25}}\n'
                          b'{"event":"end_of_stream"}\n')
        resp.status = 200
        resp.headers = {}
        return resp

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    monkeypatch.setattr(AttemptBudget, "wait", lambda *_args: None)
    monkeypatch.setattr(replay_upstream.journal.JOURNAL, "configure", lambda *_args: None)
    cfg = {"base": "https://fixture.invalid", "cookie": "secret-cookie", "xsrf": "secret-xsrf",
           "ua": "test", "turn_timeout": 2}
    manifest = asyncio.run(replay_upstream.replay(cfg, source, tmp_path / "out"))
    assert sent == [payload] * 5
    assert manifest["attempts"] == manifest["captured_attempts"] == manifest["raw_responses"] == 5
    assert manifest["all_attempts_byte_identical"] is True
    assert manifest["outcome"] == "empty" and manifest["credits_used"] == 1.25


def test_truncated_stream_trace_reports_no_end_and_does_not_retry(tmp_path, monkeypatch):
    cfg = {"base": "https://fixture.invalid", "upstream_trace_dir": str(tmp_path / "wire"),
           "upstream_error_dir": str(tmp_path / "summaries")}
    calls = []

    def opener(req, **_kw):
        calls.append(req.data)
        resp = io.BytesIO(b'{"event":"content_block_delta","type":"text","partialText":"partial"}\n')
        resp.status = 200
        resp.headers = {}
        return resp

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)

    async def run():
        return [event async for event in direct._stream_body(cfg, session, {"model": "m"})]

    events = asyncio.run(run())
    assert len(calls) == 1 and events[-1]["type"] == "error"
    summary = json.loads(next((tmp_path / "summaries").glob("*-response.json")).read_text(encoding="utf-8"))
    assert summary["outcome"] == "truncated" and summary["end_of_stream"] is False


def test_http_error_trace_marks_possible_partial_error_body(tmp_path, monkeypatch):
    cfg = {"base": "https://fixture.invalid", "upstream_trace_dir": str(tmp_path / "wire"),
           "upstream_error_dir": str(tmp_path / "summaries")}

    def reject(req, **_kw):
        raise urllib.error.HTTPError(req.full_url, 500, "failure", {}, io.BytesIO(b"x" * 20000))

    monkeypatch.setattr(direct.urllib.request, "urlopen", reject)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)

    async def run():
        return [event async for event in direct._stream_body(cfg, session, {"model": "m"})]

    with pytest.raises(apierr.ApiError):
        asyncio.run(run())
    folder = next((tmp_path / "wire").iterdir())
    assert len((folder / "response-body.ndjson").read_bytes()) == 16384
    timeline = [json.loads(line) for line in (folder / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["stage"] == "http_error_body_read" and e["may_be_incomplete"] for e in timeline)


def test_cancelled_stream_trace_is_closed_and_reports_cancellation(tmp_path, monkeypatch):
    cfg = {"base": "https://fixture.invalid", "upstream_trace_dir": str(tmp_path / "wire"),
           "upstream_error_dir": str(tmp_path / "summaries")}
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)

    class CancelResponse(io.BytesIO):
        status = 200
        headers = {}

        def __iter__(self):
            session._cancelled = True
            yield b'{"event":"end_of_stream"}\n'

    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_args, **_kw: CancelResponse())

    async def run():
        return [event async for event in direct._stream_body(cfg, session, {"model": "m"})]

    events = asyncio.run(run())
    assert events[-1] == {"type": "end", "reason": "cancelled"}
    folder = next((tmp_path / "wire").iterdir())
    timeline = [json.loads(line) for line in (folder / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
    assert timeline[-1]["stage"] == "wire_end"
    assert any(e["stage"] == "attempt_end" and e["outcome"] == "cancelled" for e in timeline)


def _finished_client_trace(root):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        folders = list(root.iterdir()) if root.exists() else []
        if folders:
            timeline = folders[0] / "timeline.jsonl"
            if timeline.exists():
                entries = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
                if entries and entries[-1]["stage"] == "wire_end":
                    return folders[0], entries
        time.sleep(0.01)
    pytest.fail("client trace was not closed")


@pytest.mark.parametrize("stream", [False, True])
def test_client_trace_captures_exact_http_body_relay_events_and_response(relay, tmp_path, stream):
    root = tmp_path / "client-traces"
    relay.cfg["client_trace_dir"] = str(root)
    body = json.dumps({"model": "claude-opus-5-5", "stream": stream,
                       "messages": [{"role": "user", "content": "private task"}]}, indent=2).encode()
    with relay.request("/v1/chat/completions", raw=body) as resp:
        response = resp.read()
    folder, timeline = _finished_client_trace(root)
    assert (folder / "request-body.json").read_bytes() == body
    assert (folder / "response-body.bin").read_bytes() == response
    assert any(e["stage"] == "validated_request" and e["parsed"]["model"] == "claude-opus-5-5" for e in timeline)
    assert any(e["stage"] == "relay_event" and e["event"]["type"] == "text" for e in timeline)
    assert any(e["stage"] == "client_response_headers" and e["http_status"] == 200 for e in timeline)
    captured = b"".join(p.read_bytes() for p in folder.iterdir())
    assert relay.key.encode() not in captured and b"abc123456789xyz" not in captured


def test_client_trace_failure_does_not_change_response(relay, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("blocking file")
    relay.cfg["client_trace_dir"] = str(blocked / "trace")
    status, body = relay.json("/v1/chat/completions", {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and body["choices"][0]["message"]["content"] == "ok"


def test_client_trace_records_stream_error_after_http_200(relay, tmp_path):
    root = tmp_path / "client-traces"
    relay.cfg["client_trace_dir"] = str(root)
    relay.fake.script = [{"type": "error", "error": apierr.upstream_error("upstream failed")}]
    status, response = relay.sse("/v1/chat/completions", {"model": "claude-opus-5-5", "stream": True,
                                                          "messages": [{"role": "user", "content": "hi"}]})
    folder, timeline = _finished_client_trace(root)
    assert status == 200 and "event: error" in response
    assert (folder / "response-body.bin").read_bytes() == response.encode()
    assert any(e["stage"] == "relay_event" and e["event"]["type"] == "error" for e in timeline)
    assert any(e["stage"] == "client_end" and e["http_status"] == 200 and e["stream_status"] == 502
               for e in timeline)
