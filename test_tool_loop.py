import asyncio, json, pathlib, subprocess, sys, time, urllib.request, uuid, websockets
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

async def send_and_collect(ws, prompt_text):
    prompt = {
        "type": "user_prompt",
        "prompt": prompt_text,
        "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"], "reasoningEffort": "medium"},
        "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
        "r2OnboardingTemplateId": None, "messageId": str(uuid.uuid4()),
        "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
    }
    await ws.send(json.dumps(prompt, ensure_ascii=True))
    deadline = time.time() + 30
    collected = []
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
        if k == "text":
            collected.append(ev.get("content") or "")
        if k in ("agent_loop_done", "error"):
            break
    # In Retool, clear_streaming / deltas might contain chunks
    # Note: Retool sometimes sends deltas, or sends full replacements if clear_streaming was received.
    # Let's check how proxy.py currently handles text deltas: out.put(ev.get("content") or "")
    return "".join(collected)

async def test_full_loop():
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

        system_instruction = (
            "CRITICAL INSTRUCTION FOR LOCAL ENVIRONMENT:\n"
            "You are functioning as an AI coding engine connected to the user's local PC.\n"
            "Local Project Directory: C:\\specialinay_papka\n"
            "You can run local commands via <exec_command>command</exec_command>.\n"
            "When given command results, analyze them and answer the user."
        )

        user_msg = "What is the git status in my project?"
        reply1 = await send_and_collect(ws, f"{system_instruction}\n\nUser: {user_msg}")
        print("Reply 1:", reply1)

        import re
        m = re.search(r'<exec_command>(.*?)</exec_command>', reply1, re.DOTALL)
        if m:
            cmd = m.group(1).strip()
            print(f"Executing locally: '{cmd}'")
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=r"c:\specialinay_papka")
            out = proc.stdout + proc.stderr
            print("Local output:", out[:200])

            followup = f"[Local Command Execution Output for '{cmd}']:\nExit Code: {proc.returncode}\n{out}\n\nPlease explain what this means to the user."
            reply2 = await send_and_collect(ws, followup)
            print("Reply 2:", reply2)

asyncio.run(test_full_loop())
