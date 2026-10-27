import os, subprocess, json

def build_project_context(workspace_root):
    if not workspace_root or not os.path.exists(workspace_root):
        return ""
    
    workspace_root = os.path.abspath(workspace_root)
    lines = [f"=== PROJECT CONTEXT: {workspace_root} ==="]
    
    # 1. Git info
    git_dir = os.path.join(workspace_root, ".git")
    if os.path.exists(git_dir):
        try:
            branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                    cwd=workspace_root, capture_output=True, text=True, timeout=5).stdout.strip()
            last_commit = subprocess.run(["git", "log", "-1", "--oneline"],
                                         cwd=workspace_root, capture_output=True, text=True, timeout=5).stdout.strip()
            status = subprocess.run(["git", "status", "--short"],
                                    cwd=workspace_root, capture_output=True, text=True, timeout=5).stdout.strip()
            lines.append(f"Git Branch: {branch}")
            if last_commit:
                lines.append(f"Last Commit: {last_commit}")
            if status:
                lines.append(f"Git Status:\n{status[:1000]}")
        except Exception:
            pass

    # 2. File tree (up to depth 2, max 60 entries)
    ignored = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".system_generated", ".gemini", "chunks"}
    files_list = []
    try:
        for root, dirs, files in os.walk(workspace_root):
            dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".")]
            rel_dir = os.path.relpath(root, workspace_root)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            if depth > 2:
                dirs.clear()
                continue
            for f in files:
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
        lines.append(f"Workspace Files ({len(files_list)} shown):")
        lines.append(", ".join(files_list[:40]))
        if len(files_list) > 40:
            lines.append(", ".join(files_list[40:]))

    # 3. Key files (README, package.json, requirements.txt)
    for fname in ("README.md", "package.json", "pyproject.toml", "requirements.txt"):
        fpath = os.path.join(workspace_root, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    snippet = f.read(1000).strip()
                    lines.append(f"\n--- {fname} ---\n{snippet}")
            except Exception:
                pass
                
    lines.append("=== END OF PROJECT CONTEXT ===")
    return "\n".join(lines)

print(build_project_context(r"c:\specialinay_papka"))
