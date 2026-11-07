"""Offline CLI probes against a local fake model server; no provider credits used.

Run with claude, opencode or opencode2. Installed user settings are loaded, with
only the endpoint/key overridden for this process. A single child agent is
requested; the fake model never emits file, shell or network tool calls.
OpenCode's override moves only the retool-relay provider, so its run is pinned
to that provider too: --model, and the default, small and agent models in the
override; the user's own retool-relay choices are kept, any other is replaced.

The override is verified rather than assumed: credential-looking variables are
removed from the child environment, OpenCode must report the fake endpoint and
only retool-relay models in its resolved configuration before any model request
is sent, and the probe fails unless the fake server actually received requests.
"""
import argparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client_context
import models
from apierr import ApiError
from config import DEFAULTS as RELAY_DEFAULTS
from scripts import sync_virtual_models

records = []
task_sent = False
PROBE_KEY = "offline-test-key"
# Variables a client could use to reach the real relay if an override were ignored.
CREDENTIAL_ENV = re.compile(r"API[_-]?KEY|AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|SECRET|PASSWORD|RELAY|RETOOL", re.I)
PROVIDER = "retool-relay"
# The OpenCode agents a probe run can reach: the parent (build), the child the fake spawns (explore;
# general is the other built-in subagent) and the hidden ones OpenCode starts by itself.
RUN_AGENTS = ("build", "explore", "general", "title", "summary", "compaction")
SMALL_AGENTS = ("title", "summary")
# The parent when none of the user's models is on the relay: the default of the catalog sync.
DEFAULT_MODEL = "claude-opus-5-5"


class ProbeError(RuntimeError):
    """The probe cannot guarantee that requests stay on the local fake server."""


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


def config_documents(config, mode):
    """The configuration objects of a resolved config: OpenCode 2 lists one per source, unmerged."""
    if mode == "opencode2" and isinstance(config, list):
        return [document["info"] for document in config
                if isinstance(document, dict) and isinstance(document.get("info"), dict)]
    if mode != "opencode2" and isinstance(config, dict):
        return [config]
    raise ProbeError("the client's resolved configuration has an unexpected shape; no model request was sent "
                     "because the models it would use cannot be checked")


def relay_models(config, mode):
    """The models the client's resolved configuration defines for the relay provider."""
    section = "providers" if mode == "opencode2" else "provider"
    configured = {}
    for info in config_documents(config, mode):
        configured.update(((info.get(section) or {}).get(PROVIDER) or {}).get("models") or {})
    return configured


def catalog_policy(config, catalog_output, mode, *, allowed_entries=None, require_complete=False):
    """Check both schemas and the listed catalog; migrated disabled flags lie."""
    configured = relay_models(config, mode)
    if allowed_entries is None:
        managed = {key: '__managed__' for provider, key in models.PROVIDER_RESOURCE_KEY.items() if provider != 'google'}
        allowed_entries = [entry for entry in models.catalog() if models.catalog_routable(entry, managed)]
    allowed = {entry['id']: entry for entry in allowed_entries}

    variants, fields = [], []

    def reject(name, where, problem):
        variants.append(name)
        fields.append({"model": name, "field": where, "problem": problem})

    for name, model in configured.items():
        if not isinstance(model, dict):
            reject(name, "entry", "is not an object")
            continue
        choices = model.get('variants') or {}
        if isinstance(choices, dict):
            choices = list(choices.items())
        elif isinstance(choices, list) and all(isinstance(choice, dict) for choice in choices):
            choices = [(choice.get('id'), choice.get('settings', {})) for choice in choices]
        else:
            reject(name, "variants", "is neither an object nor a list of objects")
            continue
        levels = allowed.get(name, {}).get('reasoning_effort_levels', [])
        if not allowed.get(name, {}).get('reasoning', True):
            levels = ['none']
        default = 'settings' if 'settings' in model else 'options'
        choices.insert(0, (None, model.get('settings', model.get('options', {}))))
        for position, (label, options) in enumerate(choices):
            where = default if position == 0 else 'variants.%s' % label
            if not isinstance(options, dict):
                reject(name, where, "is not an object")
                break
            efforts = effort_controls(options)
            if not efforts and label in models.EFFORT_ORDER:
                efforts.append(label)
            if any(not isinstance(value, str) or value not in levels for value in efforts):
                reject(name, where, "has a reasoning effort the route does not accept")
                break
            if len({value for value in efforts if isinstance(value, str)}) > 1:
                reject(name, where, "sets conflicting reasoning efforts")
                break
    listed = {line.strip().removeprefix("retool-relay/") for line in catalog_output.splitlines()
              if line.strip().startswith("retool-relay/")}
    policy = {"configured_models": len(configured), "catalog_models": len(listed),
              "forbidden_models": sorted(set(configured) - set(allowed)),
              "forbidden_variants": sorted(set(variants)),
              "forbidden_variant_fields": fields,
              "catalog_forbidden_models": sorted(listed - set(allowed)),
              "catalog_mismatch": sorted(listed.symmetric_difference(configured)),
              "missing_models": sorted(set(allowed) - set(configured)) if require_complete else []}
    policy["passed"] = bool(configured and listed) and not any(policy[key] for key in (
        "forbidden_models", "forbidden_variants", "catalog_forbidden_models", "catalog_mismatch", "missing_models"))
    return policy


def run_models(config, mode):
    """(field, reference) for each model setting of `config` that an OpenCode probe run can send a request with.

    OpenCode 2 lists its sources apart, so a field can come once per source there.
    """
    found = []
    for info in config_documents(config, mode):
        # Any top-level text under a key with "model" in its name, so a renamed small-model key still counts.
        found += [(key, value) for key, value in info.items() if "model" in key.lower() and isinstance(value, str)]
        for section in ("agent", "agents"):  # OpenCode 1 and OpenCode 2
            agents = info.get(section)
            for name in RUN_AGENTS:
                agent = agents.get(name) if isinstance(agents, dict) else None
                if isinstance(agent, dict) and agent.get("model") is not None:
                    found.append(("%s.%s.model" % (section, name), agent["model"]))
    return found


def is_relay(reference):
    return isinstance(reference, str) and reference.startswith(PROVIDER + "/")


def check_pinned(config, mode):
    """Refuse a run whose resolved configuration names a model of another provider.

    The probe overrides only the retool-relay endpoint, so such a model would get a real, paid request
    with the user's own credentials (auth.json, a key in the file), outside the stripped environment.
    OpenCode 2 lists its sources unmerged, so there a model of another provider in any of them refuses.
    """
    stray = sorted({"%s=%s" % (field, reference) for field, reference in run_models(config, mode)
                    if not is_relay(reference)})
    if stray:
        raise ProbeError("the client's resolved configuration uses models of other providers (%s); no model request "
                         "was sent because it could reach a real provider" % ", ".join(stray))


def model_pins(config, mode):
    """Override settings that keep every model of a probe run on the relay provider, and the run's --model.

    The user's own retool-relay choices are kept, so the probe still tests them; every other model goes to
    the user's relay default (else one of the relay models). The parent is also passed as --model: with no
    `model` set, OpenCode runs the model its TUI used last, which no resolved configuration shows.
    """
    own = {}
    for field, reference in run_models(config, mode):
        if is_relay(reference):
            own[field.replace("agents.", "agent.", 1)] = reference
    default = own.get("model")
    if default is None:
        configured = relay_models(config, mode)
        name = DEFAULT_MODEL if DEFAULT_MODEL in configured else next(iter(configured), None)
        if name is None:
            raise ProbeError("the client's resolved configuration defines no %s model to pin the run to; "
                             "no model request was sent" % PROVIDER)
        default = "%s/%s" % (PROVIDER, name)
    parent = own.get("agent.build.model") or default
    small = own.get("small_model") or default
    agents = {name: {"model": own.get("agent.%s.model" % name) or (small if name in SMALL_AGENTS else parent)}
              for name in RUN_AGENTS}
    return {"model": default, "small_model": small, "agent": agents}, parent


def routable_entries(relay_config):
    """Catalog the running relay would list for this configuration file.

    The call mirrors proxy.py: the whole root config plus one backend per enabled
    account, so root options such as show_unrouted_models apply.
    """
    from scripts.audit_models import load_accounts
    try:
        saved = json.loads(Path(relay_config).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError("relay configuration not found: %s (use --relay-config)" % relay_config) from None
    except OSError as error:
        raise ValueError("cannot read relay configuration %s: %s" % (relay_config, error.strerror or error)) from None
    except ValueError as error:
        raise ValueError("relay configuration %s is not valid JSON (%s)" % (relay_config, error)) from None
    if not isinstance(saved, dict):
        raise ValueError("relay configuration %s must contain a JSON object" % relay_config)
    cfg = {**RELAY_DEFAULTS, **saved}
    backends = [SimpleNamespace(cfg=account) for account in load_accounts(relay_config)]
    return [entry for entry in models.catalog() if models.catalog_routable(entry, cfg, backends)]


def desktop_cli(app_data):
    """Newest CLI bundled with OpenCode Desktop; the folder name carries its version."""
    def version(folder):
        match = re.match(r"\d+(?:\.\d+)*", folder.name)
        return tuple(int(part) for part in match.group().split(".")) if match else ()
    folders = [folder for folder in (Path(app_data) / "ai.opencode.desktop" / "cli").glob("*")
               if (folder / "opencode-cli.exe").is_file()]
    return str(max(folders, key=lambda folder: (version(folder), folder.name)) / "opencode-cli.exe") if folders else None


def default_executable(mode, environ=None, home=None):
    environ = os.environ if environ is None else environ
    roaming = Path(environ.get("APPDATA") or Path(home or Path.home()) / "AppData" / "Roaming")
    npm = roaming / "npm" / "node_modules"
    if mode == "claude":
        return str(npm / "@anthropic-ai/claude-code/bin/claude.exe")
    if mode == "opencode2":
        return desktop_cli(roaming)
    native = npm / "opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe"
    return str(native if native.exists() else npm / "opencode-ai/bin/opencode")


def run_client(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)


def resolved_config(exe, mode, env, cwd, url):
    """OpenCode's resolved configuration under `env`, refused unless it uses the probe endpoint."""
    suffix = ["--standalone"] if mode == "opencode2" else []
    resolved = run_client([exe, "debug", "config", *suffix], env=env, cwd=cwd, timeout=30)
    resolved.check_returncode()
    config = json.loads(resolved.stdout)
    if url + "/v1" not in json.dumps(config):
        raise ProbeError("the client's resolved configuration does not use the probe endpoint; "
                         "no model request was sent because it could reach the real relay")
    return config


def main(argv=None):
    global task_sent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["claude", "opencode", "opencode2"])
    parser.add_argument("--executable", help="Override the installed client executable path "
                        "(OpenCode Desktop's newest bundled CLI is detected automatically)")
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--require-complete-catalog', action='store_true')
    sync_virtual_models.add_arguments(parser)
    options = parser.parse_args(argv)
    mode = options.mode
    exe = options.executable or default_executable(mode)
    if not exe or not (Path(exe).is_file() or shutil.which(exe)):
        parser.error("cannot find the %s executable%s; pass --executable PATH" % (mode, " at " + exe if exe else ""))
    expected = None
    if mode != "claude" and Path(options.relay_config).is_file():
        try:
            expected = routable_entries(options.relay_config)
            # The virtual models <model>@<profile> the relay lists are part of the catalog a sync writes.
            expected = sync_virtual_models.with_virtual_models(expected, options)[0]
        except ValueError as error:
            parser.error(str(error))
    records.clear()
    task_sent = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.handle_error = lambda *args: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%s" % server.server_port
    env = {key: value for key, value in os.environ.items() if not CREDENTIAL_ENV.search(key)}
    env["PYTHONUTF8"] = "1"
    if mode == "claude":
        env["PROXI_PROBE_CHILD"] = "1"
        os.environ["PROXI_PROBE_CHILD"] = "1"
        settings = {"env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": PROBE_KEY, "ANTHROPIC_AUTH_TOKEN": ""}}
        # A second channel, so an ignored --settings env block still cannot reach the real relay.
        env.update(settings["env"])
        args = [exe,
                "--print", "--output-format", "json", "--no-session-persistence", "--setting-sources", "user",
                "--settings", json.dumps(settings), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--tools", "Agent", "--allowedTools", "Agent", "--agents", json.dumps({"proxi-smoke": {
                    "description": "Offline child test", "prompt": "Return SUBAGENT_OK, no tools.", "model": "inherit", "effort": "xhigh", "tools": []}}),
                "Run the proxi-smoke agent once, then return PROXI_SMOKE_OK. Do not use any other tools."]
    else:
        override = {"provider": {PROVIDER: {"options": {"baseURL": url + "/v1", "apiKey": PROBE_KEY}}}}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(override)
        args = [exe, "run", "--format", "json", "--title", "Proxi offline defaults probe"]
        if mode == "opencode2":
            args.insert(2, "--standalone")
    passed = False
    policy = pinned = None
    try:
        with tempfile.TemporaryDirectory(prefix="proxi-client-probe-") as work:
            if mode != "claude":
                # The override moves only the relay provider, so the run's models are pinned to it as well,
                # and the configuration the client resolves with those pins is checked before any request.
                pins, parent = model_pins(resolved_config(exe, mode, env, work, url), mode)
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps({**override, **pins})
                config = resolved_config(exe, mode, env, work, url)
                check_pinned(config, mode)
                pinned = {"--model": parent, **pins}
                suffix = ["--standalone"] if mode == "opencode2" else []
                catalog = run_client([exe, "models", *suffix], env=env, cwd=work, timeout=30)
                catalog.check_returncode()
                policy = catalog_policy(config, catalog.stdout, mode, allowed_entries=expected,
                                        require_complete=options.require_complete_catalog)
                args += ["--model", parent, "Return PROXI_SMOKE_OK without tools."]
            result = run_client(args, env=env, cwd=work, timeout=85)
        try:
            stats = json.loads(result.stdout).get("subagent_stats")
        except ValueError:
            stats = None
        turns = [row for row in records if not row["path"].split("?")[0].endswith("count_tokens")]
        error = None
        if not turns:
            error = ("the probe server received no model request, so the client ignored the endpoint override "
                     "and may have contacted the real relay")
        passed = (error is None and result.returncode == 0 and "PROXI_SMOKE_OK" in result.stdout
                  and len(turns) >= 3 and all(row["effort"] == "high" for row in turns))
        if mode == "claude":
            passed = passed and bool(stats and stats.get("spawned") == 1 and stats.get("completed") == 1)
        else:
            passed = passed and policy["passed"]
        report = {"mode": mode, "passed": passed, "executable": exe, "exit_code": result.returncode, "requests": records,
                  "catalog_policy": policy,
                  "pinned_models": pinned,
                  "subagent_stats": stats,
                  "success_marker": "PROXI_SMOKE_OK" in result.stdout,
                  "stderr_tail": result.stderr[-1000:] if result.returncode else "",
                  "stdout_tail": result.stdout[-1000:] if result.returncode else ""}
        if error:
            report["error"] = error
            print("error: " + error, file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except ProbeError as error:
        print("error: %s" % error, file=sys.stderr)
        print(json.dumps({"mode": mode, "passed": False, "executable": exe, "error": str(error), "requests": records},
                         ensure_ascii=False, indent=2))
    except subprocess.TimeoutExpired:
        print(json.dumps({"mode": mode, "timeout": True, "requests": records}, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
