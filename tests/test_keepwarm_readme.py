"""Every reason a keepwarm cycle can stop with has a Russian journal text and is named in the README."""
from pathlib import Path

import direct

ROOT = Path(__file__).resolve().parents[1]


def test_every_stop_reason_has_a_russian_text_and_is_named_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert [reason for reason in direct._STOP_TEXT if "`%s`" % reason not in readme] == []
    assert all(text.startswith("Прогрев остановлен") for text in direct._STOP_TEXT.values())
    assert {"last_turn_failed", "cache_expired", "cache_rewritten"} <= set(direct._STOP_TEXT)
