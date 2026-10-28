
import json, sys, time, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"
H = {"content-type": "application/json", "authorization": "Bearer " + KEY}

def start_stream(text):
    data = json.dumps({"model": "claude-opus-5-5", "stream": True,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST", headers=H)
    return urllib.request.urlopen(req, timeout=120)

print("request 1: connect, read one chunk, drop")
resp = start_stream("Напиши пять слов через запятую")
for line in resp:
    if line.strip():
        print("  first chunk:", line[:80])
        break
resp.close()

time.sleep(1)
print("request 2: must answer normally")
t0 = time.time()
resp2 = start_stream("Скажи одно слово: живо")
text = []
for line in resp2:
    s = line.decode("utf-8", "replace").strip()
    if s.startswith("data: ") and s[6:] != "[DONE]":
        try:
            d = json.loads(s[6:])["choices"][0]["delta"].get("content")
            if d: text.append(d)
        except Exception: pass
print(f"  answer ({time.time()-t0:.0f}s):", repr("".join(text)[:80]))
