
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
prompt = sys.argv[1]
data = json.dumps({"model": sys.argv[2] if len(sys.argv) > 2 else "claude-opus-5-5",
                   "stream": True, "messages": [{"role": "user", "content": prompt}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=data, method="POST",
                             headers={"content-type": "application/json", "authorization": "Bearer live-test-key"})
try:
    with urllib.request.urlopen(req, timeout=2800) as r:
        for line in r:
            s = line.decode("utf-8", "replace").strip()
            if not s.startswith("data: ") or s[6:] == "[DONE]":
                continue
            try:
                d = json.loads(s[6:])["choices"][0]["delta"]
            except Exception:
                continue
            if d.get("reasoning_content"):
                print("· ", d["reasoning_content"], sep="", end="", flush=True)
            if d.get("content"):
                print(d["content"], end="", flush=True)
except urllib.error.HTTPError as e:
    print("ERR", e.code, e.read()[:200].decode())
