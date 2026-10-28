
import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8787"; KEY = "live-test-key"

def stream(prompt):
    data = json.dumps({"model": "claude-opus-5-5", "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST",
                                 headers={"content-type": "application/json", "authorization": "Bearer " + KEY})
    chunks = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data: ") and s[6:] != "[DONE]":
                try:
                    d = json.loads(s[6:])["choices"][0]["delta"].get("content")
                    if d:
                        chunks.append(d)
                except Exception:
                    pass
    return "".join(chunks)

print("=== denied command (whoami, not allowlisted)")
out = stream("Ответь РОВНО одной строкой без пояснений: <exec_command>whoami</exec_command>")
print(" denial text:", "not allowlisted" in out)
print(" tail:", out[-220:].replace(chr(10), " "))

print("=== redaction (type config.json)")
out = stream("Ответь РОВНО одной строкой без пояснений: <exec_command>type config.json</exec_command>")
print(" masked:", "accessToken=[redacted]" in out)
print(" raw jwt leaked:", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ4c3Jm" in out)
print(" tail:", out[-200:].replace(chr(10), " "))
