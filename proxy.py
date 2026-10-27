"""OpenAI-compatible facade over the Retool agent websocket."""
import asyncio, json, os, queue, sys, threading, time, uuid, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
PORT = int(os.environ.get("PROXY_PORT", "8787"))
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "180"))
RECV_TIMEOUT = int(os.environ.get("RECV_TIMEOUT", "60"))
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


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
        # Сливаем накопившиеся кадры начального состояния (replayed backlog)
        while True:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.4)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await self.ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break

    async def run_turn(self, prompt, out):
        async with self.lock:
            try:
                await self.ensure_ws()
                await self.ws.send(json.dumps({
                    "type": "user_prompt",
                    "prompt": prompt,
                    "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"]},
                    "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
                    "r2OnboardingTemplateId": None,
                    "messageId": str(uuid.uuid4()),
                    "expectedThreadId": CFG["thread_id"],
                    "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser",
                }, ensure_ascii=True))

                deadline = time.time() + TURN_TIMEOUT
                had_text = False

                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        out.put(f"\n[turn timeout after {TURN_TIMEOUT}s]")
                        if self.ws is not None:
                            try:
                                await self.ws.close()
                            except Exception:
                                pass
                            self.ws = None
                        break

                    try:
                        raw = await asyncio.wait_for(self.ws.recv(), timeout=min(remaining, RECV_TIMEOUT))
                    except asyncio.TimeoutError:
                        if time.time() >= deadline:
                            out.put(f"\n[turn timeout after {TURN_TIMEOUT}s]")
                            if self.ws is not None:
                                try:
                                    await self.ws.close()
                                except Exception:
                                    pass
                                self.ws = None
                            break
                        continue

                    ev = json.loads(raw)
                    kind = ev.get("type")
                    print("ev:", json.dumps(ev, ensure_ascii=False)[:300], flush=True)

                    if kind == "ping":
                        await self.ws.send('{"type":"pong"}')
                    elif kind == "text":
                        out.put(ev.get("content") or "")
                        had_text = True
                    elif kind == "hitl":
                        # HITL (human-in-the-loop / askUser) requested
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
                        if q_texts:
                            msg = "\n".join(q_texts)
                            out.put(("\n" if had_text else "") + msg)
                        elif hitl_obj.get("message"):
                            out.put(("\n" if had_text else "") + str(hitl_obj["message"]))
                        else:
                            out.put(("\n" if had_text else "") + f"[hitl: {hitl_type}]")
                        break
                    elif kind == "agent_loop_done":
                        break
                    elif kind == "error":
                        out.put(f"\n[error] {ev}")
                        break
            except Exception as exc:
                out.put(f"\n[proxy error] {exc}")
                if self.ws is not None:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
            finally:
                out.put(None)


SESSION = Session()


def flatten(messages):
    parts = []
    for m in messages:
        role = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
        if role == "system":
            parts.append(f"[system] {c}")
        else:
            parts.append(f"{(m.get('name') or role)}: {c}" if role != "user" else str(c))
    return "\n\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": CFG["model"], "object": "model", "owned_by": "retool-agent"}]})
        if self.path.startswith("/health"):
            return self._json(200, {"ok": True})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            return self._json(400, {"error": {"message": "bad json"}})
        prompt = flatten(req.get("messages", []))
        model = req.get("model") or CFG["model"]
        cid = "chatcmpl-" + uuid.uuid4().hex[:16]
        created = int(time.time())
        stream = bool(req.get("stream"))

        out = queue.Queue()

        def pump():
            fut = asyncio.run_coroutine_threadsafe(SESSION.run_turn(prompt, out), LOOP)
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
                        chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                                 "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return "".join(collected)

        if not stream:
            text = pump()
            return self._json(200, {"id": cid, "object": "chat.completion", "created": created, "model": model,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                 "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        first = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        self.wfile.write(b"data: " + json.dumps(first).encode() + b"\n\n")
        pump()
        stop = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(b"data: " + json.dumps(stop).encode() + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    if not CFG.get("cookie"):
        sys.exit("config.json: поле cookie пустое")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"proxy on http://127.0.0.1:{PORT}/v1  model={CFG['model']}")
    srv.serve_forever()
