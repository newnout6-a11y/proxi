
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
prompt = sys.argv[1] if len(sys.argv) > 1 else "Покажи первые три строки файла config.json"
data = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": prompt}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=data, method="POST",
                             headers={"content-type": "application/json", "authorization": "Bearer live-test-key"})
try:
    with urllib.request.urlopen(req, timeout=200) as r:
        b = json.loads(r.read().decode())
        print("OK len", len(b["choices"][0]["message"]["content"]))
        print(b["choices"][0]["message"]["content"])
except urllib.error.HTTPError as e:
    print("ERR", e.code, e.read()[:200].decode())
