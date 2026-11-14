"""Golden test of the legacy head (prompt control plan, invariant I1).

Without a prompts file the relay must build the same head and send the same body to Retool as before the prompt
layer existed: the head, the cache blocks, the roles of the messages, the request purpose and the refusals of
every cell are compared byte for byte with tests/golden/prompt_matrix.json.

The matrix is the one of the research report (17 request classes x 3 modes x 3 roles = 153 cells), driven through
the real validate.parse_*, TurnRunner, SessionPool and direct.build_body with the network replaced by a stub that
records the body the transport would POST. The cells of the "real" section feed bodies shaped like captures of
OpenCode, the Claude Agent SDK runner of Claude Desktop and Codex through the same path (every text is synthetic,
see prompt_bodies.py).

Rewrite the fixture after a deliberate change of the legacy behaviour with
    PROXI_UPDATE_GOLDEN=1 python -m pytest tests/test_prompt_golden.py -o addopts=
"""
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import os
import pathlib
import re

import pytest

import client_context
import direct
import prompt_policy
import validate
from prompt_bodies import (CLAUDE_SDK_HEADERS, CODEX_HEADERS, OPENCODE_COMPACTION_V1, OPENCODE_HEADERS,
                           OPENCODE_SUMMARY, OPENCODE_TITLE_V1, OPENCODE_TITLE_V2, claude_sdk_body, codex_body,
                           opencode_body, service_body)
from test_native_context_reuse import TOOLS, request, run, setup

GOLDEN = pathlib.Path(__file__).parent / "golden" / "prompt_matrix.json"
UPDATE = os.environ.get("PROXI_UPDATE_GOLDEN") == "1"
WORKSPACE_NAME = "pc-golden-ws"
LONG_HEAD = 2000
PARSE_CFG = {"strict_models": True, "model": "claude-opus-5-5", "allow_effort_downgrade": True,
             "show_thinking": False}

CLIENT = "CLIENT_SYSTEM"
MY = "MY_ROLE"
TITLE_CC = ("You are naming a coding session so the user can pick it out of a long list of sessions.\n"
            "Reply with a short title.")
CLASSIFIER = "You are a security monitor for autonomous AI coding agents.\nstable policy"
OC_TITLE = "You are a title generator. You output ONLY a thread title. Nothing else."
OC_COMPACT = "You are an anchored context summarization assistant for coding sessions."
ROLES = {"None": None, '""': "", "MY_ROLE": MY}
MODES = ["standalone", "append", "replace"]


def structured(dialect, messages, system, tools):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
              "additionalProperties": False}
    data = {"model": "claude-opus-5-5"}
    if dialect == "chat":
        data["messages"] = ([{"role": "system", "content": system}] if system else []) + list(messages)
        data["response_format"] = {"type": "json_schema", "json_schema": {"name": "r", "schema": schema}}
    elif dialect == "responses":
        data["input"] = list(messages)
        if system:
            data["instructions"] = system
        data["text"] = {"format": {"type": "json_schema", "name": "r", "schema": schema}}
    else:
        data["messages"] = list(messages)
        data["max_tokens"] = 64
        if system:
            data["system"] = system
        data["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if tools:
        if dialect == "chat":
            data["tools"] = tools
        elif dialect == "responses":
            data["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
        else:
            data["tools"] = [{"name": tool["function"]["name"], "description": "d",
                              "input_schema": tool["function"]["parameters"]} for tool in tools]
    return data


# The research report's classes A..Q, in its order and with its labels.
CASES = [
    ("A main, tools, client system [OpenCode/Claude Code/Codex-like]",
     dict(tools=True, system=CLIENT, client="opencode")),
    ("B main, tools, NO client system [native fallback]", dict(tools=True, system="", client="opencode")),
    ("C tool-less + client system, known client OpenCode [client-text]",
     dict(tools=False, system=CLIENT, client="opencode")),
    ("D tool-less + client system, unknown client [Codex/Harness/aider -> generation]",
     dict(tools=False, system=CLIENT, client="")),
    ("E tool-less, NO system, unknown client [curl -> relay task]", dict(tools=False, system="", client="")),
    ("F tool-less, NO system, known client OpenCode [client-text without system]",
     dict(tools=False, system="", client="opencode")),
    ("G Claude Code title (SDK prefix) [session-title]", dict(tools=False, system=TITLE_CC, client="claude-code")),
    ("H Claude Code auto-mode classifier [auto-mode-check]",
     dict(tools=False, system=CLASSIFIER, client="claude-code")),
    ("I Claude Desktop connection probe [connection-probe]",
     dict(tools=False, system="", client="claude-desktop", content=".", max_tokens=1)),
    ("J OpenCode title generator (real wording) [client-text]", dict(tools=False, system=OC_TITLE, client="opencode")),
    ("K OpenCode compaction (real wording) [client-text]", dict(tools=False, system=OC_COMPACT, client="opencode")),
    ("L Claude Code compaction via request-class header",
     dict(tools=False, system=OC_COMPACT, client="claude-code", request_class="compaction")),
    ("M Claude Code auxiliary via request-class header",
     dict(tools=False, system=OC_COMPACT, client="claude-code", request_class="auxiliary")),
    ("N structured + tools + client system", dict(tools=True, system=CLIENT, client="opencode", structured=True)),
    ("O structured, tool-less, client system", dict(tools=False, system=CLIENT, client="", structured=True)),
    ("P structured, tool-less, NO system", dict(tools=False, system="", client="", structured=True)),
    ("Q structured + tools, NO system", dict(tools=True, system="", client="", structured=True)),
]

# Bodies shaped like real captures: body, dialect, the headers the client sends and the parse settings.
REAL = [
    ("R1 OpenCode 2.x main agent: chat, 13 tools, reasoning_content, mid-dialog reminders",
     dict(data=opencode_body(), dialect="chat", headers=OPENCODE_HEADERS)),
    ("R2 Claude Agent SDK runner (Desktop): 3 system blocks with a billing header, a role:system entry per turn",
     dict(data=claude_sdk_body(), dialect="anthropic", headers=CLAUDE_SDK_HEADERS)),
    ("R3 Codex 0.164: Responses, no instructions field, three leading developer items, AGENTS.md as a user item",
     dict(data=codex_body(), dialect="responses", headers=CODEX_HEADERS,
          parse={"model": "gpt-6-sol"})),
    ("R4 OpenCode 2.x title agent (tool-less, own system)",
     dict(data=service_body(OPENCODE_TITLE_V2), dialect="chat", headers=OPENCODE_HEADERS)),
    ("R5 OpenCode summary agent (tool-less, own system)",
     dict(data=service_body(OPENCODE_SUMMARY), dialect="chat", headers=OPENCODE_HEADERS)),
    ("R6 OpenCode 1.x title agent with the plugin's X-Proxi-Agent header",
     dict(data=service_body(OPENCODE_TITLE_V1), dialect="chat",
          headers={**OPENCODE_HEADERS, "x-proxi-client": "opencode", "x-proxi-agent": "title"})),
    ("R7 OpenCode 1.x compaction agent with the plugin's X-Proxi-Agent header",
     dict(data=service_body(OPENCODE_COMPACTION_V1, "synthetic history to summarize"), dialect="chat",
          headers={**OPENCODE_HEADERS, "x-proxi-client": "opencode", "x-proxi-agent": "compaction"})),
]

TEXT_ANSWER = [{"event": "content_block_start", "type": "text"},
               {"event": "content_block_delta", "type": "text", "partialText": "ok"},
               {"event": "content_block_end", "type": "text"},
               {"event": "metadata", "usage": {"inputTokens": 4, "outputTokens": 1, "creditsUsed": 0.1}},
               {"event": "end_of_stream"}]


class Response(io.BytesIO):
    status = 200
    headers = {"request-id": "upstream-id"}


@pytest.fixture
def wire(monkeypatch):
    """The scripted Retool: every body the transport would POST is kept, the answer is always "ok"."""
    sent = []

    def opener(req, **_kwargs):
        sent.append(json.loads(req.data))
        return Response(("\n".join(json.dumps(event) for event in TEXT_ANSWER) + "\n").encode("utf-8"))

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return sent


def make_workspace(tmp_path):
    root = tmp_path / WORKSPACE_NAME
    root.mkdir()
    (root / "README.md").write_text("# Demo project\nSYNTHETIC README LINE.\n", encoding="utf-8")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return root


def head_text(body):
    messages = body.get("messages") or []
    if messages and messages[0].get("role") == "system":
        content = messages[0]["content"]
        return "\n".join(block.get("text", "") for block in content) if isinstance(content, list) else str(content)
    return None


def role_runs(messages):
    runs = []
    for message in messages:
        if runs and runs[-1][0] == message["role"]:
            runs[-1][1] += 1
        else:
            runs.append([message["role"], 1])
    return ["%s×%d" % (name, count) if count > 1 else name for name, count in runs]


def head_record(text):
    """The head itself when it is short; otherwise its length, a full sha256 and both ends, which keeps the fixture small."""
    if text is None or len(text) <= LONG_HEAD:
        return text
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "start": text[:100], "end": text[-100:]}


class Cell:
    """What one run of the relay made of a request: normalised so the fixture holds no machine-specific text."""

    def __init__(self, root):
        self.real = os.path.realpath(str(root))

    def norm(self, text):
        if text is None:
            return None
        return text.replace(self.real, "<workspace>").replace(WORKSPACE_NAME, "<ws>")

    def record(self, purpose, events, sent):
        error = next((event["error"] for event in events if event.get("type") == "error"), None)
        result = {"purpose": purpose, "error": ("%s: %s" % (error.code, error.message)) if error else None,
                  "head": None, "head_blocks": None, "roles": None, "wire": None}
        if sent:
            body = sent[0]
            head = head_text(body)
            blocks = body["messages"][0]["content"] if head is not None else []
            result.update(head=head_record(self.norm(head)),
                          head_blocks=[[len(self.norm(block.get("text", ""))), bool(block.get("cache"))]
                                       for block in blocks] if head is not None else [],
                          roles=role_runs(body["messages"]))
            raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            # The wire body of a relay task names the workspace (the cache key is a hash of it): left out there.
            if self.real not in raw and WORKSPACE_NAME not in raw:
                result["wire"] = {"sha": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
                                  "messages": len(body["messages"]), "tools": len(body.get("tools") or []),
                                  "keys": sorted(body)}
        return result


def relay(root, **cfg):
    settings = dict(transport="direct", cache_keepwarm_seconds=0, base="https://fixture.invalid", cookie_values=[],
                    project_context=True, exec_policy="allowlist", reasoning_effort="high",
                    workspace_root=str(root), context_max_bytes=6000, show_thinking=False, exec_allowlist=[],
                    openai_provider_resource_name="test-openai")
    settings.update(cfg)
    backend, pool, runner = setup(root, **settings)
    backend.headers = lambda: {}
    return pool, runner


async def drive(pool, runner, parsed):
    try:
        return await run(runner, parsed)
    finally:
        for session in list(pool._conversations.values()):
            await session.aclose()


def matrix_cell(root, wire, spec, mode, role, attach=None):
    """One cell of the research matrix, as the report's offline rig ran it."""
    wire.clear()
    pool, runner = relay(root, relay_system_prompt_mode=mode, relay_system_prompt=ROLES[role])
    tools = copy.deepcopy(TOOLS) if spec.get("tools") else []
    messages = [{"role": "user", "content": spec.get("content", "Inspect ORIGINAL_TASK.")}]
    dialect = spec.get("dialect", "chat")
    parsed = request(messages, key="header:client-a", system=spec.get("system", ""), tools=tools, dialect=dialect)
    if spec.get("structured"):
        data = structured(dialect, messages, spec.get("system", ""), tools)
        parser = {"chat": validate.parse_chat, "responses": validate.parse_responses,
                  "anthropic": validate.parse_anthropic}[dialect]
        parsed = parser(data, {"strict_models": True, "model": "claude-opus-5-5"})
        parsed.conversation_key = "header:client-a"
    if spec.get("max_tokens") is not None:
        parsed.max_tokens = spec["max_tokens"]
    identity = {key: spec[key] for key in ("client", "request_class") if spec.get(key)}
    token = client_context.bind(identity)
    try:
        # proxy.py computes request_purpose once, with the identity bound, before the runner starts.
        parsed.request_purpose = client_context.request_kind(parsed)
        if attach is not None:
            attach(parsed, runner)
        with contextlib.redirect_stdout(io.StringIO()):
            events = asyncio.run(drive(pool, runner, parsed))
    finally:
        client_context.reset(token)
    return Cell(root).record(parsed.request_purpose, events, wire)


def real_cell(root, wire, spec, mode, role, attach=None):
    """One cell of a capture-shaped body: parsed and identified the way proxy._completion does before the runner."""
    wire.clear()
    data, dialect, headers = copy.deepcopy(spec["data"]), spec["dialect"], spec["headers"]
    pool, runner = relay(root, relay_system_prompt_mode=mode, relay_system_prompt=ROLES[role])
    identity = client_context.describe(data, headers, {"client_metadata_home": str(root)})
    token = client_context.bind({**identity, "request_id": "golden"})
    try:
        parser = {"chat": validate.parse_chat, "responses": validate.parse_responses,
                  "anthropic": validate.parse_anthropic}[dialect]
        parsed = parser(data, {**PARSE_CFG, **spec.get("parse", {})})
        parsed.conversation_key, parsed.parent_conversation_key = validate.conversation_affinity(
            data, headers, client_context.current())
        parsed.request_id, parsed.thinking_placeholder, parsed.dialect = "golden", "", dialect
        parsed.request_purpose = client_context.request_kind(parsed)
        if attach is not None:
            attach(parsed, runner)
        with contextlib.redirect_stdout(io.StringIO()):
            events = asyncio.run(drive(pool, runner, parsed))
    finally:
        client_context.reset(token)
    return Cell(root).record(parsed.request_purpose, events, wire)


def produce(root, wire, section, label, attach=None):
    runner_of = {"matrix": matrix_cell, "real": real_cell}[section]
    specs = dict(CASES if section == "matrix" else REAL)
    return {"%s|%s" % (role, mode): runner_of(root, wire, specs[label], mode, role, attach)
            for role in ROLES for mode in MODES}


def load_golden():
    with open(GOLDEN, encoding="utf-8") as handle:
        return json.load(handle)


def describe(before, after):
    if isinstance(before, str) and isinstance(after, str):
        at = next((index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]),
                  min(len(before), len(after)))
        return "differs at char %d (%d -> %d chars): %r != %r" % (
            at, len(before), len(after), before[max(0, at - 30):at + 50], after[max(0, at - 30):at + 50])
    return "%s != %s" % (str(before)[:200], str(after)[:200])


def differences(expected, produced):
    """Field by field, so a failure names the cell and what moved instead of dumping two walls of text."""
    found = []
    for cell in sorted(set(expected) | set(produced)):
        before, after = expected.get(cell), produced.get(cell)
        if before == after:
            continue
        if before is None or after is None:
            found.append("%s: cell %s" % (cell, "missing from the fixture" if before is None else "not produced"))
            continue
        for field in sorted(set(before) | set(after)):
            if before.get(field) != after.get(field):
                found.append("%s.%s %s" % (cell, field, describe(before.get(field), after.get(field))))
    return found


LABELS = {"matrix": [label for label, _ in CASES], "real": [label for label, _ in REAL]}


@pytest.mark.skipif(not UPDATE, reason="set PROXI_UPDATE_GOLDEN=1 to rewrite tests/golden/prompt_matrix.json")
def test_rewrite_the_golden_fixture(tmp_path, wire):
    root = make_workspace(tmp_path)
    golden = {"version": 1, "matrix": {}, "real": {}}
    for section, labels in LABELS.items():
        for label in labels:
            golden[section][label] = produce(root, wire, section, label)
    GOLDEN.parent.mkdir(exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(golden, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


@pytest.mark.skipif(UPDATE, reason="the fixture is being rewritten")
@pytest.mark.parametrize("label", LABELS["matrix"])
def test_matrix_cells_are_byte_for_byte_the_recorded_legacy_behaviour(tmp_path, wire, label):
    expected = load_golden()["matrix"][label]
    assert len(expected) == 9
    assert differences(expected, produce(make_workspace(tmp_path), wire, "matrix", label)) == []


@pytest.mark.skipif(UPDATE, reason="the fixture is being rewritten")
@pytest.mark.parametrize("label", LABELS["real"])
def test_capture_shaped_bodies_are_byte_for_byte_the_recorded_legacy_behaviour(tmp_path, wire, label):
    expected = load_golden()["real"][label]
    assert len(expected) == 9
    assert differences(expected, produce(make_workspace(tmp_path), wire, "real", label)) == []


def inactive_layer(tmp_path):
    """What proxy._completion leaves on a request when no prompts file exists: the purpose, a store, a legacy plan."""
    store = prompt_policy.PromptStore({"prompts_file": str(tmp_path / "no-such-prompts.json")})

    def attach(parsed, runner):
        parsed.prompt_purpose = client_context.prompt_purpose(parsed)
        parsed.prompt_plan = store.plan(prompt_policy.PlanInput.from_request(
            parsed, client_context.current(), {}, getattr(parsed, "dialect", None) or "chat"))
        assert parsed.prompt_plan.legacy
        runner.prompts = store

    return attach


@pytest.mark.skipif(UPDATE, reason="the fixture is being rewritten")
@pytest.mark.parametrize("section,label", [("matrix", name) for name in LABELS["matrix"]]
                         + [("real", name) for name in LABELS["real"]])
def test_a_prompt_layer_with_no_prompts_file_changes_nothing_in_any_cell(tmp_path, wire, section, label):
    # plan I1: the store is asked, a legacy plan comes back, the runner takes the code that was always there
    expected = load_golden()[section][label]
    root = make_workspace(tmp_path)
    assert differences(expected, produce(root, wire, section, label, attach=inactive_layer(tmp_path))) == []


def test_the_fixture_holds_every_cell_and_nothing_else():
    golden = load_golden()
    assert golden["version"] == 1
    for section, labels in LABELS.items():
        assert list(golden[section]) == labels
        assert all(sorted(cells) == sorted("%s|%s" % (r, m) for r in ROLES for m in MODES)
                   for cells in golden[section].values())
    assert sum(len(cells) for cells in golden["matrix"].values()) == 153


def test_the_fixture_pins_the_wire_body_wherever_no_workspace_path_is_in_it():
    golden = load_golden()
    pinned = [cell["wire"] for section in ("matrix", "real") for cells in golden[section].values()
              for cell in cells.values() if cell["wire"]]
    assert len(pinned) > 120
    assert all(len(item["sha"]) == 16 and item["keys"] for item in pinned)
    # Relay tasks name the workspace in their head: those cells pin the head but not a hash of the whole body.
    relay_task = golden["matrix"]["E tool-less, NO system, unknown client [curl -> relay task]"]["None|standalone"]
    assert relay_task["wire"] is None and "<workspace>" in relay_task["head"]


def test_the_fixture_stores_no_machine_specific_or_private_text():
    raw = GOLDEN.read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\\\|/home/|/Users/|\\\\Users\\\\|AppData|pytest-of-", raw)
    assert not re.search(r"(?i)bearer |x-api-key|accessToken|xsrfToken|cookie=", raw)
    assert WORKSPACE_NAME not in raw.replace("<ws>", "")
