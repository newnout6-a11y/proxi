
import re, subprocess, sys, time
sys.path.insert(0, r"C:\specialinay_papka")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import executor

# Parent python spawns a long child, then sleeps: only a tree kill stops the child.
cmd = "python -c \"import subprocess,time; c=subprocess.Popen(['ping','-n','40','127.0.0.1']); print(c.pid, flush=True); time.sleep(40)\""
t0 = time.time()
code, output = executor.run_command(cmd, r"C:\specialinay_papka", timeout=4, policy="allow_all")
print("elapsed:", round(time.time() - t0, 1), "s | exit:", code)
print("timed out marker:", "timed out" in output)
pid = int(re.search(r"\d+", output).group(0)) if re.search(r"\d+", output) else 0
print("child pid:", pid)
time.sleep(1.5)
alive = subprocess.run(["tasklist", "/FI", "PID eq " + str(pid)], capture_output=True, text=True).stdout
print("child still alive:", str(pid) in alive)
