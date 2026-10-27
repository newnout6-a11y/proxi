
import re, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exec(open("probe_agent.py", encoding="utf-8").read().split("s, b = call(\"/\")")[0])
s, b = call("/agent-vite")
html = b.decode("utf-8", "replace")
pathlib.Path("vite.html").write_text(html, encoding="utf-8")
for m in re.finditer(r"<script[^>]*>", html):
    print("TAG:", m.group(0)[:250])
for pat in ["@vite", "src=", ".tsx", ".js"]:
    print(f"-- {pat}: {html.count(pat)}")
    for mm in list(re.finditer(re.escape(pat), html))[:3]:
        print("    ", html[max(0, mm.start()-120):mm.start()+120].replace("\n"," "))
