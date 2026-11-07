"""Local command execution with policy, clean environment and process-tree cleanup."""
import json
import os
import re
import subprocess
import threading
import time
import uuid

from redact import redact
import tool_names

EXEC_REGEX = re.compile(r"<exec_command>(.*?)</exec_command>", re.DOTALL | re.IGNORECASE)
TOOL_REGEX = re.compile(r"<tool_call\b([^>]*)>(.*?)</tool_call>",
                        re.DOTALL | re.IGNORECASE)
SEPARATORS = ("&&", "||", "|", ";", "&", "\n", "\r")
SHELLS = ("powershell", "pwsh", "cmd")
SHELL_FLAGS = ("-command", "-c", "/c", "-encodedcommand", "-e")

# cmdlets allowed inside a powershell/cmd body (head of the inner segment).
CMD_LETS = "|".join((
    "get-content|get-childitem|get-item|get-itemproperty|get-location|get-date|get-command",
    # No foreach-object: without a script block its -MemberName calls any method (InvokeScript runs any code, Delete and
    # Kill act on what the pipe brings). Select-Object -ExpandProperty reads a property.
    "get-process|get-service|select-string|select-object|sort-object|where-object",
    "measure-object|resolve-path|test-path|write-output|write-host|split-path|join-path",
    "dir|type|more|findstr|where|tasklist|ping|start-sleep|cls|echo",
))
CMD_LETS_RE = re.compile("^(" + CMD_LETS + r")\b", re.IGNORECASE)


def split_segments(cmd: str, powershell=False, reject_redirection=False):
    """Split a command line on shell separators, ignoring ones inside quotes.

    reject_redirection is the allowlist's reading: it refuses a redirection and, on a cmd.exe line, a ';' outside quotes.
    """
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
        if ch == '"' or (powershell and ch == "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if reject_redirection and ch in "<>":
            raise PolicyError("redirection requires allow_all")
        if reject_redirection and not powershell and ch == ";":
            # cmd.exe does not end a command at ';': `powershell -Command Get-Date ; echo (whoami.exe)` gave PowerShell
            # both statements, while a split here judged Get-Date alone as its body and echo as a command of its own
            # (audit of 2026-11-08). Between PowerShell statements a ';' belongs inside the quoted body.
            raise PolicyError("';' outside quotes requires allow_all: cmd.exe does not split commands there "
                              "(join commands with &&, quote a PowerShell body)")
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
    """Return the command body of a powershell/cmd segment, if it has one: the text that shell reads."""
    parts = segment.split(None, 1)
    if len(parts) < 2:
        return None
    shell = parts[0].strip('"').lower().removesuffix(".exe")
    options = r"(?:(?:-noprofile|-noninteractive|-nologo)\s+)*(?:-command|-c)\s+(.+)"
    if shell == "cmd":
        options = r"(?:(?:/d|/s)\s+)*/c\s+(.+)"
    match = re.fullmatch(options, parts[1], re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    body = match.group(1).strip()
    if shell == "cmd":
        # cmd /c drops the first quote and the last one, wherever that is: `cmd /c "dir & calc" x` runs dir, then calc
        # (audit of 2026-11-08). A single quote is no quote to cmd.exe.
        if body[:1] == '"':
            last = body.rfind('"')
            body = body[1:last] + body[last + 1:] if last > 0 else body[1:]
    else:
        # powershell.exe joins its arguments after -Command with blanks, so PowerShell reads the text without the double
        # quotes of the command line: `-Command Write-Output "x ; calc"` ran calc, and `"a > b"` wrote b (audit of
        # 2026-11-08). Single quotes reach PowerShell, which reads them as a string. \" and "" are escapes that the
        # command line parsers read in different ways.
        if '\\"' in body or '""' in body:
            raise PolicyError('\\" and "" in a PowerShell body require allow_all (use single quotes for a string)')
        body = body.replace('"', "")
    return body.strip() or None

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


def extract_tool_call(text: str, allowed=None):
    """Extract the prompt-bridge envelope used for client native tools."""
    if not text:
        return None
    matches = list(TOOL_REGEX.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    attrs = match.group(1) or ""
    raw = match.group(2).strip()
    raw = re.sub(r"^" + FENCE + r"[a-zA-Z]*\n?", "", raw)
    raw = re.sub(r"\n?" + FENCE + "$", "", raw).strip()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    attr_name_match = re.search(r'\b(?:name|function)=["\']?([^"\'\s>]+)["\']?', attrs, re.IGNORECASE)
    attr_id_match = re.search(r'\bid=["\']?([^"\'\s>]+)["\']?', attrs, re.IGNORECASE)
    name = (attr_name_match.group(1) if attr_name_match else None) or value.get("name") or value.get("tool") or value.get("toolName")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    namespace = value.get("namespace")
    if isinstance(namespace, str) and namespace:
        if name.startswith(namespace + "."):
            name = name[len(namespace) + 1:]
        if not name.startswith(tool_names.namespaced(namespace, "")):
            name = tool_names.namespaced(namespace, name)
    if allowed and name not in allowed and "." in name:
        # The dotted form of a namespaced tool ("mcp__exa.web_search_exa") is the one the model reads in the schema
        head, _, tail = name.partition(".")
        if tool_names.namespaced(head, tail) in allowed:
            name = tool_names.namespaced(head, tail)
    if allowed and name not in allowed:
        # Models may use the short function name from a namespace schema.
        # Restore the namespace only when it is unambiguous.
        matches = [candidate for candidate in allowed
                   if candidate.endswith("." + name) or candidate.endswith(tool_names.SEPARATOR + name)]
        if len(matches) == 1:
            name = matches[0]
    if allowed and name not in allowed:
        return None
    if "arguments" in value:
        arguments = value["arguments"]
    elif "input" in value:
        arguments = value["input"]
    elif "parameters" in value:
        arguments = value["parameters"]
    elif attr_name_match:
        json_name = value.get("name") or value.get("tool") or value.get("toolName")
        if json_name and (name == json_name or name.endswith("." + json_name)):
            arguments = {k: v for k, v in value.items() if k not in ("name", "id", "tool", "toolName", "namespace", "tool_call_id")}
        else:
            arguments = {k: v for k, v in value.items() if k not in ("id", "tool_call_id")}
    elif "name" in value:
        arguments = {k: v for k, v in value.items() if k not in ("name", "id", "tool", "toolName", "namespace", "tool_call_id")}
    else:
        arguments = value
    if isinstance(arguments, str):
        try:
            json.loads(arguments)
        except json.JSONDecodeError:
            return None
        encoded = arguments
    else:
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
    call_id = (attr_id_match.group(1) if attr_id_match else None) or value.get("id") or value.get("tool_call_id") or "call_" + uuid.uuid4().hex[:20]
    return {"id": str(call_id),
            "name": name, "arguments": encoded}


def extract_tool_calls(text: str, allowed=None):
    """Preserve a parallel client-tool batch instead of dropping all but its last call."""
    calls = []
    for match in TOOL_REGEX.finditer(text or ""):
        call = extract_tool_call(match.group(0), allowed)
        if call is not None:
            if any(previous["id"] == call["id"] for previous in calls):
                call["id"] = "call_" + uuid.uuid4().hex[:20]
            calls.append(call)
    return calls


def command_tool_call(text: str, tool_names):
    """Map the legacy command envelope to a client shell-like tool."""
    command = extract_command(text)
    if not command:
        return None
    names = set(tool_names or ())
    name = next((actual for candidate in ("bash", "shell", "exec_command", "run_command", "execute_command", "run_shell_command", "terminal", "command")
                 for actual in names if actual.lower() == candidate), None)
    if name is None:
        return None
    field = "cmd" if name.lower() in ("exec_command", "cmd") else "command"
    return {"id": "call_" + uuid.uuid4().hex[:20], "name": name,
            "arguments": json.dumps({field: command}, ensure_ascii=False, separators=(",", ":"))}


GIT_BLOCKED_RE = re.compile(
    r'(?:^|\s)-[cC]'                       # -c/-C incl. glued "-ccore.pager=cmd" and quoted "-c"
    r'|(?:^|\s)(?:config|alias|credential|submodule)\b'
    r'|(?:^|\s)(?:apply|am|bisect|daemon|difftool|fast-import|filter-branch)\b'
    r'|--upload-pack|--receive-pack'         # transport helpers execute arbitrary commands
    r'|ext::|--exec-path'
    r'|--config-env'                        # --config-env=core.fsmonitor=COMSPEC runs a program
    r'|--open-files-in-pager'               # git grep runs the named pager
    r'|--output(?:\s|=)'                    # git log/diff --output writes any path
    r'|--git-dir|--work-tree',              # operate on repositories outside the workspace
    re.IGNORECASE)
# Case-sensitive: `git grep -O<prog>` runs <prog>, `git grep -o` is a legit flag.
GIT_PAGER_RE = re.compile(r'(?:^|\s)-[a-zA-Z]*O')
# Under the allowlist git runs only the subcommands that read, plus fetch and push to the configured remotes. A list of what
# to refuse kept missing a spelling: rebase -x, ls-remote/archive/fetch-pack/push --exec, clone -u and clone --config
# core.sshCommand all ran any program the model wrote (audit of 2026-11-07), and commit/reset/checkout/clean change the
# user's work, which the relay's local commands are not for (README: edits go through the client's own tools). allow_all
# runs any of them. push is not here either: a model steered by text it read could send the repository with its history
# to any address (decided by the owner on 2026-11-08); fetch stays, from a configured remote only (_git_fetch_source).
GIT_READ_ONLY = frozenset((
    "status", "log", "diff", "show", "blame", "annotate", "grep", "ls-files", "ls-tree", "rev-parse", "rev-list",
    "describe", "branch", "tag", "shortlog", "reflog", "cat-file", "show-ref", "name-rev", "merge-base", "count-objects",
    "whatchanged", "cherry", "range-diff", "check-ignore", "check-attr", "version", "help", "fetch"))
# A word of `git fetch` that names a place instead of a configured remote: a URL, user@host:path or host.tld:path, a UNC or
# absolute path, a relative path. A fetch from such a place reaches any server the model names (or the local network, or
# an SMB share that takes the user's credentials) from this machine.
GIT_FETCH_PLACE_RE = re.compile(r"://|@|^[\\/~.]|^[A-Za-z]:[\\/]|^[^:/\\]*\.[^:/\\]*:")
# --exec is the short name of --upload-pack/--receive-pack for push, fetch and the transport helpers: it runs a program.
GIT_EXEC_RE = re.compile(r'(?:^|\s)--exec\b', re.IGNORECASE)
# git takes any unique prefix of a long option: `fetch --upload=<cmd>` is --upload-pack, `push --exe=<cmd>` and
# `push --rece=<cmd>` are --exec and --receive-pack, `grep --open=<cmd>` is --open-files-in-pager, and each ran the program
# (audit of 2026-11-07). The patterns above name the whole option; a long option that can be the start of one of these is
# refused here. No option git reads is a prefix of these names but the names themselves.
GIT_DANGEROUS_LONG = ("upload-pack", "receive-pack", "exec", "exec-path", "open-files-in-pager", "output", "config-env",
                      "git-dir", "work-tree")


def _git_dangerous_option(words):
    """The first long option of a git segment that is a dangerous option or a prefix of one, else None."""
    for word in words[1:]:
        if word.startswith("--") and len(word) > 2:
            name = word[2:].split("=", 1)[0].lower()
            if name and any(dangerous.startswith(name) for dangerous in GIT_DANGEROUS_LONG):
                return word.split("=", 1)[0]
    return None
# branch and tag list by default; these flags rename, delete, create or rewrite (-a lists every branch but annotates a tag).
GIT_LIST_ONLY_RE = {
    "branch": re.compile(r'(?:^|\s)(?:-[a-zA-Z]*[dDmMfu]\b|--(?:delete|move|copy|force|set-upstream-to|unset-upstream|'
                         r'edit-description|create-reflog)\b)'),
    "tag": re.compile(r'(?:^|\s)(?:-[a-zA-Z]*[asudmfFe]\b|--(?:annotate|sign|local-user|delete|message|file|force|'
                      r'edit|create-reflog)\b)'),
}
# A PowerShell provider drive that is no file system: variables (an object reaches every command), functions, the
# environment, the registry.
PS_PROVIDER_RE = re.compile(r"""(?:^|[\s'"=,:])(?:variable|function|env|alias|cert|wsman|hklm|hkcu|registry)::?""",
                            re.IGNORECASE)


def _git_subcommand(words):
    """The subcommand of a git segment: the first word after git that is no option ("" when there is none)."""
    for word in words[1:]:
        if not word.startswith("-"):
            return word.lower()
    return ""


def _git_fetch_source(words):
    """The first word after `git fetch` that names a place rather than a configured remote, else None."""
    after = words[words.index(next(word for word in words[1:] if word.lower() == "fetch")) + 1:]
    for word in after:
        if not word.startswith("-") and GIT_FETCH_PLACE_RE.search(word):
            return word
    return None


# The extensions under which Windows runs a program named without one: git.cmd is git, git-lfs.exe is git-lfs.
PROGRAM_EXTENSIONS = (".exe", ".cmd", ".bat", ".com")


def _program_name(head):
    """The program a segment runs, as the policy names it: the last part of the path, lower case, without the extension."""
    base = os.path.basename(head.lower())
    for extension in PROGRAM_EXTENSIONS:
        if base.endswith(extension):
            return base[:-len(extension)]
    return base


def _allowlisted(pattern, segment, head):
    """Whether `pattern` allows the segment. `\\b` ends a word at '-' and '.', so `^git\\b` let `git-lfs` through (and `^rg\\b`
    `rg-anything`): a pattern that matches only the start of the program's name allows nothing (audit of 2026-11-08:
    `git-lfs clone -u <program>` ran the program, the git rules never looked at it). A name followed by one of the
    PROGRAM_EXTENSIONS is the program itself."""
    for text in (segment, head):
        match = pattern.match(text)
        if match is None:
            continue
        if match.end() >= len(head):
            return True  # the pattern took the whole name (and maybe more of the segment)
        rest = head[match.end():].lower()
        if rest[:1] not in ("-", ".") or rest in PROGRAM_EXTENSIONS:
            return True
    return False

GLOBAL_BLOCKED_RE = re.compile(r'(?:^|\s)--pre(?:\s|=)', re.IGNORECASE)  # rg preprocessor = arbitrary exec


def _check_segments(segments, patterns, depth=0) -> None:
    for segment in segments:
        parts = segment.split()
        head = parts[0].strip('"') if parts else ""
        # git.cmd is git and pwsh.exe is pwsh to Windows, so they are to the rules too
        base = _program_name(head)
        # Quote-stripped copy: cmd strips quotes, so `git "-c" x=y` must be judged
        # the same as the bare flag. Deliberate over-blocks: `git diff -C`, and any
        # argument merely containing a blocked word.
        flattened = segment.replace('"', "")
        if base == "git" and (GIT_BLOCKED_RE.search(flattened) or GIT_PAGER_RE.search(flattened)):
            raise PolicyError("git command/config overrides require allow_all")
        if base == "git":
            words = flattened.split()
            subcommand = _git_subcommand(words)
            if subcommand not in GIT_READ_ONLY or GIT_EXEC_RE.search(flattened):
                raise PolicyError("git %s requires allow_all: the allowlist runs only git commands that read, and fetch "
                                  "from a configured remote, without --exec (%s)"
                                  % (subcommand or "without a subcommand", ", ".join(sorted(GIT_READ_ONLY))))
            dangerous = _git_dangerous_option(words)
            if dangerous:
                raise PolicyError("git option %s requires allow_all: git takes it for an option that runs a program or "
                                  "writes a file" % dangerous)
            if subcommand == "fetch":
                place = _git_fetch_source(words)
                if place:
                    raise PolicyError("git fetch from %s requires allow_all: the allowlist fetches only from a configured "
                                      "remote, by its name (git fetch origin)" % place)
            if (subcommand in GIT_LIST_ONLY_RE
                    and GIT_LIST_ONLY_RE[subcommand].search(" ".join(words[words.index(subcommand) + 1:]))):
                raise PolicyError("git %s may only list under the allowlist" % subcommand)
        if GLOBAL_BLOCKED_RE.search(flattened):
            raise PolicyError("command preprocessing flags require allow_all")
        shell = base in SHELLS
        # Anchored at the segment start: narrow rules like `^git (status|log)\b`
        # work again, and a pattern still cannot match a word mid-segment.
        if not any(_allowlisted(pattern, segment, head) for pattern in patterns):
            raise PolicyError(
                "command not allowlisted: '" + head + "' "
                "(exec_policy=allowlist; add a pattern to exec_allowlist)")
        body = _shell_body(segment) if shell else None
        if shell and (not body or depth >= 3):
            raise PolicyError("only explicit -Command/-c or cmd /c bodies are supported by allowlist")
        if body:
            # The shell body must satisfy the policy on its own.
            ps = head.lower().removesuffix(".exe") != "cmd"
            # $ anywhere, not only at the start of a statement: $ExecutionContext in the middle of a segment reached every
            # command. A provider drive that is no file system reads what is no file.
            if ps and (re.search(r"[{}()\x60$]", body) or PS_PROVIDER_RE.search(body)):
                raise PolicyError("PowerShell script blocks, variables, expansion and provider drives require allow_all")
            inner = split_segments(body, powershell=ps, reject_redirection=True)
            _check_segments(inner, [CMD_LETS_RE] if ps else patterns, depth + 1)


def check_policy(cmd: str, policy: str, allowlist) -> None:
    if policy == "allow_all":
        return
    if policy != "allowlist":
        raise PolicyError("local command execution is disabled")
    if not cmd or re.search(r"[%!^]", cmd):
        raise PolicyError("cmd expansion/escaping is not supported by allowlist")
    if re.search(r"[\r\n]", cmd):
        # cmd.exe runs a line up to its first line feed, inside quotes too, and drops every carriage return: the lines
        # after the first were judged and never ran, and a CR glued the next line onto a PowerShell body that PowerShell
        # then evaluated unchecked (audit of 2026-11-08).
        raise PolicyError("a line break requires allow_all: cmd.exe runs only the first line (join commands with &&)")
    patterns = [re.compile(p, re.IGNORECASE) for p in (allowlist or [])]
    _check_segments(split_segments(cmd, reject_redirection=True), patterns)


def clean_env(workspace: str) -> dict:
    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    env["PWD"] = workspace
    env["PYTHONIOENCODING"] = "utf-8"
    # A hostile workspace shipping git.bat/cmd.bat/powershell.bat must not shadow
    # the allowlisted executables via the current-directory search.
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            if encoding == "utf-8" and exc.reason == "unexpected end of data" and exc.end == len(raw):
                # A bounded capture can stop halfway through the final UTF-8
                # character. Preserve the valid prefix instead of decoding all
                # UTF-8 output as a legacy Windows code page.
                return raw[:exc.start].decode("utf-8")
            continue
    return raw.decode("utf-8", "replace")


def _kill_tree(pid: int) -> bool:
    try:
        result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def run_command(cmd: str, cwd: str, timeout: int = 30, limit: int = 4000,
                policy: str = "allowlist", allowlist=None, audit_path=None, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        return -1, "[cancelled before execution]"
    check_policy(cmd, policy, allowlist)
    started = time.time()
    env = clean_env(cwd)
    try:
        # stdin: the command reads nothing. It used to share the relay's console: `findstr x` without a file read what the
        # operator typed there, or waited for input until command_timeout with the turn and its slot held (audit 2026-11-08).
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as exc:
        _audit(audit_path, cmd, -1, 0.0, 0, error=str(exc))
        return -1, "Execution failed: " + str(exc)

    collected = {"out": [], "err": [], "size": 0}

    def reader(stream, key):
        while True:
            data = stream.read(2048)
            if not data:
                break
            # Output limits are characters; UTF-8 can require four bytes each.
            # Keep one extra character so truncation can be reported accurately.
            if limit <= 0 or collected["size"] < (limit + 1) * 4:
                collected[key].append(data)
                collected["size"] += len(data)

    threads = [threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True)]
    for thread in threads:
        thread.start()

    timed_out = False
    cancelled = False
    tree_killed = False
    try:
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                raise subprocess.TimeoutExpired(cmd, timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                proc.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
    except subprocess.TimeoutExpired:
        timed_out = not cancelled
        tree_killed = _kill_tree(proc.pid)
        if not tree_killed and proc.poll() is None:
            proc.kill()
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
        combined += "\n[timed out after " + str(timeout) + "s; " + (
            "process tree killed]" if tree_killed else "process termination requested]")
    if cancelled:
        combined += "\n[cancelled; " + ("process tree killed]" if tree_killed else "process termination requested]")
    if not combined:
        combined = "(no output)"

    truncated_len = 0
    if limit > 0 and len(combined) > limit:
        truncated_len = len(combined) - limit
        combined = combined[:limit] + "\n... [truncated " + str(truncated_len) + " chars]"

    code = -1 if timed_out or cancelled else proc.returncode
    _audit(audit_path, cmd, code, time.time() - started, len(combined))
    return code, combined


def _audit(path, cmd, code, duration, output_len, error=None):
    if not path:
        return
    try:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cmd": redact(cmd),
                  "exit": code, "seconds": round(duration, 2), "output_chars": output_len}
        if error:
            record["error"] = str(error)[:300]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
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
