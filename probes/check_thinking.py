
import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
data = json.dumps({"model": "claude-opus-5-5", "stream": True, "reasoning_effort": "high",
                   "thinking": {"type": "enabled", "budget_tokens": 8192},
                   "messages": [{"role": "user", "content": "Подумай и сравни три подхода к проверке утечек DNS в VPN, выбери лучший и обоснуй."}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=data, method="POST",
                             headers={"content-type": "application/json", "authorization": "Bearer live-test-key"})
think, text = [], []
with urllib.request.urlopen(req, timeout=280) as r:
    for line in r:
        s = line.decode("utf-8", "replace").strip()
        if not s.startswith("data: ") or s[6:] == "[DONE]":
            continue
        try:
            d = json.loads(s[6:])["choices"][0]["delta"]
        except Exception:
            continue
        if d.get("reasoning_content"):
            think.append(d["reasoning_content"])
        if d.get("content"):
            text.append(d["content"])
print("thinking chunks:", len(think), "| chars:", len("".join(think)))
print("thinking sample:", "".join(think)[:400].replace(chr(10), " "))
print("answer chars:", len("".join(text)))
