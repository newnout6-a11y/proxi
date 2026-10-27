
import re, pathlib, sys, urllib.request, gzip, concurrent.futures as cf
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
names = [n for n in pathlib.Path("chunks.txt").read_text(encoding="utf-8").split() if n]
COOKIE = pathlib.Path("cookies.txt").read_text(encoding="utf-8")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
H = {"accept": "*/*", "accept-encoding": "gzip", "cookie": COOKIE, "user-agent": UA, "referer": "https://vovasahren.retool.com/"}

def fetch(n):
    url = "https://retool-edge.com/assets_vjs/" + n
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=90) as r:
            raw = r.read()
            if r.headers.get("content-encoding") == "gzip": raw = gzip.decompress(raw)
            return n, raw.decode("utf-8", "replace")
    except Exception as e:
        return n, f"__ERR__ {e}"

store = pathlib.Path("chunks"); store.mkdir(exist_ok=True)
with cf.ThreadPoolExecutor(8) as ex:
    for n, txt in ex.map(fetch, names):
        (store / n).write_text(txt, encoding="utf-8")

hits = 0
for f in store.glob("*.js"):
    t = f.read_text(encoding="utf-8")
    for pat in ("agent-vite", "sandboxToken", "sandpack/threads"):
        for m in list(re.finditer(pat, t))[:2]:
            hits += 1
            print(f"== {f.name} [{pat}]")
            print("   ", t[max(0, m.start()-260):m.start()+260].replace("\n", " "))
print("total hits", hits)
