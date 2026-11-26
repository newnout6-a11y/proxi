"""Check Git paths for misplaced root files, private state and generated clutter."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]

# Existing relay entry points and modules stay in place. Put new files in the appropriate
# package, or review an explicit addition here when introducing a new root entry point.
ROOT_FILES = frozenset("""
    .gitattributes .gitignore AGENTS.md PROTOCOL.md README.md config.example.json
    minara_models.json prompts.example.json pytest.ini requirements.txt ruff.toml
    account_report.py accounts.py adapters.py admin.py apierr.py auth.py client_context.py
    client_sync.py compaction_tools.py config.py console_clock.py console_log.py console_records.py
    context.py conversation_infer.py conversation_state.py cost_guard.py credits.py direct.py
    executor.py forced_choice.py generation.py io_workers.py journal.py minara.py minara_accounts.py
    minara_catalog.py minara_probe.py minara_refresh.py models.py native_history.py orchestrator.py
    prompt_body.py prompt_policy.py prompt_stabilizer.py proxy.py redact.py retool_catalog.py
    retool_plans.py retool_refresh.py session.py sse.py stream_watchdog.py structured.py
    summary_guard.py tool_names.py tool_search_policy.py tool_union.py upstream_attempts.py
    upstream_diagnostics.py upstream_refresh.py validate.py web_search.py
""".split())
ROOT_DIRECTORIES = frozenset({".github", "catalog", "docs", "drivers", "scripts", "tests", "web"})
ROOT_LOCAL_DIRECTORIES = frozenset({"conversations", "logs", "probes", "prompts", "state"})
LOCAL_DIRECTORIES = frozenset({
    ".agents", ".cache", ".claude", ".codex", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tmp", ".venv", ".zcode", "__pycache__", "node_modules", "playwright-report", "temp", "test-results", "tmp",
})
LOCAL_FILES = frozenset({
    "config.json", "cookies.txt", "cost_guard.auto.json", "minara_models.auto.json", "models_dev.cache.json",
    "prompts.json", "provider_models.auto.json", "relay.pid", "relay_key.txt", "retool_models.auto.json",
    "retool_plans.auto.json", "retool_plans.auto.json.bad",
})
ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template"})
ARTIFACT_NAME = re.compile(
    r"\.(?:bak|backup)(?:[._~-]|$)|\.(?:log(?:\.\d+)?|tmp|swp|pyc|pyo|pyd|orig|rej)$"
    r"|\.json(?:\.auth-[^/]+)?\.lock$|^journal\.jsonl(?:\.\d+)?$"
)


def violation(path):
    """Return a placement or artifact rule violated by a Git-relative path."""
    parts = path.split("/")
    folded = [part.casefold() for part in parts]
    filename = folded[-1]
    if (any(part in LOCAL_DIRECTORIES or part.startswith(".verify") for part in folded)
            or (len(parts) > 1 and folded[0] in ROOT_LOCAL_DIRECTORIES)):
        return "local state directory; keep private artifacts outside Git or in .tmp/"
    if (filename in LOCAL_FILES or ARTIFACT_NAME.search(filename)
            or (filename not in ENV_EXAMPLES and (filename == ".env" or filename.startswith(".env.")))
            or folded == ["web", "src", "tokens.css"]):
        return "local, generated or backup file; keep it outside Git or in .tmp/"
    if len(parts) == 1 and path not in ROOT_FILES:
        return "root file is not allowlisted; use its package or docs/reports/"
    if len(parts) > 1 and parts[0] not in ROOT_DIRECTORIES:
        return "root directory is not allowlisted; use an existing area or document a new one"
    return None


def git_paths(root, *options):
    # NUL-separated paths preserve spaces, Unicode and characters Git would quote in text mode.
    result = subprocess.run(["git", "-C", os.fspath(root), "ls-files", "-z", *options, "--"],
                            check=True, stdout=subprocess.PIPE)
    return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}


def find_violations(root, *, worktree=False):
    paths = git_paths(root, "--cached")
    if worktree:
        paths.update(git_paths(root, "--others", "--exclude-standard"))
    return [(path, reason) for path in sorted(paths) if (reason := violation(path)) is not None]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", action="store_true", help="also check untracked files that Git does not ignore")
    args = parser.parse_args(argv)
    try:
        problems = find_violations(ROOT, worktree=args.worktree)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Repository hygiene needs a Git checkout: {exc}", file=sys.stderr)
        return 2
    if problems:
        for path, reason in problems:
            print(f"{path!r}: {reason}", file=sys.stderr)
        return 1
    print("Repository hygiene: OK (index and unignored worktree paths)" if args.worktree
          else "Repository hygiene: OK (Git index)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
