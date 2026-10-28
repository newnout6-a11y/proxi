
"""Probe 3: executor policy, clean env, timeout tree kill."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import executor

ALLOW = ["^git\\b", "^dir\\b", "^type\\b", "^python\\b", "^echo\\b", "^where\\b"]


def pol(cmd):
    try:
        executor.check_policy(cmd, "allowlist", ALLOW)
        return "ALLOW"
    except executor.PolicyError as exc:
        return "DENY  (" + str(exc)[:60] + ")"


print("== policy verdict ==")
for cmd in ["whoami", "dir", "dir && whoami", "dir\nwhoami", "dir\n& whoami",
            "setx FOO bar", "set X=1 && whoami", "$env:PATH; whoami",
            'python -c "print(1); print(2)"', "echo \"a;b\"",
            "dir > out.txt", "dir 2>&1 | findstr x", "  dir  ",
            "powershell -c whoami", "type C:\\Windows\\win.ini",
            "git status;whoami", "Git status", "dir\r\nwhoami"]:
    print("  %-26r %s" % (cmd, pol(cmd)))

print("== clean env ==")
os.environ["MY_SECRET_TOKEN"] = "supersecret"
os.environ["AWS_SECRET_ACCESS_KEY"] = "awssecret"
os.environ["RELAY_API_KEY"] = "relaykey"
env = executor.clean_env(".")
print("  kept keys:", sorted(env))
print("  MY_SECRET_TOKEN in env:", "MY_SECRET_TOKEN" in env, "| AWS:", "AWS_SECRET_ACCESS_KEY" in env,
      "| RELAY_API_KEY:", "RELAY_API_KEY" in env)
code_, out = executor.run_command("python -c \"import os;print('MY_SECRET_TOKEN' in os.environ, 'AWS_SECRET_ACCESS_KEY' in os.environ, os.getcwd())\"",
                                  os.path.abspath("."), policy="allow_all")
print("  child sees:", code_, out.replace("\n", " | "))
code_, out = executor.run_command("type C:\\Windows\\win.ini", os.path.abspath("."), policy="allowlist", allowlist=ALLOW)
print("  outside-workspace read:", code_, out[:40].replace("\n", " "))

print("== multi-line bypass proof ==")
code_, out = executor.run_command("dir\r\nwhoami", os.path.abspath("."), policy="allowlist", allowlist=["^dir\\b"], limit=200)
print("  policy(^dir only) on 2-line cmd -> exit", code_, "| head:", out[:60].replace("\n", " "))

print("== timeout kills process tree ==")
before = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ping.exe"], capture_output=True, text=True).stdout.count("ping.exe")
t0 = time.time()
code_, out = executor.run_command("cmd /c ping -n 40 127.0.0.1", os.path.abspath("."), timeout=2, policy="allow_all")
took = time.time() - t0
time.sleep(1.0)
after = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ping.exe"], capture_output=True, text=True).stdout.count("ping.exe")
print("  exit=%s took=%.1fs ping_before=%d ping_after=%d" % (code_, took, before, after))
print("  output:", out.replace("\n", " | ")[:160])

print("== timeout of shell that spawns a detached grandchild ==")
t0 = time.time()
code_, out = executor.run_command("python -c \"import subprocess,sys;subprocess.run(['ping','-n','40','127.0.0.1'])\"",
                                  os.path.abspath("."), timeout=2, policy="allow_all")
time.sleep(1.0)
after2 = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ping.exe"], capture_output=True, text=True).stdout.count("ping.exe")
print("  exit=%s took=%.1fs ping_after=%d" % (code_, time.time() - t0, after2))

print("== truncation ==")
code_, out = executor.run_command("python -c \"print('x'*5000)\"", os.path.abspath("."), policy="allow_all", limit=100)
print("  len=%d tail=%r" % (len(out), out[-30:]))

print("== output limit is bypassable via single big chunk? (limit*2 gate) ==")
code_, out = executor.run_command("python -c \"import sys;sys.stdout.write('y'*100000)\"", os.path.abspath("."), policy="allow_all", limit=50)
print("  len=%d" % len(out))

print("== probes/check_kill_tree exists ==")
print("  ", os.path.exists("probes/check_kill_tree.py"))
