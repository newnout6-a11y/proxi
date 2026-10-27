
import json, sys, time, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
body = json.dumps({"model": "claude-opus-5-5", "stream": True,
                   "messages": [{"role": "user", "content": "ответь ровно одним словом: столица Франции?"}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=body,
                             headers={"content-type": "application/json"}, method="POST")
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        print("HTTP", r.status)
        for line in r:
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            print(f"{time.time()-t0:6.2f}s {s[:220]}")
            if s.endswith("[DONE]"):
                break
except urllib.error.HTTPError as e:
    print("HTTPERR", e.code, e.read()[:400])
except Exception as e:
    print("ERR", type(e).__name__, str(e)[:200])
