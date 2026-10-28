
import json, pathlib, sys, threading, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.loads(pathlib.Path(__file__).parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"
H = {"cookie": CFG["cookie"], "x-xsrf-token": CFG["xsrf"], "user-agent": CFG["ua"],
     "origin": CFG["base"], "referer": CFG["base"] + "/", "accept": "application/json"}

def thread_count():
    url = f"{CFG['base']}/api/pages/{CFG['page_uuid']}/sandpack/threads"
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30) as r:
        return len(json.loads(r.read().decode()).get("threads", []))

def ask(prompt, out, idx):
    data = json.dumps({"model": "claude-opus-5-5", "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST",
                                 headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    chunks = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data: ") and s[6:] != "[DONE]":
                try:
                    d = json.loads(s[6:])["choices"][0]["delta"].get("content")
                    if d:
                        chunks.append(d)
                except Exception:
                    pass
    out[idx] = "".join(chunks)

before = thread_count()
out = {}
ts = [threading.Thread(target=ask, args=(f"Запомни число {n} и назови его. Ответь только числом.", out, n)) for n in (111, 222)]
for t in ts: t.start()
for t in ts: t.join()
after = thread_count()
print("threads before/after:", before, after, "| delta:", after - before)
print("answer A:", repr(out.get(111, ""))[:80])
print("answer B:", repr(out.get(222, ""))[:80])
print("isolated (A has 111, B has 222):", "111" in out.get(111, "") and "222" in out.get(222, ""))
print("no cross-contamination:", "222" not in out.get(111, "") and "111" not in out.get(222, ""))
