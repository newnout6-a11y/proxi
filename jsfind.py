
import re, urllib.request, pathlib, sys, gzip
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
COOKIE = pathlib.Path("cookies.txt").read_text(encoding="utf-8")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
H = {"accept": "*/*", "accept-encoding": "gzip", "cookie": COOKIE, "user-agent": UA, "referer": "https://vovasahren.retool.com/"}
url = "https://retool-edge.com/assets_vjs/main-mti2rlpj.js"
with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=120) as resp:
    raw = resp.read()
    if resp.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
js = raw.decode("utf-8", "replace")
pathlib.Path("bundle.js").write_text(js, encoding="utf-8")
print("bytes:", len(js), "| starts:", js[:120].replace("\n", " "))

for pat in [r"agent-vite", r"/messages", r"sandboxToken"]:
    ms = [m.start() for m in re.finditer(pat, js)]
    print(f"\n===== {pat} hits={len(ms)}")
    for pos in ms[:3]:
        print("---", pos)
        print(js[max(0, pos-500):pos+500].replace("\n", " "))
