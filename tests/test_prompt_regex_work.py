"""The regular expressions of a call are bounded by the work they do, not by the clock (item 12, a5's ultracode audit, G4.1).

The edits of a profile shared a budget of 1 s by the wall clock (0.25 s per operation). Under load the threads of the relay wait
for the GIL and for the CPU, so the same request used up that second at one time and not at another: edits were skipped, the tool
descriptions or the head that went upstream differed from one turn to the next, and with them the scope of the signed history
(the same body under load gave "Run a command." instead of "Execute a command." and another scope). Every operation is now charged
the length of its text, REGEX_WORK characters in all, whatever the machine is doing; the clock stays as a last stop, with room.
"""
import time

import pytest
import regex

import prompt_body
import prompt_policy as pp
from test_prompt_body_support import FakeSnapshot, conversation, profile

SPECS = [("bash", "Run a command."), ("read", "Read a file."), ("webfetch", "Fetch a page.")]
EDITS = [{"tool": "bash", "op": "replace_regex", "pattern": "Run", "with": "Execute"},
         {"tool": "read", "op": "replace_regex", "pattern": "Read", "with": "Open"},
         {"tool": "webfetch", "op": "replace_regex", "pattern": "Fetch", "with": "Get"}]
TURNS = [("user", "start"), ("assistant", "ok"), ("user", "more")]


class Busy:
    """The clock of a machine whose thread was kept waiting for two seconds in the middle of the first operation."""

    def __init__(self):
        self.reads = 0

    def perf_counter(self):
        self.reads += 1
        return 2.0 if self.reads >= 2 else 0.0

    def __getattr__(self, name):
        return getattr(time, name)


def rewrite():
    return prompt_body.rewrite(conversation("chat", TURNS, tools=SPECS), "chat", profile(tools=EDITS), FakeSnapshot())


def described(result):
    return [tool["function"]["description"] for tool in result["tools"]]


def skipped(changes):
    return [(change["edit"], change["reason"]) for change in changes if change.get("skipped")]


def test_a_quiet_machine_applies_every_edit():
    result, changes = rewrite()
    assert described(result) == ["Execute a command.", "Open a file.", "Get a page."] and skipped(changes) == []


def test_a_busy_machine_makes_the_same_rewrite_as_a_quiet_one(monkeypatch):
    quiet = rewrite()
    monkeypatch.setattr(pp, "time", Busy())
    busy = rewrite()
    assert busy == quiet  # before: the two seconds spent the budget of 1 s, and the other two edits were skipped


def test_what_is_refused_is_the_same_on_a_quiet_machine_and_a_busy_one(monkeypatch):
    # 39 characters of tool descriptions, 30 of work: the third edit does not fit, whatever the clock says
    monkeypatch.setattr(pp, "REGEX_WORK", 30)
    quiet = rewrite()
    monkeypatch.setattr(pp, "time", Busy())
    busy = rewrite()
    assert busy == quiet
    assert described(quiet[0]) == ["Execute a command.", "Open a file.", "Fetch a page."]
    assert skipped(quiet[1]) == [(2, "timeout")]


def test_an_operation_is_charged_the_length_of_its_text_before_it_runs():
    deadline, ran = pp.Deadline(work=100), []
    deadline.run(lambda timeout: ran.append(1), 40)
    deadline.run(lambda timeout: ran.append(2), 40)
    assert deadline.work == 20 and ran == [1, 2]
    with pytest.raises(pp.RegexTimeout):
        deadline.run(lambda timeout: ran.append(3), 40)
    assert ran == [1, 2] and deadline.work == 20  # refused, not run, and not charged
    deadline.run(lambda timeout: ran.append(4), 20)  # what is left is used to the last character
    assert deadline.work == 0 and ran == [1, 2, 4]
    deadline.run(lambda timeout: ran.append(5))  # no size, no charge: the way a call without a text always ran
    assert ran[-1] == 5


def test_every_helper_charges_the_length_of_the_text_it_runs_on():
    compiled, text = regex.compile("a"), "banana" * 5
    for run in (lambda d: pp.regex_search(compiled, text, d), lambda d: pp.regex_spans(compiled, text, d),
                lambda d: pp.regex_remove(compiled, text, d), lambda d: pp.regex_replace(compiled, "o", text, d)):
        deadline = pp.Deadline(work=1000)
        run(deadline)
        assert deadline.work == 1000 - len(text)


def test_a_text_that_does_not_fit_is_refused_by_every_helper_without_looking_at_it():
    compiled, text = regex.compile("a"), "a" * 50
    for run in (lambda d: pp.regex_search(compiled, text, d), lambda d: pp.regex_spans(compiled, text, d),
                lambda d: pp.regex_remove(compiled, text, d), lambda d: pp.regex_replace(compiled, "o", text, d)):
        with pytest.raises(pp.RegexTimeout, match="work"):
            run(pp.Deadline(work=49))


def test_the_default_work_is_read_when_a_deadline_is_made_and_one_call_gets_its_own(monkeypatch):
    monkeypatch.setattr(pp, "REGEX_WORK", 123)
    first = pp.Deadline()
    first.run(lambda timeout: None, 100)
    assert (first.work, pp.Deadline().work) == (23, 123)


def test_a_real_request_uses_a_small_part_of_the_work_a_call_may_do():
    # a megabyte of history through forty edits is 40 million characters: a sixth of what a call may scan
    assert 40 * 1024 * 1024 < pp.REGEX_WORK // 5
