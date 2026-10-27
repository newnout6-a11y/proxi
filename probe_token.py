
import json, pathlib, sys, urllib.request, urllib.error, gzip
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_post.py", encoding="utf-8").read().split("variants = [")[0])

def get(path):
    r = urllib.request.Request(BASE + path, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            raw = resp.read()
            if resp.headers.get("content-encoding") == "gzip": raw = gzip.decompress(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()

cands = [
 f"/api/pages/{PAGE}/sandpack",
 f"/api/pages/{PAGE}/sandpack/sandbox",
 f"/api/pages/{PAGE}/sandpack/sandbox-token",
 f"/api/pages/{PAGE}/sandpack/token",
 f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/sandbox",
 f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/agent-vite",
 f"/api/pages/{PAGE}/sandpack/branch",
 f"/api/pages/{PAGE}/sandpack/files",
]
for p in cands:
    s, b = get(p)
    print(f"{s}  {p}  :: {b[:220].decode('utf-8','replace').replace(chr(10),' ')}")
