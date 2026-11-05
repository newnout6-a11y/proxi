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
