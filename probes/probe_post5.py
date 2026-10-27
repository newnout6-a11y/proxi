
import json, pathlib, sys, urllib.request, urllib.error, gzip, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_post.py", encoding="utf-8").read().split("variants = [")[0])

t = str(uuid.uuid4())
variants = [
  {"messages":[{"role":"user","type":"text","content":{"role":"user","type":"text","text":"ping"}}]},
  {"messages":[{"role":"user","type":"text","content":{"type":"text","text":"ping","turnId":t}}]},
  {"messages":[{"role":"user","type":"text","turnId":t,"content":{"role":"user","type":"text","text":"ping","turnId":t,"streamedMessageId":str(uuid.uuid4())}}]},
]
for i, v in enumerate(variants):
    s, b = post(v)
    print(f"--- {i} {s}: {b[:600].decode('utf-8','replace')}")
