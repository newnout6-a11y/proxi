"""The command allowlist reads branch/tag refs; only client tools may mutate them."""
import json
from pathlib import Path
import subprocess

import pytest

import config
import executor

ALLOWLISTS = [config.DEFAULTS["exec_allowlist"], json.loads(
    (Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))["exec_allowlist"]]


@pytest.mark.parametrize("allowlist", ALLOWLISTS, ids=["defaults", "example"])
@pytest.mark.parametrize("command", [
    "git branch new", "git tag new", "git branch new HEAD", "git tag new HEAD",
    "git branch --del old", "git tag --del old", "git branch --mov old new", "git branch --cop old new",
    "git branch --no-list new", "git branch --list --no-list new", "git tag --list --no-list new",
    "git branch --list --create-reflog new", "git tag --list --create-reflog new",
    "git branch -lD old", "git tag -ld old", "git branch --track new", "git tag --trailer x=y new",
    "git branch -- new", "git tag -- new", "git branch new --", "git tag new --",
    "git branch --force new", "git tag -f new", "git branch --edit-description old",
])
def test_ref_mutations_and_unknown_options_require_allow_all(command, allowlist):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(command, "allowlist", allowlist)
    executor.check_policy(command, "allow_all", allowlist)


@pytest.mark.parametrize("command", [
    "git branch", "git tag", "git branch -a", "git branch -rvv", "git branch --show-current",
    'git branch --list "feature/*"', "git tag -l v*", "git branch -al feature*",
    "git branch --list -- feature*", "git tag --list -- -odd*", "git branch --contains HEAD",
    "git tag --merged=HEAD", "git branch --points-at HEAD", "git tag --sort=-version:refname",
    "git branch --format=literal --list", "git tag -n3 --list", "git branch --color=never --no-abbrev",
])
def test_known_listing_forms_remain_available(command):
    executor.check_policy(command, "allowlist", config.DEFAULTS["exec_allowlist"])


def test_real_ref_mutations_are_refused_and_listing_leaves_refs_unchanged(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture")
    git("branch", "kept-branch")
    git("tag", "kept-tag")
    before = git("for-each-ref", "--format=%(refname):%(objectname)")
    for command in ("git branch created", "git tag created", "git branch --del kept-branch", "git tag --del kept-tag"):
        with pytest.raises(executor.PolicyError):
            executor.run_command(command, str(tmp_path), policy="allowlist", allowlist=config.DEFAULTS["exec_allowlist"])
        assert git("for-each-ref", "--format=%(refname):%(objectname)") == before
    for command in ("git branch --list kept*", "git tag --list kept*"):
        code, output = executor.run_command(command, str(tmp_path), policy="allowlist",
                                            allowlist=config.DEFAULTS["exec_allowlist"])
        assert code == 0 and "kept" in output
    assert git("for-each-ref", "--format=%(refname):%(objectname)") == before
