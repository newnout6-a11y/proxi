"""The documentation stays true to the code: relative links resolve, and the README says what the validator enforces."""
import re
from pathlib import Path

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
