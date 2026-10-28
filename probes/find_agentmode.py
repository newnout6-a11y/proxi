
import pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
blob = (pathlib.Path(__file__).parent / "bundle.js").read_text(encoding="utf-8", errors="ignore")
for m in list(re.finditer(r"agentMode", blob))[:12]:
    print("---", m.start())
    print(blob[max(0, m.start() - 260):m.start() + 260].replace("\n", " "))
    print()
