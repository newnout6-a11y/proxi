
import re, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
js = pathlib.Path("bundle.js").read_text(encoding="utf-8")
names = sorted(set(re.findall(r"assets_vjs/([\w\-.@]+\.js)", js)))
print("total chunks:", len(names))
kw = ("assist", "chat", "agent", "ai-", "sandbox", "thread", "message", "stream", "sse")
cand = [n for n in names if any(k in n.lower() for k in kw)]
print("candidates:", len(cand))
for n in cand[:60]:
    print(" *", n)
pathlib.Path("chunks.txt").write_text("\n".join(names), encoding="utf-8")
