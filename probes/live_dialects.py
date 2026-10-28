
import json, sys, time, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"

def call(path, payload, stream=False):
    data = json.dumps(payload).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST",
                               headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    return urllib.request.urlopen(r, timeout=200)

print("--- anthropic non-stream")
try:
    with call("/v1/messages", {"model": "claude-opus-5-5", "max_tokens": 32,
                               "messages": [{"role": "user", "content": "Ответь одним словом: 2+2?"}]}) as resp:
        b = json.loads(resp.read().decode())
    print(" status", resp.status, "| text:", b["content"][0]["text"][:60], "| stop:", b["stop_reason"], "| usage:", b.get("usage"))
except urllib.error.HTTPError as e:
    print(" ERR", e.code, e.read()[:200])

print("--- anthropic stream")
names = []
try:
    with call("/v1/messages", {"model": "claude-opus-5-5", "max_tokens": 32, "stream": True,
                               "messages": [{"role": "user", "content": "Скажи: ок"}]}) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("event: "):
                names.append(s[7:])
    print(" events:", names)
except urllib.error.HTTPError as e:
    print(" ERR", e.code, e.read()[:200])

print("--- responses stream (gpt-5.2)")
names2 = []
try:
    with call("/v1/responses", {"model": "gpt-5.2", "input": "Say: ok", "stream": True}) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("event: "):
                names2.append(s[7:])
    print(" events:", names2[:8], "...", "total", len(names2))
except urllib.error.HTTPError as e:
    print(" ERR", e.code, e.read()[:250])
