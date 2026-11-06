"""R1-04: remove_tag, keep_tag_inner, remove_section and keep_sections cost time linear in the text (plan I11).

The first two looked back to the start of the line for every tag (and ran `[^>]*` past any `<`), the last two copied the tail of
the heading list for every heading: seconds per request on 200-480 KB of a system prompt, and the system prompt can carry text
an attacker controls (an AGENTS.md). The edits of the system are also remembered per (revision, profile, text) because
OpenCode sends the same system on every turn.
"""
import hashlib
import threading
import time

import pytest

import prompt_body
import prompt_policy as pp
from test_prompt_body_support import FakeSnapshot, profile
from test_prompt_policy_common import OPENCODE_SYSTEM, Sources, compile_doc, document, inp, make_store


def edited(tmp_path, text, *edits):
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": list(edits)}}}), tmp_path)
    return pp._apply_system_edits(text, snapshot.profile("p")["client_system"]["edits"], snapshot)


def timed(call):
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


# -- the reviewer's cases ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [40_000, 80_000])
def test_repeated_openers_without_a_closing_angle_cost_linear_time(tmp_path, count):
    text = "Instructions from AGENTS.md:\n" + "<env " * count
    (result, report), seconds = timed(lambda: edited(tmp_path, text, {"op": "remove_tag", "tag": "env"}))
    assert result == text and report[0]["matched"] is False
    assert seconds < 1.0, "%d KB took %.2f s" % (len(text) // 1000, seconds)


@pytest.mark.parametrize("op", ["remove_tag", "keep_tag_inner"])
def test_tens_of_thousands_of_blocks_on_one_line_cost_linear_time(tmp_path, op):
    text = "<env>x</env>" * 80_000
    (result, report), seconds = timed(lambda: edited(tmp_path, text, {"op": op, "tag": "env"}))
    assert report[0]["matched"] is True
    # Only the first tag begins the line: the rest are mentions in the middle of it and stay.
    assert result == ("" if op == "remove_tag" else "x") + text[len("<env>x</env>"):]
    assert seconds < 1.0, "%d KB took %.2f s" % (len(text) // 1000, seconds)


@pytest.mark.parametrize("separator", ["\n", " \n", "\n\n"])
def test_tens_of_thousands_of_blocks_on_their_own_lines_are_all_cut_in_linear_time(tmp_path, separator):
    text = ("<env>x</env>" + separator) * 60_000 + "tail"
    for op in ("remove_tag", "keep_tag_inner"):
        (result, _report), seconds = timed(lambda: edited(tmp_path, text, {"op": op, "tag": "env"}))
        assert result.endswith("tail") and seconds < 1.5, (op, seconds)
        assert ("<env>" not in result) and (op == "remove_tag" or result.count("x") == 60_000)


@pytest.mark.parametrize("op, edit", [("remove_section", {"op": "remove_section", "heading": "## A"}),
                                      ("keep_sections", {"op": "keep_sections", "headings": ["## A"]})])
def test_many_equal_headings_cost_linear_time(tmp_path, op, edit):
    text = "intro\n" + "## A\nx\n" * 80_000
    (result, report), seconds = timed(lambda: edited(tmp_path, text, edit))
    assert report[0]["matched"] is True and seconds < 1.0, "%d KB took %.2f s" % (len(text) // 1000, seconds)
    assert result == ("intro" if op == "remove_section" else text)


def test_nested_headings_cost_linear_time_as_well(tmp_path):
    parts = []
    for index in range(30_000):
        parts.append("%s H%d\nbody\n" % ("#" * (1 + index % 6), index))
    text = "pre\n" + "".join(parts)
    for op, edit in (("remove_section", {"op": "remove_section", "heading": "# H0"}),
                     ("keep_sections", {"op": "keep_sections", "headings": ["# H0", "## H1", "### H2"]})):
        (result, _), seconds = timed(lambda: edited(tmp_path, text, edit))
        assert seconds < 1.0 and result.startswith("pre"), (op, seconds)


# -- the same answers as the naive code ------------------------------------------------------------------------------------


def naive_starts_a_line(text, position):
    return not text[text.rfind("\n", 0, position) + 1:position].strip(" \t")


def naive_elements(text, tag):
    """The scan of the first version (a look back for every tag), over the pattern of the linear one."""
    elements, stack = [], []
    for match in pp._tag_pattern(tag).finditer(text):
        if match.group(1):
            if stack:
                opened, block = stack.pop()
                if block:
                    elements.append((opened.start(), opened.end(), match.start(), match.end()))
        elif match.group(0).endswith("/>"):
            if naive_starts_a_line(text, match.start()):
                elements.append((match.start(), match.end(), match.end(), match.end()))
        else:
            stack.append((match, naive_starts_a_line(text, match.start())))
    return elements


def naive_with_indent(text, position):
    return text.rfind("\n", 0, position) + 1 if naive_starts_a_line(text, position) else position


def naive_remove_tag(text, tag):
    elements = naive_elements(text, tag)
    spans = []
    for start, _open_end, _close_start, end in sorted(elements):
        if not spans or start >= spans[-1][1]:
            spans.append((naive_with_indent(text, start), end))
    return pp._delete_spans(text, spans) if spans else text


def naive_keep_inner(text, tag):
    spans = []
    for start, open_end, close_start, end in naive_elements(text, tag):
        spans.append((naive_with_indent(text, start), open_end))
        if close_start < end:
            spans.append((naive_with_indent(text, close_start), end))
    return pp._delete_spans(text, pp._merge_spans(spans)) if spans else text


def random_markup(rnd, size):
    pieces = ["<t>", "</t>", "<t/>", "<t k='v'>", "<t\n  k='v'\n>", "</t >", "<tt>", "</tt>", "\n", "\n", " ", "\t", "  ", "x", "y z",
              "<u>", "</u>", "\r\n", "<t>\n", "\n</t>", "<t", ">", "< t>", "a < b", "```", "# H\n"]
    return "".join(rnd.choice(pieces) for _ in range(size))


def test_the_linear_tag_edits_answer_as_the_naive_ones_do_on_random_markup(tmp_path):
    import random
    rnd = random.Random(20261106)
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_tag", "tag": "t"}, {"op": "keep_tag_inner", "tag": "t"}]}}}), tmp_path)
    run = pp._Run(snapshot, pp.Deadline(budget=1e9))
    for _ in range(1500):
        text = random_markup(rnd, rnd.randint(0, 40))
        assert pp._op_remove_tag(text, {"tag": "t"}, run)[0] == naive_remove_tag(text, "t"), repr(text)
        assert pp._op_keep_tag_inner(text, {"tag": "t"}, run)[0] == naive_keep_inner(text, "t"), repr(text)


def naive_section_end(headings, position, level, size):
    for other_level, start, _line in headings[position + 1:]:
        if other_level <= level:
            return start
    return size


def test_the_section_ends_are_the_ones_the_naive_search_finds():
    import random
    rnd = random.Random(7)
    for _ in range(500):
        count = rnd.randint(0, 40)
        headings, offset = [], 0
        for _ in range(count):
            headings.append((rnd.randint(1, 6), offset, "#"))
            offset += rnd.randint(2, 9)
        size = offset + 5
        expected = [naive_section_end(headings, position, level, size) for position, (level, _s, _l) in enumerate(headings)]
        assert pp._section_ends(headings, size) == expected


def test_remove_section_and_keep_sections_answer_as_before_on_a_document(tmp_path):
    doc = "Intro\n\n# A\na body\n\n## A1\nsub\n\n# B\nb body\n\n## A1\nagain\n\n# C\nc body\n"
    assert edited(tmp_path, doc, {"op": "remove_section", "heading": "## A1"})[0] == (
        "Intro\n\n# A\na body\n\n# B\nb body\n\n# C\nc body\n")
    assert edited(tmp_path, doc, {"op": "keep_sections", "headings": ["## A1", "# C"]})[0] == (
        "Intro\n\n## A1\nsub\n\n## A1\nagain\n\n# C\nc body\n")


def test_a_tag_may_have_attributes_up_to_512_characters_and_not_across_angle_brackets(tmp_path):
    attribute = lambda size: "<env k=\"" + "v" * size + "\">"
    assert edited(tmp_path, "a\n" + attribute(400) + "x</env>\nb", {"op": "remove_tag", "tag": "env"})[0] == "a\nb"
    kept, report = edited(tmp_path, "a\n" + attribute(600) + "x</env>\nb", {"op": "remove_tag", "tag": "env"})
    assert report[0]["matched"] is False and kept.startswith("a\n<env k=")
    kept, report = edited(tmp_path, "a\n<env k=\"<\">x</env>\nb", {"op": "remove_tag", "tag": "env"})
    assert report[0]["matched"] is False
    assert pp._tag_pattern("env").pattern.endswith("[^<>]{0,512}>")


# -- the body side ------------------------------------------------------------------------------------------------------


def test_a_cut_block_followed_by_a_long_blank_run_does_not_make_every_later_block_rescan_it():
    # After a cut, a block in the middle of the line is looked at to see whether only blanks lie between it and the cut one.
    # That look was repeated from the cut for each block: 20 000 blanks x 20 000 blocks.
    text = "<t>x</t>" + " " * 20_000 + "<t>y</t>" * 20_000
    data = {"model": "m", "messages": [{"role": "user", "content": "go"}, {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "r", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": text}, {"role": "user", "content": "more"}]}
    rules = profile(user_blocks=[{"op": "drop_tag", "tag": "t", "contains": "x"}])
    (result, changes), seconds = timed(lambda: prompt_body.rewrite(data, "chat", rules, FakeSnapshot()))
    assert seconds < 1.0, "took %.2f s" % seconds
    assert result["messages"][2]["content"] == " " * 20_000 + "<t>y</t>" * 20_000 and changes[0]["removed_chars"] == 8


# -- remembering the edits of the system -----------------------------------------------------------------------------------


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real = pp._apply_system_edits

    def counting(text, edits, snapshot, deadline=None):
        calls.append(len(text))
        return real(text, edits, snapshot, deadline)

    monkeypatch.setattr(pp, "_apply_system_edits", counting)
    return calls


def memo_document(**profile_extra):
    return document(texts={"t": "NOTE"}, profiles={
        "p": {"head": "{{client_system}}", "client_system": {"edits": [
            {"op": "remove_tag", "tag": "env"}, {"op": "remove_section", "heading": "# Code comments"},
            {"op": "append", "text": "{{text:t}}"}]}, **profile_extra},
        "q": {"head": "{{client_system}}", "client_system": {"edits": [{"op": "remove_tag", "tag": "env"}]}}})


def compose_with(store_or_snapshot, name, system, tmp_path=None):
    snapshot = store_or_snapshot
    plan = pp.Plan(rev=snapshot.rev, profile=name, rule="t", snapshot=snapshot)
    return plan.compose_head(system, Sources())


def test_the_same_system_is_edited_once_per_revision_and_profile(tmp_path, counted):
    snapshot = compile_doc(memo_document(), tmp_path)
    first = compose_with(snapshot, "p", OPENCODE_SYSTEM)
    again = compose_with(snapshot, "p", OPENCODE_SYSTEM)
    assert again == first and counted == [len(OPENCODE_SYSTEM)]
    assert "<env>" not in first[0] and "Code comments" not in first[0] and first[0].endswith("NOTE")
    compose_with(snapshot, "q", OPENCODE_SYSTEM)                       # another profile
    compose_with(snapshot, "p", OPENCODE_SYSTEM + "more")                # another text
    assert len(counted) == 3
    other = compile_doc(memo_document(), tmp_path, rev=1)               # the same document, another snapshot
    compose_with(other, "p", OPENCODE_SYSTEM)
    assert len(counted) == 4
    compose_with(other, "p", OPENCODE_SYSTEM)
    compose_with(snapshot, "p", OPENCODE_SYSTEM)
    assert len(counted) == 4


def test_a_remembered_answer_is_the_same_bytes_parts_and_warnings(tmp_path, counted):
    doc = document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_tag", "tag": "env"}, {"op": "remove_tag", "tag": "none"}]}}})
    snapshot = compile_doc(doc, tmp_path)
    runs = [compose_with(snapshot, "p", OPENCODE_SYSTEM) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2] and len(counted) == 1
    assert runs[0][2] == ["client_system.edits[1] (remove_tag) matched nothing"]
    assert [part.source for part in runs[0][1]] == ["client_edited"]


def test_the_report_of_a_remembered_edit_is_a_copy(tmp_path, counted):
    snapshot = compile_doc(memo_document(), tmp_path)
    plan = pp.Plan(rev=1, profile="p", rule="t", snapshot=snapshot)
    first = pp._compose(plan, OPENCODE_SYSTEM, Sources())[3]
    first[0]["matched"] = "TAMPERED"
    first.append({"junk": True})
    second = pp._compose(plan, OPENCODE_SYSTEM, Sources())[3]
    assert second[0]["matched"] is True and len(second) == 3 and len(counted) == 1


def test_the_memory_is_a_bounded_lru(tmp_path, counted, monkeypatch):
    monkeypatch.setattr(pp, "_MEMO_ENTRIES", 3)
    snapshot = compile_doc(memo_document(), tmp_path)
    texts = ["system %d\n<env>x</env>" % number for number in range(5)]
    for text in texts:
        compose_with(snapshot, "p", text)
    assert len(counted) == 5 and len(pp._MEMO) <= 3
    compose_with(snapshot, "p", texts[4])               # the newest are remembered
    compose_with(snapshot, "p", texts[3])
    assert len(counted) == 5
    compose_with(snapshot, "p", texts[0])               # the oldest was let go
    assert len(counted) == 6
    compose_with(snapshot, "p", texts[3])               # a use keeps an entry young
    compose_with(snapshot, "p", "another\n<env>y</env>")
    compose_with(snapshot, "p", texts[3])
    assert len(counted) == 7


def test_the_memory_has_a_limit_in_characters_too(tmp_path, counted, monkeypatch):
    monkeypatch.setattr(pp, "_MEMO_CHARS", 1000)
    snapshot = compile_doc(memo_document(), tmp_path)
    big = "line\n" * 400 + "<env>x</env>"
    compose_with(snapshot, "p", big)
    compose_with(snapshot, "p", big)
    assert len(counted) == 2                       # too big to be kept
    small = "small\n<env>x</env>"
    for _ in range(3):
        compose_with(snapshot, "p", small)
    assert len(counted) == 3
    assert pp._MEMO_HELD[0] == sum(len(entry[0]) for entry in pp._MEMO.values())


def test_the_key_is_the_text_and_not_its_identity_or_a_short_prefix(tmp_path, counted):
    snapshot = compile_doc(memo_document(), tmp_path)
    one = "A" * 5000 + "\n<env>x</env>\n"
    two = "A" * 5000 + "\n<env>y</env>\nB"
    assert compose_with(snapshot, "p", one)[0] != compose_with(snapshot, "p", two)[0]
    assert compose_with(snapshot, "p", "".join(list(one)))[0] == compose_with(snapshot, "p", one)[0]
    assert len(counted) == 2
    key = (snapshot._token, "p", hashlib.sha256(one.encode("utf-8")).digest())
    assert key in pp._MEMO


def test_a_stuck_edit_is_remembered_too_so_the_next_turn_does_not_wait_again(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "REGEX_TIMEOUT", 0.05)
    monkeypatch.setattr(pp, "REGEX_BUDGET", 0.2)
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_regex", "pattern": r"(?:(?:a{1,8}){1,8}){1,8}b"}]}}}), tmp_path)
    text = "a" * 200
    _first, first_seconds = timed(lambda: compose_with(snapshot, "p", text))
    again, second_seconds = timed(lambda: compose_with(snapshot, "p", text))
    assert first_seconds > 0.04 and second_seconds < 0.02 and again[2][0].startswith("client_system.edits[0]")


def test_edits_of_a_system_run_in_parallel_threads_agree(tmp_path):
    snapshot = compile_doc(memo_document(), tmp_path)
    expected = compose_with(snapshot, "p", OPENCODE_SYSTEM)
    seen = []

    def work():
        for _ in range(50):
            seen.append(compose_with(snapshot, "p", OPENCODE_SYSTEM))

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 300 and all(item == expected for item in seen)


def test_a_new_revision_does_not_see_the_old_memory(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": "Platform", "with": "OS"}]}}}, default_profile="p"))
    first = store.plan(inp(conversation_key="old")).compose_head(OPENCODE_SYSTEM, Sources())[0]
    assert "OS: win32" in first
    store.save(document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": "Platform", "with": "System"}]}}}, default_profile="p"), 1)
    second = store.plan(inp(conversation_key="new")).compose_head(OPENCODE_SYSTEM, Sources())[0]
    assert "System: win32" in second and "OS: win32" not in second
    again = store.plan(inp(conversation_key="old")).compose_head(OPENCODE_SYSTEM, Sources())[0]
    assert again == first                    # a conversation pinned to the first revision keeps its own head


def test_the_memory_does_not_change_what_a_head_costs_the_first_time(tmp_path):
    snapshot = compile_doc(memo_document(), tmp_path)
    plan = pp.Plan(rev=1, profile="p", rule="t", snapshot=snapshot)
    _, first = timed(lambda: plan.compose_head("x" * 17_500 + "\n<env>a</env>\n", Sources()))
    assert first < 0.05
