import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

import apierr
import client_context
import direct
import journal


@pytest.mark.parametrize("status", [400, 422])
def test_rejection_has_unique_correlated_dump_and_context_in_executor(tmp_path, monkeypatch, status):
    cfg = {"upstream_error_dir": str(tmp_path), "turn_timeout": 2}
    body = {"model": "claude-opus-5-5", "reasoningEffort": "high", "messages": [{"role": "user", "content": "hi"}]}
    session = SimpleNamespace(backend=SimpleNamespace(headers=lambda: {"cookie": "never-store-this-header"},
                              cfg={"base": "https://example.invalid"}), _cancelled=False, _request_tag="req123")

    def reject(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", status, "invalid", {"request-id": "upstream123"},
                                     io.BytesIO(b'{"message":"Invalid request format"}'))
    monkeypatch.setattr(direct.urllib.request, "urlopen", reject)

    async def run():
        token = client_context.bind({"client": "opencode", "chat_title": "Проверка ошибок", "request_id": "req123"})
        try:
            with pytest.raises(apierr.ApiError) as caught:
                async for _ in direct._stream_body(cfg, session, body):
                    pass
            return caught.value
        finally:
            client_context.reset(token)

    error = asyncio.run(run())
    assert error.status == 400
    assert error.upstream_status == status
    files = list(tmp_path.glob("*-body.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_bytes()) == body
    metadata = json.loads((tmp_path / (error.diagnostic_id + "-metadata.json")).read_text(encoding="utf-8"))
    assert metadata["chat_title"] == "Проверка ошибок"
    assert metadata["request_id"] == "req123"
    assert metadata["upstream_status"] == status
    assert "never-store-this-header" not in "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    event = next(e for e in reversed(journal.JOURNAL.query()) if e["kind"] == "upstream.rejected")
    assert event["request_id"] == "req123"
    assert event["data"]["upstream_status"] == status


def test_same_second_concurrent_dumps_do_not_overwrite(tmp_path):
    def dump(index):
        return direct._dump_rejected_body(str(index).encode(), "bad", cfg={"upstream_error_dir": str(tmp_path)},
                                          request_id="../same-id")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(dump, range(16)))
    assert len(set(ids)) == 16
    assert len(list(tmp_path.glob("*-body.json"))) == 16
    assert {p.read_bytes() for p in tmp_path.glob("*-body.json")} == {str(i).encode() for i in range(16)}


def test_diagnostic_disk_failure_keeps_original_error(tmp_path):
    target = tmp_path / "file"
    target.write_text("cannot mkdir under a file")
    assert direct._dump_rejected_body(b"{}", "bad", cfg={"upstream_error_dir": str(target / "dir")}) is None
