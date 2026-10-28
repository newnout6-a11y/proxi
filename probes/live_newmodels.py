
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; H = {"content-type": "application/json", "authorization": "Bearer live-test-key"}
s = json.loads(urllib.request.urlopen(urllib.request.Request(BASE + "/v1/models", headers=H)).read().decode())
ids = [m["id"] for m in s["data"]]
print("models:", len(ids), "| 4.1:", [i for i in ids if "4.1" in i], "| gpt-6:", [i for i in ids if i.startswith("gpt-6")][:3])
for model in ("gpt-6-sol", "claude-opus-5-5"):
    data = json.dumps({"model": model, "messages": [{"role": "user", "content": "Ответь одним словом: работаю"}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST", headers=H)
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            b = json.loads(r.read().decode())
            print(f" {model}: {r.status} {b['choices'][0]['message']['content'][:50]!r}")
    except urllib.error.HTTPError as e:
        print(f" {model}: ERR {e.code} {e.read()[:150].decode()}")
