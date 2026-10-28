
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
data = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "скажи ок"}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=data, method="POST",
                             headers={"content-type": "application/json", "authorization": "Bearer live-test-key"})
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        print("status", r.status, r.read()[:200].decode())
except urllib.error.HTTPError as e:
    print("status", e.code, e.read()[:300].decode())
