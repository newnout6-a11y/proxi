
import json, sys, time, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"
KEY = "live-test-key"

def req(path, payload=None, timeout=180, stream=False):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                               headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    return urllib.request.urlopen(r, timeout=timeout)

def j(path, payload=None):
    try:
        with req(path, payload) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

print("health:", j("/health"))

t0 = time.time()
s, b = j("/v1/chat/completions", {"model": "claude-opus-5-5", "stream": False,
                                  "messages": [{"role": "user", "content": "Ответь одним словом: столица Франции?"}]})
print(f"chat non-stream [{time.time()-t0:.1f}s]:", s, repr(b.get("choices", [{}])[0].get("message", {}).get("content", b))[:120])
print("  usage:", b.get("usage"))

t0 = time.time()
chunks, done = [], False
with req("/v1/chat/completions", {"model": "claude-opus-5-5", "stream": True,
                                  "messages": [{"role": "user", "content": "Скажи ровно: стрим ок"}]}) as resp:
    for line in resp:
        sline = line.decode("utf-8", "replace").strip()
        if not sline.startswith("data: "):
            continue
        data = sline[6:]
        if data == "[DONE]":
            done = True
            break
        try:
            ch = json.loads(data)
        except Exception:
            continue
        d = ch.get("choices", [{}])[0].get("delta", {}).get("content")
        if d:
            chunks.append(d)
        if ch.get("choices") and ch["choices"][0].get("finish_reason"):
            print("  finish:", ch["choices"][0]["finish_reason"])
print(f"chat stream [{time.time()-t0:.1f}s]: chunks={len(chunks)} done={done} text={''.join(chunks)[:80]!r}")
