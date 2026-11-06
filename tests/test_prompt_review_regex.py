"""R1-01: a regular expression can neither freeze the relay nor hold the GIL (plan I11).

Python's `re` holds the GIL while it matches, so a catastrophic pattern on untrusted text (a tool result, an AGENTS.md of the
client) froze every thread for tens of seconds, and the static analysis let such patterns through. Now every pattern of a
document is a pattern of the `regex` module, run with concurrent=True (the other threads go on) and a time limit per operation
and per call. The tests shorten the limits so that a stuck pattern costs a fraction of a second.
"""
import json
import threading
import time
import warnings

import pytest
import regex

import prompt_body
import prompt_policy as pp
from prompt_support import client_request, prompt_relay  # noqa: F401
from test_prompt_policy_common import OPENCODE_SYSTEM, Sources, compile_doc, document, inp, make_store
from test_prompt_policy_preview import chat

FAST = {"REGEX_TIMEOUT": 0.05, "REGEX_BUDGET": 0.2}


@pytest.fixture
def fast(monkeypatch):
    for name, value in FAST.items():
        monkeypatch.setattr(pp, name, value)
    return FAST


# The patterns and texts of the reviewer's report: the analysis accepts each of them.
BOMBS = {
    "words": (r"^(?:\w{1,40}\s?){1,8}$", "m", "a" * 61 + "!"),
    "nested-bounded": (r"(?:(?:a{1,8}){1,8}){1,8}b", "", "a" * 200),
    "lazy-tags": (r"<system-reminder>.*?</system-reminder>", "s", "text " + "<system-reminder>" * 20000),
}
# Patterns of the report that the `regex` module happens to answer at once; they must stay fast, not necessarily time out.
QUICK = {
    "optional-chain": ("a?" * 28 + "a" * 28, "", "a" * 28),
    "leading-wildcard": (r".*password.*", "", '{"k":"v",' * 9000),
}


def stuck_body(text):
    # The first message is not made of word characters, so that no pattern of these tests matches it.
    return {"model": "claude-opus-5-5", "messages": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                               "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": text}]}


def user_block_document(pattern, flags=""):
    return document(profiles={"p": {"history": {"user_blocks": [{"op": "remove_regex", "pattern": pattern,
                                                                  "flags": flags}]}}}, default_profile="p")


@pytest.mark.parametrize("name", list(BOMBS))
def test_the_analysis_is_only_the_first_line_these_patterns_still_get_in(tmp_path, name):
    pattern, flags, _text = BOMBS[name]
    snapshot = compile_doc(user_block_document(pattern, flags), tmp_path)
    assert isinstance(snapshot.regex(pattern, flags), regex.Pattern)


@pytest.mark.parametrize("name", list(BOMBS))
def test_a_stuck_pattern_in_a_tool_result_is_skipped_for_that_message_and_the_request_goes_on(tmp_path, fast, name):
    pattern, flags, text = BOMBS[name]
    snapshot = compile_doc(user_block_document(pattern, flags), tmp_path)
    body = stuck_body(text)
    started = time.perf_counter()
    result, changes = prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, "the rewrite took %.1f s" % elapsed
    assert result == body
    assert changes == [{"scope": "history", "op": "remove_regex", "edit": 0, "index": 2, "target": "tool_result",
                        "skipped": True, "reason": "timeout"}]


@pytest.mark.parametrize("name", list(BOMBS))
def test_a_stuck_pattern_in_the_system_prompt_leaves_it_alone_and_says_so_without_quoting_the_pattern(
        tmp_path, fast, name):
    pattern, flags, text = BOMBS[name]
    doc = document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_regex", "pattern": pattern, "flags": flags}, {"op": "append", "text": "TAIL"}]}}})
    snapshot = compile_doc(doc, tmp_path)
    plan = pp.Plan(rev=1, profile="p", rule="t", snapshot=snapshot)
    started = time.perf_counter()
    head, parts, warnings_ = plan.compose_head(text, Sources())
    assert time.perf_counter() - started < 2.0
    assert head == text + "\n\nTAIL"
    assert [part.source for part in parts][0] == "client_edited"
    assert warnings_ == ["client_system.edits[0] (remove_regex) was not applied: it ran out of time (a pattern that "
                         "takes too long on this text)"]
    assert pattern not in " ".join(warnings_)


@pytest.mark.parametrize("name", list(QUICK))
def test_the_quick_ones_of_the_report_stay_quick_through_the_real_paths(tmp_path, name):
    pattern, flags, text = QUICK[name]
    snapshot = compile_doc(user_block_document(pattern, flags), tmp_path)
    started = time.perf_counter()
    _result, changes = prompt_body.rewrite(stuck_body(text), "chat", snapshot.profile("p"), snapshot)
    assert time.perf_counter() - started < 1.0
    assert not any(change.get("skipped") for change in changes)


def test_other_threads_run_while_a_pattern_is_stuck(tmp_path, monkeypatch):
    """The GIL is released while `regex` matches: `re` would have stopped this counter for the whole time."""
    monkeypatch.setattr(pp, "REGEX_TIMEOUT", 0.4)
    pattern, flags, text = BOMBS["nested-bounded"]
    compiled = compile_doc(user_block_document(pattern, flags), tmp_path).regex(pattern, flags)
    beats, stop = [], threading.Event()

    def pulse():
        while not stop.is_set():
            beats.append(time.perf_counter())
            time.sleep(0.005)

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    time.sleep(0.05)
    before = len(beats)
    started = time.perf_counter()
    with pytest.raises(pp.RegexTimeout):
        pp.regex_search(compiled, text)
    elapsed = time.perf_counter() - started
    gaps = [later - earlier for earlier, later in zip(beats[before - 1:], beats[before:])]
    stop.set()
    thread.join(2)
    assert 0.3 < elapsed < 1.5
    assert len(beats) - before > 15, "the other thread beat %d times in %.2f s" % (len(beats) - before, elapsed)
    assert max(gaps) < 0.2


def test_the_budget_of_a_call_caps_the_total_not_every_edit(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    edits = [{"op": "remove_regex", "pattern": pattern + "|" + "x" * number, "flags": flags} for number in range(1, 13)]
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": edits}}}), tmp_path)
    started = time.perf_counter()
    result, report = pp._apply_system_edits(text, snapshot.profile("p")["client_system"]["edits"], snapshot)
    elapsed = time.perf_counter() - started
    assert result == text and [entry.get("skipped") for entry in report] == ["timeout"] * 12
    assert elapsed < 1.0, "12 stuck edits took %.2f s (the budget is %.2f s)" % (elapsed, fast["REGEX_BUDGET"])


def test_the_budget_of_a_rewrite_caps_the_total_too(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    rules = [{"op": "remove_regex", "pattern": pattern + "|" + "x" * number, "flags": flags} for number in range(1, 9)]
    doc = document(profiles={"p": {"history": {"user_blocks": rules}}})
    snapshot = compile_doc(doc, tmp_path)
    started = time.perf_counter()
    result, changes = prompt_body.rewrite(stuck_body(text), "chat", snapshot.profile("p"), snapshot)
    assert time.perf_counter() - started < 1.0
    assert [(change["edit"], change["reason"]) for change in changes] == [(number, "timeout") for number in range(8)]


def test_each_call_gets_its_own_budget(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    snapshot = compile_doc(user_block_document(pattern, flags), tmp_path)
    for _ in range(3):
        _result, changes = prompt_body.rewrite(stuck_body(text), "chat", snapshot.profile("p"), snapshot)
        assert changes[0]["reason"] == "timeout"
    _result, changes = prompt_body.rewrite(stuck_body("fine text"), "chat", snapshot.profile("p"), snapshot)
    assert changes == []


def test_a_timeout_spares_the_rules_after_it_while_there_is_time(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "REGEX_TIMEOUT", 0.05)
    monkeypatch.setattr(pp, "REGEX_BUDGET", 5.0)
    pattern, flags, text = BOMBS["nested-bounded"]
    doc = document(profiles={"p": {"history": {"user_blocks": [
        {"op": "remove_regex", "pattern": pattern, "flags": flags}, {"op": "replace_text", "find": "a", "with": "b"},
        {"op": "drop_tag", "tag": "note"}]}}})
    snapshot = compile_doc(doc, tmp_path)
    result, changes = prompt_body.rewrite(stuck_body(text + "\n<note>x</note>\n"), "chat", snapshot.profile("p"), snapshot)
    assert [(change["op"], bool(change.get("skipped"))) for change in changes] == [
        ("remove_regex", True), ("replace_text", False), ("drop_tag", False)]
    assert result["messages"][2]["content"].startswith("b" * 200)


def test_a_replacement_pattern_that_is_stuck_is_skipped_on_a_tool_description_too(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    doc = document(profiles={"p": {"tools": {"edits": [
        {"tool": "bash", "op": "replace_regex", "pattern": pattern, "with": "x"},
        {"tool": "bash", "op": "append", "text": "Fine."}]}}})
    snapshot = compile_doc(doc, tmp_path)
    data = {"model": "m", "messages": [{"role": "user", "content": "go"}], "tools": [
        {"type": "function", "function": {"name": "bash", "description": text, "parameters": {"type": "object"}}}]}
    result, changes = prompt_body.rewrite(data, "chat", snapshot.profile("p"), snapshot)
    assert changes[0] == {"scope": "tools", "op": "replace_regex", "edit": 0, "index": 0, "tool": "bash",
                          "skipped": True, "reason": "timeout"}
    assert result["tools"][0]["function"]["description"] == text + "\n\nFine."


def test_a_drop_tag_pattern_that_is_stuck_skips_the_rule_for_that_message(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    doc = document(profiles={"p": {"history": {"user_blocks": [
        {"op": "drop_tag", "tag": "note", "pattern": pattern, "flags": flags}]}}})
    snapshot = compile_doc(doc, tmp_path)
    body = stuck_body("<note>\n" + text + "\n</note>\ntail")
    result, changes = prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot)
    assert result == body
    assert [(change["op"], change["reason"]) for change in changes] == [("drop_tag", "timeout")]


def test_a_message_with_two_parts_is_all_or_nothing(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    snapshot = compile_doc(user_block_document(pattern + "|zz", flags), tmp_path)
    body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "zz first"}, {"type": "text", "text": text}]}]}
    result, changes = prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot)
    assert result == body and changes[0]["reason"] == "timeout"


def test_the_preview_reports_a_stuck_edit_without_calling_it_unmatched(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    doc = document(profiles={"p": {"client_system": {"edits": [{"op": "remove_regex", "pattern": pattern,
                                                                 "flags": flags}]}}}, default_profile="p")
    store = make_store(tmp_path, doc)
    request = chat(system=text, tools=False)
    result = store.preview(request, "chat", {"x-proxi-client": "opencode"})
    assert result["head"] == text
    assert result["edits"][0]["skipped"] == "timeout" and result["edits"][0]["matched"] is False
    assert result["edits_unmatched"] == []
    assert result["warnings"] == ["client_system.edits[0] (remove_regex) was not applied: it ran out of time (a pattern "
                                  "that takes too long on this text)"]


def test_the_preview_reports_a_stuck_history_edit(tmp_path, fast):
    pattern, flags, text = BOMBS["nested-bounded"]
    store = make_store(tmp_path, user_block_document(pattern, flags))
    request = {**stuck_body(text), "stream": True}
    result = store.preview(request, "chat", {"x-proxi-client": "opencode"})
    assert [(item["op"], item["reason"]) for item in result["history_changes"]] == [("remove_regex", "timeout")]
    assert result["edits_unmatched"] == []


def test_a_stuck_edit_reaches_the_journal_as_edit_skipped_and_the_request_is_served(prompt_relay, tmp_path, monkeypatch):
    for name, value in FAST.items():
        monkeypatch.setattr(pp, name, value)
    pattern, flags, text = BOMBS["nested-bounded"]
    (tmp_path / "prompts.json").write_text(json.dumps(user_block_document(pattern, flags)), encoding="utf-8")
    relay = prompt_relay()
    messages = [{"role": "user", "content": "..."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": text}]
    since = relay.mark()
    started = time.perf_counter()
    status, payload, headers = relay.post("chat", client_request("chat", messages, system="S", tools=True,
                                                                 stream=False))
    relay.settle(headers)
    assert status == 200, payload
    assert time.perf_counter() - started < 5.0
    skipped = relay.events(since, "prompt.edit_skipped")
    assert len(skipped) == 1 and skipped[0]["data"]["op"] == "remove_regex" and skipped[0]["data"]["reason"] == "timeout"
    assert pattern not in json.dumps(skipped)
    assert text in json.dumps(relay.posts[0]["messages"])             # the text went upstream as the client sent it


# -- the helpers -------------------------------------------------------------------------------------------------------


def test_deadline_gives_each_operation_its_share_and_refuses_when_the_budget_is_gone():
    deadline = pp.Deadline(budget=0.3, per_operation=0.1)
    seen = []
    deadline.run(lambda timeout: seen.append(timeout))
    assert seen == [0.1]
    deadline.left = 0.05
    deadline.run(lambda timeout: seen.append(timeout))
    assert seen[1] == 0.05
    deadline.left = 0.0005
    with pytest.raises(pp.RegexTimeout):
        deadline.run(lambda timeout: seen.append(timeout))
    assert len(seen) == 2


def test_deadline_charges_the_time_a_call_took_and_turns_the_engines_timeout_into_regex_timeout():
    deadline = pp.Deadline(budget=1.0, per_operation=1.0)
    deadline.run(lambda timeout: time.sleep(0.05))
    assert 0.8 < deadline.left < 0.97

    def times_out(timeout):
        raise TimeoutError

    with pytest.raises(pp.RegexTimeout):
        deadline.run(times_out)
    assert issubclass(pp.RegexTimeout, pp.EditRefused) and pp.RegexTimeout.reason == "timeout"
    assert issubclass(pp.EditTooLarge, pp.EditRefused) and pp.EditTooLarge.reason == "too_large"


def test_other_errors_pass_through_deadline_and_still_charge_it():
    deadline = pp.Deadline(budget=1.0, per_operation=1.0)

    def breaks(timeout):
        time.sleep(0.02)
        raise KeyError("x")

    with pytest.raises(KeyError):
        deadline.run(breaks)
    assert deadline.left < 0.99


def test_the_defaults_are_read_when_a_deadline_is_made(monkeypatch):
    monkeypatch.setattr(pp, "REGEX_TIMEOUT", 0.123)
    monkeypatch.setattr(pp, "REGEX_BUDGET", 0.456)
    deadline = pp.Deadline()
    assert (deadline.per_operation, deadline.left) == (0.123, 0.456)
    assert pp.REGEX_TIMEOUT == 0.123 and 0 < pp.Deadline().per_operation


def test_the_real_limits_are_a_fraction_of_a_second_and_a_few_seconds_at_most():
    assert 0 < pp.REGEX_TIMEOUT <= 0.5 and pp.REGEX_TIMEOUT <= pp.REGEX_BUDGET <= 2.0


def test_one_snapshot_serves_many_threads_at_once(tmp_path):
    doc = document(profiles={"p": {
        "client_system": {"edits": [{"op": "remove_regex", "pattern": r"<env>.*?</env>", "flags": "s"},
                                    {"op": "replace_regex", "pattern": r"(\w+) rules", "with": r"\1 laws"}]},
        "history": {"user_blocks": [{"op": "remove_regex", "pattern": r"Plan\s+mode[^\n]*"}]}}}, default_profile="p")
    snapshot = compile_doc(doc, tmp_path)
    plan = pp.Plan(rev=1, profile="p", rule="t", snapshot=snapshot)
    body = stuck_body("x\nPlan mode is on\ny")
    expected_head = plan.compose_head(OPENCODE_SYSTEM + "my rules\n", Sources())
    expected_body = prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot)
    errors = []

    def work():
        try:
            for number in range(30):
                assert plan.compose_head(OPENCODE_SYSTEM + "my rules\n" + "x" * number, Sources())[0].startswith(
                    expected_head[0][:100])
                assert prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot) == expected_body
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and "my laws" in expected_head[0] and "<env>" not in expected_head[0]


def test_the_helpers_do_what_re_does_on_ordinary_patterns(tmp_path):
    text = ("Lorem <system-reminder>note</system-reminder> end\n## Heading\n  trailing   \nfoo: bar\n\n\n\nxy xy\n" * 300)
    import re
    for pattern, flags in [(r"<system-reminder>.*?</system-reminder>", "s"), (r"(?m)^## .*$", ""), (r"\n{3,}", ""),
                           (r"[ \t]+$", "m"), (r"(?i)\bFOO\b", ""), (r"(x)(y)", ""), (r"^", "m"), (r"$", "m"), (r"\s*", "")]:
        compiled = pp.compile_pattern(pattern, flags)
        bits = sum(bit for char, bit in (("i", re.I), ("m", re.M), ("s", re.S)) if char in flags)
        theirs = re.compile(pattern, bits)
        assert pp.regex_spans(compiled, text) == [
            (match.start(), match.end()) for match in theirs.finditer(text) if match.end() > match.start()], pattern
        assert pp.regex_remove(compiled, text) == theirs.subn("", text), pattern
        assert (pp.regex_search(compiled, text) is not None) == (theirs.search(text) is not None)
        new, count, removed, added = pp.regex_replace(compiled, r"<\g<0>>", text, room=10 ** 9)
        assert (new, count) == theirs.subn(r"<\g<0>>", text), pattern
        assert added - removed == len(new) - len(text)


def test_regex_replace_counts_what_it_took_out_and_put_in(tmp_path):
    compiled = pp.compile_pattern(r"(\w+)@(\w+)")
    assert pp.regex_replace(compiled, r"\2:\1", "ann@home bob@work") == ("home:ann work:bob", 2, 16, 16)
    assert pp.regex_replace(compiled, "x", "ann@home") == ("x", 1, 8, 1)
    assert pp.regex_replace(compiled, "x", "nothing") == ("nothing", 0, 0, 0)


def test_regex_spans_refuses_a_pattern_that_matches_in_too_many_places(tmp_path, monkeypatch):
    assert pp.MAX_MATCHES == 200_000
    compiled = pp.compile_pattern(r"\w")
    text = "ab " * 500
    assert len(pp.regex_spans(compiled, text)) == 1000
    monkeypatch.setattr(pp, "MAX_MATCHES", 999)
    with pytest.raises(pp.EditTooLarge, match="more than 999 places"):
        pp.regex_spans(compiled, text)
    doc = document(profiles={"p": {"client_system": {"edits": [{"op": "remove_regex", "pattern": r"\w"}]}}})
    snapshot = compile_doc(doc, tmp_path)
    result, report = pp._apply_system_edits(text, snapshot.profile("p")["client_system"]["edits"], snapshot)
    assert result == text and report[0]["skipped"] == "too_large"


def test_regex_replace_stops_a_result_that_would_be_too_long_before_building_it():
    compiled = pp.compile_pattern(r"\w")
    text = "word " * 4000
    started = time.perf_counter()
    with pytest.raises(pp.EditTooLarge):
        pp.regex_replace(compiled, "y" * 1000, text, room=10_000)
    assert time.perf_counter() - started < 1.0
    assert pp.regex_replace(compiled, "y", text, room=0)[1] == 16000        # shrinking or keeping the size is always fine


def test_a_replacement_template_the_stdlib_refuses_is_refused_in_a_document_but_expanded_alike_otherwise(tmp_path):
    for template in (r"\x41", r"\u0041", r"\g<nope>", r"\3"):
        doc = document(profiles={"p": {"client_system": {"edits": [
            {"op": "replace_regex", "pattern": "(a)(b)", "with": template}]}}})
        with pytest.raises(pp.PromptConfigError) as raised:
            compile_doc(doc, tmp_path)
        assert raised.value.path == "profiles.p.client_system.edits[0].with"
    for template in (r"\1-\2", r"\g<2>\g<1>", r"\\", r"\n", r"\t", r"\.", r"\0", r"\101", ""):
        compiled = pp.compile_pattern("(a)(b)")
        import re
        assert pp.regex_replace(compiled, template, "xaby")[0] == re.sub("(a)(b)", template, "xaby"), template


# -- what the analysis and the compiler refuse --------------------------------------------------------------------------

WEIGHT_BOMBS = [r"a{99999999}", r"(?:a{1000}){1000}", r"(?:(?:a{50}){50}){50}", r"(?:a{300}){300}", r"a{50000}",
                r"(?:[ab]{2000}){30}"]
LIGHT = [r"\d{4}-\d{2}-\d{2}", r"(?:\w{1,40}[ ,]?){1,8}x", r"a{49000}", r"(?:a{200}){200}", r"[^\n]{0,512}", r".{1,100000}",
         r"(?:.{1,1000}\n){1,50}", r"x{3,5}y{2}", r"(?:ab|cd){1000}"]


@pytest.mark.parametrize("pattern", WEIGHT_BOMBS)
def test_a_pattern_the_regex_module_would_spell_out_at_length_is_refused_before_it_is_compiled(pattern):
    started = time.perf_counter()
    with pytest.raises(pp.PromptConfigError) as raised:
        pp._compile_pattern(pattern, "", "pattern")
    assert time.perf_counter() - started < 1.0
    assert "repeats too much" in raised.value.message and raised.value.path == "pattern"


@pytest.mark.parametrize("pattern", LIGHT)
def test_ordinary_repeat_counts_are_fine(pattern):
    started = time.perf_counter()
    compiled = pp._compile_pattern(pattern, "", "pattern")
    assert time.perf_counter() - started < 1.0 and isinstance(compiled, regex.Pattern)


def test_nested_ranges_with_big_minimums_are_refused_whichever_check_sees_them_first():
    started = time.perf_counter()
    with pytest.raises(pp.PromptConfigError):
        pp._compile_pattern(r"(?:a{1000,1005}){1000,1005}", "", "pattern")
    assert time.perf_counter() - started < 1.0


def test_the_weight_of_a_pattern_is_the_pieces_the_compiler_writes_out():
    weigh = lambda pattern: pp._unrolled(pp._sre_parse.parse(pattern, 0))
    assert weigh("abc") == 3 and weigh("a{1000}") == 1001 and weigh("a{1,1000}") == 2
    assert weigh("(?:a{10}){10}") == 11 * 11 and weigh("a|b|c") > 0
    assert weigh("(?=a{100})b") == 102 and weigh("(?:ab){0,5}") == 2 * 2


@pytest.mark.parametrize("pattern", [r"a{e<=1}", r"(?:foo){e<=1}", r"x{1<=e<=3}", r"a{i<=2}", r"a{d}", r"(?:ab){s<2,i<2}",
                                     r"a{e}", r"a{2<=e}"])
def test_a_brace_that_would_start_a_fuzzy_match_in_the_regex_module_is_refused(pattern):
    with pytest.raises(pp.PromptConfigError, match="fuzzy"):
        pp._compile_pattern(pattern, "", "pattern")


@pytest.mark.parametrize("pattern, text, found", [
    (r"a\{e<=1\}", "a{e<=1}", True), (r"[{e<=1}]+", "{e}", True), (r"a{2}", "aa", True), (r"a{2,}", "aaa", True),
    (r"a{,3}b", "aab", True), (r"x{1,2}{", "xx{", True), (r"\{name\}", "{name}", True), (r"{name}", "{name}", True),
    (r"{{x}}", "{{x}}", True), (r"a\\{x", "a\\{x", True), (r"{}", "{}", True)])
def test_a_literal_brace_and_the_repeat_counts_keep_their_meaning(pattern, text, found):
    compiled = pp._compile_pattern(pattern, "", "pattern")
    assert bool(compiled.search(text)) is found


@pytest.mark.parametrize("pattern", [r"\p{L}+", r"(?|a|b)", r"\m", r"(?V1)a", r"(?r)a", r"(?<name>a)", r"\X", r"\G",
                                     r"(?<=a+)b", r"(?P<n>a)(?P<n>b)", r"(?P>n)", r"(?(DEFINE)a)", r"\L<x>", r"[[:alpha:]]+",
                                     r"[a&&b]", r"[a-z--b]", r"[[a]", r"(?e)a", r"(?b)a", r"(?p)a", r"(?w)a", r"(?f)a"])
def test_whatever_re_refuses_or_warns_about_is_refused_though_the_regex_module_would_take_it(pattern):
    with pytest.raises(pp.PromptConfigError):
        pp._compile_pattern(pattern, "", "pattern")


@pytest.mark.filterwarnings("ignore")
def test_a_warning_of_another_thread_does_not_condemn_a_pattern(monkeypatch):
    real = pp._sre_parse.parse

    def noisy(pattern, flags=0):
        warnings.warn("somebody else's deprecation", DeprecationWarning)
        warnings.warn("Possible but not a future warning", UserWarning)
        warnings.warn("a future warning of another library", FutureWarning)
        return real(pattern, flags)

    monkeypatch.setattr(pp._sre_parse, "parse", noisy)
    assert pp._compile_pattern(r"a+b", "", "pattern").search("xaab")
    with pytest.raises(pp.PromptConfigError, match="suspicious"):
        pp._compile_pattern(r"[[:alpha:]]", "", "pattern")


def test_without_the_regex_package_the_relay_runs_but_a_document_with_a_pattern_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "_regex", None)
    with pytest.raises(pp.PromptConfigError, match="regex"):
        pp._compile_pattern("a+", "", "pattern")
    compile_doc(document(profiles={"p": {"head": "x", "client_system": {"edits": [
        {"op": "replace_text", "find": "a", "with": "b"}]}}}), tmp_path)
    assert pp.REGEX_ERRORS == (pp.re.error,) or pp.re.error in pp.REGEX_ERRORS


def test_compile_pattern_is_the_one_door_for_a_pattern_with_the_rules_of_a_document():
    assert isinstance(pp.compile_pattern("a+", "i"), regex.Pattern)
    with pytest.raises(pp.PromptConfigError):
        pp.compile_pattern("(a+)+$")
    with pytest.raises(pp.PromptConfigError, match="only i, m and s"):
        pp.compile_pattern("a", "x")


def test_a_hostile_pattern_costs_no_time_at_compile(tmp_path):
    started = time.perf_counter()
    for depth in (30, 120, 250):
        for pattern in ("(?:" * depth + "a+" + ")+" * depth, "(" * depth + "a" + ")" * depth):
            try:
                pp._compile_pattern(pattern, "", "pattern")
            except pp.PromptConfigError:
                pass
    assert time.perf_counter() - started < 2.0


def test_the_snapshot_runs_nothing_through_re_on_a_request(tmp_path, monkeypatch):
    store = make_store(tmp_path, document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_regex", "pattern": "<env>.*?</env>", "flags": "s"},
        {"op": "replace_regex", "pattern": r"(\w+) rules", "with": r"\1 laws"}]}}}, default_profile="p"))
    plan = store.plan(inp())
    plan.compose_head(OPENCODE_SYSTEM, Sources())
    calls = []
    import re
    real = re.Pattern.search
    monkeypatch.setattr(pp.re, "compile", lambda *args, **kwargs: calls.append(args) or (_ for _ in ()).throw(
        AssertionError("re.compile on a request")))
    head, _parts, _warnings = plan.compose_head(OPENCODE_SYSTEM + "\nmy rules\n", Sources())
    assert "<env>" not in head and "my laws" in head and calls == [] and real is re.Pattern.search


# -- the guarantee is the deadline ---------------------------------------------------------------------------------------------

_ATOMS = ["a", "b", "ab", ".", r"\w", r"\s", r"[ab]", r"[^a]", r"\S", r"[a-z]", r"\d", "x", r"\n", r"[\w.-]", r"[^\n]", r"[ \t]"]
_QUANTS = ["", "", "", "*", "+", "?", "{1,3}", "{0,5}", "{2,}", "{1,8}", "{1,40}", "{1,200}", "{2,6}", "*?", "+?", "{1,5}?",
           "{3}", "{1,999}"]


def random_pattern(rnd):
    """A pattern of the grammar the reviewer fuzzed the analysis with (nested groups, alternatives, lookarounds, \\1)."""
    groups = []

    def gen(depth):
        roll = rnd.random()
        if depth <= 0 or roll < 0.35:
            return rnd.choice(_ATOMS) + rnd.choice(_QUANTS)
        if roll < 0.55:
            body = "".join(gen(depth - 1) for _ in range(rnd.randint(1, 3)))
            kind = rnd.random()
            if kind < 0.5:
                groups.append(1)
                return "(" + body + ")" + rnd.choice(_QUANTS)
            if kind < 0.9:
                return "(?:" + body + ")" + rnd.choice(_QUANTS)
            return "(?>" + body + ")" + rnd.choice(_QUANTS)
        if roll < 0.75:
            return "(?:" + "|".join(gen(depth - 1) for _ in range(rnd.randint(2, 3))) + ")" + rnd.choice(_QUANTS)
        if roll < 0.85:
            return rnd.choice(["(?=", "(?!", "(?<=a)(?=", "(?<!b)(?="]) + gen(depth - 1) + ")"
        if roll < 0.92 and groups:
            return "\\1" + rnd.choice(_QUANTS)
        return rnd.choice(["^", "$", r"\b", ""]) + gen(depth - 1)

    return "".join(gen(3) for _ in range(rnd.randint(1, 4))) + rnd.choice(["", "", "x", "c", "$", "!"])


def test_no_pattern_the_analysis_accepts_holds_a_request_beyond_its_budget(tmp_path, monkeypatch):
    import random
    monkeypatch.setattr(pp, "REGEX_TIMEOUT", 0.03)
    monkeypatch.setattr(pp, "REGEX_BUDGET", 0.12)
    rnd = random.Random(20261107)
    accepted = []
    while len(accepted) < 150:
        pattern = random_pattern(rnd)
        if len(pattern) <= 200 and pattern not in accepted:
            try:
                pp.compile_pattern(pattern)
            except pp.PromptConfigError:
                continue
            accepted.append(pattern)
    texts = ["a" * 4000, "ab" * 2000, "a" * 4000 + "c", "a" * 4000 + "!", "a " * 2000, "\n".join(["aa"] * 2000), "x" * 4000,
             "ab\n" * 1300]
    slowest = 0.0
    for pattern in accepted:
        snapshot = compile_doc(user_block_document(pattern), tmp_path)
        for text in texts:
            started = time.perf_counter()
            prompt_body.rewrite(stuck_body(text), "chat", snapshot.profile("p"), snapshot)
            slowest = max(slowest, time.perf_counter() - started)
    assert slowest < 0.5, "a request was held for %.2f s" % slowest
