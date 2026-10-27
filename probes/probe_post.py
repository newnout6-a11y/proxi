
import json, pathlib, sys, urllib.request, urllib.error, gzip
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
COOKIE = pathlib.Path("cookies.txt").read_text(encoding="utf-8")
BASE = "https://vovasahren.retool.com"
PAGE = "fc1a6956-d24f-11f1-a342-e70a1f6ca7fe"
THREAD = "f3b05dc9-0e17-4cc8-8fc7-b52ca1d208fb"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
H = {"accept": "application/json", "accept-encoding": "gzip", "content-type": "application/json", "cookie": COOKIE,
     "origin": BASE, "referer": BASE + "/", "user-agent": UA, "x-xsrf-token": "74febab9-99e1-496f-ac7a-8b6a83b224e6",
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}

def post(payload):
    body = json.dumps(payload).encode()
    r = urllib.request.Request(f"{BASE}/api/pages/{PAGE}/sandpack/threads/{THREAD}/messages", data=body, headers=H, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            if resp.headers.get("content-encoding") == "gzip": raw = gzip.decompress(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("content-encoding") == "gzip": raw = gzip.decompress(raw)
        return e.code, raw
    except Exception as e:
        return -1, str(e).encode()

variants = [
  {"contentType":"text","content":{"type":"text","text":"ping"}},
  {"content":{"type":"text","text":"ping"}},
  {"text":"ping"},
  {"content":{"text":"ping","type":"text","role":"user"}},
  {"message":{"contentType":"text","content":{"type":"text","text":"ping"}}},
]
for i, v in enumerate(variants):
    s, b = post(v)
    print(f"--- {i} {s}: {b[:300].decode('utf-8','replace')}")
