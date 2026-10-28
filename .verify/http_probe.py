
"""Adversarial HTTP probe: exercises the real handler with a scripted runner."""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import urllib.error
import urllib.request

import apierr
import proxy
import config as config_module

KEY = "probe-key-abcdefghijklmnop"


class FakeCredits:
    def __init__(self):
        self.used = []
        self.balance = {"available": 10.0}

    def get_balance(self, force_refresh=False):
        return dict(self.balance)

    def record_turn(self, t, c):
        self.used.append((t, c))

    def usage(self):
        return {"session_used": 0, "turns": 0, "recent": []}


class FakeRunner:
    def __init__(self):
        self.script = None
        self.seen = []
        self.cancelled = threading.Event()

    async def run(self, parsed, out, holder):
        self.seen.append(parsed)
        script = self.script or [{"type": "text", "delta": "ok"}, {"type": "end", "reason": "stop"}]
        try:
            for ev in script:
                if callable(ev):
                    ev = ev(holder)
                if isinstance(ev, BaseException):
                    out.put({"type": "error", "error": ev})
                    return
                out.put(ev)
        finally:
            out.put(None)


def make(max_workers=4, **over):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({
        "base": "https://example.invalid", "page_uuid": "p", "thread_id": "t",
        "ua": "ua", "cookie": "accessToken=abcdef123456789; xsrfToken=q", "xsrf": "q",
        "relay_api_key": KEY, "workspace_root": ".", "cookie_values": ["abcdef123456789"],
        "host": "127.0.0.1", "port": 0, "max_workers": max_workers,
    })
    cfg.update(over)
    rt = proxy.Runtime(cfg)
    rt.credits = FakeCredits()
    runner = FakeRunner()
    rt.runner = runner
    handler = proxy.make_handler(rt)
    srv = proxy.BoundedServer(("127.0.0.1", 0), handler, threading.BoundedSemaphore(max_workers))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, runner, rt


def call(port, path, payload=None, *, key=KEY, method=None, raw=None, headers=None, timeout=15):
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                 method=method or ("POST" if body else "GET"))
    if body is not None:
        req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", "Bearer " + key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


CHAT = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}
ANTH = {"model": "claude-opus-5-5", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}

srv, runner, rt = make(max_body_bytes=2000, rate_limit_burst=1000.0, rate_limit_rps=1000.0)
P = srv.server_address[1]
results = []


def show(label, out):
    status, body, hdrs = out
    xrid = hdrs.get("x-request-id") or hdrs.get("X-Request-Id")
    results.append((label, status, (body or "")[:160].replace("\n", " "), bool(xrid)))


print("== A. auth ==")
show("no-key", call(P, "/v1/chat/completions", CHAT, key=None))
show("wrong-key", call(P, "/v1/chat/completions", CHAT, key="bad"))
show("empty-bearer", call(P, "/v1/chat/completions", CHAT, key=None, headers={"authorization": "Bearer "}))
show("query-key", call(P, "/v1/models?key=" + KEY, None, key=None))
show("x-api-key", call(P, "/v1/models", None, key=None, headers={"x-api-key": KEY}))
show("raw-auth", call(P, "/v1/models", None, key=None, headers={"authorization": KEY}))
show("basic-scheme", call(P, "/v1/models", None, key=None, headers={"authorization": "Basic " + KEY}))
show("health-no-key", call(P, "/health", None, key=None))
show("usage-no-key", call(P, "/v1/usage", None, key=None))

print("== B. body / type errors ==")
show("bad-json", call(P, "/v1/chat/completions", raw=b"{nope"))
show("array-body", call(P, "/v1/chat/completions", raw=b"[]"))
show("empty-body", call(P, "/v1/chat/completions", raw=b""))
show("ctrl-char-json", call(P, "/v1/chat/completions", raw=b'{"model":"gpt-5","messages":[{"role":"user","content":"a\x01b"}]}'))
show("nan-temperature", call(P, "/v1/chat/completions", raw=b'{"model":"gpt-5","temperature":NaN,"messages":[{"role":"user","content":"x"}]}'))
show("inf-max-tokens", call(P, "/v1/chat/completions", raw=b'{"model":"gpt-5","max_tokens":1e400,"messages":[{"role":"user","content":"x"}]}'))
show("bool-max-tokens", call(P, "/v1/chat/completions", {"model": "gpt-5", "max_tokens": True, "messages": [{"role": "user", "content": "x"}]}))
show("float-max-tokens", call(P, "/v1/chat/completions", {"model": "gpt-5", "max_tokens": 10.5, "messages": [{"role": "user", "content": "x"}]}))
show("stream-string", call(P, "/v1/chat/completions", {"model": "gpt-5", "stream": "false", "messages": [{"role": "user", "content": "x"}]}))
show("no-role", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"content": "x"}]}))
show("role-int", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": 7, "content": "x"}]}))
show("null-content", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": None}]}))
show("content-int", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": 5}]}))
show("text-block-no-text", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": [{"type": "text"}]}]}))
show("unknown-block", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": [{"type": "zzz"}]}]}))
show("tool-result-nondict", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": [7]}]}))
show("messages-string", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": "hi"}))
show("messages-missing", call(P, "/v1/chat/completions", {"model": "gpt-5"}))
show("messages-empty", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": []}))
show("model-int", call(P, "/v1/chat/completions", {"model": 5, "messages": [{"role": "user", "content": "x"}]}))
show("model-unknown", call(P, "/v1/chat/completions", {"model": "nope", "messages": [{"role": "user", "content": "x"}]}))
show("model-null-default", call(P, "/v1/chat/completions", {"model": None, "messages": [{"role": "user", "content": "x"}]}))
show("responses-input-missing", call(P, "/v1/responses", {"model": "gpt-5"}))
show("responses-input-int", call(P, "/v1/responses", {"model": "gpt-5", "input": 5}))
show("responses-input-empty-list", call(P, "/v1/responses", {"model": "gpt-5", "input": []}))
show("anth-no-max-tokens", call(P, "/v1/messages", {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "x"}]}))
show("anth-system-string", call(P, "/v1/messages", dict(ANTH, system="sys")))
show("anth-stop-sequences-string", call(P, "/v1/messages", dict(ANTH, stop_sequences="x")))

print("== C. effort / thinking ==")
show("effort-openai-minimal", call(P, "/v1/chat/completions", {"model": "gpt-5", "reasoning_effort": "minimal", "messages": [{"role": "user", "content": "x"}]}))
show("effort-openai-none", call(P, "/v1/chat/completions", {"model": "gpt-5", "reasoning_effort": "none", "messages": [{"role": "user", "content": "x"}]}))
show("effort-anthropic-minimal", call(P, "/v1/chat/completions", {"model": "claude-opus-5-5", "reasoning_effort": "minimal", "messages": [{"role": "user", "content": "x"}]}))
show("effort-glm-any", call(P, "/v1/chat/completions", {"model": "zai-org/GLM-5.3", "reasoning_effort": "high", "messages": [{"role": "user", "content": "x"}]}))
show("effort-bad", call(P, "/v1/chat/completions", {"model": "claude-opus-5-5", "reasoning_effort": "turbo", "messages": [{"role": "user", "content": "x"}]}))
show("effort-upper", call(P, "/v1/chat/completions", {"model": "claude-opus-5-5", "reasoning_effort": "HIGH", "messages": [{"role": "user", "content": "x"}]}))
show("effort-int", call(P, "/v1/chat/completions", {"model": "claude-opus-5-5", "reasoning_effort": 3, "messages": [{"role": "user", "content": "x"}]}))
show("budget-1000", call(P, "/v1/messages", dict(ANTH, thinking={"budget_tokens": 1000})))
show("budget-str", call(P, "/v1/messages", dict(ANTH, thinking={"type": "enabled", "budget_tokens": "4000"})))
show("budget-float", call(P, "/v1/messages", dict(ANTH, thinking={"type": "enabled", "budget_tokens": 4000.7})))
show("budget-bool", call(P, "/v1/messages", dict(ANTH, thinking={"type": "enabled", "budget_tokens": True})))
show("budget-list", call(P, "/v1/messages", dict(ANTH, thinking={"type": "enabled", "budget_tokens": [4000]})))
show("budget-glm", call(P, "/v1/messages", {"model": "zai-org/GLM-5.3", "max_tokens": 8, "thinking": {"type": "enabled", "budget_tokens": 4000}, "messages": [{"role": "user", "content": "x"}]}))
show("thinking-string", call(P, "/v1/messages", dict(ANTH, thinking="hi")))
show("reasoning-not-object", call(P, "/v1/responses", {"model": "gpt-5", "input": "x", "reasoning": "hi"}))

print("== D. upstream error mapping ==")
for name, err in [("401", apierr.upstream_auth_failed("x")), ("429-credit", apierr.ApiError(429, "insufficient_credits", "no credits")),
                  ("502", apierr.upstream_error("boom")), ("504", apierr.upstream_timeout()), ("500", apierr.internal("boom"))]:
    runner.script = [err]
    show("err-chat-" + name, call(P, "/v1/chat/completions", CHAT))
    show("err-anth-" + name, call(P, "/v1/messages", ANTH))
runner.script = [{"type": "text", "delta": "partial"}, {"type": "end", "reason": "timeout"}]
show("timeout-chat", call(P, "/v1/chat/completions", CHAT))
show("timeout-anth", call(P, "/v1/messages", ANTH))
runner.script = [{"type": "end", "reason": "timeout"}]
show("timeout-empty-chat", call(P, "/v1/chat/completions", CHAT))
runner.script = [{"type": "text", "delta": "x"}, {"type": "end", "reason": "cancelled"}]
show("cancelled-reason", call(P, "/v1/chat/completions", CHAT))
runner.script = [{"type": "text", "delta": "x"}, {"type": "end", "reason": "max_exec"}]
show("maxexec-reason", call(P, "/v1/chat/completions", CHAT))

print("== E. limits ==")
show("oversize", call(P, "/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": "x" * 3000}]}))
show("wrong-ctype", call(P, "/v1/chat/completions", CHAT, headers={"content-type": "text/plain"}))
show("head-models", call(P, "/v1/models", None, method="HEAD"))
show("put-models", call(P, "/v1/models", None, method="PUT"))
show("trailing-slash", call(P, "/v1/chat/completions/", CHAT))
show("options-unknown", call(P, "/v1/nope", None, method="OPTIONS"))
show("unknown-route", call(P, "/v1/nope", CHAT))
show("neg-content-length", call(P, "/v1/models", None))

for label, status, body, xrid in results:
    print("%-28s %s xrid=%s %s" % (label, status, xrid, body))

srv.shutdown()
srv.server_close()
