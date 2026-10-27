
import json, pathlib, sys, urllib.request, urllib.error, re
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
COOKIE = pathlib.Path("cookies.txt").read_text(encoding="utf-8")
BASE = "https://vovasahren.retool.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
H = {"accept": "*/*", "accept-encoding": "identity", "content-type": "application/json", "cookie": COOKIE,
     "origin": BASE, "referer": BASE + "/", "user-agent": UA, "x-xsrf-token": "74febab9-99e1-496f-ac7a-8b6a83b224e6",
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}
PAGE = "fc1a6956-d24f-11f1-a342-e70a1f6ca7fe"
THREAD = "f3b05dc9-0e17-4cc8-8fc7-b52ca1d208fb"

def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=H, method=method)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()

s, b = call("/")
html = b.decode("utf-8", "replace")
for pat in ["sandboxToken", "sandpack", "agent-vite", "threads"]:
    hits = [m.start() for m in re.finditer(pat, html)]
    print(f"HTML {pat}: {len(hits)}")
    for h in hits[:2]:
        print("   ", html[max(0,h-200):h+200].replace("\n", " "))

print()
for p in [f"/api/sandpack/agent-vite", f"/api/pages/{PAGE}/agent-vite", f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/agent-vite"]:
    for m in ("GET", "POST"):
        s2, b2 = call(p + "?sandboxToken=x", m, {} if m == "POST" else None)
        print(f"{m} {s2} {p} :: {b2[:200].decode('utf-8','replace').replace(chr(10),' ')}")
