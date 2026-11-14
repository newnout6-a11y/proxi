"""The literal revision (review R3-02): a Snapshot keeps its document with every {"file": ...} text put in, and a fingerprint of it.

A conversation pin that has to survive a restart cannot name a revision by a number or by a file that may have changed: it
names it by the fingerprint, and the revision is made again from its literal document.
"""
import hashlib
import json
import pathlib
import time

import pytest

import prompt_policy as pp
from test_prompt_policy_common import OPENCODE_SYSTEM, Sources, compile_doc, document, inp, make_store, write_json

TEXTS = {
    "rules.md": "Rules one.\nRules two.\n",
    "role.md": "ROLE FROM A FILE\n",
    "head.md": "HEAD {{text:rules}}!\n\n{{client_system}}",
    "gpt.md": "GPT BASE",
    "persist.md": "PERSIST",
    "note.md": "Привет, мир\n",
}


def write_texts(folder, newline="\n", names=None):
    """prompts/*.md next to the prompts file; the texts of TEXTS (the chosen names only)."""
    folder = pathlib.Path(folder)
    (folder / "prompts").mkdir(parents=True, exist_ok=True)
    for name, text in TEXTS.items():
        if names is None or name in names:
            (folder / "prompts" / name).write_bytes(text.replace("\n", newline).encode("utf-8"))


def file_document():
    """A document that names a file at every place a text may come from."""
    return document(
        default_profile="mine",
        texts={"rules": {"file": "prompts/rules.md"}, "plain": "inline", "note": {"file": "prompts/note.md"}},
        relay_texts={"role": {"file": "prompts/role.md"},
                     "codex_base": {"gpt-*": {"file": "prompts/gpt.md"}, "*": "other base"}},
        profiles={"mine": {"head": {"file": "prompts/head.md"},
                           "client_system": {"edits": [{"op": "remove_section", "heading": "# Code comments"},
                                                       {"op": "append", "text": "{{text:rules}}"}]},
                           "history": {"mid_system": "wrap", "user_blocks": [
                               {"op": "replace_text", "find": "x", "with": "{{text:plain}}"}]},
                           "generation": {"verbosity": "low"}},
                  "role": {"head": "{{role}}|{{text:note}}"}},
        rules=[{"name": "roles", "if": {"agent": "role*"}, "use": "role"}, {"name": "rest", "use": "mine"}],
        codex={"models": {"gpt-6*": {"persistent_instructions": {"file": "prompts/persist.md"},
                                     "include_skills_usage_instructions": True}}})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


# -- the literal document ------------------------------------------------------------------------------------------------


def test_every_file_text_is_put_in_its_place_and_the_document_itself_is_left_alone(tmp_path):
    write_texts(tmp_path)
    original = file_document()
    snapshot = compile_doc(original, tmp_path)
    literal = snapshot.literal_document
    assert literal["texts"] == {"rules": TEXTS["rules.md"], "plain": "inline", "note": TEXTS["note.md"]}
    assert literal["relay_texts"] == {"role": TEXTS["role.md"], "codex_base": {"gpt-*": "GPT BASE", "*": "other base"}}
    assert literal["profiles"]["mine"]["head"] == TEXTS["head.md"]
    assert literal["codex"]["models"]["gpt-6*"] == {"persistent_instructions": "PERSIST",
                                                    "include_skills_usage_instructions": True}
    assert "prompts/" not in json.dumps(literal)                     # no reference to a file is left
    assert snapshot.document == original == file_document()          # the admin edits the document, not the literal one
    assert original["texts"]["rules"] == {"file": "prompts/rules.md"}


def test_a_document_that_names_no_file_is_its_own_literal_document(tmp_path):
    original = document(texts={"a": "A"}, profiles={"p": {"head": "{{text:a}}"}})
    snapshot = compile_doc(original, tmp_path)
    assert snapshot.literal_document == original and snapshot.literal_document is not snapshot.literal_document
    assert snapshot._literal is None                                  # nothing was built for it


def test_the_literal_document_is_a_copy(tmp_path):
    snapshot = compile_doc(document(texts={"a": "A"}), tmp_path)
    snapshot.literal_document["texts"]["a"] = "CHANGED"
    assert snapshot.literal_document["texts"]["a"] == "A"
    write_texts(tmp_path)
    snapshot = compile_doc(file_document(), tmp_path)
    snapshot.literal_document["texts"]["rules"] = "CHANGED"
    assert snapshot.literal_document["texts"]["rules"] == TEXTS["rules.md"]


def test_a_text_that_is_named_file_is_not_mistaken_for_a_file_reference(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_text("FROM A FILE", encoding="utf-8")
    plain = compile_doc(document(texts={"file": "prompts/a.md"}), tmp_path)
    assert plain.literal_document["texts"] == {"file": "prompts/a.md"}          # a text that happens to look like a path
    named = compile_doc(document(texts={"file": {"file": "prompts/a.md"}}), tmp_path)
    assert named.literal_document["texts"] == {"file": "FROM A FILE"}


def test_one_reference_object_used_twice_is_put_in_twice(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_text("SHARED", encoding="utf-8")
    shared = {"file": "prompts/a.md"}
    snapshot = compile_doc(document(texts={"one": shared, "two": shared}), tmp_path)
    assert snapshot.literal_document["texts"] == {"one": "SHARED", "two": "SHARED"}


@pytest.mark.parametrize("newline, prefix", [("\n", b""), ("\r\n", b""), ("\n", b"\xef\xbb\xbf"), ("\r\n", b"\xef\xbb\xbf")])
def test_the_text_is_the_one_the_compiler_read_after_it_dropped_the_bom_and_the_carriage_returns(tmp_path, newline, prefix):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_bytes(prefix + "one\ntwo\n".replace("\n", newline).encode("utf-8"))
    snapshot = compile_doc(document(texts={"a": {"file": "prompts/a.md"}}), tmp_path)
    assert snapshot.literal_document["texts"]["a"] == "one\ntwo\n" == snapshot._data.texts["a"]


# -- compiling the literal document gives the same revision ---------------------------------------------------------------


def behaviour(snapshot, tmp_path):
    """Everything about a revision that a request or a catalog can see, as plain data."""
    sources = Sources(client_owned=False, structured="STRUCTURED CONTRACT")
    seen = {"names": snapshot.profile_names(), "warnings": snapshot.warnings,
            "profiles": {name: snapshot.profile(name) for name in snapshot.profile_names()},
            "relay": {name: snapshot.relay_text(name) for name in pp.RELAY_TEXT_NAMES if name != "codex_base"},
            "codex_base": {model: snapshot.codex_base(model) for model in ("gpt-5.5", "gpt-6-codex", "claude-opus-5-5")},
            "codex_overrides": {model: snapshot.codex_overrides(model) for model in ("gpt-5.5", "gpt-6-codex")},
            "contract": pp.structured_contract(snapshot, {"kind": "json_schema", "name": "n", "schema": {"type": "object"}},
                                               tools=False, tool_choice="auto")}
    heads = {}
    for name in snapshot.profile_names():
        plan = pp.Plan(rev=snapshot.rev, profile=name, rule="test", snapshot=snapshot)
        for label, system in (("opencode", OPENCODE_SYSTEM), ("empty", ""),
                              ("billing", "x-anthropic-billing-header: cc=1\n" + OPENCODE_SYSTEM)):
            head, parts, notes = plan.compose_head(system, sources)
            heads["%s/%s" % (name, label)] = (head, [(part.source, part.text) for part in parts], notes)
    seen["heads"] = heads
    return seen


def test_the_literal_document_compiles_to_the_same_revision_in_everything_a_request_can_see(tmp_path):
    write_texts(tmp_path / "with-files")
    (tmp_path / "bare").mkdir()
    original = compile_doc(file_document(), tmp_path / "with-files")
    literal = pp.compile_document(original.literal_document, tmp_path / "bare", rev=7)
    assert literal.rev == 7 and original.rev == 1
    assert behaviour(literal, tmp_path) == behaviour(original, tmp_path)
    assert original.fingerprint == literal.fingerprint
    # no file is looked at: the folder of a revision made again may not even exist
    assert pp.compile_document(original.literal_document, tmp_path / "gone", rev=8).files == ()


def test_the_rules_and_the_pins_see_the_same_selection_in_both_revisions(tmp_path):
    write_texts(tmp_path / "a")
    write_json(tmp_path / "a" / "prompts.json", file_document())
    original = make_store(tmp_path / "a")
    write_json(tmp_path / "b" / "prompts.json", original.snapshot().literal_document)
    literal = make_store(tmp_path / "b")
    for fields in ({}, {"agent": "role-x"}, {"purpose": "session-title"}, {"request_class": "compaction"},
                   {"model": "gpt-6-codex", "client": "codex"}, {"virtual_profile": "mine", "requested_model": "m"}):
        one, two = original.plan(inp(**fields)), literal.plan(inp(**fields))
        assert (one.profile, one.rule, one.protected, one.body_edits, one.generation) == (
            two.profile, two.rule, two.protected, two.body_edits, two.generation), fields
        if not one.legacy:
            assert one.compose_head(OPENCODE_SYSTEM, Sources()) == two.compose_head(OPENCODE_SYSTEM, Sources())


def test_a_document_of_another_shape_gives_a_different_revision(tmp_path):
    write_texts(tmp_path)
    changed = file_document()
    changed["profiles"]["mine"]["generation"] = {"verbosity": "high"}
    one = compile_doc(file_document(), tmp_path).literal_document
    two = compile_doc(changed, tmp_path).literal_document
    assert pp.compile_document(one, tmp_path, rev=1).profile("mine")["generation"] == {"verbosity": "low"}
    assert pp.compile_document(two, tmp_path, rev=1).profile("mine")["generation"] == {"verbosity": "high"}


# -- the fingerprint -----------------------------------------------------------------------------------------------------


def test_the_fingerprint_is_the_sha256_of_the_canonical_json_of_the_literal_document(tmp_path):
    write_texts(tmp_path)
    snapshot = compile_doc(file_document(), tmp_path)
    blob = canonical(snapshot.literal_document)
    assert snapshot.fingerprint == hashlib.sha256(blob).hexdigest() and len(snapshot.fingerprint) == 64
    assert snapshot._canonical() == blob
    assert "Привет, мир".encode("utf-8") in blob and b"\\u" not in blob       # not escaped: UTF-8
    assert blob.startswith(b'{"version":1,"default_profile":"mine","texts":{"rules":')    # compact, in the order written


def test_the_same_content_has_the_same_fingerprint_however_it_was_written(tmp_path):
    write_texts(tmp_path / "unix")
    write_texts(tmp_path / "windows", newline="\r\n")
    inline = document(default_profile="mine", texts={"rules": TEXTS["rules.md"], "plain": "inline"},
                      profiles={"mine": {"head": "{{text:rules}}"}})
    from_files = document(default_profile="mine", texts={"rules": {"file": "prompts/rules.md"}, "plain": "inline"},
                          profiles={"mine": {"head": "{{text:rules}}"}})
    prints = {compile_doc(from_files, tmp_path / "unix").fingerprint, compile_doc(from_files, tmp_path / "windows").fingerprint,
              compile_doc(inline, tmp_path / "unix").fingerprint, pp.compile_document(inline, tmp_path, rev=99).fingerprint}
    assert len(prints) == 1


def test_a_change_of_a_text_a_profile_or_the_order_of_the_keys_changes_the_fingerprint(tmp_path):
    base = document(profiles={"a": {"head": "A"}, "b": {"head": "B"}}, rules=[{"name": "r", "use": "a"}])
    swapped = document(profiles={"b": {"head": "B"}, "a": {"head": "A"}}, rules=[{"name": "r", "use": "a"}])
    edited = document(profiles={"a": {"head": "A!"}, "b": {"head": "B"}}, rules=[{"name": "r", "use": "a"}])
    prints = [compile_doc(item, tmp_path).fingerprint for item in (base, swapped, edited)]
    assert len(set(prints)) == 3                              # the order of the profiles is part of what a revision is


def test_the_order_of_codex_models_decides_which_override_wins_and_so_it_is_part_of_the_fingerprint(tmp_path):
    first = document(codex={"models": {"gpt-*": {"persistent_instructions": "ALL"},
                                       "gpt-6*": {"persistent_instructions": "SIX"}}})
    second = document(codex={"models": {"gpt-6*": {"persistent_instructions": "SIX"},
                                        "gpt-*": {"persistent_instructions": "ALL"}}})
    one, two = compile_doc(first, tmp_path), compile_doc(second, tmp_path)
    assert one.codex_overrides("gpt-6x") != two.codex_overrides("gpt-6x")
    assert one.fingerprint != two.fingerprint


def test_the_fingerprint_does_not_depend_on_the_revision_number_the_file_or_the_time(tmp_path):
    write_texts(tmp_path)
    one = compile_doc(file_document(), tmp_path, rev=1)
    time.sleep(0.01)
    (tmp_path / "prompts" / "rules.md").write_bytes((tmp_path / "prompts" / "rules.md").read_bytes())    # a new mtime
    two = compile_doc(file_document(), tmp_path, rev=40)
    assert one.fingerprint == two.fingerprint


def test_the_fingerprint_is_worked_out_once(tmp_path, monkeypatch):
    snapshot = compile_doc(document(texts={"a": "A"}), tmp_path)
    calls = []
    real = pp.Snapshot._canonical
    monkeypatch.setattr(pp.Snapshot, "_canonical", lambda self: calls.append(1) or real(self))
    assert snapshot.fingerprint == snapshot.fingerprint and len(calls) == 1


# -- the size of a literal document ----------------------------------------------------------------------------------------


def big_reference_document(tmp_path, copies, size=200 * 1024):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "big.md").write_text("x" * size, encoding="utf-8")
    return document(texts={"t%d" % number: {"file": "prompts/big.md"} for number in range(copies)})


def test_a_file_named_by_many_texts_makes_a_document_with_no_fingerprint_and_costs_nothing_to_compile(tmp_path):
    started = time.perf_counter()
    snapshot = compile_doc(big_reference_document(tmp_path, copies=200), tmp_path)        # 200 x 200 KiB: 40 MiB of JSON
    assert snapshot.fingerprint is None and snapshot._canonical() is None
    assert time.perf_counter() - started < 5
    assert len(snapshot.literal_document["texts"]) == 200
    assert snapshot.literal_document["texts"]["t0"] is not None and len(snapshot.literal_document["texts"]["t199"]) == 200 * 1024
    assert snapshot.profile_names() == ["passthrough"]                  # the revision itself is as good as any


def test_a_file_named_a_few_times_still_has_a_fingerprint(tmp_path):
    snapshot = compile_doc(big_reference_document(tmp_path, copies=8), tmp_path)           # 1.6 MiB
    assert len(snapshot.fingerprint) == 64
    assert len(snapshot._canonical()) > 8 * 200 * 1024


def test_a_literal_compile_takes_what_the_normal_one_refuses_for_size_and_refuses_files(tmp_path):
    text = "y" * (200 * 1024)
    large = document(texts={"t%d" % number: text for number in range(12)})                  # 2.4 MiB of text
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_doc(large, tmp_path)
    assert "larger than 2 MiB" in raised.value.message
    data = pp._Compiler(large, tmp_path, literal=True).compile()
    assert data.size > pp.MAX_DOCUMENT_BYTES and len(data.texts) == 12
    toobig = document(texts={"t%d" % number: text for number in range(50)})                 # 10 MiB
    with pytest.raises(pp.PromptConfigError) as raised:
        pp._Compiler(toobig, tmp_path, literal=True).compile()
    assert "larger than 8 MiB" in raised.value.message
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_text("A", encoding="utf-8")
    with pytest.raises(pp.PromptConfigError) as raised:
        pp._Compiler(document(texts={"a": {"file": "prompts/a.md"}}), tmp_path, literal=True).compile()
    assert raised.value.path == "texts.a" and "names no file" in raised.value.message


def test_a_snapshot_made_again_remembers_that_it_was_and_the_fingerprint_it_was_made_for(tmp_path):
    original = compile_doc(document(texts={"a": "A"}), tmp_path)
    data = pp._Compiler(original.literal_document, tmp_path, literal=True).compile()
    again = pp.Snapshot(5, original.literal_document, None, _compiled=data, _restored=True, _fingerprint="f" * 64)
    assert again.restored and again.fingerprint == "f" * 64 and again.rev == 5 and again.source_path is None
    assert not original.restored
