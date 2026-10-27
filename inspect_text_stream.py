import asyncio, json, pathlib, sys, time, urllib.request, uuid, websockets
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": CFG["base"],
    "referer": CFG["base"] + "/",
    "user-agent": CFG["ua"],
    "cookie": CFG["cookie"],
    "x-xsrf-token": CFG["xsrf"],
    "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"
}

def start():
    data = json.dumps({"threadId": CFG["thread_id"], "pageUuid": CFG["page_uuid"], "environmentName": "production"}).encode()
    req = urllib.request.Request(CFG["base"] + "/api/ai/retool-next-agent/start", data=data, headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

async def test_text_stream():
    st = start()
    print("Start ok, url:", st["url"][:40])
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        drained = 0
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await ws.send('{"type":"pong"}')
                drained += 1
            except asyncio.TimeoutError:
                break
        print("Drained backlog events:", drained)
        
        prompt = {
            "type": "user_prompt",
            "prompt": "Write a 3-line poem about rain.",
            "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"], "reasoningEffort": "low"},
            "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
            "r2OnboardingTemplateId": None, "messageId": str(uuid.uuid4()),
            "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
        }
        await ws.send(json.dumps(prompt, ensure_ascii=True))
        print("Sent prompt")
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            ev = json.loads(raw)
            k = ev.get("type")
            if k == "ping":
                await ws.send('{"type":"pong"}')
                continue
            if k == "text":
                print("TEXT FULL EV:", json.dumps(ev, ensure_ascii=False))
            elif k in ("agent_loop_done", "error", "clear_streaming"):
                print("EV:", k)
            if k == "agent_loop_done":
                break

asyncio.run(test_text_stream())
