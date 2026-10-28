import sys, time, subprocess
sys.path.insert(0, r"C:\specialinay_papka")
import executor
t = time.time()
code, out = executor.run_command("ping -n 90 127.0.0.1", r"C:\specialinay_papka", timeout=3, policy="allow_all", limit=200)
print("exit:", code, "%.1fs" % (time.time()-t), "| tail:", out[-70:].replace(chr(10)," / "))
time.sleep(1.5)
ps = subprocess.run(["powershell","-NoProfile","-Command","(Get-Process ping -ErrorAction SilentlyContinue).Count"], capture_output=True, text=True)
print("leftover ping:", (ps.stdout or "").strip() or "none")
