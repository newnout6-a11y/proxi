"""R1-03: the head and what the edits make of a text have a ceiling (plan I11).

A 266 KB document gave a head of 100 MiB ({{text:t}} a thousand times), and `replace_text` with find=" " and a long
`{{text:z}}` made a 100 KB system prompt 156 MB: every request that met such a document paid for it, in memory and in time, and
sent it upstream. Now the head is limited to MAX_HEAD_BYTES (a document that cannot stay under it is refused when saved, a
head that goes over it at run time is a 413), an edit may add at most MAX_EDIT_GROWTH characters to the text it edits, and the
history edits of one request (and, apart from them, the tool edits) at most MAX_BODY_GROWTH characters in all.
"""
import time
import tracemalloc

import pytest

import apierr
import prompt_body
import prompt_policy as pp
from prompt_support import client_request, prompt_relay  # noqa: F401
from test_prompt_policy_common import OPENCODE_SYSTEM, Sources, compile_doc, document, make_store

KIB = 1024


def compose(tmp_path, doc, system, sources=None, name="p"):
    snapshot = compile_doc(doc, tmp_path)
    plan = pp.Plan(rev=snapshot.rev, profile=name, rule="t", snapshot=snapshot)
    return plan.compose_head(system, sources or Sources())


def peak_of(call):
    tracemalloc.start()
    try:
        result = call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_the_limits_have_these_values():
    assert pp.MAX_HEAD_BYTES == 1024 * KIB and pp.MAX_EDIT_GROWTH == 512 * KIB and pp.MAX_BODY_GROWTH == 4096 * KIB
    assert pp.MAX_EDIT_GROWTH >= pp.MAX_TEXT_BYTES + 2          # an append of the longest text always fits


# -- the head, when the document is saved -----------------------------------------------------------------------------------


def test_the_reviewers_document_of_266_kb_is_refused_when_it_is_compiled(tmp_path):
    text = "x" * (256 * KIB - 1)
    doc = document(texts={"t": text}, profiles={"p": {"head": "{{text:t}}" * 1000}}, default_profile="p")
    started = time.perf_counter()
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_doc(doc, tmp_path)
    assert time.perf_counter() - started < 1.0
    assert raised.value.path == "profiles.p.head" and "without the client's system prompt" in raised.value.message


def test_the_reviewers_document_is_not_loaded_from_a_file_and_not_saved(tmp_path):
    doc = document(texts={"t": "x" * (256 * KIB - 1)}, profiles={"p": {"head": "{{text:t}}" * 1000}}, default_profile="p")
    store = make_store(tmp_path)
    with pytest.raises(pp.PromptConfigError) as raised:
        store.save(doc, None)
    assert raised.value.path == "profiles.p.head" and not (tmp_path / "prompts.json").exists()
    (tmp_path / "prompts.json").write_text(__import__("json").dumps(doc), encoding="utf-8")
    loaded = pp.PromptStore({"prompts_file": str(tmp_path / "prompts.json")})
    assert loaded.snapshot() is None and loaded.errors()[0]["param"] == "profiles.p.head"


@pytest.mark.parametrize("count, accepted", [(4, True), (3, True), (5, True), (6, False), (8, False)])
def test_the_texts_of_a_head_count_by_their_bytes(tmp_path, count, accepted):
    doc = document(texts={"t": "я" * (100 * KIB)}, profiles={"p": {"head": "{{text:t}}" * count}})    # 200 KiB each
    if accepted:
        compile_doc(doc, tmp_path)
    else:
        with pytest.raises(pp.PromptConfigError):
            compile_doc(doc, tmp_path)


def test_literals_texts_and_the_user_texts_of_relay_variables_are_all_counted(tmp_path):
    filler = "z" * (200 * KIB)
    for template, extra in (("{{role}}" * 6, {"relay_texts": {"role": filler}}),
                            ("{{relay_task}}" * 6, {"relay_texts": {"role": filler}}),
                            ("{{native_role}}" * 6, {"relay_texts": {"native_role": filler}}),
                            ("{{structured}}" * 6, {"relay_texts": {"structured": "{{default}}" + filler}}),
                            ((filler + "\n") * 6, {}), ("{{text:t}}" * 3 + filler * 3, {"texts": {"t": filler}})):
        with pytest.raises(pp.PromptConfigError) as raised:
            compile_doc(document(profiles={"p": {"head": template}}, **extra), tmp_path)
        assert raised.value.path == "profiles.p.head", template[:20]


def test_the_client_system_is_not_counted_and_a_big_client_is_caught_at_run_time(tmp_path):
    doc = document(profiles={"p": {"head": "{{client_system}}" * 50}})
    compile_doc(doc, tmp_path)
    with pytest.raises(apierr.ApiError) as raised:
        compose(tmp_path, doc, "s" * (30 * KIB))
    assert raised.value.status == 413


def test_a_head_made_of_texts_that_fit_is_accepted(tmp_path):
    doc = document(texts={"t": "q" * (200 * KIB)}, profiles={"p": {"head": "{{text:t}}\n\n{{text:t}}\n\n{{client_system}}"}})
    head, _parts, _warnings = compose(tmp_path, doc, OPENCODE_SYSTEM)
    assert len(head) > 400 * KIB


def test_a_profile_over_the_limit_names_itself_and_the_other_profiles_are_judged_alone(tmp_path):
    big = {"head": "{{text:t}}" * 8}
    doc = document(texts={"t": "x" * (200 * KIB)}, profiles={"fine": {"head": "{{text:t}}"}, "huge": big})
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_doc(doc, tmp_path)
    assert raised.value.path == "profiles.huge.head"


# -- the head, at run time ----------------------------------------------------------------------------------------------------


def test_a_head_over_the_limit_is_a_413_the_client_can_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_HEAD_BYTES", 5000)
    doc = document(profiles={"p": {"head": "{{client_system}}\n\n{{client_system}}"}})
    with pytest.raises(apierr.ApiError) as raised:
        compose(tmp_path, doc, "secret system text " * 150)
    error = raised.value
    assert (error.status, error.code) == (413, "prompt_too_large")
    assert "'p'" in error.message and "5000" in error.message and "secret" not in error.message
    assert error.openai_envelope()["error"]["code"] == "prompt_too_large"
    assert error.anthropic_envelope()["error"]["type"] == "request_too_large"


def test_the_head_is_measured_in_utf8_bytes_and_the_limit_itself_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_HEAD_BYTES", 1000)
    doc = document(profiles={"p": {"head": "{{client_system}}"}})
    assert len(compose(tmp_path, doc, "a" * 1000)[0]) == 1000
    with pytest.raises(apierr.ApiError):
        compose(tmp_path, doc, "a" * 1001)
    assert len(compose(tmp_path, doc, "я" * 500)[0].encode("utf-8")) == 1000
    with pytest.raises(apierr.ApiError):
        compose(tmp_path, doc, "я" * 501)               # 501 characters, 1002 bytes


def test_the_billing_line_and_the_structured_contract_count_too(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_HEAD_BYTES", 1000)
    doc = document(profiles={"p": {"head": "{{client_system}}"}})
    billing = "x-anthropic-billing-header: cc_version=2.1.310.a1b; cch=00000;"
    with pytest.raises(apierr.ApiError):
        compose(tmp_path, doc, billing + "\n" + "a" * 950)
    assert compose(tmp_path, doc, billing + "\n" + "a" * 900)[0].startswith(billing + "\n")
    with pytest.raises(apierr.ApiError):
        compose(tmp_path, doc, "a" * 600, Sources(structured="c" * 500))
    assert compose(tmp_path, doc, "a" * 400, Sources(structured="c" * 500))[0].endswith("c" * 500)


def test_a_head_is_refused_before_it_is_built(tmp_path):
    """No 100 MiB string: the sizes of the pieces are added up first."""
    doc = document(profiles={"p": {"head": "{{client_system}}" * 1000}})
    (_, peak) = peak_of(lambda: pytest.raises(apierr.ApiError, compose, tmp_path, doc, "s" * (200 * KIB)))
    assert peak < 20 * 1024 * KIB, "peak %d MB" % (peak // (1024 * KIB))


def test_a_413_for_the_head_reaches_the_client_before_anything_goes_upstream(prompt_relay, tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_HEAD_BYTES", 4000)
    doc = document(profiles={"p": {"head": "{{client_system}}\n\n{{client_system}}"}}, default_profile="p")
    (tmp_path / "prompts.json").write_text(__import__("json").dumps(doc), encoding="utf-8")
    relay = prompt_relay()
    status, payload, _headers = relay.post("chat", client_request(
        "chat", [{"role": "user", "content": "hi"}], system="word " * 600, tools=False, stream=False))
    assert status == 413 and payload["error"]["code"] == "prompt_too_large" and "'p'" in payload["error"]["message"]
    assert relay.posts == []
    status, payload, _headers = relay.post("chat", client_request(
        "chat", [{"role": "user", "content": "hi"}], system="word " * 100, tools=False, stream=False))
    assert status == 200 and len(relay.posts) == 1


def test_the_preview_of_a_head_over_the_limit_is_a_413_like_the_request_itself(tmp_path, monkeypatch):
    from test_prompt_policy_preview import chat
    monkeypatch.setattr(pp, "MAX_HEAD_BYTES", 3000)
    store = make_store(tmp_path, document(profiles={"p": {"head": "{{client_system}}\n\n{{client_system}}"}},
                                          default_profile="p"))
    with pytest.raises(apierr.ApiError) as raised:
        store.preview(chat(system="word " * 400, tools=False), "chat", {"x-proxi-client": "opencode"})
    assert raised.value.status == 413 and raised.value.code == "prompt_too_large"
    assert store.preview(chat(system="word " * 100, tools=False), "chat", {"x-proxi-client": "opencode"})["head_bytes"] > 1000


# -- what an edit of the system may add ---------------------------------------------------------------------------------------


def edited(tmp_path, text, *edits):
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": list(edits)}}},
                                    texts={"z": "Z" * 8192}), tmp_path)
    return pp._apply_system_edits(text, snapshot.profile("p")["client_system"]["edits"], snapshot)


def test_the_reviewers_replacement_of_every_space_by_a_text_is_not_applied_and_costs_no_memory(tmp_path):
    doc = document(texts={"z": "Z" * 8192}, profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": " ", "with": "{{text:z}}"}]}}})
    system = "word " * 20000                                   # 100 KB, 20 000 spaces: 156 MB when replaced
    (result, peak) = peak_of(lambda: compose(tmp_path, doc, system))
    head, parts, warnings = result
    assert head == system and [part.source for part in parts] == ["client"]
    assert warnings == ["client_system.edits[0] (replace_text) was not applied: its result would be too large"]
    assert peak < 20 * 1024 * KIB, "peak %d MB" % (peak // (1024 * KIB))


def test_the_report_says_too_large(tmp_path):
    _result, report = edited(tmp_path, "a b c " * 40000, {"op": "replace_text", "find": " ", "with": "{{text:z}}"})
    assert report == [{"index": 0, "op": "replace_text", "matched": False, "chars_before": 240000,
                       "chars_after": 240000, "skipped": "too_large"}]


def test_a_replacement_that_grows_the_text_by_less_than_the_limit_is_applied(tmp_path):
    result, report = edited(tmp_path, "a b c", {"op": "replace_text", "find": " ", "with": "{{text:z}}"})
    assert report[0]["matched"] is True and "skipped" not in report[0] and result.count("Z") == 2 * 8192
    one, _ = edited(tmp_path, "a b", {"op": "replace_text", "find": " ", "with": "{{text:z}}", "all": False})
    assert one == "a" + "Z" * 8192 + "b"


def test_the_growth_limit_is_exact_for_replace_text(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 100)
    snapshot = compile_doc(document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": "a", "with": "bbbbbbbbbbbb"}]}}}), tmp_path)       # +11 for each "a"
    edits = snapshot.profile("p")["client_system"]["edits"]
    assert pp._apply_system_edits("a" * 9, edits, snapshot)[1][0]["matched"] is True       # +99
    assert pp._apply_system_edits("a" * 10, edits, snapshot)[1][0]["skipped"] == "too_large"     # +110
    shrinking = compile_doc(document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": "aaaa", "with": "b"}]}}}), tmp_path)
    assert pp._apply_system_edits("aaaa" * 1000, shrinking.profile("p")["client_system"]["edits"], shrinking)[1][0][
        "matched"] is True


def test_replace_regex_stops_at_the_growth_limit_too(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 1000)
    system = "word " * 20000
    (result, peak) = peak_of(lambda: edited(tmp_path, system, {"op": "replace_regex", "pattern": r"\w", "with": "{{text:z}}"}))
    assert result[0] == system and result[1][0]["skipped"] == "too_large" and peak < 5 * 1024 * KIB
    ok, report = edited(tmp_path, "ab", {"op": "replace_regex", "pattern": r"(\w)", "with": r"\1\1"})
    assert ok == "aabb" and report[0]["matched"] is True


def test_prepend_and_append_of_the_longest_text_fit_but_not_past_the_head_limit(tmp_path):
    big = "B" * (256 * KIB)
    snapshot = compile_doc(document(texts={"b": big}, profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": "{{text:b}}"}, {"op": "prepend", "text": "{{text:b}}"}]}}}), tmp_path)
    edits = snapshot.profile("p")["client_system"]["edits"]
    result, report = pp._apply_system_edits("system", edits, snapshot)
    assert [entry["matched"] for entry in report] == [True, True]
    assert len(result) == 2 * len(big) + len("system") + 4
    near = "n" * (900 * KIB)
    result, report = pp._apply_system_edits(near, edits, snapshot)           # 900 KiB + 256 KiB is over what a head may be
    assert [entry.get("skipped") for entry in report] == ["too_large", "too_large"] and result == near


def test_edits_after_a_refused_one_still_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 50)
    result, report = edited(tmp_path, "a b", {"op": "replace_text", "find": " ", "with": "{{text:z}}"},
                            {"op": "replace_text", "find": "a", "with": "A"}, {"op": "append", "text": "tail"})
    assert result == "A b\n\ntail" and [entry.get("skipped") for entry in report] == ["too_large", None, None]


def test_a_text_that_is_already_over_the_head_limit_may_still_be_cut_down(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_HEAD_BYTES", 2000)
    system = "# Keep\nsmall\n\n# Drop\n" + "x" * 5000
    result, report = edited(tmp_path, system, {"op": "remove_section", "heading": "# Drop"},
                            {"op": "append", "text": "tail"})
    assert result == "# Keep\nsmall\n\ntail" and [entry["matched"] for entry in report] == [True, True]


# -- what the edits of a request body may add ----------------------------------------------------------------------------------


def conversation(contents):
    messages = [{"role": "user", "content": "..."}]
    for number, content in enumerate(contents):
        messages.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": "c%d" % number, "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": "c%d" % number, "content": content})
    return {"model": "claude-opus-5-5", "messages": messages}


def rewrite_with(tmp_path, body, *, texts=None, history=None, tools=None, relay_texts=None):
    section = {}
    if history is not None:
        section["history"] = history
    if tools is not None:
        section["tools"] = {"edits": tools}
    extra = {"relay_texts": relay_texts} if relay_texts else {}
    snapshot = compile_doc(document(texts=texts or {}, profiles={"p": section}, **extra), tmp_path)
    return prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot)


def test_the_reviewers_amplification_of_a_long_history_stops_at_the_ceiling(tmp_path):
    # Nine "e" in each message, each made 8192 characters: 73 719 more characters per message, and a history of
    # a hundred of them 7 MB (a 2 MB history of such text would have been 160 GB).
    body = conversation(["the tree between the elephants"] * 100)
    (result, peak) = peak_of(lambda: rewrite_with(
        tmp_path, body, texts={"z": "Z" * 8192},
        history={"user_blocks": [{"op": "replace_text", "find": "e", "with": "{{text:z}}"}]}))
    out, changes = result
    applied = [change for change in changes if not change.get("skipped")]
    refused = [change for change in changes if change.get("skipped")]
    assert len(applied) == pp.MAX_BODY_GROWTH // 73_719 == 56 and len(refused) == 44
    assert {change["reason"] for change in refused} == {"too_large"}
    grown = sum(len(m["content"]) for m in out["messages"] if m.get("role") == "tool") - 100 * len("the tree between the elephants")
    assert grown == 56 * 73_719 <= pp.MAX_BODY_GROWTH
    assert peak < 40 * 1024 * KIB, "peak %d MB" % (peak // (1024 * KIB))


def test_one_text_that_would_grow_by_more_than_the_limit_is_left_as_it_was_and_reported(tmp_path):
    text = "word " * 20000
    body = conversation([text, "ok e"])
    (result, peak) = peak_of(lambda: rewrite_with(
        tmp_path, body, texts={"z": "Z" * 8192},
        history={"user_blocks": [{"op": "replace_text", "find": " ", "with": "{{text:z}}"}]}))
    out, changes = result
    assert out["messages"][2]["content"] == text
    assert changes[0] == {"scope": "history", "op": "replace_text", "edit": 0, "index": 2, "target": "tool_result",
                          "skipped": True, "reason": "too_large"}
    assert out["messages"][4]["content"].startswith("ok" + "Z" * 8192)         # the next message is edited
    assert peak < 20 * 1024 * KIB


def test_the_growth_of_all_the_history_edits_of_a_request_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 1000)
    monkeypatch.setattr(pp, "MAX_BODY_GROWTH", 3500)
    body = conversation(["x e"] * 20)                                    # each edit adds 999 characters
    out, changes = rewrite_with(tmp_path, body, texts={"z": "Z" * 1000},
                                history={"user_blocks": [{"op": "replace_text", "find": "e", "with": "{{text:z}}"}]})
    applied = [change["index"] for change in changes if not change.get("skipped")]
    refused = [change["index"] for change in changes if change.get("skipped")]
    assert applied == [2, 4, 6] and refused == list(range(8, 42, 2)) and {c["reason"] for c in changes if c.get(
        "skipped")} == {"too_large"}
    assert sum(len(m["content"]) for m in out["messages"] if m["role"] == "tool") <= 20 * 4 + 3 * 999 + 4 * 17


def test_a_decision_depends_only_on_the_messages_before_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 1000)
    monkeypatch.setattr(pp, "MAX_BODY_GROWTH", 3500)
    rules = {"user_blocks": [{"op": "replace_text", "find": "e", "with": "{{text:z}}"}]}
    short = rewrite_with(tmp_path, conversation(["x e"] * 8), texts={"z": "Z" * 1000}, history=rules)[0]
    long = rewrite_with(tmp_path, conversation(["x e"] * 18), texts={"z": "Z" * 1000}, history=rules)[0]
    assert long["messages"][:len(short["messages"])] == short["messages"]       # a conversation that grows keeps its past


def test_the_growth_of_the_tools_is_counted_apart_from_the_history(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 1000)
    monkeypatch.setattr(pp, "MAX_BODY_GROWTH", 2500)
    body = conversation(["x e"] * 6)
    body["tools"] = [{"type": "function", "function": {"name": "t%d" % n, "description": "d", "parameters": {
        "type": "object"}}} for n in range(6)]
    out, changes = rewrite_with(
        tmp_path, body, texts={"z": "Z" * 900},
        history={"user_blocks": [{"op": "replace_text", "find": "e", "with": "{{text:z}}"}]},
        tools=[{"tool": "*", "op": "append", "text": "{{text:z}}"}])
    by_scope = {scope: [bool(change.get("skipped")) for change in changes if change["scope"] == scope]
                for scope in ("history", "tools")}
    assert by_scope == {"history": [False, False, True, True, True, True], "tools": [False, False, True, True, True, True]}
    assert [len(tool["function"]["description"]) > 100 for tool in out["tools"]] == [True, True, False, False, False, False]
    assert {c["reason"] for c in changes if c.get("skipped")} == {"too_large"}


def test_a_wrapper_for_the_mid_conversation_system_messages_counts_against_the_history_growth(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_BODY_GROWTH", 2000)
    body = {"model": "m", "messages": [{"role": "system", "content": "HEAD"}, {"role": "user", "content": "go"}]}
    for number in range(10):
        body["messages"] += [{"role": "assistant", "content": "a%d" % number},
                             {"role": "user", "content": "u%d" % number},
                             {"role": "system", "content": "rule %d" % number}]
    wrapper = "<w>" + "w" * 497 + "{{text}}</w>"
    out, changes = rewrite_with(tmp_path, body, history={"mid_system": "wrap"}, relay_texts={"reminder": wrapper})
    wrapped = [change for change in changes if not change.get("skipped")]
    refused = [change for change in changes if change.get("skipped")]
    assert len(wrapped) == 3 and len(refused) == 7 and {c["reason"] for c in refused} == {"too_large"}
    assert all(change["op"] == "mid_system" and change["mode"] == "wrap" for change in changes)
    assert [m["role"] for m in out["messages"]].count("system") == 1 + 7        # the refused ones stay system messages


def test_a_set_that_makes_a_description_much_longer_is_bounded_like_an_append(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 500)
    monkeypatch.setattr(pp, "MAX_BODY_GROWTH", 10_000)
    body = {"model": "m", "messages": [{"role": "user", "content": "go"}], "tools": [
        {"type": "function", "function": {"name": "a", "description": "short", "parameters": {"type": "object"}}}]}
    out, changes = rewrite_with(tmp_path, body, texts={"big": "B" * 600, "ok": "O" * 400},
                                tools=[{"tool": "a", "op": "set", "text": "{{text:big}}"},
                                       {"tool": "a", "op": "set", "text": "{{text:ok}}"},
                                       {"tool": "a", "op": "prepend", "text": "{{text:big}}"},
                                       {"tool": "a", "op": "append", "text": "tail"}])
    assert [(c["op"], c.get("reason")) for c in changes] == [("set", "too_large"), ("set", None), ("prepend", "too_large"),
                                                           ("append", None)]
    assert out["tools"][0]["function"]["description"] == "O" * 400 + "\n\ntail"


def test_a_growing_replace_text_is_refused_and_a_shrinking_remove_regex_is_not_on_the_history(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_EDIT_GROWTH", 1000)
    out, changes = rewrite_with(tmp_path, conversation(["word x " * 5000]), texts={"z": "Z" * 100},
                                history={"user_blocks": [{"op": "remove_regex", "pattern": "word"}]})
    assert out["messages"][2]["content"] == " x " * 5000                            # shrinking is always allowed
    out, changes = rewrite_with(tmp_path, conversation(["word " * 5000]), tools=None,
                                history={"user_blocks": [{"op": "replace_text", "find": "word", "with": "wording"}]})
    assert changes[0]["skipped"] is True and out["messages"][2]["content"] == "word " * 5000


def test_the_limits_do_not_touch_ordinary_edits(tmp_path):
    body = conversation(["note: PLAN MODE\n<system-reminder>\nPlan mode\n</system-reminder>\ntail", "plain"])
    out, changes = rewrite_with(tmp_path, body, history={"user_blocks": [
        {"op": "drop_tag", "tag": "system-reminder", "contains": "Plan mode"},
        {"op": "replace_text", "find": "PLAN", "with": "plan"}]})
    assert out["messages"][2]["content"] == "note: plan MODE\ntail"
    assert not any(change.get("skipped") for change in changes)
