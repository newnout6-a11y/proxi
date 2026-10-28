
import pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).parent
blob = ""
for f in list((HERE / "chunks").glob("*.js")) + [HERE / "bundle.js"]:
    t = f.read_text(encoding="utf-8", errors="ignore")
    if "COPILOT_MODEL" in t:
        blob = t
        print("FILE:", f.name, "len", len(t))
        break
for m in list(re.finditer(r"COPILOT_MODEL[A-Z0-9_]*", blob))[:3]:
    print("---- at", m.start(), m.group(0))
    print(blob[max(0, m.start() - 200):m.start() + 2500])
    print()
