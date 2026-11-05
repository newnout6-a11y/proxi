"""prompt_body.rewrite contract: pure, deterministic, idempotent, private, tolerant of odd bodies."""
import copy
import json
import re

import pytest

import prompt_body
import prompt_policy
from test_prompt_body_support import (
    DIALECTS, REMINDER, TOOL_REMINDER, FakeSnapshot, conversation, drop_tag, dump, history_of, leaves,
    neutral, parse, profile, rich_conversation)

PROFILES = {
    "drop_tag": profile(user_blocks=[drop_tag()]),
    "contains": profile(user_blocks=[drop_tag(contains="Plan mode")]),
    "pattern": profile(user_blocks=[drop_tag(pattern="Instructions from")]),
    "remove_regex": profile(user_blocks=[{"op": "remove_regex", "pattern": r"Use tabs\.", "flags": "i"}]),
    "replace_text": profile(user_blocks=[{"op": "replace_text", "find": "Thanks", "with": "Thanks a lot"}]),
    "replace_grows": profile(user_blocks=[{"op": "replace_text", "find": "Thanks", "with": "Thanks (twice)"}]),
    "wrap": profile(mid_system="wrap"),
    "plain": profile(mid_system="plain"),
    "drop": profile(mid_system="drop"),
    "tools": profile(tools=[
        {"tool": "bash", "op": "append", "text": "Ask before running anything destructive."},
        {"tool": "READ", "op": "prepend", "text": "Read-only."},
        {"tool": "read", "op": "replace_text", "find": "Read a file", "with": "Read a file from disk"},
        {"tool": "bash", "op": "replace_regex", "pattern": r"Run (\w+)", "with": r"Execute \1"},
        {"tool": "web*", "hide": True}]),
    "set": profile(tools=[{"tool": "bash", "op": "set", "text": "Only this."}]),
    "all": profile(mid_system="wrap", user_blocks=[drop_tag(), {"op": "replace_text", "find": "tests", "with": "suite"}],
                   tools=[{"tool": "bash", "op": "append", "text": "Careful."}, {"tool": "webfetch", "hide": True}]),
}


def run(data, dialect, name="all", snapshot=None):
    return prompt_body.rewrite(data, dialect, PROFILES[name], snapshot or FakeSnapshot())


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_empty_profile_returns_an_equal_body_that_shares_nothing_mutable(dialect):
    data = rich_conversation(dialect)
    data["extra"] = ({"nested": [1, 2, {"deep": "x"}]}, [("tuple", {"in": "list"})])
    result, changes = prompt_body.rewrite(data, dialect, profile(), FakeSnapshot())
    assert result == data and changes == []
    assert result is not data
    original = {id(item) for item in leaves(data)}
    assert not original & {id(item) for item in leaves(result)}
    assert result["extra"][0] is not data["extra"][0] and result["extra"][0]["nested"] is not data["extra"][0]["nested"]
    assert result["extra"][1][0][1] is not data["extra"][1][0][1]


@pytest.mark.parametrize("name", sorted(PROFILES))
@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_input_is_never_mutated_and_the_result_is_private(dialect, name):
    data = rich_conversation(dialect)
    before = copy.deepcopy(data)
    result, changes = run(data, dialect, name)
    assert data == before
    assert changes, "the matrix profiles all match something in the rich conversation"
    assert not {id(item) for item in leaves(data)} & {id(item) for item in leaves(result)}
    for container in leaves(result):
        # Writing into the result must not reach the caller's body.
        if isinstance(container, dict) and container:
            container["__probe__"] = True
        elif isinstance(container, list):
            container.append("__probe__")
    assert data == before


@pytest.mark.parametrize("name", sorted(PROFILES))
@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_same_input_always_gives_the_same_bytes(dialect, name):
    first = run(rich_conversation(dialect), dialect, name)
    second = run(rich_conversation(dialect), dialect, name)
    assert dump(first[0]) == dump(second[0])
    assert dump(first[1]) == dump(second[1])


@pytest.mark.parametrize("name", sorted(PROFILES))
@pytest.mark.parametrize("dialect", DIALECTS)
def test_rewriting_twice_changes_nothing_more(dialect, name):
    once, first_changes = run(rich_conversation(dialect), dialect, name)
    twice, second_changes = run(once, dialect, name)
    assert dump(twice) == dump(once)
    assert [c for c in second_changes if not c.get("skipped")] == []
    assert first_changes


@pytest.mark.parametrize("dialect", DIALECTS)
def test_several_appends_and_prepends_in_one_profile_settle_after_one_run(dialect):
    merged = profile(tools=[
        {"tool": "bash", "op": "append", "text": "First rule."}, {"tool": "bash", "op": "append", "text": "Second rule."},
        {"tool": "bash", "op": "prepend", "text": "Heading."}, {"tool": "bash", "op": "prepend", "text": "Subheading."}])
    once, changes = prompt_body.rewrite(rich_conversation(dialect), dialect, merged, FakeSnapshot())
    twice, again = prompt_body.rewrite(once, dialect, merged, FakeSnapshot())
    assert len(changes) == 4 and again == [] and dump(twice) == dump(once)


CHANGE_KEYS = {"scope", "op", "mode", "edit", "index", "target", "tool", "removed_chars", "added_chars", "deleted",
               "kept", "skipped", "reason"}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_changes_describe_edits_without_carrying_any_text(dialect):
    snapshot = FakeSnapshot(reminder="NOTE SECRET_WRAPPER {{text}}")
    secrets = ["SECRET_DESCRIPTION", "SECRET_FIND", "SECRET_WITH", "SECRET_PATTERN", "SECRET_WRAPPER"]
    data = rich_conversation(dialect)
    history_of(data, dialect).append({"role": "user", "content": "SECRET_FIND is here"} if dialect != "responses"
                                     else {"type": "message", "role": "user", "content": "SECRET_FIND is here"})
    merged = profile(
        mid_system="wrap",
        user_blocks=[drop_tag(contains="Plan mode"), {"op": "remove_regex", "pattern": "SECRET_PATTERN"},
                     {"op": "replace_text", "find": "SECRET_FIND", "with": "SECRET_WITH"}],
        tools=[{"tool": "bash", "op": "append", "text": "SECRET_DESCRIPTION"}, {"tool": "webfetch", "hide": True}])
    result, changes = prompt_body.rewrite(data, dialect, merged, snapshot)
    assert len(changes) >= 6
    wire = json.dumps(changes)
    assert not any(secret in wire for secret in secrets)
    assert REMINDER not in wire and "Late rule" not in wire
    for change in changes:
        assert set(change) <= CHANGE_KEYS and change["scope"] in ("history", "tools")
        assert all(isinstance(change.get(k, 0), int) for k in ("removed_chars", "added_chars", "index"))
    assert any(secret in json.dumps(result) for secret in ("SECRET_WRAPPER", "SECRET_WITH", "SECRET_DESCRIPTION"))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_change_entries_name_the_rule_the_message_and_the_target(dialect):
    rules = profile(mid_system="wrap",
                    user_blocks=[drop_tag(contains="Instructions from"), {"op": "replace_text", "find": "tests",
                                                                          "with": "suite"}],
                    tools=[{"tool": "bash", "op": "append", "text": "Careful."}, {"tool": "webfetch", "hide": True}])
    result, changes = prompt_body.rewrite(rich_conversation(dialect), dialect, rules, FakeSnapshot())
    shift = 1 if dialect == "chat" else 0      # chat keeps its head inside messages
    wrapper = len(prompt_body._render_reminder(None, "Late rule: reply in Russian.")) - len("Late rule: reply in Russian.")
    assert [c for c in changes if c["scope"] == "history"] == [
        {"scope": "history", "op": "drop_tag", "edit": 0, "index": 2 + shift, "target": "tool_result",
         "removed_chars": len("\n\n" + TOOL_REMINDER), "added_chars": 0},
        {"scope": "history", "op": "mid_system", "mode": "wrap", "index": 3 + shift, "target": "system",
         "removed_chars": 0, "added_chars": wrapper},
        {"scope": "history", "op": "replace_text", "edit": 1, "index": 5 + shift, "target": "user",
         "removed_chars": len("tests"), "added_chars": len("suite")},
        {"scope": "history", "op": "drop_tag", "edit": 0, "index": 7 + shift, "target": "tool_result",
         "removed_chars": len(TOOL_REMINDER), "added_chars": 0}]
    assert [(c["op"], c["tool"], c["index"], c["edit"]) for c in changes if c["scope"] == "tools"] == [
        ("append", "bash", 0, 0), ("hide", "webfetch", 2, 1)]


@pytest.mark.parametrize("dialect", ["unknown", "", None])
def test_an_unknown_dialect_returns_a_copy_untouched(dialect):
    data = rich_conversation("chat")
    result, changes = prompt_body.rewrite(data, dialect, PROFILES["all"], FakeSnapshot())
    assert result == data and result is not data and changes == []


@pytest.mark.parametrize("bad", [None, [], "profile", {"history": None, "tools": []}, {"history": [], "tools": None}])
def test_a_profile_without_usable_sections_changes_nothing(bad):
    data = rich_conversation("chat")
    result, changes = prompt_body.rewrite(data, "chat", bad, FakeSnapshot())
    assert result == data and changes == []


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("body", [
    {}, {"model": "m"}, {"messages": None, "input": None, "tools": None},
    {"messages": "text", "input": 7, "tools": "none"},
    {"messages": [None, 3, "text", [], {"role": 4}, {"content": []}, {}], "tools": [None, 1, [], {}, {"name": 5}]},
    {"input": [None, 3, "text", {"type": "message"}, {"type": "function_call_output"}], "tools": [{"type": "namespace"}]},
])
def test_odd_bodies_never_raise(dialect, body):
    result, _ = prompt_body.rewrite(body, dialect, PROFILES["all"], FakeSnapshot())
    assert result == body


@pytest.mark.parametrize("dialect", DIALECTS)
def test_odd_message_shapes_are_left_alone(dialect):
    odd = [{"role": "user", "content": {"type": "text", "text": REMINDER}},
           {"role": "user", "content": [None, 5, {"type": "text"}, {"type": "text", "text": 9},
                                        {"type": "mystery", "text": REMINDER}]},
           {"role": "tool", "tool_call_id": "x", "content": None},
           {"role": "user", "content": None}]
    body = {"messages": copy.deepcopy(odd)} if dialect != "responses" else {"input": copy.deepcopy(odd)}
    result, changes = prompt_body.rewrite(body, dialect, PROFILES["all"], FakeSnapshot())
    assert result == body and changes == []


class RaisesNotImplemented:
    """The skeleton Snapshot of a branch where prompt_policy is not implemented yet."""

    def regex(self, pattern, flags=""):
        raise NotImplementedError

    def relay_text(self, name):
        raise NotImplementedError


class WithoutHooks:
    pass


@pytest.mark.parametrize("snapshot", [None, WithoutHooks(), RaisesNotImplemented()],
                         ids=["none", "no-hooks", "not-implemented"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_without_snapshot_hooks_patterns_compile_locally_and_the_built_in_wrapper_is_used(dialect, snapshot):
    merged = profile(mid_system="wrap", user_blocks=[
        {"op": "remove_regex", "pattern": r"plan MODE is active\.[^\n]*", "flags": "i"},
        drop_tag(pattern=r"AGENTS\.md")])
    result, changes = prompt_body.rewrite(rich_conversation(dialect), dialect, merged, snapshot)
    turns = neutral(result, dialect)
    assert ("system", "Late rule: reply in Russian.") not in turns
    assert ("user", "<system-reminder>\nLate rule: reply in Russian.\n</system-reminder>") in turns
    assert not any("Plan mode is active" in str(t) or "AGENTS.md" in str(t) for t in turns)
    assert {c["op"] for c in changes} == {"remove_regex", "drop_tag", "mid_system"}


def test_the_snapshot_supplies_the_compiled_patterns_and_the_wrapper():
    snapshot = FakeSnapshot(reminder="[note]\n{{text}}\n[/note]")
    merged = profile(mid_system="wrap", user_blocks=[
        {"op": "remove_regex", "pattern": "Use tabs", "flags": "im"}, drop_tag(pattern="Plan", flags="s")])
    result, _ = prompt_body.rewrite(rich_conversation("chat"), "chat", merged, snapshot)
    assert ("Use tabs", "im") in snapshot.regex_calls and ("Plan", "s") in snapshot.regex_calls
    assert ("user", "[note]\nLate rule: reply in Russian.\n[/note]") in neutral(result, "chat")


@pytest.mark.parametrize("pattern,flags", [("(", ""), ("a", "x"), ("[z-a]", "")])
def test_a_broken_pattern_is_a_skipped_edit_and_the_other_edits_still_apply(pattern, flags):
    merged = profile(user_blocks=[{"op": "remove_regex", "pattern": pattern, "flags": flags}, drop_tag()])
    result, changes = prompt_body.rewrite(rich_conversation("chat"), "chat", merged, None)
    assert changes[0] == {"scope": "history", "op": "remove_regex", "edit": 0, "skipped": True,
                          "reason": "invalid pattern"}
    assert changes[1]["op"] == "drop_tag" and "system-reminder" not in dump(result)


def test_the_snapshots_own_compile_error_is_a_skipped_edit_too():
    class Strict(FakeSnapshot):
        def regex(self, pattern, flags=""):
            raise KeyError(pattern)

    merged = profile(user_blocks=[{"op": "remove_regex", "pattern": "x"}, drop_tag(pattern="y")])
    result, changes = prompt_body.rewrite(rich_conversation("chat"), "chat", merged, Strict())
    assert [(c["op"], c["skipped"], c["reason"]) for c in changes] == [
        ("remove_regex", True, "invalid pattern"), ("drop_tag", True, "invalid pattern")]
    assert result == rich_conversation("chat")


@pytest.mark.parametrize("spec,reason", [
    ({"op": "drop_tag"}, "missing tag"), ({"op": "remove_regex"}, "missing pattern"),
    ({"op": "replace_text", "with": "x"}, "missing find"), ({"op": "bogus"}, "unsupported op"),
    ({"op": "replace_regex", "pattern": "a", "with": "b"}, "unsupported op")])
def test_a_rule_that_cannot_run_is_skipped_with_a_reason(spec, reason):
    result, changes = prompt_body.rewrite(rich_conversation("chat"), "chat", profile(user_blocks=[spec]), None)
    assert changes == [{"scope": "history", "op": spec["op"], "edit": 0, "skipped": True, "reason": reason}]
    assert result == rich_conversation("chat")


def test_summarize_changes_counts_per_rule_and_keeps_the_first_positions():
    changes = [{"scope": "history", "op": "drop_tag", "edit": 0, "index": i, "removed_chars": 10, "added_chars": 0}
               for i in range(12)]
    changes += [{"scope": "history", "op": "drop_tag", "edit": 1, "index": 3, "removed_chars": 1, "added_chars": 2},
                {"scope": "history", "op": "mid_system", "mode": "drop", "index": 4, "removed_chars": 5},
                {"scope": "tools", "op": "hide", "edit": 0, "index": 1, "tool": "bash", "skipped": True,
                 "reason": "tool_choice names the tool"},
                {"scope": "tools", "op": "hide", "edit": 0, "index": 2, "tool": "bash", "skipped": True,
                 "reason": "tool_choice names the tool"}]
    summary = prompt_body.summarize_changes(changes)
    assert summary[0] == {"scope": "history", "op": "drop_tag", "edit": 0, "count": 12, "removed_chars": 120,
                          "added_chars": 0, "indexes": list(range(8))}
    assert summary[1]["edit"] == 1 and summary[1]["count"] == 1 and summary[1]["added_chars"] == 2
    assert summary[2]["mode"] == "drop" and "edit" not in summary[2]
    assert summary[3] == {"scope": "tools", "op": "hide", "edit": 0, "tool": "bash", "count": 2, "removed_chars": 0,
                          "added_chars": 0, "indexes": [1, 2], "skipped": True, "reason": "tool_choice names the tool"}
    assert prompt_body.summarize_changes([]) == [] and prompt_body.summarize_changes(None) == []
    assert prompt_body.summarize_changes(changes, indexes=2)[0]["indexes"] == [0, 1]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_rewritten_body_still_parses_in_every_dialect(dialect):
    result, _ = run(rich_conversation(dialect), dialect, "all")
    parsed = parse(dialect, result)
    assert parsed.system == "HEAD" and [t["function"]["name"] for t in parsed.tools] == ["bash", "read"]
    assert parsed.native_messages is not None and "system-reminder>\nPlan" not in parsed.prompt


def test_the_real_snapshot_gives_prompt_body_what_it_reads(tmp_path):
    document = {"version": 1, "profiles": {"cut": {
        "head": "{{client_system}}",
        "history": {"mid_system": "wrap", "user_blocks": [
            {"op": "drop_tag", "tag": "system-reminder", "contains": "Plan mode"},
            {"op": "remove_regex", "pattern": "Use tabs", "flags": "i"}]},
        "tools": {"edits": [{"tool": "bash", "op": "append", "text": "Ask first."}]}}}}
    try:
        snapshot = prompt_policy.compile_document(document, tmp_path, rev=1)
        cut = snapshot.profile("cut")
    except NotImplementedError:
        pytest.skip("prompt_policy.Snapshot is not implemented in this branch yet")
    for dialect in DIALECTS:
        result, changes = prompt_body.rewrite(rich_conversation(dialect), dialect, cut, snapshot)
        assert {c["op"] for c in changes} == {"drop_tag", "remove_regex", "mid_system", "append"}
        again, _ = prompt_body.rewrite(result, dialect, cut, snapshot)
        assert dump(again) == dump(result)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_profile_and_the_snapshot_are_read_only(dialect):
    rules = copy.deepcopy(PROFILES["all"])
    rules["history"]["user_blocks"].append({"op": "remove_regex", "pattern": "Use tabs", "flags": "i"})
    rules["tools"]["edits"].append({"tool": "read", "op": "replace_regex", "pattern": "(Read) a", "with": r"\1 the"})
    before = copy.deepcopy(rules)
    snapshot = FakeSnapshot(reminder="[note]\n{{text}}\n[/note]")
    prompt_body.rewrite(rich_conversation(dialect), dialect, rules, snapshot)
    assert rules == before and snapshot.reminder == "[note]\n{{text}}\n[/note]"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_same_profile_gives_the_same_bytes_from_many_threads(dialect):
    from concurrent.futures import ThreadPoolExecutor

    rules, snapshot = PROFILES["all"], FakeSnapshot()
    expected = dump(prompt_body.rewrite(rich_conversation(dialect), dialect, rules, snapshot))

    def work(_):
        return dump(prompt_body.rewrite(rich_conversation(dialect), dialect, rules, snapshot))

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(work, range(64))) == {expected}
