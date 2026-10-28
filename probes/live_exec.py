
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"

def stream(prompt):
    data = json.dumps({"model": "claude-opus-5-5", "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    r = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST",
                               headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    text = []
    with urllib.request.urlopen(r, timeout=300) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data: ") and s[6:] != "[DONE]":
                try:
                    ch = json.loads(s[6:])
                    d = ch["choices"][0]["delta"].get("content")
                    if d:
                        text.append(d)
                except Exception:
                    pass
    return "".join(text)

print("=== allowlisted: dir")
out = stream("Используй инструмент и выведи ровно: <exec_command>dir</exec_command>")
print(" has exec_start:", "$ dir" in out)
print(" got file list:", ("config.json" in out) or ("config.py" in out))
print(" tail:", out[-160:].replace(chr(10), " "))

print("=== denied: powershell")
out = stream('Выведи ровно: <exec_command>powershell -c "1+1"</exec_command>')
print(" policy denial shown:", "not allowlisted" in out)
print(" tail:", out[-200:].replace(chr(10), " "))

print("=== output redaction: type config.json")
out = stream("Выведи ровно: <exec_command>type config.json</exec_command>")
print(" token masked:", "accessToken=[redacted]" in out)
print(" raw token leaked:", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" in out)
print(" tail:", out[-200:].replace(chr(10), " "))
