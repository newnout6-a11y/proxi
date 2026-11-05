"""Scaffolding of the prompt-runtime tests: a fake prompt store that keeps to the contract of prompt_policy, and the
real relay (HTTP server, pool, runner, transport) in front of a scripted Retool.

The engine (prompt_policy) and the body rewrite (prompt_body) belong to other work packages. These tests pin what
the runtime does with whatever they return, so the store here stands in for them: it takes pins, protected rules and
refusals from the contract (plan 4.3) and renders heads the way compose_head promises to (plan I5, I9, I10).
"""
import dataclasses
import http.client
import json
import re
import threading
import time

import pytest

import apierr
import config
import direct
import journal
import prompt_policy
import proxy
from test_review_fixes_pr19 import END, response, text
from test_review_fixes_proxy import FakeManager
from upstream_attempts import AttemptBudget

KEY = "prompt-runtime-relay-key"
BILLING = "x-anthropic-billing-header:"
VARIABLE = re.compile(r"\{\{([a-z_]+(?::[A-Za-z0-9_]+)?)\}\}")
PATHS = {"chat": "/v1/chat/completions", "anthropic": "/v1/messages", "responses": "/v1/responses"}
READ_FILE = {"name": "read_file", "description": "Read the client's file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "number"}}}}


@dataclasses.dataclass(frozen=True)
class FakePlan(prompt_policy.Plan):
    """A plan whose head is rendered from a template: {{client_system}}, {{role}}, {{text:<name>}}, ...

    The engine's promises are kept: a billing header line stays first (I5), the variables of a relay task are empty
    for a request the client owns (I9) and the structured contract is added to the end when no template places it (I10).
    """
    template: str = ""
    texts: dict = dataclasses.field(default_factory=dict)
    warn: tuple = ()

    def compose_head(self, client_system, sources):
        billing = ""
        if client_system.startswith(BILLING):
            billing, _, client_system = client_system.partition("\n")
        parts, position = [], 0
        for match in VARIABLE.finditer(self.template):
            if match.start() > position:
                parts.append(prompt_policy.Part("literal", self.template[position:match.start()]))
            parts.append(prompt_policy.Part(*self._variable(match.group(1), client_system, sources)))
            position = match.end()
        if position < len(self.template):
            parts.append(prompt_policy.Part("literal", self.template[position:]))
        contract = sources.structured()
        if contract and "{{structured}}" not in self.template:
            parts += [prompt_policy.Part("literal", "\n\n"), prompt_policy.Part("relay:structured", contract)]
        if billing:
            parts = [prompt_policy.Part("billing", billing), prompt_policy.Part("literal", "\n")] + parts
        return "".join(part.text for part in parts), parts, list(self.warn)

    def _variable(self, name, client_system, sources):
        if name in prompt_policy.RELAY_TASK_VARIABLES and sources.client_owned:
            return "literal", ""
        known = {"client_system": ("client", lambda: client_system), "role": ("relay:role", sources.relay_role),
                 "native_role": ("relay:native_role", sources.native_role),
                 "relay_task": ("relay:task", sources.relay_task), "local_exec": ("relay:local_exec", sources.local_exec),
                 "project_context": ("relay:project_context", sources.project_context),
                 "exec_reminder": ("relay:exec_reminder", sources.exec_reminder),
                 "structured": ("relay:structured", sources.structured)}
        if name.startswith("text:"):
            return name, self.texts[name[5:]]
        source, produce = known[name]
        return source, produce()


class FakeSnapshot:
    """The part of prompt_policy.Snapshot the runtime reads: one profile per name and the relay's own texts."""

    def __init__(self, rev, profiles, relay_texts=None, rules=(), default_profile=None):
        self.rev, self.profiles, self.relay_texts = rev, profiles, dict(relay_texts or {})
        self.rules, self.default_profile = list(rules), default_profile

    def profile(self, name):
        return self.profiles[name]

    def relay_text(self, name):
        return self.relay_texts.get(name)


def profile(template, *, edits=False, generation=None, texts=None):
    return {"template": template, "edits": edits, "generation": dict(generation or {}), "texts": dict(texts or {})}


class FakePrompts:
    """The store as the runtime sees it: reload, plan with pins and refusals, record, unpin."""

    def __init__(self):
        self.revisions, self.current, self.pins = {}, None, {}
        self.inputs, self.plans, self.order, self.records = [], [], [], []
        self.fail, self.refuse = {}, None

    def publish(self, rev, profiles, **options):
        """A new revision becomes the current one; conversations already pinned keep theirs."""
        profiles = {"passthrough": profile("{{client_system}}"), **profiles}
        self.revisions[rev] = FakeSnapshot(rev, profiles, **options)
        self.current = rev

    def snapshot(self):
        return self.revisions.get(self.current)

    def maybe_reload(self):
        self.order.append("reload")
        if "reload" in self.fail:
            raise self.fail["reload"]

    def plan(self, inp):
        self.order.append("plan")
        self.inputs.append(inp)
        self.plans.append(self._plan(inp))
        return self.plans[-1]

    def _plan(self, inp):
        if "plan" in self.fail:
            raise self.fail["plan"]
        refusal = self.refuse(inp) if self.refuse else None
        if refusal is not None:
            raise refusal
        if self.current is None:
            return prompt_policy.Plan()
        key = (inp.conversation_key, inp.purpose) if inp.conversation_key else None
        pinned = key in self.pins and self.pins[key] in self.revisions
        rev = self.pins[key] if pinned else self.current
        if key and not pinned:
            self.pins[key] = rev
        snapshot = self.revisions[rev]
        name, rule, protected = self._select(snapshot, inp)
        if name is None:
            return prompt_policy.Plan(rev=rev, pinned=pinned, snapshot=snapshot)
        chosen = snapshot.profiles[name]
        return FakePlan(rev=rev, profile=name, rule=rule, pinned=pinned, protected=protected,
                        body_edits=bool(chosen["edits"]), generation=dict(chosen["generation"]), snapshot=snapshot,
                        template=chosen["template"], texts=chosen["texts"])

    @staticmethod
    def _select(snapshot, inp):
        for rule in prompt_policy.PROTECTED_RULES:
            condition = rule["if"]
            if (inp.purpose in condition.get("purpose", ()) or inp.request_class in condition.get("request_class", ())):
                return rule["use"], rule["name"], True
        for name, predicate in snapshot.rules:
            if predicate(inp):
                return name, "rule for " + name, False
        return snapshot.default_profile, "default" if snapshot.default_profile else None, False

    def record(self, entry):
        self.order.append("record")
        if "record" in self.fail:
            raise self.fail["record"]
        self.records.append(entry)

    def recent(self, limit=50):
        return list(reversed(self.records))[:limit]

    def unpin(self, conversation_key=None):
        gone = [key for key in self.pins if conversation_key is None or key[0] == conversation_key]
        for key in gone:
            del self.pins[key]
        return len(gone)


def account(name="acc-a"):
    return {"account_id": name, "page_uuid": "page-" + name, "thread_id": "thread-" + name,
            "cookie": "accessToken=cookie-%s-123456; xsrfToken=q" % name, "xsrf": "q",
            "provider_resource_name": "__managed__", "openai_provider_resource_name": "__managed__"}


class Relay:
    """The relay over HTTP: its fake prompt store, the bodies it posted to Retool and the journal of what it did."""

    def __init__(self, runtime, server, port, posts, scripts):
        self.runtime, self.server, self.port, self.posts, self.scripts = runtime, server, port, posts, scripts
        self.prompts = runtime.prompts

    def call(self, path, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", path, json.dumps(payload).encode(),
                               {"content-type": "application/json", "authorization": "Bearer " + KEY,
                                **(headers or {})})
            reply = connection.getresponse()
            raw = reply.read().decode()
            return reply.status, (json.loads(raw or "{}") if "json" in reply.headers.get("content-type", "")
                                  else raw), reply.headers
        finally:
            connection.close()

    def post(self, dialect, payload, headers=None):
        return self.call(PATHS[dialect], payload, headers)

    def events(self, since, kind=None):
        found = journal.JOURNAL.query(since=since, limit=5000, order="asc")
        return [event for event in found if kind is None or event["kind"] == kind or (
            kind.endswith(".") and event["kind"].startswith(kind))]

    @staticmethod
    def mark():
        return journal.JOURNAL.last_seq

    @staticmethod
    def settle(headers, timeout=5):
        """Wait for the http.request event of the request these response headers belong to: it is journaled after the reply."""
        request_id, deadline = headers["request-id"], time.monotonic() + timeout
        while not any(event["kind"] == "http.request" and event.get("request_id") == request_id
                      for event in journal.JOURNAL.query(limit=5000)):
            assert time.monotonic() < deadline, "the HTTP request was never journaled"
            time.sleep(0.01)


@pytest.fixture
def prompt_relay(tmp_path, monkeypatch):
    """start(**cfg) -> Relay: real HTTP, pool, runner and transport; Retool is scripted, its bodies are kept."""
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    started, posts, scripts = [], [], []

    def opener(request_, **_kwargs):
        posts.append(json.loads(request_.data))
        return response(*(scripts.pop(0) if scripts else [text("OK"), *END]))

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)

    def start(**overrides):
        cfg = dict(config.DEFAULTS)
        cfg.update({"base": "https://example.invalid", "page_uuid": "page-acc-a", "thread_id": "thread-a",
                    "ua": "test-agent", "cookie": "accessToken=cookie-acc-a-123456; xsrfToken=q", "xsrf": "q",
                    "relay_api_key": KEY, "workspace_root": str(tmp_path), "command_log": str(tmp_path / "c.log"),
                    "host": "127.0.0.1", "port": 0, "cookie_values": [], "client_metadata_home": str(tmp_path),
                    "cache_keepwarm_seconds": 0, "upstream_error_dir": str(tmp_path), "accounts": [account()],
                    "prompts_file": str(tmp_path / "prompts.json"), "allow_effort_downgrade": False})
        cfg.update(overrides)
        runtime = proxy.Runtime(cfg)
        for backend in runtime.accounts.backends:
            manager = FakeManager(100.0)
            runtime.accounts.managers[backend] = manager
            backend.credits = manager
        runtime.credits = runtime.accounts.managers[runtime.accounts.backends[0]]
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((runtime, server))
        return Relay(runtime, server, server.server_address[1], posts, scripts)

    yield start
    for runtime, server in started:
        server.shutdown()
        try:
            server.server_close()
        except OSError:
            pass
        runtime.close()


def fake_relay(prompt_relay, **cfg):
    """A Relay whose runtime and runner use a FakePrompts store."""
    relay = prompt_relay(**cfg)
    relay.prompts = relay.runtime.prompts = relay.runtime.runner.prompts = FakePrompts()
    return relay


# -- the three dialects of one conversation --------------------------------------------------------------------
def client_request(dialect, messages, *, system="CLIENT_SYSTEM", tools=True, model="claude-opus-5-5", **extra):
    """The request of a client in `dialect`; messages are already in that dialect's form."""
    if dialect == "chat":
        data = {"model": model, "messages": ([{"role": "system", "content": system}] if system else []) + messages}
        if tools:
            data["tools"] = [{"type": "function", "function": READ_FILE}]
    elif dialect == "anthropic":
        data = {"model": model, "max_tokens": 64, "messages": messages}
        if system:
            data["system"] = system
        if tools:
            data["tools"] = [{"name": READ_FILE["name"], "description": READ_FILE["description"],
                              "input_schema": READ_FILE["parameters"]}]
    else:
        data = {"model": model, "input": messages}
        if system:
            data["instructions"] = system
        if tools:
            data["tools"] = [{"type": "function", **READ_FILE}]
    return {**data, **extra}


def after_tool_call(dialect, messages, payload, value="file contents"):
    """The messages of the next request: the reply of the relay and the client's tool result (what an agent loop does)."""
    if dialect == "chat":
        assistant = payload["choices"][0]["message"]
        return messages + [assistant, {"role": "tool", "tool_call_id": assistant["tool_calls"][0]["id"],
                                       "content": value}]
    if dialect == "anthropic":
        call = next(block["id"] for block in payload["content"] if block["type"] == "tool_use")
        return messages + [{"role": "assistant", "content": payload["content"]},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call,
                                                         "content": value}]}]
    call = next(item["call_id"] for item in payload["output"] if item["type"] == "function_call")
    return messages + payload["output"] + [{"type": "function_call_output", "call_id": call, "output": value}]


def strip_cache(value):
    if isinstance(value, dict):
        return {key: strip_cache(item) for key, item in value.items() if key != "cache"}
    if isinstance(value, list):
        return [strip_cache(item) for item in value]
    return value


def head_of(body):
    """The text of the system head of a body posted to Retool, billing block included; None without one."""
    first = body["messages"][0]
    if first["role"] != "system":
        return None
    return "\n".join(block["text"] for block in first["content"])


def sdk_tool_reply(call_id, signature, name="Read"):
    """What Retool streams for an answer that thinks (signed) and then calls one tool, as the Agent SDK's tools are named."""
    return [{"event": "content_block_start", "type": "thinking"},
            {"event": "content_block_delta", "type": "signature", "partialSignature": signature},
            {"event": "content_block_end", "type": "thinking"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"path": "a"})},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": call_id}, *END]


def runner_turns(relay, headers, turns=3, prepare=None):
    """Drive the Desktop runner shape (prompt_bodies.claude_sdk_body) through `turns` requests of one conversation.

    Every answer but the last calls a tool; the next request carries the result and a role:system reminder after it,
    as the runner sends one on every turn. Returns (status, payload, response headers, body posted to Retool) per turn."""
    from prompt_bodies import claude_sdk_body
    body = claude_sdk_body(turns=0)
    body["stream"] = False
    if prepare:
        prepare(body)
    results = []
    for turn in range(turns):
        relay.scripts.append(sdk_tool_reply("t%d" % turn, "opaque-%d" % turn) if turn < turns - 1
                             else [text("done"), *END])
        status, payload, answer_headers = relay.post("anthropic", body, headers)
        results.append((status, payload, answer_headers, relay.posts[-1]))
        if turn < turns - 1:
            call = next(block["id"] for block in payload["content"] if block["type"] == "tool_use")
            body["messages"] += [{"role": "assistant", "content": payload["content"]},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call,
                                                               "content": "file contents"}]},
                                 {"role": "system", "content": "Synthetic per-turn reminder, number %02d." % turn}]
    return results
