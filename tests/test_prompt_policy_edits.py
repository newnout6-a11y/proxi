"""Edits of the client's system prompt (plan 2.3): each op, its whitespace tidying and the not-matched report."""
import pytest

import prompt_policy as pp
from test_prompt_policy_common import OPENCODE_SYSTEM, compile_doc, compose, document, profile_doc


def run_edits(tmp_path, system, *edits):
    """(edited text, report) of the edits of a profile over `system`."""
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": list(edits)}}}), tmp_path)
    return pp._apply_system_edits(system, snapshot.profile("p")["client_system"]["edits"], snapshot)


def edited(tmp_path, system, *edits):
    return run_edits(tmp_path, system, *edits)[0]


def matched(tmp_path, system, *edits):
    return [entry["matched"] for entry in run_edits(tmp_path, system, *edits)[1]]


DOC = "Intro\n\n# A\na body\n\n## A1\nsub\n\n# B\nb body\n\n# C\nc body\n"


# -- remove_section --------------------------------------------------------------------------------------------------


def test_remove_section_takes_the_subsections_with_it(tmp_path):
    assert edited(tmp_path, DOC, {"op": "remove_section", "heading": "# A"}) == (
        "Intro\n\n# B\nb body\n\n# C\nc body\n")


def test_remove_section_of_a_subsection_stops_at_the_next_heading_of_the_same_or_a_higher_level(tmp_path):
    assert edited(tmp_path, DOC, {"op": "remove_section", "heading": "## A1"}) == (
        "Intro\n\n# A\na body\n\n# B\nb body\n\n# C\nc body\n")


def test_remove_section_of_the_last_section_leaves_no_dangling_blank_lines(tmp_path):
    assert edited(tmp_path, DOC, {"op": "remove_section", "heading": "# C"}) == (
        "Intro\n\n# A\na body\n\n## A1\nsub\n\n# B\nb body")


def test_remove_section_of_the_first_heading_of_the_text(tmp_path):
    assert edited(tmp_path, "# A\nx\n\n# B\ny\n", {"op": "remove_section", "heading": "# A"}) == "# B\ny\n"


def test_remove_section_removes_every_heading_with_that_text(tmp_path):
    text = "# T0\n\n## S\na\n## S\nb\n# T\nz\n"
    assert edited(tmp_path, text, {"op": "remove_section", "heading": "## S"}) == "# T0\n\n# T\nz\n"


def test_remove_section_matches_the_whole_line_and_ignores_surrounding_blanks(tmp_path):
    text = "x\n\n   # Title  \nbody\n\n# Titles\nmore\n"
    assert edited(tmp_path, text, {"op": "remove_section", "heading": "  # Title"}) == "x\n\n# Titles\nmore\n"
    assert matched(tmp_path, "# Title and more\nx", {"op": "remove_section", "heading": "# Title"}) == [False]


def test_remove_section_works_on_crlf_text(tmp_path):
    text = "x\r\n\r\n# A\r\nbody\r\n\r\n# B\r\nend\r\n"
    assert edited(tmp_path, text, {"op": "remove_section", "heading": "# A"}) == "x\r\n\r\n# B\r\nend\r\n"


def test_remove_section_ignores_headings_inside_code_fences(tmp_path):
    text = "# Real\nx\n```sh\n# Not a heading\nls\n```\n~~~\n# Nor this\n~~~\n# Next\ny\n"
    assert matched(tmp_path, text, {"op": "remove_section", "heading": "# Not a heading"},
                   {"op": "remove_section", "heading": "# Nor this"}) == [False, False]
    assert edited(tmp_path, text, {"op": "remove_section", "heading": "# Real"}) == "# Next\ny\n"


def test_a_longer_fence_closes_a_shorter_one_and_a_shorter_does_not(tmp_path):
    text = "````\n```\n# inside\n```\n````\n# Out\nz\n"
    assert matched(tmp_path, text, {"op": "remove_section", "heading": "# inside"}) == [False]
    assert edited(tmp_path, text, {"op": "remove_section", "heading": "# Out"}) == "````\n```\n# inside\n```\n````"


def test_an_indented_code_line_and_a_hashtag_are_not_headings(tmp_path):
    text = "    # code\n#hashtag\n####### seven\n# Real\nx\n"
    assert matched(tmp_path, text, {"op": "remove_section", "heading": "# code"}) == [False]
    assert edited(tmp_path, text, {"op": "remove_section", "heading": "# Real"}) == "    # code\n#hashtag\n####### seven"


# -- keep_sections ---------------------------------------------------------------------------------------------------


def test_keep_sections_keeps_the_preamble_and_the_listed_sections(tmp_path):
    assert edited(tmp_path, DOC, {"op": "keep_sections", "headings": ["# B"]}) == "Intro\n\n# B\nb body"
    assert edited(tmp_path, DOC, {"op": "keep_sections", "headings": ["# A", "# C"]}) == (
        "Intro\n\n# A\na body\n\n## A1\nsub\n\n# C\nc body\n")


def test_keep_sections_of_a_subsection_drops_its_parent_heading(tmp_path):
    assert edited(tmp_path, DOC, {"op": "keep_sections", "headings": ["## A1"]}) == "Intro\n\n## A1\nsub"


def test_keep_sections_without_a_match_changes_nothing(tmp_path):
    assert run_edits(tmp_path, DOC, {"op": "keep_sections", "headings": ["# Nope"]}) == (
        DOC, [{"index": 0, "op": "keep_sections", "matched": False, "chars_before": len(DOC),
               "chars_after": len(DOC)}])
    assert matched(tmp_path, "no headings at all", {"op": "keep_sections", "headings": ["# A"]}) == [False]


def test_keep_sections_when_the_text_starts_with_a_heading(tmp_path):
    assert edited(tmp_path, "# A\na\n\n# B\nb\n", {"op": "keep_sections", "headings": ["# B"]}) == "# B\nb\n"


# -- remove_tag / keep_tag_inner -------------------------------------------------------------------------------------


def test_remove_tag_removes_a_block_that_fills_whole_lines(tmp_path):
    text = "a\n<available_skills>\nx\ny\n</available_skills>\nb"
    assert edited(tmp_path, text, {"op": "remove_tag", "tag": "available_skills"}) == "a\nb"
    assert edited(tmp_path, "a\n\n<t>x</t>\n\nb", {"op": "remove_tag", "tag": "t"}) == "a\n\nb"


def test_remove_tag_takes_a_block_with_attributes_and_an_indented_one(tmp_path):
    assert edited(tmp_path, '<t k="v">x</t> b', {"op": "remove_tag", "tag": "t"}) == " b"
    assert edited(tmp_path, "<t\n  k='v'\n>x</t >b", {"op": "remove_tag", "tag": "t"}) == "b"
    assert edited(tmp_path, "a\n   <t>\nx\n   </t>\nb", {"op": "remove_tag", "tag": "t"}) == "a\nb"
    assert edited(tmp_path, "a\n\t<t>x</t> tail\nb", {"op": "remove_tag", "tag": "t"}) == "a\n tail\nb"


def test_a_tag_that_is_only_mentioned_in_a_line_is_not_a_block(tmp_path):
    for text in ('a <t k="v">x</t> b', "Wrap it in <t> and </t> tags.", "- <t>item</t>", "x<t>1</t>y<t>2</t>z",
                 "see `<t>` and\nthe closing `</t>` later", "a<t/>b"):
        assert matched(tmp_path, text, {"op": "remove_tag", "tag": "t"}) == [False], text
        assert edited(tmp_path, text, {"op": "remove_tag", "tag": "t"}) == text
        assert matched(tmp_path, text, {"op": "keep_tag_inner", "tag": "t"}) == [False], text


def test_a_mention_in_a_sentence_does_not_swallow_the_text_up_to_a_later_closing_tag(tmp_path):
    text = "Use <env> for the environment.\n\nMore rules.\n\n<env>\nreal block\n</env>\nEnd"
    assert edited(tmp_path, text, {"op": "remove_tag", "tag": "env"}) == "Use <env> for the environment.\n\nMore rules.\n\nEnd"
    assert edited(tmp_path, text, {"op": "keep_tag_inner", "tag": "env"}) == (
        "Use <env> for the environment.\n\nMore rules.\n\nreal block\nEnd")


def test_remove_tag_removes_the_outermost_of_nested_blocks(tmp_path):
    assert edited(tmp_path, "<t>1<t>2</t>3</t>after", {"op": "remove_tag", "tag": "t"}) == "after"
    assert edited(tmp_path, "<t>\n1\n<t>\n2\n</t>\n3\n</t>\nafter", {"op": "remove_tag", "tag": "t"}) == "after"
    assert edited(tmp_path, "<t>1</t>y\n<t>2</t>z", {"op": "remove_tag", "tag": "t"}) == "y\nz"


def test_an_inline_opening_tag_still_counts_for_the_nesting_of_a_block(tmp_path):
    text = "<t>\nouter, with an inline <t>mention</t> inside\n</t>\nafter"
    assert edited(tmp_path, text, {"op": "remove_tag", "tag": "t"}) == "after"


def test_remove_tag_handles_self_closing_tags_and_leaves_unbalanced_ones(tmp_path):
    assert edited(tmp_path, "<t/>b\n<t />c", {"op": "remove_tag", "tag": "t"}) == "b\nc"
    assert matched(tmp_path, "x <t> y", {"op": "remove_tag", "tag": "t"}) == [False]
    assert matched(tmp_path, "<t>\nnever closed", {"op": "remove_tag", "tag": "t"}) == [False]
    assert matched(tmp_path, "x </t> y", {"op": "remove_tag", "tag": "t"}) == [False]
    assert edited(tmp_path, "<t>unclosed\n<t>closed</t>", {"op": "remove_tag", "tag": "t"}) == "<t>unclosed"


def test_remove_tag_does_not_confuse_tags_that_share_a_prefix(tmp_path):
    text = "<ab>x</ab>\n<a>y</a>\n<a-b>z</a-b>"
    assert edited(tmp_path, text, {"op": "remove_tag", "tag": "a"}) == "<ab>x</ab>\n<a-b>z</a-b>"


def test_keep_tag_inner_drops_only_the_tags_of_blocks(tmp_path):
    assert edited(tmp_path, "a\n<env>\nWD\n</env>\nb", {"op": "keep_tag_inner", "tag": "env"}) == "a\nWD\nb"
    assert edited(tmp_path, "a\n  <t>\n  x\n  </t>\nb", {"op": "keep_tag_inner", "tag": "t"}) == "a\n  x\nb"
    assert edited(tmp_path, "<t>\na\n<t>\nb\n</t>\nc\n</t>", {"op": "keep_tag_inner", "tag": "t"}) == "a\nb\nc"
    assert edited(tmp_path, '<t k="1">y</t>z', {"op": "keep_tag_inner", "tag": "t"}) == "yz"
    assert edited(tmp_path, "<t/>b", {"op": "keep_tag_inner", "tag": "t"}) == "b"
    assert edited(tmp_path, "<t>a<t>b</t>c</t>", {"op": "keep_tag_inner", "tag": "t"}) == "a<t>b</t>c"
    assert matched(tmp_path, "no tags", {"op": "keep_tag_inner", "tag": "t"}) == [False]


def test_opencode_style_prompt_loses_skills_and_env(tmp_path):
    text = OPENCODE_SYSTEM + "<available_skills>\n  <skill>x</skill>\n</available_skills>\n"
    result = edited(tmp_path, text, {"op": "remove_tag", "tag": "available_skills"},
                    {"op": "remove_tag", "tag": "env"}, {"op": "remove_section", "heading": "# Code comments"})
    assert "available_skills" not in result and "<env>" not in result and "Code comments" not in result
    assert result.startswith("You are an AI agent") and "# Tail\nToday's date: Thu Nov 05 2026" in result
    assert "\n\n\n" not in result


# -- remove_between --------------------------------------------------------------------------------------------------


def test_remove_between_keeps_the_markers_unless_inclusive(tmp_path):
    text = "keep START drop END keep"
    assert edited(tmp_path, text, {"op": "remove_between", "start": "START", "end": "END"}) == "keep STARTEND keep"
    assert edited(tmp_path, text, {"op": "remove_between", "start": "START", "end": "END", "inclusive": True}) == (
        "keep  keep")


def test_remove_between_removes_every_pair_and_stops_at_a_missing_end(tmp_path):
    text = "a<!--1-->b<!--2-->c<!--3"
    assert edited(tmp_path, text, {"op": "remove_between", "start": "<!--", "end": "-->", "inclusive": True}) == (
        "abc<!--3")
    assert matched(tmp_path, "no markers", {"op": "remove_between", "start": "<", "end": ">"}) == [False]
    assert matched(tmp_path, "a < b", {"op": "remove_between", "start": "<", "end": ">"}) == [False]


def test_remove_between_on_whole_lines(tmp_path):
    text = "a\n<!-- begin -->\nx\ny\n<!-- end -->\nb"
    assert edited(tmp_path, text, {"op": "remove_between", "start": "<!-- begin -->", "end": "<!-- end -->",
                                   "inclusive": True}) == "a\nb"


# -- regex and text --------------------------------------------------------------------------------------------------


def test_remove_regex_with_flags(tmp_path):
    text = "x\n<env>\nWD\n</env>\ny"
    assert edited(tmp_path, text, {"op": "remove_regex", "pattern": "<env>.*?</env>", "flags": "s"}) == "x\ny"
    assert matched(tmp_path, text, {"op": "remove_regex", "pattern": "<env>.*?</env>"}) == [False]
    assert edited(tmp_path, "Keep\nPLAN mode on\nkeep", {"op": "remove_regex", "pattern": "^plan mode.*$",
                                                          "flags": "im"}) == "Keep\nkeep"


def test_replace_regex_uses_groups_and_an_empty_replacement_removes(tmp_path):
    assert edited(tmp_path, "ann@home bob@work", {"op": "replace_regex", "pattern": r"(\w+)@(\w+)",
                                                  "with": r"\2:\1"}) == "home:ann work:bob"
    assert edited(tmp_path, "a\nSECRET\nb", {"op": "replace_regex", "pattern": "SECRET", "with": ""}) == "a\nb"
    assert matched(tmp_path, "abc", {"op": "replace_regex", "pattern": "z", "with": "y"}) == [False]


def test_replace_regex_can_insert_at_every_line(tmp_path):
    assert edited(tmp_path, "a\nb", {"op": "replace_regex", "pattern": "^", "flags": "m", "with": "> "}) == "> a\n> b"


def test_replace_text_all_or_first(tmp_path):
    assert edited(tmp_path, "a b a b", {"op": "replace_text", "find": "a", "with": "X"}) == "X b X b"
    assert edited(tmp_path, "a b a b", {"op": "replace_text", "find": "a", "with": "X", "all": False}) == "X b a b"
    assert matched(tmp_path, "abc", {"op": "replace_text", "find": "zz", "with": "y"}) == [False]


def test_replace_text_with_nothing_removes_the_line_it_empties(tmp_path):
    assert edited(tmp_path, "keep\nremove me\nkeep too", {"op": "replace_text", "find": "remove me", "with": ""}) == (
        "keep\nkeep too")
    assert edited(tmp_path, "x remove y", {"op": "replace_text", "find": "remove ", "with": ""}) == "x y"
    assert edited(tmp_path, "a-a-a", {"op": "replace_text", "find": "a", "with": "", "all": False}) == "-a-a"


def test_prepend_and_append_leave_one_blank_line(tmp_path):
    assert edited(tmp_path, "S", {"op": "append", "text": "T"}) == "S\n\nT"
    assert edited(tmp_path, "S\n", {"op": "append", "text": "T"}) == "S\n\nT"
    assert edited(tmp_path, "S\n\n", {"op": "append", "text": "T"}) == "S\n\nT"
    assert edited(tmp_path, "S", {"op": "prepend", "text": "T"}) == "T\n\nS"
    assert edited(tmp_path, "S", {"op": "prepend", "text": "T\n"}) == "T\n\nS"
    assert edited(tmp_path, "", {"op": "append", "text": "T"}) == "T"
    assert edited(tmp_path, "", {"op": "prepend", "text": "T"}) == "T"
    assert matched(tmp_path, "S", {"op": "append", "text": "T"}, {"op": "prepend", "text": "U"}) == [True, True]


def test_edits_run_in_order(tmp_path):
    text = "one two"
    assert edited(tmp_path, text, {"op": "replace_text", "find": "one", "with": "two"},
                  {"op": "replace_text", "find": "two", "with": "three"}) == "three three"
    assert edited(tmp_path, text, {"op": "replace_text", "find": "two", "with": "three"},
                  {"op": "replace_text", "find": "one", "with": "two"}) == "two three"


def test_the_report_says_what_each_edit_did(tmp_path):
    text, report = run_edits(tmp_path, DOC, {"op": "remove_section", "heading": "# A"},
                             {"op": "remove_tag", "tag": "none"}, {"op": "append", "text": "TAIL"})
    assert [entry["matched"] for entry in report] == [True, False, True]
    assert [entry["op"] for entry in report] == ["remove_section", "remove_tag", "append"]
    assert report[0]["chars_before"] == len(DOC) and report[0]["chars_after"] < len(DOC)
    assert report[1]["chars_before"] == report[1]["chars_after"] == report[0]["chars_after"]
    assert report[2]["chars_after"] == len(text)


# -- the tidying of blank lines around a deletion --------------------------------------------------------------------


@pytest.mark.parametrize("text, spans, expected", [
    ("abc", [], "abc"),
    ("a  b", [(1, 3)], "ab"),
    ("A\n\nXX\n\n\n\nB", [(3, 5)], "A\n\nB"),
    ("A\n\nXX\n\nB", [(3, 5)], "A\n\nB"),
    ("A\nXX\nB", [(2, 4)], "A\nB"),
    ("A\n\nXX B", [(3, 5)], "A\n\n B"),
    ("XX\n\nB", [(0, 2)], "B"),
    ("A\n\nXX", [(3, 5)], "A"),
    ("A\n\nXX\n", [(3, 5)], "A"),
    ("XX", [(0, 2)], ""),
    ("\n\nXX\n\nB", [(2, 4)], "B"),
    ("A\nXX\nYY\nB", [(2, 4), (5, 7)], "A\nB"),
    ("A\n\nXX\n\nYY\n\nB", [(3, 5), (7, 9)], "A\n\nB"),
    ("A\n\nXXYY\n\nB", [(3, 5), (5, 7)], "A\n\nB"),
    ("A\n\n\n\nB\n\n\n\nC", [(5, 6)], "A\n\nC"),
    ("A\n\n\nB\n\n\nC", [(4, 5)], "A\n\nC"),
])
def test_delete_spans_tidies_the_blank_lines_around_a_deletion(text, spans, expected):
    assert pp._delete_spans(text, spans) == expected


# -- through compose_head --------------------------------------------------------------------------------------------


def test_edited_text_is_labelled_client_edited_and_an_untouched_one_client(tmp_path):
    doc = profile_doc(client_system={"edits": [{"op": "remove_tag", "tag": "env"}]})
    head, parts, warnings = compose(doc, tmp_path, OPENCODE_SYSTEM)
    assert [part.source for part in parts] == ["client_edited"] and "<env>" not in head and warnings == []
    head, parts, _ = compose(doc, tmp_path, "nothing to cut")
    assert [part.source for part in parts] == ["client"] and head == "nothing to cut"


def test_a_missing_target_is_a_warning_and_not_an_error(tmp_path):
    doc = profile_doc(client_system={"edits": [
        {"op": "remove_section", "heading": "# Gone"}, {"op": "remove_tag", "tag": "env"},
        {"op": "keep_sections", "headings": ["# Nothing"]}, {"op": "replace_text", "find": "zzz", "with": "y"},
        {"op": "remove_between", "start": "<<", "end": ">>"}, {"op": "remove_regex", "pattern": "qqq"},
        {"op": "replace_regex", "pattern": "qqq", "with": "r"}, {"op": "keep_tag_inner", "tag": "nope"}]})
    head, _parts, warnings = compose(doc, tmp_path, OPENCODE_SYSTEM)
    assert "<env>" not in head
    unmatched = [line for line in warnings if "matched nothing" in line]
    assert len(unmatched) == 7
    assert "client_system.edits[0] (remove_section) matched nothing" in warnings
    assert not any("# Gone" in line for line in warnings)
    assert not any("edits[1]" in line for line in warnings)


def test_edits_are_ignored_when_the_template_has_no_client_system(tmp_path):
    doc = profile_doc(head="own text", client_system={"edits": [{"op": "remove_tag", "tag": "env"}]})
    head, _parts, warnings = compose(doc, tmp_path, OPENCODE_SYSTEM)
    assert head == "own text"
    assert any("not applied" in line for line in warnings)
    assert any("not part of the head" in line for line in warnings)


def test_client_system_raw_is_never_edited(tmp_path):
    doc = profile_doc(head="{{client_system_raw}}|{{client_system}}",
                      client_system={"edits": [{"op": "replace_text", "find": "Platform", "with": "OS"}]})
    head, _parts, _warnings = compose(doc, tmp_path, "Platform: x")
    assert head == "Platform: x|OS: x"
