"""Regression tests for the stack-wide review fixes (PR #1..#13 follow-up)."""
import asyncio
import json
import os
import queue
import shutil
import subprocess
import time

import pytest

import apierr
import auth
import config
import context
import direct
import executor
import journal
import upstream_refresh
import validate
from session import SessionLease


# --- executor ---------------------------------------------------------------

DEFAULTS = config.DEFAULTS["exec_allowlist"]


@pytest.mark.parametrize("cmd", [
    "git grep -Ocalc.exe foo",
    "git grep --open-files-in-pager=calc.exe foo",
    "git --config-env=core.fsmonitor=COMSPEC status",
    'cmd /c "git --config-env=core.pager=COMSPEC log"',
    "git log --output=C:/x.bat",
    "git diff --output=../x.txt",
    "git --git-dir=C:/other/.git log",
    "git --work-tree=C:/ status",
])
def test_git_program_and_write_bypasses_are_blocked(cmd):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(cmd, "allowlist", DEFAULTS)


@pytest.mark.parametrize("cmd", ["git grep -o foo", "git grep -n -i foo", "git status", "git log -5 --oneline"])
def test_legit_git_flags_still_pass(cmd):
    executor.check_policy(cmd, "allowlist", DEFAULTS)


def test_narrow_allowlist_rules_match_the_segment_again():
    narrow = [r"^git (status|log)\b"]
    executor.check_policy("git status", "allowlist", narrow)
    executor.check_policy("git log", "allowlist", narrow)
    for cmd in ("git push", "calc git status"):
        with pytest.raises(executor.PolicyError):
            executor.check_policy(cmd, "allowlist", narrow)


# --- orchestrator -----------------------------------------------------------

class _Backend:
    def __init__(self, cfg):
        self.cfg = cfg


class _Session:
    thread_id = "direct-x"

    def __init__(self, events, backend_cfg=None):
        self._events = events
        self.providers = []
        self.backend = _Backend(backend_cfg or {})

    def set_direct(self, *args, **kwargs):
        pass

    async def stream(self, prompt, provider, tool_names=None):
        self.providers.append(dict(provider))
        for event in self._events:
            yield event

    def cancel(self):
        pass


class _Pool:
    def __init__(self, session, lease=False):
        self.session = session
        self.lease = lease

    async def acquire(self, *args, **kwargs):
        return SessionLease(self.session) if self.lease else self.session

    async def release(self, session):
        pass


def _run(cfg, session, lease=False):
    from orchestrator import TurnRunner
    parsed = validate.parse_chat({"model": "claude-opus-5-5",
                                  "messages": [{"role": "user", "content": "hi"}]}, cfg)
    parsed.conversation_key = "header:t"
    parsed.request_id = "req123"
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *_: None})(), _Pool(session, lease))
    out = queue.Queue()
    asyncio.run(runner.run(parsed, out, {}))
    items = []
    while not out.empty():
        item = out.get()
        if item is not None:  # the runner closes `out` with a None sentinel
            items.append(item)
    return items


def _cfg(tmp_path, **extra):
    return dict(config.DEFAULTS, workspace_root=str(tmp_path), cookie_values=[], transport="direct",
                project_context=False, exec_policy="off", **extra)


def test_managed_resource_configured_only_on_the_account(tmp_path):
    cfg = _cfg(tmp_path, provider_resource_name="", openai_provider_resource_name="")
    session = _Session([{"type": "text", "delta": "ok"}, {"type": "end", "reason": "stop"}],
                       backend_cfg={"provider_resource_name": "__managed__"})
    items = _run(cfg, session)
    assert not [i for i in items if i["type"] == "error"], items
    assert "providerResourceName" not in session.providers[0], "__managed__ must omit the field, never send null"


def test_account_resource_uuid_wins_over_global(tmp_path):
    cfg = _cfg(tmp_path, provider_resource_name="global-uuid")
    session = _Session([{"type": "text", "delta": "ok"}, {"type": "end", "reason": "stop"}],
                       backend_cfg={"provider_resource_name": "account-uuid"})
    _run(cfg, session)
    assert session.providers[0]["providerResourceName"] == "account-uuid"


def test_truncated_stream_error_reaches_the_client(tmp_path):
    cfg = _cfg(tmp_path, provider_resource_name="a")
    session = _Session([{"type": "text", "delta": "partial"},
                        {"type": "error", "error": apierr.upstream_error("stream ended without end_of_stream")}],
                       backend_cfg={"provider_resource_name": "a"})
    items = _run(cfg, session)
    kinds = [i["type"] for i in items]
    assert "error" in kinds, "a truncated upstream stream must not end as a clean stop"
    assert {"type": "end", "reason": "stop"} not in items


def test_prefix_tokens_are_written_to_the_real_session_through_a_lease(tmp_path):
    cfg = _cfg(tmp_path, provider_resource_name="a")
    session = _Session([{"type": "text", "delta": "ok"},
                        {"type": "credits", "value": 0.8,
                         "usage": {"input_tokens": 4, "output_tokens": 5, "cache_read": 190000, "cache_write": 10}},
                        {"type": "end", "reason": "stop"}],
                       backend_cfg={"provider_resource_name": "a"})
    _run(cfg, session, lease=True)
    assert getattr(session, "_last_prefix_tokens", 0) > 190000
    assert session._request_tag == "req123", "console lines must carry the relay request id"


# --- direct -----------------------------------------------------------------

class _Resp:
    def __init__(self, lines):
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]
        self.fp = None

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class _Refresher:
    def refresh(self, **kwargs):
        return False


class _DirectBackend:
    cfg = {"base": "https://example.invalid"}
    refresher = _Refresher()

    def headers(self):
        return {"cookie": "c"}


class _DirectSession:
    thread_id = "direct-t"

    def __init__(self):
        self._cancelled = False
        self.backend = _DirectBackend()


def test_ping_raises_on_upstream_error_event(monkeypatch):
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *a, **k: _Resp([
        json.dumps({"event": "error", "error": "overloaded", "statusCode": 529})]))
    with pytest.raises(apierr.ApiError):
        direct._ping(_DirectSession(), {"model": "claude-opus-5-5"})


def test_metadata_only_reply_is_retried(monkeypatch):
    replies = [
        [json.dumps({"event": "metadata", "usage": {"creditsUsed": 0.1}}), json.dumps({"event": "end_of_stream"})],
        [json.dumps({"event": "content_block_start", "type": "text"}),
         json.dumps({"event": "content_block_delta", "type": "text", "partialText": "hello"}),
         json.dumps({"event": "end_of_stream"})],
    ]
    calls = []

    def fake_urlopen(*args, **kwargs):
        calls.append(1)
        return _Resp(replies[len(calls) - 1])

    monkeypatch.setattr(direct.urllib.request, "urlopen", fake_urlopen)

    async def collect():
        return [e async for e in direct._stream_body({"turn_timeout": 30}, _DirectSession(),
                                                     {"model": "claude-opus-5-5"})]

    events = asyncio.run(collect())
    assert len(calls) == 2, "credits-only attempt must be treated as empty and retried"
    assert {"type": "text", "delta": "hello"} in events


class _WarmSession:
    def __init__(self):
        self._cancelled = False
        self.thread_id = "t"
        self._busy = True
        self._turn_started_at = time.time() - 201
        self._turn_pinged = False
        self._turn_seq = 1
        self._turn_body = {"model": "claude-opus-5-5", "marker": "active"}
        self._warm_body = None
        self._warm_at = 0
        self._last_request_at = time.time()
        self._warm_failures = 0
        self._keepwarm_cost = 0.0
        self._last_prefix_tokens = 0


_WARM_CFG = {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800,
             "cache_keepwarm_during_turns": True, "cache_keepwarm_turn_offset": 200}


async def _run_keepwarm(session, seconds):
    task = asyncio.create_task(direct._keepwarm(session, _WARM_CFG))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_first_turn_ping_uses_the_active_turn_body(monkeypatch):
    session = _WarmSession()
    bodies = []
    monkeypatch.setattr(direct, "_ping", lambda _s, body: bodies.append(body) or 0.85)
    asyncio.run(_run_keepwarm(session, 1.5))
    assert bodies and bodies[0]["marker"] == "active", "first turn has no _warm_body: ping the active body"
    assert session._turn_pinged is True


def test_late_ping_does_not_mark_a_newer_turn(monkeypatch):
    session = _WarmSession()

    def slow_ping(_s, _body):
        session._turn_seq += 1  # a new turn began while the ping was in flight
        session._turn_pinged = False
        return 0.85

    monkeypatch.setattr(direct, "_ping", slow_ping)
    asyncio.run(_run_keepwarm(session, 1.5))
    assert session._turn_pinged is False


def test_warm_anchor_is_the_turn_start_not_its_end(monkeypatch):
    session = _DirectSession()

    async def fake_stream(cfg, sess, body, param_types=None):
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "_stream_body", fake_stream)
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *a: None)
    monkeypatch.setattr(direct, "build_body", lambda *a, **k: {"model": "m", "openAICacheKey": "k"})

    async def go():
        async for _ in direct.iter_events({}, session, {"model": "m"}, "p", "h", [], None, None):
            await asyncio.sleep(0.3)

    asyncio.run(go())
    assert session._warm_at == session._turn_started_at


# --- auth / refresh / journal / validate / context / config ------------------

def test_new_xsrf_survives_legacy_cookie_deletion_in_the_same_batch():
    cfg = {"cookie": "accessToken=a; xsrfToken=old", "xsrf": "old"}
    auth.merge_set_cookie(cfg, ["__Host-xsrfToken=new; Path=/", "xsrfToken=; Max-Age=0"])
    assert cfg["xsrf"] == "new"


def test_refresh_tenant_is_hostname_only(monkeypatch, tmp_path):
    cfg = {"base": "https://user:pw@tenant.example.invalid/path?q=secret", "page_uuid": "p1", "ua": "t",
           "cookie": "accessToken=old; xsrfToken=x", "xsrf": "x", "cookie_values": [], "relay_api_key": "k",
           "upstream_timeout": 10, "_config_path": str(tmp_path / "c.json"), "_persist_auth": False}
    journal.JOURNAL.reset()
    monkeypatch.setattr(upstream_refresh.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    upstream_refresh.Refresher(cfg).refresh()
    tenants = [e["data"].get("tenant") for e in journal.JOURNAL.query() if e["kind"] == "auth.refresh"]
    assert tenants == ["tenant.example.invalid"]


def test_journal_reset_removes_rotated_generation(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    j = journal.Journal()
    j.configure({}, path)
    j.emit("x.y", "old")
    with open(path + ".1", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 5, "kind": "old.event", "data": {}}) + "\n")
    j.reset()
    assert not os.path.exists(path + ".1")
    fresh = journal.Journal()
    fresh.configure({}, path)
    assert not [e for e in fresh.query() if e["kind"] == "old.event"]


def test_failed_rotation_backs_off(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "journal.jsonl")
    j = journal.Journal()
    j.configure({}, path)
    monkeypatch.setattr(journal, "MAX_FILE_BYTES", 10)

    def locked(*a):
        raise OSError("locked")

    monkeypatch.setattr(journal.os, "replace", locked)
    for _ in range(5):
        j.emit("x.y", "event")
    assert capsys.readouterr().out.count("rotation failed") == 1


def test_responses_input_image_rejected_for_non_vision_model(tmp_path, monkeypatch):
    import models
    entry = dict(models.resolve('zai-org/GLM-5.3')[0], id='synthetic-no-vision', vision=False)
    monkeypatch.setitem(models._BY_ID, entry['id'], entry)
    cfg = _cfg(tmp_path, strict_models=True)
    with pytest.raises(apierr.ApiError) as err:
        validate.parse_responses({"model": entry['id'], "input": [
            {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}]}, cfg)
    assert err.value.status == 422


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_renamed_sensitive_file_is_not_in_context(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path,
                       check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / ".env").write_text("X=1", encoding="utf-8")
    git("add", ".env")
    git("commit", "-qm", "init")
    git("mv", ".env", "renamed.txt")
    text = context.build_project_context(str(tmp_path))
    assert ".env" not in text


def test_agent_transport_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_TRANSPORT", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base": "https://example.invalid", "page_uuid": "p", "ua": "u",
                                "cookie": "accessToken=a", "xsrf": "x", "transport": "agent",
                                "workspace_root": str(tmp_path)}), encoding="utf-8")
    with pytest.raises(apierr.ApiError) as err:
        config.load(str(path))
    assert "direct" in err.value.message


# --- second batch: credits per attempt, unknown block end, admin locks -------

def test_every_upstream_attempt_is_billed(tmp_path):
    import credits as credits_module
    manager = credits_module.CreditsManager("https://example.invalid", lambda: {}, None)
    manager.record_turn("t1", 1.0)
    manager.record_turn("t1#2", 0.5)
    manager.record_turn("t1", 1.0)  # a true duplicate is still dropped
    assert round(manager.usage()["session_used"], 4) == 1.5


def test_unknown_tool_end_does_not_close_the_open_call():
    lines = [json.dumps({"event": "content_block_start", "type": "tool_call", "toolCallId": "a", "toolName": "r"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"p":'}),
             json.dumps({"event": "content_block_end", "type": "tool_call", "toolCallId": "ghost"}),
             json.dumps({"event": "content_block_delta", "type": "tool_call", "partialInputs": '1}'}),
             json.dumps({"event": "content_block_end", "type": "tool_call", "toolCallId": "a"}),
             json.dumps({"event": "end_of_stream"})]

    async def collect():
        q = asyncio.Queue()
        for line in lines:
            q.put_nowait(line)
        q.put_nowait(None)
        return [e async for e in direct._drain({}, _DirectSession(), q, [], [], None, {})]

    finals = [e for e in asyncio.run(collect()) if e["type"] == "tool_call" and not e.get("partial")]
    assert len(finals) == 1 and json.loads(finals[0]["arguments"]) == {"p": 1}
