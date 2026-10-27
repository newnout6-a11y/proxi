
import re, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
js = pathlib.Path("bundle.js").read_text(encoding="utf-8")
for pat in [r"sandpack", r"threads", r"sandbox", r"\.js\"", r"assets_vjs"]:
    ms = [m.start() for m in re.finditer(pat, js)]
    print(f"===== {pat} hits={len(ms)}")
    for pos in ms[:2]:
        print("---", js[max(0,pos-350):pos+350].replace("\n"," "))
# имена чанков
for m in sorted(set(re.findall(r'"([\w./-]{3,60}\.js)"', js)))[:40]:
    print("CHUNK:", m)
