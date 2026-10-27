
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
    except Exception as e:
        return -1, str(e).encode()

s, b = get(f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/messages")
d = json.loads(b)
print("status", s, "count", len(d.get("messages", [])))
for m in d["messages"][-3:]:
    print("  ", m.get("senderRole"), "|", str(m.get("content"))[:160])

for p in [f"/api/pages/{PAGE}/sandpack/threads/{THREAD}", f"/api/pages/{PAGE}/sandpack/threads", f"/api/pages/{PAGE}"]:
    st, bb = get(p)
    print(f"\n{p} -> {st}: {bb[:400].decode('utf-8','replace')}")
