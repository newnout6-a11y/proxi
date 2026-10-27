"""Local command execution engine for Retool AI agent."""
import re, subprocess

EXEC_REGEX = re.compile(r"<exec_command>(.*?)</exec_command>", re.DOTALL | re.IGNORECASE)

def extract_command(text: str) -> str | None:
    if not text:
        return None
    m = EXEC_REGEX.search(text)
    if m:
        cmd = m.group(1).strip()
        # Clean up any potential markdown backticks inside tags
        cmd = re.sub(r"^```[a-zA-Z]*\n?", "", cmd)
        cmd = re.sub(r"\n?```$", "", cmd)
        return cmd.strip()
    return None

def run_command(cmd: str, cwd: str, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=timeout)
        out = ""
        for encoding in ("utf-8", "cp866", "cp1251", "latin-1"):
            try:
                out = (proc.stdout or b"").decode(encoding)
                err = (proc.stderr or b"").decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            out = (proc.stdout or b"").decode("utf-8", errors="replace")
            err = (proc.stderr or b"").decode("utf-8", errors="replace")

        combined = out.strip()
        if err.strip():
            combined += ("\nSTDERR:\n" if combined else "") + err.strip()

        if not combined:
            combined = "(command executed with no output)"

        if len(combined) > 4000:
            combined = combined[:3500] + f"\n... [truncated {len(combined) - 3500} characters]"

        return proc.returncode, combined
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {timeout} seconds."
    except Exception as e:
        return -1, f"Execution failed: {str(e)}"

def format_command_followup(cmd: str, exit_code: int, output: str) -> str:
    return (
        f"[Command Result for `{cmd}`]:\n"
        f"Exit code: {exit_code}\n"
        f"Output:\n{output}\n\n"
        f"Please proceed with answering the user's request based on this result."
    )
