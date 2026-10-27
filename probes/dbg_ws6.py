
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

async def main():
    st = start()
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    t0 = time.time()
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        prompt = {"type": "user_prompt", "prompt": "Say exactly: hello",
                  "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"]},
                  "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
                  "r2OnboardingTemplateId": None, "messageId": str(uuid.uuid4()),
                  "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"}
        await ws.send(json.dumps(prompt))
        print("sent prompt", flush=True)
        deadline = time.time() + 100
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            k = ev.get("type")
            if k == "ping":
                await ws.send('{"type":"pong"}')
                continue
            print(f"{time.time()-t0:6.1f}s {k} :: {json.dumps(ev, ensure_ascii=False)[:200]}", flush=True)
            if k == "agent_loop_done":
                break
asyncio.run(main())
