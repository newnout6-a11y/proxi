"""Regular expressions of a document are bounded and checked for catastrophic backtracking (plan I11)."""
import time

import pytest

import prompt_policy as pp
from test_prompt_policy_common import compile_doc, document


def compile_pattern(pattern, flags=""):
    return pp._compile_pattern(pattern, flags, "pattern")


BOMBS = [
    r"(a+)+", r"(a*)*", r"(a*)+", r"(a+)*", r"(?:a|aa)+", r"(?:ab|a|)+", r"(.*a){20}", r"(.*a){3}", r"(a|a)*",
    r"(a+)+$", r"^(a+)+$", r"(\w+\s?)+$", r"(x+x+)+y", r"(?:a?){25}a{25}", r"((a+)+)+", r"(?:\d+\.?)+", r"(.*)*",
    r"(?:.+)+x", r"(?s)(?:.+\n)+", r"(a+){10}", r"(?:[a-z]+\s*)+$", r"(\s*\w+)*", r"(?:a|a?)+", r"(a{1,2}){1,50}b",
    r"(?:(?:a+)+b)", r"(?=(a+)+)b", r"(?!(?:a+)+b)c", r"(.+)+", r"(\S+\s*)+$",
    r"(?:a*b*)+c", r"(?:x*){2,}y", r"^(([a-z])+.)+[A-Z]([a-z])+$", r"(?:\s|\s\s)+x", r"(?:.*,){9}x", r"(?:a?)*",
    r"(?:(?:ab)*)*", r"(?i)(?:[a-z]+\d*)+", r"(?:\w+\s*)*:",
    # neighbouring unbounded repeats over the same characters: polynomial, but with no way to bound the text
    r"a*a*", r".*.*=.*", r"\s*\s*x", r"\w+\w+\w+", r"a*b?a*", r"(?:ab)*(?:ab)*c", r"[a-c]+[b-d]+x",
    # long items that are not plain repeats: a group or an optional wrapper around an unbounded repeat
    r"(?i)a{2,}(a+)?b", r"(?i)\s*\s?(?:\d+[^a]|[^a]*?\d)", r"([ab]{2,}|[ab]+?).*?ab+", r"(?s).\w*([ab]*\d)-?",
    r"\s*(?:foo|bar)?\s*x", r"(?:a+)?a+b", r"(?:\w+,)*\w*(?:\w+,)*x",
]

NORMAL = [
    r"<system-reminder>.*?</system-reminder>", r"(?s)<env>.*?</env>", r"^## .*$", r"\n{3,}", r"[ \t]+$",
    r"Today's date is \d{4}-\d{2}-\d{2}", r"(?i)\bplan mode\b", r"(?:foo|bar)\s*:", r"(?:\r?\n)+",
    r"^#{1,6}[ \t]+\S.*$", r"(?:[^\n]*\n)+", r"\s+$", r"(\w+\s)+", r"(?:\s*,\s*\w+)*", r"https?://\S+",
    r"[A-Z][a-z]+ [A-Z][a-z]+", r"(?m)^[ \t]*\n(?:[ \t]*\n)+", r"<(?:a|b|c)\b[^>]*>", r"(?:foo|fob)+",
    r"(\d{1,3}\.){3}\d{1,3}", r"(?s)Skills:.*?(?=\n\n)", r"(?m)^\s*#.*$", r"(?:ab)+", r"(a|b)+", r"\bfoo\b",
    r"(?:^|\n)#+ .*", r"x{2,5}", r"(?i)(?:note|warning):\s.*", r"\[[^\]]*\]\([^)]*\)", r"(?:a|ab)+c", r"(a|ab)*c",
    r"(?:ab|a)+c", r"(?:[A-Za-z]+-)+[A-Za-z]+", r"(?:\w+/)*\w+\.md", r"(?:a|b)*c", r"[a-z]+@[a-z]+\.[a-z]+",
    r"(?:\d+,)*\d+", r"(?s)\{.*?\}", r"```.*?```", r"(?m)^(?:[ \t]*\n){3,}", r"^\s*$", r"\s*\n\s*", r"(?:#[^\n]*\n)+",
    r"[^\s]+@[^\s]+", r"(?:foo)?bar", r"(?:a+b)+", r"(?:[ab]|[bc])+d", r"(?i)(?:a|A)+", r"(?>a+)+", r"(?:a++)+",
    r"(?:a+)++b", r"(?:\w++\s?)+", r"(?s)^.*$", r"(?=a+)a", r"(?<=\n)[^\n]+(?=\n)",
]


@pytest.mark.parametrize("pattern", BOMBS)
def test_a_pattern_that_can_backtrack_catastrophically_is_refused(pattern):
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_pattern(pattern)
    assert raised.value.path == "pattern"
    assert "catastrophically" in raised.value.message or "invalid" in raised.value.message


@pytest.mark.parametrize("pattern", BOMBS)
def test_the_same_patterns_are_refused_inside_a_document(tmp_path, pattern):
    for edit_holder in (
            {"client_system": {"edits": [{"op": "remove_regex", "pattern": pattern}]}},
            {"history": {"user_blocks": [{"op": "remove_regex", "pattern": pattern}]}},
            {"history": {"user_blocks": [{"op": "drop_tag", "tag": "t", "pattern": pattern}]}},
            {"tools": {"edits": [{"tool": "x", "op": "replace_regex", "pattern": pattern, "with": "y"}]}}):
        with pytest.raises(pp.PromptConfigError) as raised:
            compile_doc(document(profiles={"p": edit_holder}), tmp_path)
        assert raised.value.path.endswith(".pattern") and raised.value.path.startswith("profiles.p.")


TEXT = ("Lorem ipsum dolor sit amet, <system-reminder>note</system-reminder> end\n## Heading here\n"
        "  trailing   \nfoo: bar, baz, qux\n\n\n\n```code\nx = {1: 2}\n```\n" * 7200)


def test_the_test_text_is_about_a_megabyte():
    assert 1_000_000 < len(TEXT.encode("utf-8")) < 1_300_000


@pytest.mark.parametrize("pattern", NORMAL)
def test_ordinary_patterns_are_accepted_and_stay_fast_on_a_megabyte(pattern):
    compiled = compile_pattern(pattern)
    started = time.perf_counter()
    for _ in compiled.finditer(TEXT):
        pass
    assert time.perf_counter() - started < 3.0


def test_a_safe_pattern_survives_hostile_input_that_breaks_its_unsafe_twin():
    # (a+)+$ is refused; its safe twin is linear on the input that makes the original explode
    hostile = "a" * 5000 + "!"
    compiled = compile_pattern(r"a+$")
    started = time.perf_counter()
    assert compiled.search(hostile) is None
    assert time.perf_counter() - started < 5.0
    atomic = compile_pattern(r"(?>a+)+$")
    started = time.perf_counter()
    assert atomic.search("a" * 4000 + "!") is None
    assert time.perf_counter() - started < 5.0


WILDCARD_STARTS = [
    (r"(?s).*foo", ""), (r".*foo", "s"), (r"[^>]*>", ""), (r"[\s\S]*x", ""), (r"(?s)(.*)x", ""), (r"(?s)(?:.*|a)b", ""),
    (r"(?ms)^.*x", ""), (r"(?s)(?=x).*y", ""), (r"(?s)(?<=a).*b", ""), (r".+?foo", "s"), (r"(?s)(?:\w+|.*?)x", ""),
]
ANCHORED_OR_BOUNDED = [
    (r"(?s)^.*foo", ""), (r"\A(?s:.*)foo", ""), (r".*foo", ""), (r"(?s)<a>.*</a>", ""), (r"x(?s:.*)y", ""),
    (r"(?s).*", ""), (r"(?s)(.*)", ""), (r"[^>]+", ""), (r"\s+$", "s"), (r"(?m)^[^\n]*foo", ""), (r"(?s).{0,50}foo", ""),
    (r"(?:^|x).*y", "s"), (r"x[^>]*>", ""),
]


@pytest.mark.parametrize("pattern, flags", WILDCARD_STARTS)
def test_a_pattern_that_starts_with_a_wildcard_repeat_is_refused(pattern, flags):
    with pytest.raises(pp.PromptConfigError, match="starts with an unbounded repeat"):
        compile_pattern(pattern, flags)


@pytest.mark.parametrize("pattern, flags", ANCHORED_OR_BOUNDED)
def test_an_anchored_or_bounded_wildcard_is_fine(pattern, flags):
    assert compile_pattern(pattern, flags)


def test_the_wildcard_rule_keeps_a_big_text_without_a_match_fast_where_the_refused_pattern_would_not():
    text = "x" * 200_000
    safe = compile_pattern(r"(?s)^.*foo")
    started = time.perf_counter()
    assert safe.search(text) is None
    assert time.perf_counter() - started < 3.0

def test_pattern_length_is_bounded():
    assert pp.MAX_PATTERN_CHARS == 512
    assert compile_pattern("a" * 512)
    with pytest.raises(pp.PromptConfigError, match="512"):
        compile_pattern("a" * 513)


@pytest.mark.parametrize("flags, ignore_case", [("", False), (None, False), ("i", True), ("sim", True), ("s", False)])
def test_flags_are_a_subset_of_ims(flags, ignore_case):
    compiled = compile_pattern("a.b", flags)
    assert bool(compiled.flags & pp.re.IGNORECASE) is ignore_case
    assert bool(compiled.flags & pp.re.DOTALL) is bool(flags and "s" in flags)
    assert bool(compiled.flags & pp.re.MULTILINE) is bool(flags and "m" in flags)


@pytest.mark.parametrize("flags", ["x", "u", "a", "imsx", "I", 5, ["i"]])
def test_flags_outside_ims_are_refused(flags):
    with pytest.raises(pp.PromptConfigError, match="only i, m and s"):
        compile_pattern("a", flags)


def test_the_flags_change_what_the_pattern_does():
    assert compile_pattern("a.b", "s").search("a\nb")
    assert not compile_pattern("a.b", "").search("a\nb")
    assert compile_pattern("^b", "m").search("a\nb")
    assert compile_pattern("A", "i").search("a")


@pytest.mark.parametrize("pattern", ["(", "[a", "a{2,1}", "(?P<n>a)(?P<n>b)", "\\", "*a", "(?<=a+)b", ""])
def test_a_pattern_that_does_not_compile_is_refused(pattern):
    with pytest.raises(pp.PromptConfigError):
        compile_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"[[:alpha:]]+", r"[a-z--b]", r"[a&&b]", r"[[a]"])
def test_a_pattern_python_warns_about_is_refused(pattern):
    with pytest.raises(pp.PromptConfigError, match="suspicious"):
        compile_pattern(pattern)


def test_the_warning_check_does_not_depend_on_the_re_cache():
    for _ in range(3):
        with pytest.raises(pp.PromptConfigError, match="suspicious"):
            compile_pattern(r"[[:digit:]]")


def test_deeply_nested_patterns_never_crash_the_check():
    for depth in (50, 120, 250):
        pattern = "(" * depth + "a" + ")" * depth
        try:
            compile_pattern(pattern)
        except pp.PromptConfigError:
            pass
    for depth in (20, 60, 120):
        pattern = "(?:" * depth + "a+" + ")+" * depth
        with pytest.raises(pp.PromptConfigError):
            compile_pattern(pattern)


def test_a_possessive_quantifier_or_an_atomic_group_is_the_way_out():
    for pattern in (r"(?:a++)+", r"(?>a+)+", r"(?:\w++\s?)+", r"(?:a+)++"):
        assert compile_pattern(pattern)
    with pytest.raises(pp.PromptConfigError, match="possessive"):
        compile_pattern(r"(\w+\s?)+$")


def test_ignore_case_is_part_of_the_analysis():
    compile_pattern(r"(?:a|b)+c")
    with pytest.raises(pp.PromptConfigError):
        compile_pattern(r"(?i)(?:a+|A+)+")
    compile_pattern(r"(?:[a-z]+-)+[a-z]+", "i")


def test_scoped_flags_are_followed():
    with pytest.raises(pp.PromptConfigError):
        compile_pattern(r"(?s:(.+\n)+)")
    compile_pattern(r"(?-s:(.+\n)+)", "s")


def test_the_snapshot_hands_out_the_pattern_it_compiled(tmp_path):
    doc = document(profiles={"p": {
        "client_system": {"edits": [{"op": "remove_regex", "pattern": "(a)b", "flags": "si"}]},
        "history": {"user_blocks": [{"op": "drop_tag", "tag": "t", "pattern": "x+y", "flags": "m"}]},
        "tools": {"edits": [{"tool": "t", "op": "replace_regex", "pattern": "q+", "with": "z"}]}}})
    snapshot = compile_doc(doc, tmp_path)
    first = snapshot.regex("(a)b", "is")
    assert first is snapshot.regex("(a)b", "si") is snapshot.regex("(a)b", "is")
    assert first.flags & pp.re.DOTALL and first.flags & pp.re.IGNORECASE
    assert snapshot.regex("x+y", "m").flags & pp.re.MULTILINE
    assert snapshot.regex("q+").search("aqqb")
    assert snapshot.regex("q+") is snapshot.regex("q+", "")


def test_an_unknown_pattern_goes_through_the_same_checks(tmp_path):
    snapshot = compile_doc(document(), tmp_path)
    assert snapshot.regex("fresh", "i").search("FRESH")
    assert snapshot.regex("fresh", "i") is snapshot.regex("fresh", "i")
    with pytest.raises(pp.PromptConfigError, match="catastrophically"):
        snapshot.regex("(a+)+$")
    with pytest.raises(pp.PromptConfigError, match="only i, m and s"):
        snapshot.regex("a", "z")


def test_a_replacement_template_is_checked_against_the_pattern(tmp_path):
    ok = document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_regex", "pattern": "(?P<x>a)(b)", "with": r"\g<x>-\2-\g<2>-\\"}]}}})
    compile_doc(ok, tmp_path)
    for bad in (r"\3", r"\g<y>", "\\"):
        doc = document(profiles={"p": {"client_system": {"edits": [
            {"op": "replace_regex", "pattern": "(a)(b)", "with": bad}]}}})
        with pytest.raises(pp.PromptConfigError) as raised:
            compile_doc(doc, tmp_path)
        assert raised.value.path == "profiles.p.client_system.edits[0].with"
