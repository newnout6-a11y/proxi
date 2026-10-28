import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import executor

def run(cmd, allow=("^echo\\b", "^dir\\b", "^type\\b"), limit=300):
    try:
        return executor.run_command(cmd, os.path.abspath("."), policy="allowlist", allowlist=list(allow), limit=limit)
    except executor.PolicyError as e:
        return ("POLICY", str(e)[:50])

for cmd in ["echo A", "echo A\r\necho B", "echo A\necho B", "echo A & echo B", "echo A;echo B"]:
    print("%-22r ->" % cmd, run(cmd))

home = os.path.expanduser("~")
p = os.path.join(home, "verify_outside_secret.txt")
with open(p, "w", encoding="utf-8") as fh:
    fh.write("TOPSECRET=1\n")
print("exists:", os.path.exists(p))
print("read via type:", run("type " + p, allow=("^type\\b",)))
os.remove(p)