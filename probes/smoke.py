
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"
def get(path, key):
    req = urllib.request.Request(BASE + path, headers={"authorization": "Bearer " + key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()[:80].decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:80].decode()
print("models with ?key:", get("/v1/models?key=live-test-key", None)[0])
print("non-ascii key:", get("/v1/models", "ключ-测试"))
payload = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "Скажи одно слово: работаю"}]}).encode()
req = urllib.request.Request(BASE + "/v1/chat/completions", data=payload, method="POST",
                             headers={"content-type": "application/json", "authorization": "Bearer live-test-key"})
try:
    with urllib.request.urlopen(req, timeout=150) as r:
        b = json.loads(r.read().decode())
        print("chat:", r.status, repr(b["choices"][0]["message"]["content"][:60]))
except urllib.error.HTTPError as e:
    print("chat ERR", e.code, e.read()[:120])
