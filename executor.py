"""Local command execution with policy, clean environment and process-tree cleanup."""
import json
import os
import re
import subprocess
import threading
import time

EXEC_REGEX = re.compile(r"<exec_command>(.*?)</exec_command>", re.DOTALL | re.IGNORECASE)
SEPARATORS = ("&&", "||", "|", ";", "&", "\n", "\r")
SHELLS = ("powershell", "pwsh", "cmd")
SHELL_FLAGS = ("-command", "-c", "/c", "-encodedcommand", "-e")

# cmdlets allowed inside a powershell/cmd body (head of the inner segment).
CMD_LETS = "|".join((
    "get-content|get-childitem|get-item|get-itemproperty|get-location|get-date|get-command",
    "get-process|get-service|select-string|select-object|sort-object|where-object|foreach-object",
    "measure-object|resolve-path|test-path|write-output|write-host|split-path|join-path",
    "dir|type|more|findstr|where|tasklist|ping|start-sleep|cls|echo|copy|move|mkdir|remove-item",
))
CMD_LETS_RE = re.compile("^(" + CMD_LETS + r")\b", re.IGNORECASE)


def split_segments(cmd: str):
    """Split a command line on shell separators, ignoring ones inside quotes."""
    segments, buf = [], []
    quote = ""
    chars = list(cmd or "")
    i = 0
    while i < len(chars):
        ch = chars[i]
        if quote:
            if ch == quote:
                quote = ""
            buf.append(ch)
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in "|;&\n\r":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _shell_body(segment: str):
    """Return the command body of a powershell/cmd segment, if it has one."""
    parts = segment.split(None, 1)
    if len(parts) < 2:
        return None
    rest = parts[1]
    lowered = rest.lower()
    for flag in SHELL_FLAGS:
        idx = lowered.find(flag)
        if idx == -1:
            continue
        body = rest[idx + len(flag):].strip()
        if body[:1] in ('"', "'") and body[-1:] == body[:1]:
            body = body[1:-1]
        return body.strip() or None
    return None

# Variables kept from the parent environment; everything else is dropped.
ENV_KEEP = ("PATH", "PATHEXT", "SystemRoot", "SystemDrive", "ComSpec", "TEMP", "TMP",
            "WINDIR", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES")

FENCE = chr(96) * 3


class PolicyError(Exception):
    pass


def extract_command(text: str):
    if not text:
        return None
    matches = list(EXEC_REGEX.finditer(text))
    if not matches:
        return None
    # The model sometimes echoes the tag in prose; the last one is the real command.
    cmd = matches[-1].group(1).strip()
    cmd = re.sub(r"^" + FENCE + r"[a-zA-Z]*\n?", "", cmd)
    cmd = re.sub(r"\n?" + FENCE + "$", "", cmd)
    return cmd.strip() or None


def _check_segments(segments, patterns, depth=0) -> None:
    for segment in segments:
        head = segment.split(" ")[0].strip('"')
        if head.startswith("$env:") or head.lower() == "set":
            continue
        shell = head.lower().removesuffix(".exe") in SHELLS
        if not shell and not any(pattern.search(segment) for pattern in patterns):
            raise PolicyError(
                "command not allowlisted: '" + head + "' "
                "(exec_policy=allowlist; add a pattern to exec_allowlist)")
        body = _shell_body(segment) if shell else None
        if body and depth < 3:
            # The shell body must satisfy the policy on its own.
            inner = [s for s in split_segments(body) if s.strip()]
            _check_segments(inner, [CMD_LETS_RE] + patterns, depth + 1)


def check_policy(cmd: str, policy: str, allowlist) -> None:
    if policy == "allow_all":
        return
    patterns = [re.compile(p, re.IGNORECASE) for p in (allowlist or [])]
    _check_segments(split_segments(cmd), patterns)


def clean_env(workspace: str) -> dict:
    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    env["PWD"] = workspace
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
    except Exception:
        pass


def run_command(cmd: str, cwd: str, timeout: int = 30, limit: int = 4000,
                policy: str = "allowlist", allowlist=None, audit_path=None):
    check_policy(cmd, policy, allowlist)
    started = time.time()
    env = clean_env(cwd)
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as exc:
        _audit(audit_path, cmd, -1, 0, str(exc))
        return -1, "Execution failed: " + str(exc)

    collected = {"out": [], "err": [], "size": 0}

    def reader(stream, key):
        while True:
            data = stream.read(2048)
            if not data:
                break
            if collected["size"] < limit * 2:
                collected[key].append(data)
                collected["size"] += len(data)

    threads = [threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True)]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    for thread in threads:
        thread.join(timeout=2)

    out = _decode(b"".join(collected["out"])).strip()
    err = _decode(b"".join(collected["err"])).strip()
    combined = out
    if err:
        combined += ("\nSTDERR:\n" if combined else "") + err
    if timed_out:
        combined += "\n[timed out after " + str(timeout) + "s, process tree killed]"
    if not combined:
        combined = "(no output)"

    truncated_len = 0
    if len(combined) > limit:
        truncated_len = len(combined) - limit
        combined = combined[:limit] + "\n... [truncated " + str(truncated_len) + " chars]"

    code = -1 if timed_out else proc.returncode
    _audit(audit_path, cmd, code, time.time() - started, len(combined))
    return code, combined


def _audit(path, cmd, code, duration, output_len):
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cmd": cmd,
                                     "exit": code, "seconds": round(duration, 2),
                                     "output_chars": output_len}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def format_command_followup(cmd: str, exit_code: int, output: str, history: str = "") -> str:
    parts = []
    if history:
        parts.append(history)
    parts.append("[Command result for '" + cmd + "']\nExit code: " + str(exit_code) + "\nOutput:\n" + output)
    parts.append("Continue the original task using this output. "
                 "Only emit another <exec_command> if more information is needed.")
    return "\n\n".join(parts)
