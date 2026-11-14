"""Every reason a keepwarm cycle can stop with has a Russian journal text and is named in the README.

The README also names the numbers keepwarm works with (the ceiling of the interval, the price of a rewrite, when the cycle
stops because pings fail): they are pinned here against the scheduler, the admin form and credits.
"""
import re
from pathlib import Path

import credits
import direct

ROOT = Path(__file__).resolve().parents[1]


def readme():
    return " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())


def test_every_stop_reason_has_a_russian_text_and_is_named_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert [reason for reason in direct._STOP_TEXT if "`%s`" % reason not in readme] == []
    assert all(text.startswith("Прогрев остановлен") for text in direct._STOP_TEXT.values())
    assert {"last_turn_failed", "cache_expired", "cache_rewritten"} <= set(direct._STOP_TEXT)


def test_the_ceiling_of_the_interval_and_of_the_first_ping_is_275_seconds_in_the_scheduler_the_admin_and_the_readme():
    import admin
    ceiling = int(direct.PING_LATEST)
    assert ceiling == 275 and direct.PING_LATEST < direct.PING_DEADLINE
    schema = admin._schema()
    assert schema["cache_keepwarm_seconds"]["max"] == schema["cache_keepwarm_turn_offset"]["max"] == ceiling
    text = readme()
    assert "[30, 275]" in text and "тоже не больше 275 с" in text
    assert "Админка даёт задать не больше 275 с" in text


def test_five_failed_pings_fit_the_cache_window_only_for_a_short_interval():
    # The README says the reason `failures` is out of reach with the default 240 s: the retries come PING_RETRY * 2 ** n
    # seconds after the failures and each must still start inside the window of the last confirmed touch.
    delays = [direct.PING_RETRY * 2 ** number for number in range(4)]                      # 10, 20, 40, 80
    assert delays == [10.0, 20.0, 40.0, 80.0]
    assert 240 + sum(delays[:3]) > direct.PING_DEADLINE                # the default interval: the third retry is too late
    assert direct.PING_DEADLINE - sum(delays) == 130                   # all five attempts fit an interval of about 130 s
    text = readme()
    for promise in ("`failures`", "`cache_expired` с `after_failure`", "около 130 с и меньше", "10, 20, 40 и 80 с"):
        assert promise in text, promise


def test_the_prices_the_readme_gives_for_reading_and_rewriting_the_prefix():
    # credits.py holds the rates of Opus 5.5 (read 4e-6, write 1e-4: 25 times); the README adds Sonnet 5.5, whose write price
    # is 5e-5 (12.5 times), as the least-squares fit of the live journal gives it (1334 Sonnet turns, 63 Opus turns).
    assert credits.RATE_CACHE_READ == 4e-6 and credits.RATE_CACHE_WRITE == 1e-4
    assert round(credits.RATE_CACHE_WRITE / credits.RATE_CACHE_READ) == 25
    text = readme()
    for promise in ("`4e-6`", "`1e-4`", "`5e-5`", "12,5 раза", "Sonnet 5.5", "Opus 5.5"):
        assert promise in text, promise
    # "25 times dearer" is true of Opus only: every mention sits next to its model
    for match in re.finditer(r"в 25 раз", text):
        assert "Opus" in text[max(0, match.start() - 140):match.end() + 60], text[match.start() - 60:match.end() + 60]


def test_the_stop_reasons_the_readme_lists_after_a_rewrite_say_the_mark_lives_on_the_session():
    text = readme()
    assert "потеря ограничена одной перезаписью на сессию" in text
    assert "`compacted`" in text and "compacted" in direct._STOP_TEXT

