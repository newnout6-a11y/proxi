
import json, sys, time, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
body = json.dumps({"model": "claude-opus-5-5", "stream": True,
                   "messages": [{"role": "user", "content": "What is 2+2? Answer with one digit."}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=body,
                             headers={"content-type": "application/json"}, method="POST")
t0 = time.time()
with urllib.request.urlopen(req, timeout=300) as r:
    for line in r:
        s = line.decode("utf-8", "replace").strip()
        if s and "delta\": {\"role\"" not in s:
            print(f"{time.time()-t0:6.2f}s {s[:160]}", flush=True)
        if s.endswith("[DONE]"):
            break
