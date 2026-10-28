
import json, pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).parent
blob = (HERE / "bundle.js").read_text(encoding="utf-8", errors="ignore")
i = blob.find("COPILOT_MODELS={")
chunk = blob[i:i + 4000]
print(chunk[:3000])
