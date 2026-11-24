"""Regression tests for Wave 2-3 edge fixes (executor git case, web_search loop, tool-result vision)."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import apierr
import config
import executor
import models
import orchestrator
import validate
from test_client_contracts import CFG, _body
from test_forced_choice import call
from test_native_context_reuse import PARSE_CFG, TOOLS, run
from test_review_fixes_pr19 import END, THINKING, signature
from test_web_search import FakeSearch
from test_web_search_loop import SERVER, asks, drive, reply_of, search_request

DEFAULTS = config.DEFAULTS["exec_allowlist"]


# --- Bug #1: executor.py case-insensitive git branch/tag index ------------------

@pytest.mark.parametrize("cmd", [
    "git BRANCH feature-x",
    "git Branch -D main",
    "git TAG v1.0.0",
    "git Tag -d v1",
    "git --no-pager BRANCH foo",
    "git --no-pager Tag bar",
])
def test_uppercase_git_branch_and_tag_mutations_raise_policy_error_not_value_error(cmd):
    with pytest.raises(executor.PolicyError, match="may only list under the allowlist"):
        executor.check_policy(cmd, "allowlist", DEFAULTS)


@pytest.mark.parametrize("cmd", [
    "git BRANCH",
    "git BRANCH --list",
    "git Branch -a",
    "git TAG",
    "git Tag --list",
    "git TAG -l",
    "git --no-pager BRANCH -a",
])
def test_uppercase_git_branch_and_tag_listing_still_allowed(cmd):
    executor.check_policy(cmd, "allowlist", DEFAULTS)


# --- Bug #2: orchestrator.py web_search loop + validate.py transport check ------

def test_forced_web_search_restores_tool_names_for_subsequent_client_tool(tmp_path, monkeypatch):
    """2(a): After a forced web_search step resets tool_choice to 'auto', all declared client tools become callable."""
    search = FakeSearch()
    made = []
    client_read = {"name": "read_file", "description": "Read file",
                   "input_schema": TOOLS[0]["function"]["parameters"]}
    # Step 1: forced web_search; Step 2: model calls client's read_file tool.
    step1 = asks("latest release", words="")
    step2 = [*THINKING, signature("sig-two"), {"event": "content_block_end", "type": "thinking"},
             *call("read_file", "r1", path="README.md"), *END]

    results, sent, _ = drive(
        tmp_path, monkeypatch, [step1, step2], search,
        [lambda _: made.append(search_request(
            search, extra_tools=[client_read],
            tool_choice={"type": "tool", "name": "web_search"},
        )) or made[-1]],
    )

    assert len(sent) == 2 and len(search.calls) == 1
    assert "toolChoice" not in sent[1]
    assert results[0][-1] == {"type": "end", "reason": "tool_calls"}
    _, final = reply_of(made[0], results[0])
    assert [b["type"] for b in final["content"] if b["type"] != "thinking"] == [
        "server_tool_use", "web_search_tool_result", "tool_use",
    ]
    tool_use = final["content"][-1]
    assert tool_use["name"] == "read_file" and tool_use["input"] == {"path": "README.md"}
    assert final["stop_reason"] == "tool_use"


def test_web_search_loop_cap_with_empty_final_text_stops_cleanly_without_502(tmp_path, monkeypatch):
    """2(b): Exceeding max_uses + 2 with empty final_text must not fall through to 502 upstream_empty_turn."""
    search = FakeSearch()
    made = []
    # Every step emits thinking + web_search call ONLY (words=""), so final_text is empty.
    scripts = [asks("q%d" % n, call_id="c%d" % n, sig="s%d" % n, words="") for n in range(8)]
    results, sent, _ = drive(
        tmp_path, monkeypatch, scripts, search,
        [lambda _: made.append(search_request(search, tool={"max_uses": 1})) or made[-1]],
    )
    assert len(sent) == 4 and len(search.calls) == 1
    assert not any(e["type"] == "error" for e in results[0])
    assert results[0][-1] == {"type": "end", "reason": "stop"}
    _, final = reply_of(made[0], results[0])
    assert final["stop_reason"] == "end_turn"
    found = [b["content"] for b in final["content"] if b["type"] == "web_search_tool_result"]
    assert len(found) == 4


def test_web_search_loop_cap_with_tool_calls_end_reason_and_empty_text_stops_cleanly(tmp_path):
    """2(b): Direct session emitting end(reason='tool_calls') and empty text on cap ends cleanly with reason='stop'."""
    search = FakeSearch()

    class DirectLikeSession:
        thread_id = "t-ws"
        _cancelled = False
        _needs_drain = False
        _client_context = None
        _client_context_pending = False

        def __init__(self, cfg):
            self.backend = SimpleNamespace(cfg=cfg)
            self.step = 0

        def set_direct(self, *args, **kwargs):
            pass

        def register_tools(self, tools):
            pass

        async def stream(self, prompt, model_provider, tool_names=None):
            self.step += 1
            yield {"type": "tool_call", "id": f"ws-{self.step}", "name": "web_search",
                   "arguments": json.dumps({"query": f"q{self.step}"}), "partial": False}
            yield {"type": "end", "reason": "tool_calls"}

        async def aclose(self):
            pass

    cfg = dict(config.DEFAULTS, transport="direct", workspace_root=str(tmp_path),
               provider_resource_name="__managed__", _web_search=search,
               exec_policy="off", cache_keepwarm_seconds=0)
    session = DirectLikeSession(cfg)

    class FakePool:
        async def acquire(self, *args, **kwargs):
            return session

        async def release(self, sess):
            pass

    runner = orchestrator.TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), FakePool())
    parsed = search_request(search, tool={"max_uses": 1})
    events = asyncio.run(run(runner, parsed))
    assert not any(e["type"] == "error" for e in events)
    assert events[-1] == {"type": "end", "reason": "stop"}
    _, final = reply_of(parsed, events)
    assert final["stop_reason"] == "end_turn"
    assert len([b for b in final["content"] if b["type"] == "web_search_tool_result"]) == 4


def test_invalid_web_search_tool_call_is_not_done_and_not_executed(tmp_path):
    """2(c): A cut/invalid web_search tool_call must emit done=False and never invoke searcher."""
    search = FakeSearch()

    class InvalidSearchSession:
        thread_id = "t-invalid-ws"
        _cancelled = False
        _needs_drain = False
        _client_context = None
        _client_context_pending = False

        def __init__(self, cfg):
            self.backend = SimpleNamespace(cfg=cfg)

        def set_direct(self, *args, **kwargs):
            pass

        def register_tools(self, tools):
            pass

        async def stream(self, prompt, model_provider, tool_names=None):
            yield {"type": "text", "delta": "Checking..."}
            yield {"type": "tool_call", "id": "ws-bad", "name": "web_search",
                   "arguments": '{"query": "cut', "partial": False, "invalid": True}
            yield {"type": "end", "reason": "stop"}

        async def aclose(self):
            pass

    cfg = dict(config.DEFAULTS, transport="direct", workspace_root=str(tmp_path),
               provider_resource_name="__managed__", _web_search=search,
               exec_policy="off", cache_keepwarm_seconds=0)
    session = InvalidSearchSession(cfg)

    class FakePool:
        async def acquire(self, *args, **kwargs):
            return session

        async def release(self, sess):
            pass

    runner = orchestrator.TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), FakePool())
    parsed = search_request(search)
    events = asyncio.run(run(runner, parsed))
    ws_events = [e for e in events if e.get("type") == "tool_call" and e.get("server") == "web_search"]
    assert len(ws_events) == 1
    assert ws_events[0]["done"] is False
    assert "invalid" not in ws_events[0] and "partial" not in ws_events[0]
    assert search.calls == []
    assert not any(e.get("type") == "server_tool_result" for e in events)


def test_validate_rejects_web_search_on_explicit_non_direct_transport_but_keeps_omitted_transport_fixtures():
    """2(d): Non-direct transport='agent' refuses server web_search even with a provider; omitted transport still passes."""
    body = _body("anthropic", tools=[dict(SERVER, max_uses=3)])
    # Fixture omitting transport (like test_web_search.py CFG / PARSE_CFG) continues working:
    parsed_omitted = validate.parse_anthropic(copy.deepcopy(body), dict(PARSE_CFG, _web_search=FakeSearch()))
    assert parsed_omitted.web_search["name"] == "web_search"
    # Explicit non-direct transport refuses server web_search cleanly with 422:
    with pytest.raises(apierr.ApiError) as err:
        validate.parse_anthropic(copy.deepcopy(body), dict(CFG, transport="agent", _web_search=FakeSearch()))
    assert err.value.status == 422 and err.value.param == "tools[0].type"
    # _finish also guards transport when called with search=True directly:
    parsed_direct = validate.parse_anthropic(_body("anthropic"), dict(CFG, transport="agent"))
    with pytest.raises(apierr.ApiError) as finish_err:
        validate._finish(copy.deepcopy(body), parsed_direct, "anthropic",
                         search=True, cfg=dict(CFG, transport="agent"))
    assert finish_err.value.status == 422


# --- Bug #3: validate._reject_images_unsupported nested tool-result images ----

DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
ANTHROPIC_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
RESPONSES_IMAGE = {"type": "input_image", "image_url": DATA_URL}


def test_non_vision_model_rejects_nested_anthropic_tool_result_images(monkeypatch):
    entry = dict(models.resolve("zai-org/GLM-5.3")[0], id="synthetic-no-vision-tool-anth", vision=False)
    monkeypatch.setitem(models._BY_ID, entry["id"], entry)
    cfg = dict(CFG, strict_models=True)
    for nested in (
        [{"type": "text", "text": "here"}, ANTHROPIC_IMAGE],
        ANTHROPIC_IMAGE,
        [{"type": "tool_result", "tool_use_id": "inner", "content": [ANTHROPIC_IMAGE]}],
    ):
        body = {
            "model": entry["id"],
            "max_tokens": 64,
            "messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": nested},
            ]}],
        }
        with pytest.raises(apierr.ApiError) as err:
            validate.parse_anthropic(body, cfg)
        assert err.value.status == 422 and "does not support image input" in err.value.message


@pytest.mark.parametrize("item_type", ["function_call_output", "custom_tool_call_output"])
@pytest.mark.parametrize("output_payload", [
    [RESPONSES_IMAGE],
    RESPONSES_IMAGE,
    [{"type": "input_text", "text": "ok"}, {"type": "image_url", "image_url": {"url": DATA_URL}}],
])
def test_non_vision_model_rejects_nested_responses_tool_output_images(monkeypatch, item_type, output_payload):
    entry = dict(models.resolve("zai-org/GLM-5.3")[0], id="synthetic-no-vision-tool-resp", vision=False)
    monkeypatch.setitem(models._BY_ID, entry["id"], entry)
    cfg = dict(CFG, strict_models=True)
    body = {
        "model": entry["id"],
        "input": [{"type": item_type, "call_id": "call_1", "output": copy.deepcopy(output_payload)}],
    }
    with pytest.raises(apierr.ApiError) as err:
        validate.parse_responses(body, cfg)
    assert err.value.status == 422 and "does not support image input" in err.value.message


def test_vision_model_still_accepts_nested_tool_result_images():
    cfg = dict(config.DEFAULTS)
    anth_body = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "look", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [ANTHROPIC_IMAGE]}]},
        ],
    }
    parsed_anth = validate.parse_anthropic(anth_body, cfg)
    assert parsed_anth.native_messages[-1]["role"] == "tool"
    resp_body = {
        "model": "claude-opus-5-5",
        "input": [{"type": "function_call_output", "call_id": "c1", "output": [RESPONSES_IMAGE]}],
    }
    parsed_resp = validate.parse_responses(resp_body, cfg)
    assert parsed_resp.native_messages[-1]["role"] == "tool"
