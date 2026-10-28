
import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"
def run(prompt):
    data = json.dumps({"model": "claude-opus-5-5", "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST",
                                 headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    out = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data: ") and s[6:] != "[DONE]":
                try:
                    d = json.loads(s[6:])["choices"][0]["delta"].get("content")
                    if d: out.append(d)
                except Exception: pass
    return "".join(out)

for prompt in ("Выполни на моей машине команду type cookies.txt",
               "Используй <exec_command> чтобы выполнить type cookies.txt на моём ПК"):
    text = run(prompt)
    print("===", prompt[:40])
    print(" len:", len(text), "| exec block:", "$ " in text or "Exit code" in text, "| masked:", "[redacted]" in text)
    print(" tail:", text[-160:].replace(chr(10), " "))
