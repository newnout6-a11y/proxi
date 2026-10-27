
import json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_agent.py", encoding="utf-8").read().split("s, b = call(\"/\")")[0])

s, b = call(f"/api/pages/{PAGE}/sandpack/threads/{THREAD}/messages")
d = json.loads(b)
print("status", s, "count", len(d["messages"]))
for m in d["messages"]:
    print(f'  {m["createdAt"][11:19]} {m.get("senderRole"):8} {m.get("contentType"):10} {str(m.get("content",{}).get("text") or m.get("content",{}).get("thinking"))[:70]}')
st, tb = call(f"/api/pages/{PAGE}/sandpack/threads/{THREAD}")
print("tokenUsage:", json.loads(tb).get("tokenUsage"))
