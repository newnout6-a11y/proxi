"""The reviewer's remark on rewrite(rewrite(x)) == rewrite(x): it holds, except where a deletion makes a new copy of what it deletes.

`replace_text` with find="ab" and with="" turns "aabb" into "ab": the removal joins the "a" and the "b" around it into a new
"ab", which a second run removes. The relay never rewrites its own output: the client sends the history it has, the relay rewrites
that once per request, so the upstream request is the same on every turn (plan I2); the property that matters is determinism,
and it holds. The docstring of prompt_body.rewrite says so; these tests pin what is true.
"""
import pytest

import prompt_body
from test_prompt_body_support import DIALECTS, FakeSnapshot, conversation, history_of, profile


def run(data, rules):
    return prompt_body.rewrite(data, "chat", rules, FakeSnapshot())[0]


def single(text):
    return {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": text}]}


def text_of(data):
    return data["messages"][0]["content"]


def test_a_removal_that_joins_its_neighbours_into_a_new_copy_is_not_idempotent():
    rules = profile(user_blocks=[{"op": "replace_text", "find": "ab", "with": ""}])
    once = run(single("aabb and xxyy"), rules)
    twice = run(once, rules)
    assert text_of(once) == "ab and xxyy" and text_of(twice) == " and xxyy"


def test_the_same_input_always_gives_the_same_output():
    rules = profile(user_blocks=[{"op": "replace_text", "find": "ab", "with": ""}, {"op": "remove_regex", "pattern": "xy"}])
    data = single("aabb and xxyy")
    results = [text_of(run(data, rules)) for _ in range(5)]
    assert len(set(results)) == 1 and data == single("aabb and xxyy")


@pytest.mark.parametrize("rule, text", [
    ({"op": "replace_text", "find": "a", "with": "b"}, "banana"),
    ({"op": "replace_text", "find": "a", "with": "aa"}, "banana"),
    ({"op": "replace_text", "find": "ab", "with": "xy"}, "aabb ab"),
    ({"op": "remove_regex", "pattern": r"\d+"}, "a1b22c333"),
    ({"op": "remove_regex", "pattern": "[xy]"}, "xxyy zz"),
    ({"op": "drop_tag", "tag": "t"}, "a\n<t>1</t>\nb\n<t>2</t>\nc"),
    ({"op": "drop_tag", "tag": "t", "contains": "1"}, "a\n<t>1</t>\nb\n<t>2</t>\nc")])
def test_an_edit_whose_result_cannot_contain_what_it_looks_for_is_idempotent(rule, text):
    rules = profile(user_blocks=[rule])
    once = run(single(text), rules)
    assert run(once, rules) == once


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_rewritten_history_is_what_every_later_turn_sends_upstream(dialect):
    turns = [("lead", "H"), ("user", "first <system-reminder>\nPlan mode\n</system-reminder>"), ("assistant", "ok"),
             ("user", "second")]
    rules = profile(user_blocks=[{"op": "drop_tag", "tag": "system-reminder"}])
    short = prompt_body.rewrite(conversation(dialect, turns[:2]), dialect, rules, FakeSnapshot())[0]
    longer = prompt_body.rewrite(conversation(dialect, turns), dialect, rules, FakeSnapshot())[0]
    assert history_of(longer, dialect)[:len(history_of(short, dialect))] == history_of(short, dialect)


# -- replace_text whose replacement holds the searched text -------------------------------------------------------------------


def alternation_reference(text, find, replacement, replace_all):
    """The version that used one regular expression, `replacement|find`: the oracle of the str.find version."""
    import re
    both = re.compile(re.escape(replacement) + "|" + re.escape(find))
    removed = added = 0
    done = False

    def swap(match):
        nonlocal removed, added, done
        if done:
            return match.group(0)
        done = not replace_all
        if match.group(0) == replacement:
            return replacement
        removed += len(find)
        added += len(replacement)
        return replacement

    if find not in text:
        return None
    new = both.sub(swap, text)
    return None if new == text else (new, removed, added)


def test_replace_text_with_a_replacement_that_holds_the_searched_text_answers_as_the_alternation_did():
    import random
    from types import SimpleNamespace
    rnd = random.Random(20261107)
    context = SimpleNamespace(allowance=lambda scope: 10 ** 9)
    cases = [("a", "xay"), ("a", "aa"), ("ab", "aab"), ("ab", "abab"), ("x", "axa"), ("a", "ba"), ("ab", "xaby"), ("aa", "aaa")]
    for find, replacement in cases:
        for replace_all in (True, False):
            fn = prompt_body._replace_text_fn(find, replacement, replace_all, context, "history")
            for _ in range(600):
                text = "".join(rnd.choice("abxy ") for _ in range(rnd.randint(0, 14)))
                assert fn(text) == alternation_reference(text, find, replacement, replace_all), (find, replacement, replace_all, text)


def test_such_a_replacement_costs_linear_time_on_a_text_full_of_the_prefixes_of_its_literals():
    # `re` tried the long replacement at every position of this text: 400 000 x 100 000 character comparisons, under the GIL.
    import time
    replacement = "a" * 100_000 + "b"
    text = "a" * 400_000
    started = time.perf_counter()
    result, changes = prompt_body.rewrite(
        {"model": "m", "messages": [{"role": "user", "content": text}]}, "chat",
        profile(user_blocks=[{"op": "replace_text", "find": "a", "with": replacement, "all": False}]), FakeSnapshot())
    assert time.perf_counter() - started < 1.0
    assert result["messages"][0]["content"] == replacement + text[1:]
    assert changes == [{"scope": "history", "op": "replace_text", "edit": 0, "index": 0, "target": "user",
                        "removed_chars": 1, "added_chars": 100_001}]