import asyncio, json, pathlib, sys, time, urllib.request, uuid, websockets
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
H = {"accept": "application/json", "content-type": "application/json", "origin": CFG["base"], "referer": CFG["base"] + "/",
     "user-agent": CFG["ua"], "cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"],
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}

def start():
    data = json.dumps({"threadId": CFG["thread_id"], "pageUuid": CFG["page_uuid"], "environmentName": "production"}).encode()
    req = urllib.request.Request(CFG["base"] + "/api/ai/retool-next-agent/start", data=data, headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

async def inspect_text_frames():
    st = start()
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break

        prompt = {
            "type": "user_prompt",
            "prompt": "Count 1 to 5 slowly",
            "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"]},
            "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
            "r2OnboardingTemplateId": None, "messageId": str(uuid.uuid4()),
            "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
        }

        await ws.send(json.dumps(prompt, ensure_ascii=True))
        deadline = time.time() + 25
        i = 0
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(5, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            k = ev.get("type")
            if k == "ping":
                await ws.send('{"type":"pong"}')
                continue
            if k in ("text", "clear_streaming"):
                i += 1
                print(f"Frame #{i} [{k}]: content={repr(ev.get('content'))} messageId={ev.get('messageId')}")
            if k in ("agent_loop_done", "error"):
                break

asyncio.run(inspect_text_frames())
