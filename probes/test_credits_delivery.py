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

async def test_credits_flow():
    st = start()
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        # Drain backlog
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break
        
        my_msg_id = str(uuid.uuid4())
        prompt = {
            "type": "user_prompt",
            "prompt": "Respond with: PONG_TEST",
            "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"], "reasoningEffort": "low"},
            "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
            "r2OnboardingTemplateId": None, "messageId": my_msg_id,
            "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
        }
        await ws.send(json.dumps(prompt, ensure_ascii=True))
        print("Prompt sent, my_msg_id:", my_msg_id)

        got_done = False
        credits_val = None
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                if got_done:
                    print("Timeout after done, exiting loop.")
                    break
                continue
            ev = json.loads(raw)
            k = ev.get("type")
            if k == "ping":
                await ws.send('{"type":"pong"}')
                continue
            print(f"EV: {k} -> {str(ev)[:160]}")
            if k == "run_credits_persisted":
                credits_val = ev.get("creditsUsed")
                print(">>> FOUND CREDITS:", credits_val)
            if k == "agent_loop_done":
                got_done = True
                print(">>> GOT AGENT_LOOP_DONE, waiting up to 1.5s for trailing frames...")
                # Continue loop briefly to catch run_credits_persisted
        print("Done. credits_val =", credits_val)

asyncio.run(test_credits_flow())
