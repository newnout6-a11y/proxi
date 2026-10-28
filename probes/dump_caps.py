
import pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
blob = (pathlib.Path(__file__).parent / "bundle.js").read_text(encoding="utf-8", errors="ignore")
for pat in ("MODEL_CAPABILITIES", "ANTHROPIC_THINKING_EFFORT_LEVELS", "OPENAI_BASE_REASONING_EFFORT_LEVELS", "GOOGLE_REASONING_EFFORT_LEVELS"):
    i = blob.find(pat + "=")
    print("=====", pat, "at", i)
    if i > 0:
        print(blob[i:i + 900])
    print()
