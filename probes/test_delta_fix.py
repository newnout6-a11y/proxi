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
    print(f"\n[RUNNING LOCAL CMD]: {cmd}")
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=30)
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

async def test():
    st = start()
    url = st["url"] + "?sandboxToken=" + st["sandboxToken"]
    async with websockets.connect(url, origin=CFG["base"], max_size=None, ping_interval=None) as ws:
        # Drain backlog
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.6)
                ev = json.loads(raw)
                if ev.get("type") == "ping":
                    await ws.send('{"type":"pong"}')
            except asyncio.TimeoutError:
                break

        system_instruction = (
            "CRITICAL INSTRUCTION FOR LOCAL ENVIRONMENT:\n"
            "You are connected to the user's local Windows PC.\n"
            "Workspace: C:\\specialinay_papka\n"
            "To execute commands locally on user's PC, format the command as:\n"
            "<exec_command>command</exec_command>\n"
            "You will receive the output in the next turn."
        )
        user_msg = "Please check the current git status."
        full_prompt = f"{system_instruction}\n\nUser: {user_msg}"

        async def send_turn(prompt_text):
            my_id = str(uuid.uuid4())
            p = {
                "type": "user_prompt",
                "prompt": prompt_text,
                "modelProvider": {"model": CFG["model"], "providerResourceName": CFG["provider_resource_name"], "reasoningEffort": "low"},
                "attachments": [], "taggedResourceNames": [], "taggedResourceIds": [],
                "r2OnboardingTemplateId": None, "messageId": my_id,
                "expectedThreadId": CFG["thread_id"], "agentMode": 2, "testingEnabled": False, "requestOrigin": "Browser"
            }
            await ws.send(json.dumps(p, ensure_ascii=True))
            
            last_text = ""
            turn_credits = 0.0
            deadline = time.time() + 45
            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30.0))
                except asyncio.TimeoutError:
                    break
                ev = json.loads(raw)
                k = ev.get("type")
                if k == "ping":
                    await ws.send('{"type":"pong"}')
                    continue
                if k == "text":
                    content = ev.get("content") or ""
                    delta = content[len(last_text):]
                    last_text = content
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                elif k == "run_credits_persisted":
                    turn_credits = ev.get("creditsUsed", 0.0)
                elif k == "agent_loop_done":
                    # wait up to 1.5s for run_credits_persisted
                    t_end = time.time() + 1.5
                    while time.time() < t_end and turn_credits == 0.0:
                        try:
                            raw2 = await asyncio.wait_for(ws.recv(), timeout=0.5)
                            ev2 = json.loads(raw2)
                            if ev2.get("type") == "run_credits_persisted":
                                turn_credits = ev2.get("creditsUsed", 0.0)
                                break
                            elif ev2.get("type") == "ping":
                                await ws.send('{"type":"pong"}')
                        except asyncio.TimeoutError:
                            break
                    break
            return last_text, turn_credits

        print("\n--- TURN 1 ---")
        reply1, cred1 = await send_turn(full_prompt)
        print(f"\n[Turn 1 Done: cred={cred1}]")

        m = re.search(r"<exec_command>(.*?)</exec_command>", reply1, re.DOTALL)
        if m:
            cmd = m.group(1).strip()
            code, out = run_local_cmd(cmd, r"c:\specialinay_papka")
            print(f"[Command Output (code {code})]:\n{out}\n")

            print("--- TURN 2 (Sending command output to agent) ---")
            followup = f"[Command Result for `{cmd}`]:\nExit code: {code}\nOutput:\n{out}\n\nPlease summarize this for the user."
            reply2, cred2 = await send_turn(followup)
            print(f"\n[Turn 2 Done: cred={cred2}]")

asyncio.run(test())
