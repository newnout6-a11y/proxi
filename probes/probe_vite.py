
import re, pathlib, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_agent.py", encoding="utf-8").read().split("s, b = call(\"/\")")[0])

s, b = call("/agent-vite")
html = b.decode("utf-8", "replace")
print("status", s, "len", len(html))
for m in re.findall(r'src="([^"]+)"', html):
    print("SRC:", m)
print(html[:600].replace("\n", " "))
