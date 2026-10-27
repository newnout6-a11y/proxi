
import json, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_agent.py", encoding="utf-8").read().split("s, b = call(\"/\")")[0])

paths = [
 f"/api/pages/{PAGE}/sandpack/agent-vite",
 f"/api/pages/{PAGE}/sandpack/agent",
 f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/vite",
 f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/stream",
 f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/run",
 f"/agent-vite",
 f"/api/agent-vite",
 f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/messages/stream",
]
for p in paths:
    for m in ("GET", "POST"):
        s, b = call(p + "?sandboxToken=x", m, {} if m == "POST" else None)
        head = b[:120].decode("utf-8", "replace").replace("\n", " ")
        if "<!DOCTYPE html>" in head[:40]:
            head = head.split("<pre>")[-1][:90]
        print(f"{m} {s} {p} :: {head}")
