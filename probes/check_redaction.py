
import json, pathlib, sys
sys.path.insert(0, r"C:\specialinay_papka")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import executor
from redact import redact

CFG = json.loads(pathlib.Path(r"C:\specialinay_papka\config.json").read_text(encoding="utf-8"))
values = [seg.split("=", 1)[1] for seg in CFG["cookie"].split("; ") if "=" in seg]

code, out = executor.run_command("type cookies.txt", r"C:\specialinay_papka", policy="allow_all", limit=5000)
print("exit:", code, "| raw chars:", len(out))
print("raw has accessToken JWT:", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ4c3JmVG9rZW4i" in out)
masked = redact(out, cookie_values=values)
print("masked has [redacted]:", "[redacted]" in masked)
print("masked still has JWT:", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" in masked)
print("masked sample:", masked[:180].replace("\n", " "))
