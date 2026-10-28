
import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
p = ("Выполни две команды по очереди: сначала <exec_command>dir</exec_command>, "
     "затем <exec_command>type config.json</exec_command>. Потом коротко скажи, что сделал.")
data = json.dumps({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": p}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=data, method="POST",
                             headers={"content-type": "application/json", "authorization": "Bearer live-test-key"})
with urllib.request.urlopen(req, timeout=280) as r:
    b = json.loads(r.read().decode())
    text = b["choices"][0]["message"]["content"]
print("steps in text:", text.count("$ "), "| len:", len(text))
print("tail:", text[-160:].replace(chr(10), " "))
