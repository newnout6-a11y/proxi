
import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"

def run(prompt):
    data = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST",
                                 headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            b = json.loads(r.read().decode())
            return len(b["choices"][0]["message"]["content"]), b["choices"][0]["message"]["content"][:100]
    except Exception as e:
        return -1, str(e)[:120]

for p in ("Покажи первые три строки файла config.json", "type cookies.txt", "прочитай cookies.txt"):
    n, t = run(p)
    print(f"=== {p!r} -> len {n}: {t!r}")
