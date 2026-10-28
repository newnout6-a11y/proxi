
import concurrent.futures as cf, gzip, json, pathlib, re, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).parent
CFG = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
UA = CFG["ua"]
H = {"accept": "*/*", "accept-encoding": "gzip", "cookie": CFG["cookie"], "user-agent": UA, "referer": CFG["base"] + "/"}
names = [n for n in (HERE / "chunks.txt").read_text(encoding="utf-8").split() if n]
out = HERE / "chunks"
out.mkdir(exist_ok=True)

def fetch(n):
    try:
        req = urllib.request.Request("https://retool-edge.com/assets_vjs/" + n, headers=H)
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            if r.headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
        (out / n).write_text(raw.decode("utf-8", "replace"), encoding="utf-8")
        return n, len(raw)
    except Exception as e:
        return n, "ERR " + str(e)[:60]

with cf.ThreadPoolExecutor(8) as ex:
    for n, res in ex.map(fetch, names):
        if isinstance(res, str):
            print(n, res)
print("files:", len(list(out.glob("*.js"))))

blob = ""
for f in list(out.glob("*.js")) + [HERE / "bundle.js"]:
    blob += f.read_text(encoding="utf-8", errors="ignore")
ids = sorted(set(re.findall(r"(?:claude|gpt)-[a-z0-9][a-z0-9.\-]{2,32}", blob)))
print("model-like ids:", ids[:50])
for pat in ("wireModel", "availableModels", "modelOptions", "COPILOT_MODEL", "ANTHROPIC_MODELS"):
    print(pat, "->", len(re.findall(pat, blob)))
