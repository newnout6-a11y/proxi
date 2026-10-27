"""OpenAI-compatible facade over Retool AI Agent with Local PC Execution, Context Injection and Credits Tracking."""
import asyncio, json, os, queue, re, sys, threading, time, uuid, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websockets

from credits import CreditsManager
from context import build_system_instructions
from executor import extract_command, run_command, format_command_followup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CFG = json.load(open(CONFIG_PATH, encoding="utf-8"))

PORT = int(os.environ.get("PROXY_PORT", CFG.get("port", 8787)))
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", 180))
RECV_TIMEOUT = int(os.environ.get("RECV_TIMEOUT", 60))

SUPPORTED_MODELS = [
    {"id": "claude-opus-5-5", "provider": "anthropic"},
    {"id": "claude-sonnet-4-6", "provider": "anthropic"},
    {"id": "claude-haiku-4-5-20251001", "provider": "anthropic"},
    {"id": "gpt-5.2", "provider": "openai"},
    {"id": "gpt-5.1", "provider": "openai"},
    {"id": "gpt-5", "provider": "openai"},
    {"id": "gpt-4.1", "provider": "openai"},
]

def api_headers():
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": CFG["base"],
        "referer": CFG["base"] + "/",
        "user-agent": CFG["ua"],
        "cookie": CFG["cookie"],
        "x-xsrf-token": CFG["xsrf"],
        "x-retool-client-version": "4.84.0-8ec403c (Build 409410)",
    }

def api_post(path, payload):
    data = json.dumps(payload, ensure_ascii=True).encode("ascii")
    req = urllib.request.Request(CFG["base"] + path, data=data, headers=api_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")

CREDITS = CreditsManager(CFG["base"], api_headers)
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def resolve_model_provider(requested_model, requested_effort=None):
    model = (requested_model or CFG.get("model", "claude-opus-5-5")).strip()
    
    # Provider mapping
    if any(k in model.lower() for k in ("gpt", "openai", "o1", "o3")):
        provider = CFG.get("openai_provider_resource_name", "1b7f4c97-241f-4117-a710-18c7cbfa74b1")
    elif any(k in model.lower() for k in ("claude", "anthropic", "sonnet", "opus", "haiku")):
        provider = CFG.get("provider_resource_name", "ffd4d570-7c6b-414f-b95a-1a695676d0a8")
    else:
        provider = CFG.get("provider_resource_name", "ffd4d570-7c6b-414f-b95a-1a695676d0a8")

    effort = requested_effort or CFG.get("reasoning_effort", "high")
    return model, provider, effort


class Session:
    def __init__(self):
        self.ws = None
        self.lock = asyncio.Lock()

    async def ensure_ws(self):
        if self.ws is not None and not self.ws.closed:
            return
        st = await LOOP.run_in_executor(
            None,
            lambda: api_post("/api/ai/retool-next-agent/start", {
                "threadId": CFG["thread_id"],
                "pageUuid": CFG["page_uuid"],
                "environmentName": "production",
            }),
        )
        sep = "&" if "?" in st["url"] else "?"
        url = st["url"] + sep + "sandboxToken=" + st["sandboxToken"]
        self.ws = await websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None)
        
        # Drain backlog
        while True:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.4)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await self.ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break

    async def run_single_ws_turn(self, prompt, model_provider, out, show_text=True):
        await self.ensure_ws()
        await self.ws.send(json.dumps({
            "type": "user_prompt",
            "prompt": prompt,
            "modelProvider": model_provider,
            "attachments": [],
            "taggedResourceNames": [],
            "taggedResourceIds": [],
            "r2OnboardingTemplateId": None,
            "messageId": str(uuid.uuid4()),
            "expectedThreadId": CFG["thread_id"],
            "agentMode": 2,
            "testingEnabled": False,
            "requestOrigin": "Browser",
        }, ensure_ascii=True))

        deadline = time.time() + TURN_TIMEOUT
        current_msg_id = None
        last_content = ""
        full_turn_text = ""
        turn_credits = 0.0

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                if show_text:
                    out.put(f"\n[turn timeout after {TURN_TIMEOUT}s]")
                break

            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=min(remaining, RECV_TIMEOUT))
            except asyncio.TimeoutError:
                if time.time() >= deadline:
                    if show_text:
                        out.put(f"\n[turn timeout after {TURN_TIMEOUT}s]")
                    break
                continue

            ev = json.loads(raw)
            kind = ev.get("type")

            if kind == "ping":
                await self.ws.send('{"type":"pong"}')
            elif kind == "text":
                content = ev.get("content") or ""
                # Content in Retool WebSocket text frames is cumulative for the current turn
                if len(content) >= len(last_content):
                    delta = content[len(last_content):]
                else:
                    delta = content

                last_content = content
                full_turn_text = content

                if show_text and delta:
                    out.put(delta)
            elif kind == "clear_streaming":
                # Retool UI event; content remains cumulative in subsequent text frames
                pass
            elif kind == "run_credits_persisted":
                turn_credits = ev.get("creditsUsed", 0.0)
                CREDITS.record_turn_cost(turn_credits)
            elif kind == "hitl":
                hitl_obj = ev.get("hitl") or {}
                hitl_type = hitl_obj.get("hitlType") or ev.get("hitlType") or "askUser"
                questions = hitl_obj.get("hitlProps", {}).get("questions", [])
                q_texts = []
                if isinstance(questions, list):
                    for q in questions:
                        if isinstance(q, dict) and q.get("question"):
                            q_texts.append(q["question"])
                        elif isinstance(q, str):
                            q_texts.append(q)
                hitl_msg = "\n".join(q_texts) if q_texts else (hitl_obj.get("message") or f"[hitl: {hitl_type}]")
                if show_text:
                    out.put(f"\n{hitl_msg}")
                full_turn_text += f"\n{hitl_msg}"
                break
            elif kind == "agent_loop_done":
                # Wait briefly (up to 0.8s) for trailing run_credits_persisted
                t_end = time.time() + 0.8
                while time.time() < t_end and turn_credits == 0.0:
                    try:
                        raw_trail = await asyncio.wait_for(self.ws.recv(), timeout=0.3)
                        ev_trail = json.loads(raw_trail)
                        if ev_trail.get("type") == "run_credits_persisted":
                            turn_credits = ev_trail.get("creditsUsed", 0.0)
                            CREDITS.record_turn_cost(turn_credits)
                            break
                        elif ev_trail.get("type") == "ping":
                            await self.ws.send('{"type":"pong"}')
                    except asyncio.TimeoutError:
                        break
                break
            elif kind == "error":
                err_msg = f"\n[error] {ev}"
                if show_text:
                    out.put(err_msg)
                full_turn_text += err_msg
                break

        return full_turn_text, turn_credits

    async def run_turn(self, messages, requested_model, requested_effort, out, usage_box):
        async with self.lock:
            total_credits = 0.0
            try:
                # 1. Resolve model & provider
                model, provider, effort = resolve_model_provider(requested_model, requested_effort)
                model_provider = {
                    "model": model,
                    "providerResourceName": provider,
                    "reasoningEffort": effort
                }
                print(f"[Turn] Model: {model} | Provider: {provider} | Effort: {effort}", flush=True)

                # 2. Build instructions & context
                ws_root = CFG.get("workspace_root") or os.getcwd()
                enable_ctx = bool(CFG.get("project_context", True))
                enable_exec = bool(CFG.get("local_exec", True))
                max_exec_turns = int(CFG.get("max_exec_turns", 5))
                show_exec_output = bool(CFG.get("show_exec_output", True))

                sys_instruction = build_system_instructions(ws_root, enable_ctx, enable_exec)
                
                # Flatten user / chat messages
                user_content = flatten_messages(messages)
                current_prompt = f"{sys_instruction}\n\n{user_content}" if sys_instruction else user_content

                exec_step = 0
                while True:
                    turn_text, turn_cred = await self.run_single_ws_turn(
                        current_prompt, model_provider, out, show_text=True
                    )
                    total_credits += turn_cred

                    # 3. Check for local command execution
                    if enable_exec and exec_step < max_exec_turns:
                        cmd = extract_command(turn_text)
                        if cmd:
                            exec_step += 1
                            print(f"[LOCAL EXEC #{exec_step}]: `{cmd}` in `{ws_root}`", flush=True)
                            if show_exec_output:
                                out.put(f"\n\n*[Executing: `{cmd}`]*\n")

                            ret, output = run_command(cmd, cwd=ws_root, timeout=40)
                            print(f"[LOCAL EXEC #{exec_step} Result]: Exit {ret} ({len(output)} chars)", flush=True)

                            if show_exec_output:
                                out.put(f"```text\n{output}\n```\n\n")

                            current_prompt = format_command_followup(cmd, ret, output)
                            continue

                    break

            except Exception as exc:
                print(f"[proxy error] {exc}", flush=True)
                out.put(f"\n[proxy error] {exc}")
                if self.ws is not None:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
            finally:
                usage_box["total_credits"] = total_credits
                out.put(None)


SESSION = Session()


def flatten_messages(messages):
    parts = []
    for m in messages:
        role = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
        if role == "system":
            parts.append(f"[System Instruction]: {c}")
        elif role == "user":
            parts.append(f"User: {c}")
        elif role == "assistant":
            parts.append(f"Assistant: {c}")
        else:
            parts.append(f"{role}: {c}")
    return "\n\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            data = [{"id": m["id"], "object": "model", "owned_by": m["provider"]} for m in SUPPORTED_MODELS]
            return self._json(200, {"object": "list", "data": data})

        if self.path.startswith("/v1/balance") or self.path.startswith("/credits") or self.path.startswith("/balance"):
            bal = CREDITS.get_balance(force_refresh=True)
            return self._json(200, bal)

        if self.path.startswith("/health"):
            return self._json(200, {"ok": True, "model": CFG["model"]})

        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})

        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            return self._json(400, {"error": {"message": "bad json"}})

        messages = req.get("messages", [])
        requested_model = req.get("model") or CFG.get("model")
        requested_effort = req.get("reasoning_effort") or req.get("thinking", {}).get("effort")
        stream = bool(req.get("stream"))

        cid = "chatcmpl-" + uuid.uuid4().hex[:16]
        created = int(time.time())

        out = queue.Queue()
        usage_box = {"total_credits": 0.0}

        def pump():
            fut = asyncio.run_coroutine_threadsafe(
                SESSION.run_turn(messages, requested_model, requested_effort, out, usage_box),
                LOOP
            )
            collected = []
            try:
                while True:
                    try:
                        piece = out.get(timeout=0.05)
                    except queue.Empty:
                        if fut.done() and out.empty():
                            break
                        continue
                    if piece is None:
                        break
                    collected.append(piece)
                    if stream:
                        chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": requested_model,
                            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                        }
                        self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            return "".join(collected)

        if not stream:
            text = pump()
            bal = CREDITS.get_balance()
            return self._json(200, {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": requested_model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": len(str(messages)) // 4,
                    "completion_tokens": len(text) // 4,
                    "total_tokens": (len(str(messages)) + len(text)) // 4,
                    "credits_used": round(usage_box["total_credits"], 4),
                    "credits_available": bal.get("available")
                }
            })

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        try:
            first = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": requested_model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            self.wfile.write(b"data: " + json.dumps(first, ensure_ascii=False).encode("utf-8") + b"\n\n")
            pump()
            stop = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": requested_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(b"data: " + json.dumps(stop, ensure_ascii=False).encode("utf-8") + b"\n\ndata: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


if __name__ == "__main__":
    if not CFG.get("cookie"):
        sys.exit("config.json: поле cookie пустое")

    bal = CREDITS.get_balance()
    avail = bal.get("available")
    alloc = bal.get("monthly_allocation")
    reset_d = bal.get("reset_date")
    print("=" * 60)
    print(" Retool AI Agent OpenAI-Compatible Proxy Relay")
    print(f" Proxy URL:     http://127.0.0.1:{PORT}/v1")
    print(f" Default Model: {CFG.get('model', 'claude-opus-5-5')}")
    print(f" Workspace:     {CFG.get('workspace_root', os.getcwd())}")
    print(f" Local Exec:    {CFG.get('local_exec', True)}")
    print(f" Proj Context:  {CFG.get('project_context', True)}")
    print(f" Effort:        {CFG.get('reasoning_effort', 'high')}")
    if avail is not None:
        print(f" Balance:       {avail} / {alloc} credits (Reset: {reset_d})")
    print("=" * 60)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.serve_forever()
