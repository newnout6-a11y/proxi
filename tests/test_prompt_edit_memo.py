"""A regular expression edit gives a text the same result on every turn, however busy the machine is.

The history comes back whole with every request, and every turn ran each pattern over every message again under the time
limit of one operation: a pattern that is slow on a text (`[ \t]+$` on a table padded with blanks) finished on a quiet
machine and ran out of time on a busy one, so the body sent upstream changed from turn to turn and its cache was written
again (audit prompt-a-1; the owner's decision of 2026-11-08)."""
from types import SimpleNamespace

import pytest

import prompt_body
import prompt_policy as policy

TEXT = "row 1   \nrow 2\t\t\nrow 3"


def context(**deadline):
    return SimpleNamespace(snapshot=SimpleNamespace(_token=("rev", 1)), deadline=policy.Deadline(**deadline))


def remove(pattern, ctx):
    return prompt_body._remove_regex_fn(policy.compile_pattern(pattern, "m"), ctx)(TEXT)


def test_a_text_keeps_its_result_on_a_turn_where_the_pattern_would_run_out_of_time():
    first = remove(r"[ \t]+$", context())
    assert first[0] == "row 1\nrow 2\nrow 3"
    busy = context(per_operation=0.0)  # every operation would run out of time on this turn
    assert remove(r"[ \t]+$", busy) == first


def test_a_text_the_pattern_ran_out_of_time_on_keeps_that_refusal():
    with pytest.raises(policy.OperationTimeout):
        remove(r"[ \t]+$", context(per_operation=0.0))
    with pytest.raises(policy.OperationTimeout):
        remove(r"[ \t]+$", context())  # a quiet turn gives the same body as the busy one did


def test_a_request_whose_budget_ran_out_is_refused_as_before_and_nothing_is_remembered_for_the_text():
    with pytest.raises(policy.RegexTimeout, match="work"):
        remove(r"[ \t]+$", context(work=0))
    assert remove(r"[ \t]+$", context())[0] == "row 1\nrow 2\nrow 3"
    with pytest.raises(policy.RegexTimeout, match="work"):
        remove(r"[ \t]+$", context(work=len(TEXT) - 1))  # a remembered text still counts against the work of its request


def test_another_revision_another_edit_or_another_text_is_its_own_entry():
    remove(r"[ \t]+$", context())
    other = SimpleNamespace(snapshot=SimpleNamespace(_token=("rev", 2)), deadline=policy.Deadline(per_operation=0.0))
    with pytest.raises(policy.OperationTimeout):
        prompt_body._remove_regex_fn(policy.compile_pattern(r"[ \t]+$", "m"), other)(TEXT)
    with pytest.raises(policy.OperationTimeout):
        remove(r"row \d", context(per_operation=0.0))
