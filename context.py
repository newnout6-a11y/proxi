"""Project context and system instruction builder."""
import os, subprocess

def build_project_context(workspace_root: str) -> str:
    if not workspace_root or not os.path.exists(workspace_root):
        return ""

    workspace_root = os.path.abspath(workspace_root)
    lines = [f"[PROJECT CONTEXT: {workspace_root}]"]

    # 1. Git info
    git_dir = os.path.join(workspace_root, ".git")
    if os.path.exists(git_dir):
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace_root, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            last_commit = subprocess.run(
                ["git", "log", "-1", "--oneline"],
                cwd=workspace_root, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=workspace_root, capture_output=True, text=True, timeout=5
            ).stdout.strip()

            if branch:
                lines.append(f"Git Branch: {branch}")
            if last_commit:
                lines.append(f"Last Commit: {last_commit}")
            if status:
                lines.append(f"Git Status:\n{status[:800]}")
            else:
                lines.append("Git Status: clean")
        except Exception:
            pass

    # 2. File tree (up to depth 2, max 60 entries)
    ignored = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        ".system_generated", ".gemini", "chunks", "probes", "experiments", ".idea", ".vscode"
    }
    files_list = []
    try:
        for root, dirs, files in os.walk(workspace_root):
            dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".")]
            rel_dir = os.path.relpath(root, workspace_root)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            if depth > 2:
                dirs.clear()
                continue
            for f in sorted(files):
                if f.startswith(".") or f.endswith((".pyc", ".tmp", ".log")):
                    continue
                rel_path = f if rel_dir == "." else os.path.join(rel_dir, f)
                files_list.append(rel_path.replace("\\", "/"))
                if len(files_list) >= 60:
                    break
            if len(files_list) >= 60:
                break
    except Exception:
        pass

    if files_list:
        lines.append(f"Files ({len(files_list)} shown): {', '.join(files_list[:40])}")
        if len(files_list) > 40:
            lines.append(f"More files: {', '.join(files_list[40:])}")

    # 3. Key documentation / configuration files
    for fname in ("README.md", "PROTOCOL.md", "package.json", "pyproject.toml", "requirements.txt"):
        fpath = os.path.join(workspace_root, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    snippet = f.read(1200).strip()
                    lines.append(f"\n--- {fname} ---\n{snippet}")
            except Exception:
                pass

    lines.append("[END OF PROJECT CONTEXT]")
    return "\n".join(lines)


def build_system_instructions(workspace_root: str, enable_context=True, enable_local_exec=True) -> str:
    parts = []
    if enable_context and workspace_root:
        ctx = build_project_context(workspace_root)
        if ctx:
            parts.append(ctx)

    if enable_local_exec and workspace_root:
        parts.append(
            f"[LOCAL EXECUTION INSTRUCTION]\n"
            f"You are connected to the user's local PC via an automated execution bridge.\n"
            f"Workspace directory: {os.path.abspath(workspace_root)}\n"
            f"Operating System: Windows\n"
            f"You have the capability to run local shell commands on the user's PC:\n"
            f"<exec_command>command to run</exec_command>\n"
            f"Whenever you need to inspect files, check git history, run tests, or execute scripts, "
            f"output the command inside <exec_command>...</exec_command> tags. "
            f"The bridge will execute it and provide you the command output in the next turn."
        )

    return "\n\n".join(parts)
