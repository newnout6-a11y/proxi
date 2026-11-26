"""The repository guard looks at actual Git paths, including force-added ignored files."""

import subprocess

import pytest

from scripts import check_repo_hygiene as hygiene


@pytest.mark.parametrize("path", [
    "AGENTS.md", "config.example.json", "proxy.py", ".github/workflows/ci.yml", "catalog/model_metadata.py",
    "drivers/new/provider.py", "docs/reports/27 ноября итог.md", "scripts/integrations/session.js",
    "tests/fixtures/balance-response.example.json", "tests/fixtures/stream.jsonl", "drivers/.env.example",
    "web/dist/assets/index-123.js", "web/dist/assets/tokens.css", "web/src/prompts/Conditions.jsx",
    "web/package-lock.json", "web/extra/uv.lock",
])
def test_versioned_sources_reports_fixtures_and_release_artifacts_have_a_home(path):
    assert hygiene.violation(path) is None


@pytest.mark.parametrize("path", [
    "docs/.env", "docs/.ENV.local", "tests/data/CONFIG.JSON", "docs/client/retool_models.auto.json",
    "tests/data/tenant.json.auth-id.lock", "docs/reports/very-old.bAk_before_update", "docs/report.backup_02.json",
    "scripts/data/debug.log.1", "docs/reports/journal.jsonl.3", "web/other/task.tmp", "web/src/tokens.css",
    "web/archive/test-results/trace.zip", "docs/.claude/custom.md", "web/feature/node_modules/module/index.js",
    "logs/requests/notes.md", "docs/cache/__PYCACHE__/data.bin", "scripts/.tmp/dump.json", "docs/.verify-case/x.bin",
])
def test_local_secrets_backups_and_generated_files_are_refused_under_valid_roots(path):
    assert "local" in hygiene.violation(path)


@pytest.mark.parametrize("path", ["agent-audit.md", "new_unreviewed_module.py", "one/agent-output.md", "NewArea/a.py"])
def test_unknown_root_files_and_directories_require_an_explicit_home(path):
    assert "not allowlisted" in hygiene.violation(path)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def create(root, path, body="fixture\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_index_and_worktree_scope_use_projects_git_ignores(tmp_path, monkeypatch, capsys):
    git(tmp_path, "init", "-q")
    create(tmp_path, ".gitignore", ".tmp/\n*.bak\n")
    create(tmp_path, "README.md")
    create(tmp_path, "docs/reports/Ночная проверка.md")
    create(tmp_path, "web/dist/assets/index-123.js")
    create(tmp_path, "Отчёт агента.md")
    create(tmp_path, ".tmp/черновик.md")
    create(tmp_path, "tests/fixtures/private.bak")
    git(tmp_path, "add", "--", ".gitignore", "README.md", "docs", "web")
    monkeypatch.setattr(hygiene, "ROOT", tmp_path)

    assert hygiene.main([]) == 0
    assert hygiene.main(["--worktree"]) == 1
    output = capsys.readouterr()
    assert "Отчёт агента.md" in output.err and ".tmp" not in output.err and "private.bak" not in output.err

    git(tmp_path, "add", "-f", "--", "tests/fixtures/private.bak")
    assert hygiene.main([]) == 1
    assert "tests/fixtures/private.bak" in capsys.readouterr().err


def test_pre_commit_blocks_misplaced_files_before_creating_a_commit(tmp_path):
    git(tmp_path, "init", "-q")
    create(tmp_path, "scripts/check_repo_hygiene.py", hygiene.ROOT.joinpath(
        "scripts/check_repo_hygiene.py").read_text(encoding="utf-8"))
    hook = tmp_path / "scripts/hooks/pre-commit"
    hook.parent.mkdir()
    hook.write_bytes(hygiene.ROOT.joinpath("scripts/hooks/pre-commit").read_bytes())
    hook.chmod(0o755)
    git(tmp_path, "config", "core.hooksPath", "scripts/hooks")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    create(tmp_path, "agent-scratch.md")
    git(tmp_path, "add", "scripts")
    command = ["git", "-C", str(tmp_path), "commit", "--no-gpg-sign", "-m", "Fixture"]
    refused = subprocess.run(command, check=False, capture_output=True)
    assert refused.returncode == 1 and b"agent-scratch.md" in refused.stderr
    draft = tmp_path / "docs/reports/agent-scratch.md"
    draft.parent.mkdir(parents=True)
    (tmp_path / "agent-scratch.md").rename(draft)
    git(tmp_path, "add", "docs")
    subprocess.run(command, check=True, capture_output=True)
