import sys, time, subprocess
sys.path.insert(0, r"C:\specialinay_papka")
import executor, config
AL = config.DEFAULTS["exec_allowlist"]
for c in ['powershell -NoProfile -Command "Get-ChildItem; Get-Date"',
          'powershell -NoProfile -Command "Get-ChildItem | Select-Object Name"',
          'powershell -NoProfile -Command Get-ChildItem',
          'findstr "a|b" file.txt',
          'git status | findstr a']:
    try:
        executor.check_policy(c, "allowlist", AL); print("ALLOW  ", c)
    except executor.PolicyError as e:
        print("REJECT ", c, "->", str(e)[:60])
t = time.time()
code, out = executor.run_command("ping -n 90 127.0.0.1", r"C:\specialinay_papka", timeout=3, policy="allowlist", allowlist=AL, limit=200)
print("ping exit:", code, "%.1fs" % (time.time()-t), "| tail:", out[-70:].replace(chr(10)," / "))
time.sleep(1.5)
ps = subprocess.run(["powershell","-NoProfile","-Command","(Get-Process ping -ErrorAction SilentlyContinue).Count"], capture_output=True, text=True)
print("leftover ping:", (ps.stdout or "").strip() or "none")
