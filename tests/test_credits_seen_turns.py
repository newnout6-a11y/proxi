"""The ids a credits manager keeps to count each billed attempt once are bounded: every id of a long run is new."""
import credits
from credits import CreditsManager


def manager():
    value = CreditsManager("https://tenant.invalid", lambda: {})
    value._balance = {"available": 1000.0}
    return value


def test_a_repeated_turn_id_is_counted_once():
    value = manager()
    value.record_turn("turn-a", 0.25)
    value.record_turn("turn-a", 0.25)
    assert value.session_used == 0.25


def test_the_remembered_ids_stay_bounded_over_a_long_run():
    value = manager()
    for number in range(credits.SEEN_TURNS_MAX * 3):
        value.record_turn("turn-%d" % number, 0.001)
    assert len(value._seen_turns) == credits.SEEN_TURNS_MAX
    # the newest ids are still known, so an attempt reported twice in a row still counts once
    used = value.session_used
    value.record_turn("turn-%d" % (credits.SEEN_TURNS_MAX * 3 - 1), 0.001)
    assert value.session_used == used


def test_usage_counts_every_billed_turn_since_start_not_only_the_kept_history():
    value = manager()
    for number in range(credits.HISTORY_MAX + 50):
        value.record_turn("turn-%d" % number, 0.01)
    usage = value.usage()
    assert usage["turns"] == credits.HISTORY_MAX + 50  # it stopped at 200 while session_used kept growing
    assert len(usage["recent"]) <= 50 and round(usage["session_used"], 2) == round(0.01 * (credits.HISTORY_MAX + 50), 2)
