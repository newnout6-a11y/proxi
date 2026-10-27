import asyncio, json, os, pathlib, re, subprocess, sys, time, urllib.request, uuid, websockets
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

def run_local_cmd(cmd, cwd):
    print(f"[EXECUTING LOCALLY]: {cmd}")
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=30)
        # Try decoding stdout and stderr
        out = ""
        for encoding in ("utf-8", "cp866", "cp1251", "latin-1"):
            try:
                out = (proc.stdout or b"").decode(encoding)
                err = (proc.stderr or b"").decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            out = (proc.stdout or b"").decode("utf-8", errors="replace")
            err = (proc.stderr or b"").decode("utf-8", errors="replace")
        
        combined = out.strip()
        if err.strip():
            combined += ("\nSTDERR:\n" if combined else "") + err.strip()
        return proc.returncode, combined
    except Exception as e:
        return -1, str(e)

async def test_cmd_loop():
    st = start()
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        # Drain backlog
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.4)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break
        
        system_instruction = (
            "[SYSTEM INSTRUCTION]\n"
            "You are connected to the user's local PC.\n"
            "Workspace: C:\\specialinay_papka\n"
            "You have access to a tool to execute local shell commands:\n"
            "<exec_command>command</exec_command>\n"
            "When asked to check something on the PC or in the repo, emit the command in <exec_command> tag."
        )

        user_query = "What git branches exist in this repository?"
        full_prompt = f"{system_instruction}\n\nUser: {user_query}"

        # Send turn 1
        prompt_payload = {
            "type": "user_prompt",
            "prompt": full_prompt,
            "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"], "reasoningEffort": "low"},
            "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
            "r2OnboardingTemplateId": None, "messageId": str(uuid.uuid4()),
            "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
        }
        await ws.send(json.dumps(prompt_payload, ensure_ascii=True))
        
        last_content = ""
        credits_used = 0.0
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            k = ev.get("type")
            if k == "ping":
                await ws.send('{"type":"pong"}')
                continue
            if k == "text":
                c = ev.get("content") or ""
                delta = c[len(last_content):]
                last_content = c
                sys.stdout.write(delta)
                sys.stdout.flush()
            elif k == "run_credits_persisted":
                credits_used = ev.get("creditsUsed", 0.0)
            elif k == "agent_loop_done":
                # Wait briefly for run_credits_persisted
                try:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    ev2 = json.loads(raw2)
                    if ev2.get("type") == "run_credits_persisted":
                        credits_used = ev2.get("creditsUsed", 0.0)
                except Exception:
                    pass
                break
        
        print("\n\nFull text 1:\n", last_content)
        print("Turn 1 credits used:", credits_used)

        # Check for command
        m = re.search(r"<exec_command>(.*?)</exec_command>", last_content, re.DOTALL)
        if m:
            cmd = m.group(1).strip()
            ret, out = run_local_cmd(cmd, r"c:\specialinay_papka")
            print(f"\nExecution result (exit {ret}):\n{out}\n")

            follow_up = f"[Command Result for `{cmd}`]:\nExit code: {ret}\nOutput:\n{out}\n\nPlease summarize the result for the user."
            prompt_payload2 = {
                "type": "user_prompt",
                "prompt": follow_up,
                "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"], "reasoningEffort": "low"},
                "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
                "r2OnboardingTemplateId": None, "messageId": str(uuid.uuid4()),
                "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
            }
            await ws.send(json.dumps(prompt_payload2, ensure_ascii=True))
            last_content2 = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                ev = json.loads(raw)
                k = ev.get("type")
                if k == "ping":
                    await ws.send('{"type":"pong"}')
                    continue
                if k == "text":
                    c = ev.get("content") or ""
                    delta = c[len(last_content2):]
                    last_content2 = c
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                elif k == "run_credits_persisted":
                    credits_used += ev.get("creditsUsed", 0.0)
                elif k == "agent_loop_done":
                    break
            print("\n\nFinal reply:\n", last_content2)

asyncio.run(test_cmd_loop())
