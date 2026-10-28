
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"
def call(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                               headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(r, timeout=200) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

s, b = call("/v1/models")
ids = [m["id"] for m in b.get("data", [])]
print("models:", s, "count", len(ids))
print(" top claude:", [i for i in ids if "opus" in i][:4])
print(" top gpt:", [i for i in ids if "gpt-5.6" in i or i in ("gpt-5.5",)][:5])

for model in ("claude-opus-4-8", "gpt-5.5"):
    s2, b2 = call("/v1/chat/completions", {"model": model, "stream": False,
                                           "messages": [{"role": "user", "content": "Ответь одним словом: работаю?"}],
                                           "reasoning_effort": "low"})
    text = b2.get("choices", [{}])[0].get("message", {}).get("content", b2)
    print(f" {model}: {s2} {str(text)[:70]!r}")
