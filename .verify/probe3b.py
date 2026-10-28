
import os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import executor

# 1) second line really executes?
code_, out = executor.run_command("dir /b\r\nwhoami", os.path.abspath("."), policy="allowlist",
                                  allowlist=["^dir\\b"], limit=400)
print("multiline exit", code_)
print(out)

# 2) secret outside the workspace
secret = r"C:\Windows\Temp\verify_secret.txt"
open(secret, "w").write("TOPSECRET=1")
code_, out = executor.run_command("type " + secret, os.path.abspath("."), policy="allowlist",
                                  allowlist=["^type\\b"], limit=200)
print("outside read exit", code_, "->", out.strip()[:60])
code_, out = executor.run_command("python -c \"import os;print(list(os.environ))\"", os.path.abspath("."), policy="allow_all", limit=400)
print("child env keys count", out.count(","))
os.remove(secret)
