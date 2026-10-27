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

async def test_frame(mp_extra=None, top_extra=None):
    st = start()
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        # drain backlog
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break

        mp = {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"]}
        if mp_extra:
            mp.update(mp_extra)

        prompt = {
            "type": "user_prompt",
            "prompt": "Say test ok",
            "modelProvider": mp,
            "attachments": [],
            "taggedResourceNames": [],
            "taggedResourceIds": [],
            "r2OnboardingTemplateId": None,
            "messageId": str(uuid.uuid4()),
            "expectedThreadId": CFG["thread_id"],
            "agentMode": 2,
            "testingEnabled": False,
            "requestOrigin": "Browser"
        }
        if top_extra:
            prompt.update(top_extra)

        await ws.send(json.dumps(prompt, ensure_ascii=True))
        print("Sent prompt with:", mp_extra, top_extra)
        deadline = time.time() + 20
        received = []
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(5, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            k = ev.get("type")
            received.append(k)
            if k == "text":
                print("Text delta:", ev.get("content"))
            elif k in ("error", "agent_loop_done", "run_credits_persisted"):
                print(f"Event: {k}:", ev)
            if k == "agent_loop_done":
                break
        print("Received events:", received)

asyncio.run(test_frame({"reasoningEffort": "high"}))
