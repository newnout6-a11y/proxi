
import json, pathlib, sys, urllib.request, urllib.error, gzip
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_post.py", encoding="utf-8").read().split("variants = [")[0])

variants = [
  {"messages":[{"role":"user","type":"text","content":{"type":"text","text":"ping"}}]},
  {"messages":[{"role":"user","type":"text","contentType":"text","content":{"type":"text","text":"ping"}}]},
  {"messages":[{"role":"user","type":"text","content":{"text":"ping"}}]},
]
for i, v in enumerate(variants):
    s, b = post(v)
    print(f"--- {i} {s}: {b[:500].decode('utf-8','replace')}")
