"""R1-11: the journal and the warnings of a head carry no fragment of what an edit looks for (plan I7).

Reviewer 1 found the first 60 characters of every `find`, `heading` and `pattern` that matched nothing in the warnings of
compose_head (and so in `prompt.plan`), and up to 80 characters of a `{{...}}` placeholder in `prompt.config_error`. The
journal keeps lengths, sha8, sources, indexes and ops; the targets stay in the preview report (`edits`, `edits_unmatched`)
and the full text of a file error stays in `PromptStore.errors()` for the admin.
"""
import json

import pytest

import journal
import prompt_policy as pp
from prompt_support import client_request, prompt_relay  # noqa: F401
from test_prompt_policy_common import Sources, compose, document, inp, make_store
from test_prompt_policy_preview import chat

SECRET = "ACME-INTERNAL-CODENAME-PHOENIX"


def mark():
    return journal.JOURNAL.last_seq


def journaled(since):
    return json.dumps(journal.JOURNAL.query(since=since, limit=5000, order="asc"), ensure_ascii=False)


TARGET_EDITS = [
    {"op": "remove_section", "heading": "# " + SECRET},
    {"op": "keep_sections", "headings": ["# " + SECRET, "## " + SECRET]},
    {"op": "remove_tag", "tag": "tag-" + SECRET.lower()},
    {"op": "keep_tag_inner", "tag": "inner-" + SECRET.lower()},
    {"op": "remove_between", "start": "<<" + SECRET, "end": ">>"},
    {"op": "remove_regex", "pattern": SECRET + "[0-9]+"},
    {"op": "replace_regex", "pattern": SECRET + "[a-z]+", "with": "x"},
    {"op": "replace_text", "find": "the %s project guidelines" % SECRET, "with": "x"},
]
SYSTEM = "You are an agent.\n\n# Rules\nBe brief.\n"


def doc_with(edits):
    return document(texts={"private": "PRIVATE-RULES-TEXT-12345"},
                    profiles={"p": {"head": "{{text:private}}\n\n{{client_system}}",
                                    "client_system": {"edits": edits}}}, default_profile="p")


def test_the_warnings_of_a_head_name_the_edit_and_never_its_target(tmp_path):
    head, _parts, warnings = compose(doc_with(TARGET_EDITS), tmp_path, SYSTEM)
    assert warnings == ["client_system.edits[%d] (%s) matched nothing" % (index, edit["op"])
                        for index, edit in enumerate(TARGET_EDITS)]
    assert SECRET not in " ".join(warnings) and "project guidelines" not in " ".join(warnings)
    assert head == "PRIVATE-RULES-TEXT-12345\n\n" + SYSTEM      # the head itself is untouched


def test_an_edit_that_matched_leaves_no_warning(tmp_path):
    edits = [{"op": "replace_text", "find": "agent", "with": "assistant"}, {"op": "append", "text": SECRET}]
    _head, _parts, warnings = compose(doc_with(edits), tmp_path, SYSTEM)
    assert warnings == []


def test_the_preview_report_still_carries_the_targets(tmp_path):
    store = make_store(tmp_path, doc_with(TARGET_EDITS))
    result = store.preview(chat(system=SYSTEM), "chat", {"x-proxi-client": "opencode"})
    unmatched = [item for item in result["edits_unmatched"] if item["scope"] == "client_system"]
    assert [(item["index"], item["op"]) for item in unmatched] == [(index, edit["op"])
                                                                   for index, edit in enumerate(TARGET_EDITS)]
    assert all(item["target"] for item in unmatched)
    assert unmatched[0]["target"] == "# " + SECRET and unmatched[7]["target"].startswith("the " + SECRET[:10])
    assert [entry["matched"] for entry in result["edits"]] == [False] * len(TARGET_EDITS)
    assert result["warnings"] == ["client_system.edits[%d] (%s) matched nothing" % (index, edit["op"])
                                  for index, edit in enumerate(TARGET_EDITS)]


def test_prompt_plan_of_a_real_request_carries_no_fragment_of_an_edit(prompt_relay, tmp_path):
    document_ = document(texts={"t": "PRIVATE-RULES-TEXT-12345"},
                         profiles={"p": {"head": "{{text:t}}\n\n{{client_system}}", "client_system": {"edits": [
                             {"op": "replace_text", "find": "the %s project guidelines" % SECRET, "with": "x"},
                             {"op": "remove_section", "heading": "# " + SECRET}]}}}, default_profile="p")
    (tmp_path / "prompts.json").write_text(json.dumps(document_), encoding="utf-8")
    relay = prompt_relay()
    since = relay.mark()
    status, _payload, headers = relay.post("chat", client_request(
        "chat", [{"role": "user", "content": "hi"}], system="S " * 50, tools=False, stream=False))
    relay.settle(headers)
    assert status == 200
    events = [event for event in relay.events(since) if event["kind"].startswith(("prompt.", "admin."))]
    plan = [event for event in events if event["kind"] == "prompt.plan"]
    assert plan and plan[0]["data"]["warnings"] == ["client_system.edits[0] (replace_text) matched nothing",
                                                    "client_system.edits[1] (remove_section) matched nothing"]
    text = json.dumps(events, ensure_ascii=False)
    assert SECRET not in text and "PRIVATE-RULES-TEXT-12345" not in text and "project guidelines" not in text


INVALID = {
    "unknown variable": (document(profiles={"p": {"head": "{{%s}}" % SECRET}}), "profiles.p.head"),
    "unknown text": (document(profiles={"p": {"head": "{{text:%s}}" % SECRET}}), "profiles.p.head"),
    "unknown text in an edit": (document(profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": "x {{text:%s}}" % SECRET}]}}}), "profiles.p.client_system.edits[0].text"),
    "unknown variable in a wrapper": (document(relay_texts={"reminder": "{{text}} {{%s}}" % SECRET}),
                                      "relay_texts.reminder"),
    "invalid pattern": (document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_regex", "pattern": "(?P=%s)" % SECRET}]}}}), "profiles.p.client_system.edits[0].pattern"),
    "invalid replacement": (document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_regex", "pattern": "(a)", "with": "\\g<%s>" % SECRET}]}}}),
        "profiles.p.client_system.edits[0].with"),
    "duplicate rule": (document(profiles={"p": {}}, rules=[{"name": SECRET, "use": "p"}, {"name": SECRET, "use": "p"}]),
                       "rules[1].name"),
    "missing file": (document(texts={"t": {"file": "prompts/%s.md" % SECRET}}), "texts.t.file"),
}


@pytest.mark.parametrize("case", list(INVALID))
def test_prompt_config_error_says_where_and_what_kind_and_never_quotes(tmp_path, case):
    invalid, param = INVALID[case]
    since = mark()
    store = make_store(tmp_path, invalid)
    assert store.snapshot() is None
    (error,) = store.errors()
    assert error["param"] == param
    (event,) = [item for item in journal.JOURNAL.query(since=since, limit=5000, order="asc")
                if item["kind"] == "prompt.config_error"]
    assert event["data"]["param"] == param and event["data"]["error"]
    assert SECRET not in journaled(since), journaled(since)


@pytest.mark.parametrize("case", ["unknown variable", "unknown text", "duplicate rule", "missing file"])
def test_the_admin_still_gets_the_full_text_of_the_error(tmp_path, case):
    invalid, _param = INVALID[case]
    store = make_store(tmp_path, invalid)
    assert SECRET[:40] in store.errors()[0]["message"]


def test_an_invalid_change_of_a_running_store_is_journaled_without_the_text(tmp_path):
    import os
    from test_prompt_policy_common import Clock, write_json
    clock = Clock()
    store = make_store(tmp_path, document(profiles={"p": {"head": "GOOD"}}, default_profile="p"), clock=clock)
    since = mark()
    write_json(tmp_path / "prompts.json", document(texts={"x": SECRET}, profiles={"p": {"head": "{{%s}}" % SECRET}}))
    info = (tmp_path / "prompts.json").stat()
    os.utime(tmp_path / "prompts.json", ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    clock.advance(pp.RELOAD_INTERVAL + 0.01)
    store.maybe_reload()
    assert store.snapshot().rev == 1 and SECRET in store.errors()[0]["message"]
    assert SECRET not in journaled(since)
    (event,) = [item for item in journal.JOURNAL.query(since=since, limit=5000, order="asc")
                if item["kind"] == "prompt.config_error"]
    assert event["data"]["param"] == "profiles.p.head"
    assert event["data"]["error"] == "unknown variable in a placeholder at character 0"
    assert "profiles.p.head" in event["message"]


def test_a_duplicate_key_of_the_file_is_journaled_without_the_key(tmp_path):
    since = mark()
    (tmp_path / "prompts.json").write_text('{"version": 1, "%s": 1, "%s": 2}' % (SECRET, SECRET), encoding="utf-8")
    store = pp.PromptStore({"prompts_file": str(tmp_path / "prompts.json")})
    assert store.snapshot() is None and SECRET in store.errors()[0]["message"]
    assert SECRET not in journaled(since)


def test_other_errors_keep_their_own_message_in_the_journal(tmp_path):
    since = mark()
    store = make_store(tmp_path, document(profiles={"p": {"generation": {"effort": "extreme"}}}))
    (event,) = [item for item in journal.JOURNAL.query(since=since, limit=5000, order="asc")
                if item["kind"] == "prompt.config_error"]
    assert event["data"]["param"] == "profiles.p.generation.effort" and "must be null or one of" in event["data"]["error"]
    assert store.errors()[0]["message"] == event["data"]["error"]


def test_the_brief_of_an_error_defaults_to_its_message():
    error = pp.PromptConfigError("a.b", "must be a string")
    assert error.brief == error.message == "must be a string" and str(error) == "a.b: must be a string"
    quiet = pp.PromptConfigError("a.b", "unknown variable {{SECRET}}", "unknown variable")
    assert quiet.brief == "unknown variable" and "SECRET" in quiet.message and "SECRET" in str(quiet)
