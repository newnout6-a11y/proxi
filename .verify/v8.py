import sys
sys.path.insert(0, r"C:\specialinay_papka")
import executor, config
cases = ["git status\npowershell -c remove-item x", "git status & powershell -c del_x", "dir > out.txt", "cmd /c del x", "git status\ndel x"]
for c in cases:
    try:
        executor.check_policy(c, "allowlist", config.DEFAULTS["exec_allowlist"]); print("ALLOW ", repr(c))
    except executor.PolicyError as e:
        print("REJECT", repr(c), "->", str(e)[:50])
