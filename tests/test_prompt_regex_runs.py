"""A pattern that starts with a run of one character class is tried only where such a run starts: the same matches, in linear
time.

`[ \t]+$` on a table padded with blanks tried the run at every position of every run of blanks and scanned it to its end
each time: 1.7-2.4 s for one run of 20k blanks, so the 1 s limit of an operation decided whether the edit applied
(audit prompt-a-1; the owner's decision of 2026-11-08)."""
import random
import time

import pytest

import prompt_policy as pp


def compiled(pattern, flags=""):
    return pp.compile_pattern(pattern, flags)


@pytest.mark.parametrize("pattern, flags", [
    (r"[ \t]+$", "m"), (r"\s+\n", ""), (r"[ab]+a", ""), (r"[ab]+a(?=b)", ""), (r"\d+(?:\.\d+)?", ""), (r"[ \t]++$", "m"),
    (r"-+", ""), (r"(?i)[A-Z]+x", ""), (r"a+(b)\1", ""), (r"[ab]+\b", ""), (r"[-a]+-", ""),
])
def test_the_matches_are_those_of_the_pattern_as_written(pattern, flags):
    import regex
    plain = regex.compile(pattern, pp._regex_bits(flags))
    fast = compiled(pattern, flags)
    assert fast.pattern != pattern  # the run is tried where it starts only
    rng = random.Random(pattern)
    for _ in range(3000):
        text = "".join(rng.choice("ab \t\nx-,.@yA1") for _ in range(rng.randint(0, 30)))
        assert [m.span() for m in fast.finditer(text)] == [m.span() for m in plain.finditer(text)], text
        assert fast.subn("<\\g<0>>", text) == plain.subn("<\\g<0>>", text), text


@pytest.mark.parametrize("pattern, flags", [
    (r"[ \t]*$", "m"),  # a run of none: an empty match inside a run would be lost
    (r"[ab]+?a", ""),  # lazy
    (r"[ab]+b|c", ""),  # the run starts one branch only
    (r"(?x) [ ]+ $", ""),  # verbose: the text of the pattern is read another way
    (r"^\s+", "m"),  # anchored already
    (r"ab+", ""),  # no run at the start
])
def test_other_patterns_are_compiled_as_written(pattern, flags):
    assert compiled(pattern, flags).pattern == pattern


def test_a_run_of_twenty_thousand_blanks_takes_no_time():
    text = " " * 20000 + "x\n"
    started = time.perf_counter()
    assert pp.regex_remove(compiled(r"[ \t]+$", "m"), text) == (text, 0)
    assert time.perf_counter() - started < 0.2
