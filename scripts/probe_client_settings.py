"""Offline CLI probes against a local fake model server; no provider credits used.

Run with claude, opencode or opencode2. Installed user settings are loaded, with
only the endpoint/key overridden for this process. A single child agent is
requested; the fake model never emits file, shell or network tool calls.
"""
import argparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client_context
import models
from apierr import ApiError

records = []
task_sent = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.reply({"object": "list", "data": []})

    def reply(self, data, status=200):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        global task_sent
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        records.append({"path": self.path, "model": body.get("model"), "effort": body.get("output_config", {}).get("effort") or body.get("reasoning_effort"),
                        "thinking": body.get("thinking"), "client": self.headers.get("x-proxi-client"),
                        "session_id": self.headers.get("x-proxi-session-id"), "title": self.headers.get("x-proxi-chat-title"),
                        "agent": self.headers.get("x-proxi-agent"),
                        "correlation_headers": {k:v for k,v in self.headers.items() if any(s in k.lower() for s in ['session','agent'])},
                        "tools": [x.get("name") or x.get("function",{}).get("name") for x in body.get("tools",[])]})
        records[-1]["resolved_identity"] = client_context.describe(body, self.headers)
        path = urlsplit(self.path).path
        if path.endswith("count_tokens"):
            self.reply({"input_tokens": 128})
            return
        if path.endswith("messages"):
            tool = next((x for x in body.get("tools", []) if x.get("name") in ("Agent", "Task")), None)
            call = tool and not task_sent and os.environ.get("PROXI_PROBE_CHILD") == "1"
            if call:
                task_sent = True
                block = {"type": "tool_use", "id": "toolu_smoke", "name": tool["name"],
                         "input": {"description": "Offline subagent effort probe", "prompt": "Return SUBAGENT_OK. Do not use tools.", "subagent_type": "proxi-smoke"}}
            else:
                block = {"type": "text", "text": "PROXI_SMOKE_OK"}
            stop = "tool_use" if call else "end_turn"
            response = {"id": "msg_smoke", "type": "message", "role": "assistant", "model": body.get("model"),
                        "content": [block], "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}
            if not body.get("stream"):
                self.reply(response)
                return
            events = [("message_start", {"type": "message_start", "message": {**response, "content": [], "stop_reason": None}}),
                      ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {**block, **({"input": {}} if call else {"text": ""})}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if call else {"type": "text_delta", "text": block["text"]}}),
                      ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                      ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 10}}),
                      ("message_stop", {"type": "message_stop"})]
            raw = "".join("event: %s\ndata: %s\n\n" % (name, json.dumps(data)) for name, data in events).encode()
        else:
            tool = next((x for x in body.get("tools", []) if x.get("function", {}).get("name") in ("task", "subagent")), None)
            call = tool and not task_sent
            if call:
                task_sent = True
                task_input = {"description": "Offline child effort probe", "prompt": "Return SUBAGENT_OK without tools."}
                task_input["agent" if tool["function"]["name"] == "subagent" else "subagent_type"] = "explore"
                task_call = {"index": 0, "id": "call_smoke", "type": "function", "function": {"name": tool["function"]["name"], "arguments": json.dumps({
                    **task_input})}}
            response = {"id": "chatcmpl_smoke", "object": "chat.completion", "created": int(time.time()), "model": body.get("model"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [task_call]} if call else {"role": "assistant", "content": "PROXI_SMOKE_OK"}, "finish_reason": "tool_calls" if call else "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            if not body.get("stream"):
                self.reply(response)
                return
            chunk = {**response, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [task_call]} if call else {"role": "assistant", "content": "PROXI_SMOKE_OK"}, "finish_reason": None}]}
            final = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}]}
            raw = ("data: %s\n\ndata: %s\n\ndata: [DONE]\n\n" % (json.dumps(chunk), json.dumps(final))).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def effort_controls(value, depth=0):
    if not isinstance(value, dict) or depth > 4:
        return []
    found = [value[key] for key in ('reasoningEffort', 'reasoning_effort', 'effort') if value.get(key) is not None]
    for key in ('settings', 'options', 'body', 'extra_body', 'reasoning', 'output_config'):
        found.extend(effort_controls(value.get(key), depth + 1))
    provider_options = value.get('providerOptions')
    if isinstance(provider_options, dict):
        found.extend(effort_controls(provider_options.get('openai'), depth + 1))
    thinking = value.get('thinking')
    if isinstance(thinking, dict):
        if thinking.get('type') == 'disabled':
            found.append('none')
        if thinking.get('effort') is not None:
            found.append(thinking['effort'])
        if thinking.get('budget_tokens') is not None:
            try:
                found.append(models.effort_from_budget(thinking['budget_tokens']))
            except ApiError:
                found.append(False)
    return found


def catalog_policy(config, catalog_output, mode, *, allowed_entries=None, require_complete=False):
    """Check both schemas and the listed catalog; migrated disabled flags lie."""
    if mode == "opencode2":
        configured = {}
        for document in config:
            configured.update((document.get("info") or {}).get("providers", {}).get("retool-relay", {}).get("models", {}))
    else:
        configured = config.get("provider", {}).get("retool-relay", {}).get("models", {})
    if allowed_entries is None:
        managed = {key: '__managed__' for provider, key in models.PROVIDER_RESOURCE_KEY.items() if provider != 'google'}
        allowed_entries = [entry for entry in models.catalog() if models.catalog_routable(entry, managed)]
    allowed = {entry['id']: entry for entry in allowed_entries}

    variants = []
    for name, model in configured.items():
        if not isinstance(model, dict):
            variants.append(name)
            continue
        choices = model.get('variants') or {}
        if isinstance(choices, dict):
            choices = list(choices.items())
        elif isinstance(choices, list) and all(isinstance(choice, dict) for choice in choices):
            choices = [(choice.get('id'), choice.get('settings', {})) for choice in choices]
        else:
            variants.append(name)
            continue
        levels = allowed.get(name, {}).get('reasoning_effort_levels', [])
        if not allowed.get(name, {}).get('reasoning', True):
            levels = ['none']
        choices.insert(0, (None, model.get('settings', model.get('options', {}))))
        for label, options in choices:
            if not isinstance(options, dict):
                variants.append(name)
                break
            efforts = effort_controls(options)
            if not efforts and label in models.EFFORT_ORDER:
                efforts.append(label)
            if (any(not isinstance(value, str) or value not in levels for value in efforts)
                    or len({value for value in efforts if isinstance(value, str)}) > 1):
                variants.append(name)
                break
    listed = {line.strip().removeprefix("retool-relay/") for line in catalog_output.splitlines()
              if line.strip().startswith("retool-relay/")}
    policy = {"configured_models": len(configured), "catalog_models": len(listed),
              "forbidden_models": sorted(set(configured) - set(allowed)),
              "forbidden_variants": sorted(set(variants)),
              "catalog_forbidden_models": sorted(listed - set(allowed)),
              "catalog_mismatch": sorted(listed.symmetric_difference(configured)),
              "missing_models": sorted(set(allowed) - set(configured)) if require_complete else []}
    policy["passed"] = bool(configured and listed) and not any(policy[key] for key in (
        "forbidden_models", "forbidden_variants", "catalog_forbidden_models", "catalog_mismatch", "missing_models"))
    return policy


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["claude", "opencode", "opencode2"])
    parser.add_argument("--executable", help="Override the installed client executable path")
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--require-complete-catalog', action='store_true')
    options = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.handle_error = lambda *args: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%s" % server.server_port
    mode = options.mode
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    home = Path.home()
    if mode == "claude":
        env["PROXI_PROBE_CHILD"] = "1"
        os.environ["PROXI_PROBE_CHILD"] = "1"
        settings = {"env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": "offline-test-key", "ANTHROPIC_AUTH_TOKEN": ""}}
        args = [options.executable or str(home / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"),
                "--print", "--output-format", "json", "--no-session-persistence", "--setting-sources", "user",
                "--settings", json.dumps(settings), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--tools", "Agent", "--allowedTools", "Agent", "--agents", json.dumps({"proxi-smoke": {
                    "description": "Offline child test", "prompt": "Return SUBAGENT_OK, no tools.", "model": "inherit", "effort": "xhigh", "tools": []}}),
                "Run the proxi-smoke agent once, then return PROXI_SMOKE_OK. Do not use any other tools."]
    else:
        config = {"provider": {"retool-relay": {"options": {"baseURL": url + "/v1", "apiKey": "offline-test-key"}}}}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        exe = str(home / "AppData/Roaming/npm/node_modules/opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe")
        if not Path(exe).exists():
            exe = str(home / "AppData/Roaming/npm/node_modules/opencode-ai/bin/opencode")
        if mode == "opencode2":
            exe = str(home / "AppData/Roaming/ai.opencode.desktop/cli/2.2.5-x64/opencode-cli.exe")
        exe = options.executable or exe
        args = [exe, "run", "--format", "json", "--title", "Proxi offline defaults probe", "Return PROXI_SMOKE_OK without tools."]
        if mode == "opencode2":
            args.insert(2, "--standalone")
    passed = False
    policy = None
    try:
        with tempfile.TemporaryDirectory(prefix="proxi-client-probe-") as work:
            if mode != "claude":
                suffix = ["--standalone"] if mode == "opencode2" else []
                resolved = subprocess.run([exe, "debug", "config", *suffix], env=env, cwd=work,
                                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                resolved.check_returncode()
                config = json.loads(resolved.stdout)
                catalog = subprocess.run([exe, "models", *suffix], env=env, cwd=work, capture_output=True,
                                         text=True, encoding="utf-8", errors="replace", timeout=30)
                catalog.check_returncode()
                expected = None
                if Path(options.relay_config).is_file():
                    from scripts.audit_models import load_accounts
                    account_configs = load_accounts(options.relay_config)
                    expected = [entry for entry in models.catalog()
                                if models.catalog_routable(entry, {'accounts': account_configs})]
                policy = catalog_policy(config, catalog.stdout, mode, allowed_entries=expected,
                                        require_complete=options.require_complete_catalog)
            result = subprocess.run(args, env=env, cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=85)
        try:
            stats = json.loads(result.stdout).get("subagent_stats")
        except ValueError:
            stats = None
        turns = [row for row in records if not row["path"].split("?")[0].endswith("count_tokens")]
        passed = result.returncode == 0 and "PROXI_SMOKE_OK" in result.stdout and len(turns) >= 3 and all(row["effort"] == "high" for row in turns)
        if mode == "claude":
            passed = passed and bool(stats and stats.get("spawned") == 1 and stats.get("completed") == 1)
        else:
            passed = passed and policy["passed"]
        print(json.dumps({"mode": mode, "passed": passed, "exit_code": result.returncode, "requests": records,
                          "catalog_policy": policy,
                          "subagent_stats": stats,
                          "success_marker": "PROXI_SMOKE_OK" in result.stdout,
                          "stderr_tail": result.stderr[-1000:] if result.returncode else "",
                          "stdout_tail": result.stdout[-1000:] if result.returncode else ""}, ensure_ascii=False, indent=2))
    except subprocess.TimeoutExpired:
        print(json.dumps({"mode": mode, "timeout": True, "requests": records}, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
