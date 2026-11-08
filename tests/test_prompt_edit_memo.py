"""A regular expression edit gives a text the same result on every turn, however busy the machine is.

The history comes back whole with every request, and every turn ran each pattern over every message again under the time
limit of one operation: a pattern that is slow on a text (`[ \t]+$` on a table padded with blanks) finished on a quiet
machine and ran out of time on a busy one, so the body sent upstream changed from turn to turn and its cache was written
again (audit prompt-a-1; the owner's decision of 2026-11-08)."""
from types import SimpleNamespace
import threading

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


@pytest.mark.parametrize("winner", ["busy", "quiet"])
@pytest.mark.parametrize("refusal", [policy.OperationTimeout, policy.EditTooLarge])
def test_concurrent_first_edits_return_the_committed_winner(monkeypatch, winner, refusal):
    """Both threads miss; one refuses and one succeeds. Neither may return a losing, private outcome."""
    barrier, committed = threading.Barrier(2), threading.Event()
    real_remove, real_remember = policy.regex_remove, prompt_body._remember
    outcomes, errors, contexts = {}, [], {}

    def simulate(pattern, text, deadline):
        barrier.wait(timeout=5)
        if threading.current_thread().name == "busy":
            deadline.charge(len(text))
            raise refusal("synthetic loaded operation")
        return real_remove(pattern, text, deadline)

    def commit(key, found, held):
        if threading.current_thread().name != winner:
            assert committed.wait(timeout=5)
        result = real_remember(key, found, held)
        if threading.current_thread().name == winner:
            committed.set()
        return result

    def worker():
        name = threading.current_thread().name
        ctx = contexts[name] = context()
        try:
            outcomes[name] = remove(r"[ \t]+$", ctx)
        except (policy.OperationTimeout, policy.EditTooLarge) as exc:
            outcomes[name] = (type(exc), str(exc))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(policy, "regex_remove", simulate)
    monkeypatch.setattr(prompt_body, "_remember", commit)
    threads = [threading.Thread(target=worker, name=name) for name in ("busy", "quiet")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    expected = (refusal, "synthetic loaded operation") if winner == "busy" else ("row 1\nrow 2\nrow 3", 5, 0)
    assert outcomes == {"busy": expected, "quiet": expected}
    assert all(ctx.deadline.work == policy.REGEX_WORK - len(TEXT) for ctx in contexts.values())
    # Later turns must see the same result, and a memo hit still charges the request budget exactly once.
    monkeypatch.setattr(prompt_body, "_remember", real_remember)
    later = context()
    if winner == "busy":
        with pytest.raises(refusal, match="synthetic loaded operation"):
            remove(r"[ \t]+$", later)
    else:
        assert remove(r"[ \t]+$", later) == expected
    assert later.deadline.work == policy.REGEX_WORK - len(TEXT)
