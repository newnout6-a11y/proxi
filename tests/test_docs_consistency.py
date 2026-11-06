"""The documentation stays true to the code: relative links resolve, and the README says what the validator enforces."""
import json
import re
from pathlib import Path

import pytest

import apierr
import config

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "PROTOCOL.md", ROOT / "web" / "DESIGN.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def test_relative_links_in_the_docs_point_at_existing_files():
    missing = []
    for doc in DOCS:
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue  # an address, or an anchor of the same page
            path = target.split("#", 1)[0]
            if not (doc.parent / path).resolve().exists():
                missing.append(f"{doc.relative_to(ROOT).as_posix()}: {target}")
    assert missing == []


def load_with(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_readme_rows_of_the_dump_limit_say_what_config_load_enforces(tmp_path, monkeypatch):
    # 0 keeps every set; a negative value is an error at start, not another way to say "no limit"
    assert load_with(tmp_path, monkeypatch, upstream_error_max_dumps=0)["upstream_error_max_dumps"] == 0
    with pytest.raises(apierr.ApiError, match="non-negative"):
        load_with(tmp_path, monkeypatch, upstream_error_max_dumps=-1)
    rows = [line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| upstream_error_max_dumps |")]
    assert rows
    for row in rows:
        assert "или меньше" not in row and "отрицательное значение relay не принимает" in row, row


def test_the_example_config_notes_say_what_the_code_does_now():
    # config.example.json is copied by people: its notes are the first documentation of a setting they read.
    import direct
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert "%d с" % int(direct.PING_LATEST) in example["cache_keepwarm_note"]
    assert "%d" % int(direct.PING_LATEST) in example["cache_keepwarm_turn_note"]
    assert "280" in example["cache_keepwarm_turn_note"] and int(direct.PING_DEADLINE) == 280
    assert "prompt-pins" in example["conversation_state_note"]
    # the legacy role reads nothing of a profile and a profile reads nothing of the role
    assert "только на прежнем пути" in example["relay_system_prompt_note"]
    assert "relay_texts" in example["relay_system_prompt_note"]
    assert "prompts/" in example["prompts_file_note"] and "regex" in example["prompts_file_note"]


def test_the_leftovers_of_saving_the_prompts_file_stay_out_of_git():
    # a save leaves the previous version beside the file (prompts.json.bak) and, if it was interrupted, a temporary one
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/prompts.json" in ignored and "/prompts/" in ignored
    assert "/prompts.json.bak" in ignored and "/.prompts-*.tmp" in ignored
