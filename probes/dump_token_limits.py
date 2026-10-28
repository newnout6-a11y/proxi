
import pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
blob = (pathlib.Path(__file__).parent / "bundle.js").read_text(encoding="utf-8", errors="ignore")
i = blob.find("TOKEN_LIMITS")
print("TOKEN_LIMITS at", i)
print(blob[i:i + 1400] if i > 0 else "(нет)")
m = re.search(r"maxCompletionTokens[^,;]{0,80}", blob)
print("\nmaxCompletionTokens:", m.group(0) if m else "(нет)")
