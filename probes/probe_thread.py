
import json, pathlib, sys, urllib.request, urllib.error, gzip
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_post.py", encoding="utf-8").read().split("variants = [")[0])

def get(path):
    r = urllib.request.Request(BASE + path, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            if resp.headers.get("content-encoding") == "gzip": raw = gzip.decompress(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()

s, b = get(f"/api/pages/{PAGE}/sandpack/threads/{THREAD}")
d = json.loads(b)
print("KEYS:", list(d.keys()))
for k, v in d.items():
    print(f"  {k} = {str(v)[:200]}")
