"""compile_document: every field is checked, errors name the exact path (plan 2, 4.3)."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

import prompt_policy as pp
from test_prompt_policy_common import compile_doc, document, profile_doc


def edits_doc(*edits, section="client_system"):
    return document(profiles={"p": {section: {"edits": list(edits)}}})


def rejected(doc, tmp_path):
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_doc(doc, tmp_path)
    return raised.value


INVALID = [
    # document
    ([], "", "JSON object"),
    ({}, "version", "must be 1"),
    ({"version": 2}, "version", "must be 1"),
    ({"version": "1"}, "version", "must be 1"),
    ({"version": True}, "version", "must be 1"),
    ({"version": 1, "typo": 1}, "typo", "unknown key"),
    # texts
    (document(texts=[]), "texts", "must be an object"),
    (document(texts={"bad name": "x"}), "texts.bad name", "text name"),
    (document(texts={"a": 5}), "texts.a", "string or"),
    (document(texts={"a": {"file": "x.md", "more": 1}}), "texts.a", "string or"),
    (document(texts={"a": {"file": 5}}), "texts.a.file", "non-empty relative path"),
    # relay_texts
    (document(relay_texts={"foo": "x"}), "relay_texts.foo", "unknown key"),
    (document(relay_texts=[]), "relay_texts", "must be an object"),
    (document(relay_texts={"reminder": "no placeholder"}), "relay_texts.reminder", "{{text}}"),
    (document(relay_texts={"reminder": "{{text}} {{role}}"}), "relay_texts.reminder", "unknown variable {{role}}"),
    (document(relay_texts={"structured": "  "}), "relay_texts.structured", "must not be empty"),
    (document(relay_texts={"structured": "{{text}}"}), "relay_texts.structured", "unknown variable {{text}}"),
    (document(relay_texts={"role": 5}), "relay_texts.role", "string or"),
    (document(relay_texts={"codex_base": 5}), "relay_texts.codex_base", "string"),
    (document(relay_texts={"codex_base": {"file": "a.md", "gpt-*": "x"}}), "relay_texts.codex_base.file", "mixed"),
    (document(relay_texts={"codex_base": {"": "x"}}), "relay_texts.codex_base.", "glob"),
    # profiles
    (document(profiles=[]), "profiles", "must be an object"),
    (document(profiles={"-bad": {}}), "profiles.-bad", "profile name"),
    (document(profiles={"has space": {}}), "profiles.has space", "profile name"),
    (document(profiles={"a" * 65: {}}), "profiles." + "a" * 65, "profile name"),
    (document(profiles={"passthrough": {"head": "x"}}), "profiles.passthrough", "cannot be redefined"),
    (document(profiles={"p": []}), "profiles.p", "must be an object"),
    (document(profiles={"p": {"foo": 1}}), "profiles.p.foo", "unknown key"),
    (document(profiles={"p": {"description": 5}}), "profiles.p.description", "string"),
    (document(profiles={"p": {"head": "{{nope}}"}}), "profiles.p.head", "unknown variable {{nope}}"),
    (document(profiles={"p": {"head": "{{text:nope}}"}}), "profiles.p.head", "unknown text 'nope'"),
    (document(profiles={"p": {"head": "{{client_system"}}), "profiles.p.head", "does not start a variable"),
    (document(profiles={"p": {"head": "{{}}"}}), "profiles.p.head", "empty"),
    (document(profiles={"p": {"head": 5}}), "profiles.p.head", "string or"),
    # client_system edits
    (document(profiles={"p": {"client_system": []}}), "profiles.p.client_system", "must be an object"),
    (document(profiles={"p": {"client_system": {"foo": 1}}}), "profiles.p.client_system.foo", "unknown key"),
    (document(profiles={"p": {"client_system": {"edits": {}}}}), "profiles.p.client_system.edits", "must be a list"),
    (edits_doc(5), "profiles.p.client_system.edits[0]", "must be an object"),
    (edits_doc({}), "profiles.p.client_system.edits[0].op", "must be one of"),
    (edits_doc({"op": "explode"}), "profiles.p.client_system.edits[0].op", "must be one of"),
    (edits_doc({"op": "remove_tag", "tag": "a"}, {"op": "remove_section"}),
     "profiles.p.client_system.edits[1].heading", "is required"),
    (edits_doc({"op": "remove_section", "heading": "no hashes"}), "profiles.p.client_system.edits[0].heading",
     "markdown heading"),
    (edits_doc({"op": "remove_section", "heading": "####### seven"}), "profiles.p.client_system.edits[0].heading",
     "markdown heading"),
    (edits_doc({"op": "remove_section", "heading": "# ok", "extra": 1}), "profiles.p.client_system.edits[0].extra",
     "unknown key"),
    (edits_doc({"op": "keep_sections", "headings": []}), "profiles.p.client_system.edits[0].headings", "non-empty"),
    (edits_doc({"op": "keep_sections", "headings": ["# a", "b"]}), "profiles.p.client_system.edits[0].headings[1]",
     "markdown heading"),
    (edits_doc({"op": "remove_tag", "tag": "has space"}), "profiles.p.client_system.edits[0].tag", "tag name"),
    (edits_doc({"op": "keep_tag_inner", "tag": ""}), "profiles.p.client_system.edits[0].tag", "must not be empty"),
    (edits_doc({"op": "remove_between", "start": "a"}), "profiles.p.client_system.edits[0].end", "is required"),
    (edits_doc({"op": "remove_between", "start": "a", "end": "b", "inclusive": "yes"}),
     "profiles.p.client_system.edits[0].inclusive", "true or false"),
    (edits_doc({"op": "remove_regex"}), "profiles.p.client_system.edits[0].pattern", "is required"),
    (edits_doc({"op": "remove_regex", "pattern": "("}), "profiles.p.client_system.edits[0].pattern",
     "invalid regular expression"),
    (edits_doc({"op": "remove_regex", "pattern": "(a+)+$"}), "profiles.p.client_system.edits[0].pattern",
     "catastrophically"),
    (edits_doc({"op": "remove_regex", "pattern": "a" * 513}), "profiles.p.client_system.edits[0].pattern", "512"),
    (edits_doc({"op": "remove_regex", "pattern": "a", "flags": "x"}), "profiles.p.client_system.edits[0].flags",
     "only i, m and s"),
    (edits_doc({"op": "replace_regex", "pattern": "a"}), "profiles.p.client_system.edits[0].with", "is required"),
    (edits_doc({"op": "replace_regex", "pattern": "(a)", "with": "\\2"}), "profiles.p.client_system.edits[0].with",
     "invalid replacement"),
    (edits_doc({"op": "replace_regex", "pattern": "(a)", "with": "\\g<nope>"}),
     "profiles.p.client_system.edits[0].with", "invalid replacement"),
    (edits_doc({"op": "replace_text", "with": "b"}), "profiles.p.client_system.edits[0].find", "is required"),
    (edits_doc({"op": "replace_text", "find": "a", "with": "b", "all": 1}), "profiles.p.client_system.edits[0].all",
     "true or false"),
    (edits_doc({"op": "replace_text", "find": "a"}), "profiles.p.client_system.edits[0].with", "is required"),
    (edits_doc({"op": "prepend"}), "profiles.p.client_system.edits[0].text", "is required"),
    (edits_doc({"op": "append", "text": ""}), "profiles.p.client_system.edits[0].text", "must not be empty"),
    (edits_doc({"op": "append", "text": "{{client}}"}), "profiles.p.client_system.edits[0].text",
     "unknown variable {{client}}"),
    (edits_doc({"op": "append", "text": "{{text:none}}"}), "profiles.p.client_system.edits[0].text", "unknown text"),
    # history
    (document(profiles={"p": {"history": []}}), "profiles.p.history", "must be an object"),
    (document(profiles={"p": {"history": {"foo": 1}}}), "profiles.p.history.foo", "unknown key"),
    (document(profiles={"p": {"history": {"mid_system": "nope"}}}), "profiles.p.history.mid_system", "one of"),
    (edits_doc({"op": "append", "text": "x"}, section="history"), "profiles.p.history.edits", "unknown key"),
    (document(profiles={"p": {"history": {"user_blocks": [{"op": "keep_sections", "headings": ["# a"]}]}}}),
     "profiles.p.history.user_blocks[0].op", "must be one of"),
    (document(profiles={"p": {"history": {"user_blocks": [{"op": "drop_tag"}]}}}),
     "profiles.p.history.user_blocks[0].tag", "is required"),
    (document(profiles={"p": {"history": {"user_blocks": [
        {"op": "drop_tag", "tag": "a", "contains": "x", "pattern": "y"}]}}}),
     "profiles.p.history.user_blocks[0]", "either contains or pattern"),
    (document(profiles={"p": {"history": {"user_blocks": [{"op": "drop_tag", "tag": "a", "flags": "i"}]}}}),
     "profiles.p.history.user_blocks[0].flags", "need a pattern"),
    (document(profiles={"p": {"history": {"user_blocks": [{"op": "remove_regex", "pattern": "(.*)*"}]}}}),
     "profiles.p.history.user_blocks[0].pattern", "catastrophically"),
    # tools
    (document(profiles={"p": {"tools": {"edits": [{"op": "append", "text": "x"}]}}}),
     "profiles.p.tools.edits[0].tool", "is required"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": 5, "op": "append", "text": "x"}]}}}),
     "profiles.p.tools.edits[0].tool", "glob"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": "a"}]}}}), "profiles.p.tools.edits[0].op", "is required"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": "a", "op": "explode"}]}}}),
     "profiles.p.tools.edits[0].op", "must be one of"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": "a", "hide": True, "op": "append", "text": "x"}]}}}),
     "profiles.p.tools.edits[0].op", "takes no other key"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": "a", "hide": "yes"}]}}}),
     "profiles.p.tools.edits[0].hide", "true or false"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": "a", "op": "set"}]}}}),
     "profiles.p.tools.edits[0].text", "is required"),
    (document(profiles={"p": {"tools": {"edits": [{"tool": "a", "op": "replace_regex", "pattern": "x",
                                                   "with": "y", "bogus": 1}]}}}),
     "profiles.p.tools.edits[0].bogus", "unknown key"),
    # generation and publication
    (document(profiles={"p": {"generation": {"effort": "extreme"}}}), "profiles.p.generation.effort", "one of"),
    (document(profiles={"p": {"generation": {"verbosity": "loud"}}}), "profiles.p.generation.verbosity", "one of"),
    (document(profiles={"p": {"generation": {"temperature": 1}}}), "profiles.p.generation.temperature", "unknown key"),
    (document(profiles={"p": {"generation": []}}), "profiles.p.generation", "must be an object"),
    (document(profiles={"p": {"publish_as_model": "yes"}}), "profiles.p.publish_as_model", "false, true or"),
    (document(profiles={"p": {"publish_as_model": ["gpt-*", 5]}}), "profiles.p.publish_as_model[1]", "glob"),
    # rules
    (document(rules={}), "rules", "must be a list"),
    (document(rules=[5]), "rules[0]", "must be an object"),
    (document(rules=[{"use": "passthrough"}]), "rules[0].name", "is required"),
    (document(rules=[{"name": "", "use": "passthrough"}]), "rules[0].name", "is required"),
    (document(rules=[{"name": "a", "use": "passthrough"}, {"name": "a", "use": "passthrough"}]),
     "rules[1].name", "duplicate"),
    (document(rules=[{"name": "a", "use": "passthrough", "extra": 1}]), "rules[0].extra", "unknown key"),
    (document(rules=[{"name": "a", "use": "passthrough", "enabled": "no"}]), "rules[0].enabled", "true or false"),
    (document(rules=[{"name": "a", "use": "passthrough", "override_protected": 1}]), "rules[0].override_protected",
     "true or false"),
    (document(rules=[{"name": "a"}]), "rules[0].use", "must be the name of a profile"),
    (document(rules=[{"name": "a", "use": "missing"}]), "rules[0].use", "must be the name of a profile"),
    (document(rules=[{"name": "a", "use": "$nowhere"}]), "rules[0].use", "must be the name of a profile"),
    (document(rules=[{"name": "a", "use": "passthrough", "if": []}]), "rules[0].if", "must be an object"),
    (document(rules=[{"name": "a", "use": "passthrough", "if": {"colour": "red"}}]), "rules[0].if.colour",
     "unknown condition"),
    (document(rules=[{"name": "a", "use": "passthrough", "if": {"tools": "yes"}}]), "rules[0].if.tools",
     "true or false"),
    (document(rules=[{"name": "a", "use": "passthrough", "if": {"client": 5}}]), "rules[0].if.client", "glob"),
    (document(rules=[{"name": "a", "use": "passthrough", "if": {"client": []}}]), "rules[0].if.client", "glob"),
    (document(rules=[{"name": "a", "use": "passthrough", "if": {"model": ["gpt-*", ""]}}]), "rules[0].if.model[1]",
     "glob"),
    # default profile, protection
    (document(default_profile="missing"), "default_profile", "must be null or the name"),
    (document(default_profile=5), "default_profile", "must be null or the name"),
    (document(protected=[]), "protected", "must be an object"),
    (document(protected={"enabled": "yes"}), "protected.enabled", "true or false"),
    (document(protected={"foo": 1}), "protected.foo", "unknown key"),
    # codex
    (document(codex=[]), "codex", "must be an object"),
    (document(codex={"foo": 1}), "codex.foo", "unknown key"),
    (document(codex={"models": []}), "codex.models", "must be an object"),
    (document(codex={"models": {"gpt-*": []}}), "codex.models.gpt-*", "must be an object"),
    (document(codex={"models": {"gpt-*": {"base": "x"}}}), "codex.models.gpt-*.base", "unknown key"),
    (document(codex={"models": {"gpt-*": {"include_skills_usage_instructions": "yes"}}}),
     "codex.models.gpt-*.include_skills_usage_instructions", "true or false"),
    (document(codex={"models": {"gpt-*": {"persistent_instructions": 5}}}),
     "codex.models.gpt-*.persistent_instructions", "string or"),
]


@pytest.mark.parametrize("doc, path, fragment", INVALID, ids=[f"{path or 'root'}-{i}" for i, (_, path, _) in
                                                            enumerate(INVALID)])
def test_invalid_document_names_the_exact_path(tmp_path, doc, path, fragment):
    error = rejected(doc, tmp_path)
    assert error.path == path
    assert fragment in error.message
    assert str(error) == (f"{path}: {error.message}" if path else error.message)


def test_minimal_document_and_the_builtin_passthrough(tmp_path):
    snapshot = compile_doc(document(), tmp_path, rev=7)
    assert snapshot.rev == 7
    assert snapshot.profile_names() == ["passthrough"]
    assert snapshot.profile("passthrough") == {
        "name": "passthrough", "description": "the client's system prompt, unchanged",
        "head": "{{client_system}}", "client_system": {"edits": []},
        "history": {"mid_system": "keep", "user_blocks": []}, "tools": {"edits": []},
        "generation": {}, "publish_as_model": False}


def test_profile_order_is_builtin_first_then_document_order(tmp_path):
    doc = document(profiles={"zeta": {}, "alpha": {}, "mid": {}})
    assert compile_doc(doc, tmp_path).profile_names() == ["passthrough", "zeta", "alpha", "mid"]


def test_unknown_profile_is_a_key_error(tmp_path):
    with pytest.raises(KeyError):
        compile_doc(document(), tmp_path).profile("nope")


def test_the_snapshot_keeps_a_copy_of_the_document_and_hands_out_copies(tmp_path):
    doc = document(texts={"a": "A"}, profiles={"p": {"head": "{{text:a}}"}})
    snapshot = compile_doc(doc, tmp_path, rev=1)
    doc["texts"]["a"] = "CHANGED"
    assert snapshot.document["texts"]["a"] == "A"
    snapshot.document["texts"]["a"] = "MUTATED"
    snapshot.profile("p")["head"] = "MUTATED"
    assert snapshot.document["texts"]["a"] == "A"
    assert snapshot.profile("p")["head"] == "{{text:a}}"
    with pytest.raises(AttributeError):
        snapshot.rev = 5
    with pytest.raises(AttributeError):
        snapshot.document = {}


def test_a_snapshot_can_be_built_directly_from_a_document(tmp_path):
    (tmp_path / "t.md").write_text("FILE", encoding="utf-8")
    doc = document(texts={"a": {"file": "t.md"}}, profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": "{{text:a}}"}]}}})
    snapshot = pp.Snapshot(3, doc, str(tmp_path / "prompts.json"))
    assert snapshot.rev == 3 and snapshot.source_path == str(tmp_path / "prompts.json")
    assert snapshot.profile("p")["client_system"]["edits"] == [{"op": "append", "text": "FILE"}]
    with pytest.raises(pp.PromptConfigError):
        pp.Snapshot(1, {"version": 3}, None, base_dir=tmp_path)


# -- texts and files -------------------------------------------------------------------------------------------------


def text_of(snapshot, name="p"):
    return snapshot.profile(name)["client_system"]["edits"][0]["text"]


def file_doc(name):
    return document(texts={"t": {"file": name}}, profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": "{{text:t}}"}]}}})


def test_a_text_file_is_read_as_utf8_without_bom_with_unix_newlines(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_bytes("\ufeffпервая\r\nвторая\rтретья\n".encode("utf-8"))
    snapshot = compile_doc(file_doc("prompts/a.md"), tmp_path)
    assert text_of(snapshot) == "первая\nвторая\nтретья\n"
    path, mtime, size = snapshot.files[0]
    assert pathlib.Path(path) == (tmp_path / "prompts" / "a.md").resolve()
    assert size == (tmp_path / "prompts" / "a.md").stat().st_size and mtime > 0


def test_the_same_file_is_read_once_and_listed_once(tmp_path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    doc = document(texts={"x": {"file": "a.md"}, "y": {"file": "./a.md"}, "z": {"file": ".//a.md"}})
    assert len(compile_doc(doc, tmp_path).files) == 1


def test_a_missing_file_is_an_error_at_its_path(tmp_path):
    error = rejected(file_doc("prompts/missing.md"), tmp_path)
    assert error.path == "texts.t.file" and "file not found" in error.message


def test_a_directory_is_not_a_text(tmp_path):
    (tmp_path / "prompts").mkdir()
    error = rejected(file_doc("prompts"), tmp_path)
    assert error.path == "texts.t.file" and "regular file" in error.message


def test_a_file_that_is_not_utf8_is_refused(tmp_path):
    (tmp_path / "a.md").write_bytes(b"\xff\xfeh\x00i\x00")
    error = rejected(file_doc("a.md"), tmp_path)
    assert "UTF-8" in error.message


@pytest.mark.parametrize("name", [
    "../outside.md", "..\\outside.md", "prompts/../../outside.md", "prompts\\..\\..\\outside.md", "sub/./../../outside.md",
    "/etc/passwd", "\\windows\\win.ini", "C:\\Windows\\win.ini", "C:outside.md", "c:/outside.md",
    "\\\\server\\share\\a.md", "//server/share/a.md", "a\x00.md", "", "   "])
def test_a_file_cannot_leave_the_folder_of_the_prompts_file(tmp_path, name):
    inner = tmp_path / "inner"
    inner.mkdir()
    (tmp_path / "outside.md").write_text("SECRET", encoding="utf-8")
    with pytest.raises(pp.PromptConfigError) as raised:
        compile_doc(file_doc(name), inner)
    assert raised.value.path == "texts.t.file"
    assert "SECRET" not in str(raised.value)


def test_an_absolute_path_inside_the_folder_is_still_refused(tmp_path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    error = rejected(file_doc(str(tmp_path / "a.md")), tmp_path)
    assert error.path == "texts.t.file" and "relative path" in error.message


def test_a_symlink_that_leads_out_is_refused(tmp_path):
    inner, outside = tmp_path / "inner", tmp_path / "outside"
    inner.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET", encoding="utf-8")
    try:
        os.symlink(outside / "secret.md", inner / "file-link.md")
        os.symlink(outside, inner / "dir-link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    for name in ("file-link.md", "dir-link/secret.md"):
        error = rejected(file_doc(name), inner)
        assert error.path == "texts.t.file" and "inside the folder" in error.message


def test_a_symlink_that_stays_inside_is_fine(tmp_path):
    (tmp_path / "real.md").write_text("REAL", encoding="utf-8")
    try:
        os.symlink(tmp_path / "real.md", tmp_path / "alias.md")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert text_of(compile_doc(file_doc("alias.md"), tmp_path)) == "REAL"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions exist on Windows")
def test_a_junction_that_leads_out_is_refused(tmp_path):
    inner, outside = tmp_path / "inner", tmp_path / "outside"
    inner.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET", encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(inner / "junction"), str(outside)],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction: " + made.stdout + made.stderr)
    error = rejected(file_doc("junction/secret.md"), inner)
    assert error.path == "texts.t.file" and "inside the folder" in error.message


def test_a_text_limit_applies_to_strings_and_files_alike(tmp_path):
    limit = pp.MAX_TEXT_BYTES
    assert limit == 256 * 1024
    ok = compile_doc(document(texts={"a": "x" * limit}), tmp_path)
    assert ok.document["texts"]["a"] == "x" * limit
    assert rejected(document(texts={"a": "x" * (limit + 1)}), tmp_path).path == "texts.a"
    # the limit counts bytes, not characters
    assert rejected(document(texts={"a": "я" * (limit // 2 + 1)}), tmp_path).path == "texts.a"
    (tmp_path / "big.md").write_bytes(b"x" * (limit + 1))
    assert rejected(document(texts={"a": {"file": "big.md"}}), tmp_path).path == "texts.a.file"
    (tmp_path / "ok.md").write_bytes(b"x" * limit)
    compile_doc(document(texts={"a": {"file": "ok.md"}}), tmp_path)
    assert rejected(profile_doc(head="x" * (limit + 1)), tmp_path).path == "profiles.p.head"


def test_a_document_with_its_files_is_limited_to_two_mebibytes(tmp_path):
    assert pp.MAX_DOCUMENT_BYTES == 2 * 1024 * 1024
    texts = {f"t{i}": "x" * (255 * 1024) for i in range(9)}
    assert rejected(document(texts=texts), tmp_path).path == ""
    files = {}
    for index in range(9):
        (tmp_path / f"f{index}.md").write_bytes(b"y" * (255 * 1024))
        files[f"t{index}"] = {"file": f"f{index}.md"}
    error = rejected(document(texts=files), tmp_path)
    assert error.path == "texts.t8.file" and "together" in error.message


def test_text_references_in_edits_are_expanded_but_not_their_own_braces(tmp_path):
    doc = document(texts={"a": "A {{client}} \\1"}, profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": "x {{text:a}} y"},
        {"op": "replace_text", "find": "f", "with": "{{text:a}}"},
        {"op": "replace_regex", "pattern": "(f)", "with": "\\1-{{text:a}}"},
        {"op": "append", "text": "\\{{literal}} and \\{{text:a}}"}]}}})
    edits = compile_doc(doc, tmp_path).profile("p")["client_system"]["edits"]
    assert edits[0]["text"] == "x A {{client}} \\1 y"
    assert edits[1]["with"] == "A {{client}} \\1"
    # a replacement of replace_regex must still be a valid re.sub template: the text's backslash is escaped
    assert edits[2]["with"] == "\\1-A {{client}} \\\\1"
    assert edits[3]["text"] == "{{literal}} and {{text:a}}"


def test_the_expansion_of_texts_is_bounded(tmp_path):
    big = "x" * (200 * 1024)
    edits = [{"op": "append", "text": "{{text:big}}"} for _ in range(100)]
    doc = document(texts={"big": big}, profiles={"p": {"client_system": {"edits": edits}}})
    error = rejected(doc, tmp_path)
    assert "expand" in error.message and error.path.startswith("profiles.p.client_system.edits[")
    one = document(texts={"a": "y" * (200 * 1024)}, profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": "{{text:a}}{{text:a}}"}]}}})
    assert "once {{text:...}} is expanded" in rejected(one, tmp_path).message


def test_head_may_come_from_a_file_and_keeps_its_variables(tmp_path):
    (tmp_path / "head.md").write_text("A\n\n{{client_system}}\n", encoding="utf-8")
    snapshot = compile_doc(profile_doc(head={"file": "head.md"}), tmp_path)
    assert snapshot.profile("p")["head"] == "A\n\n{{client_system}}\n"
    (tmp_path / "bad.md").write_text("{{unknown}}", encoding="utf-8")
    assert rejected(profile_doc(head={"file": "bad.md"}), tmp_path).path == "profiles.p.head"


# -- normalized profiles ---------------------------------------------------------------------------------------------


def test_a_profile_is_normalized_with_every_section(tmp_path):
    doc = document(profiles={"bare": {}, "full": {
        "description": "d", "head": "H",
        "client_system": {"edits": [
            {"op": "remove_section", "heading": "# A"}, {"op": "keep_sections", "headings": ["# B"]},
            {"op": "remove_tag", "tag": "t"}, {"op": "keep_tag_inner", "tag": "u"},
            {"op": "remove_between", "start": "<", "end": ">"},
            {"op": "remove_regex", "pattern": "x"}, {"op": "replace_regex", "pattern": "y", "flags": "si", "with": "z"},
            {"op": "replace_text", "find": "a", "with": ""}, {"op": "prepend", "text": "P"}, {"op": "append", "text": "Q"}]},
        "history": {"mid_system": "plain", "user_blocks": [
            {"op": "drop_tag", "tag": "system-reminder"}, {"op": "drop_tag", "tag": "a", "contains": "k"},
            {"op": "drop_tag", "tag": "a", "pattern": "p", "flags": "m"}, {"op": "remove_regex", "pattern": "r"},
            {"op": "replace_text", "find": "f", "with": "w", "all": False}]},
        "tools": {"edits": [
            {"tool": "bash", "op": "append", "text": "A"}, {"tool": "b*", "op": "prepend", "text": "B"},
            {"tool": "c", "op": "set", "text": ""}, {"tool": "d", "op": "replace_text", "find": "f", "with": "g"},
            {"tool": "e", "op": "replace_regex", "pattern": "h", "with": "i"}, {"tool": "gone", "hide": True}]},
        "generation": {"effort": "max", "verbosity": None}, "publish_as_model": ["gpt-*", "claude-*"]}})
    snapshot = compile_doc(doc, tmp_path)
    assert snapshot.profile("bare") == {
        "name": "bare", "description": "", "head": "{{client_system}}", "client_system": {"edits": []},
        "history": {"mid_system": "keep", "user_blocks": []}, "tools": {"edits": []}, "generation": {},
        "publish_as_model": False}
    full = snapshot.profile("full")
    assert full["description"] == "d" and full["head"] == "H"
    assert full["client_system"]["edits"] == [
        {"op": "remove_section", "heading": "# A"}, {"op": "keep_sections", "headings": ["# B"]},
        {"op": "remove_tag", "tag": "t"}, {"op": "keep_tag_inner", "tag": "u"},
        {"op": "remove_between", "start": "<", "end": ">", "inclusive": False},
        {"op": "remove_regex", "pattern": "x", "flags": ""},
        {"op": "replace_regex", "pattern": "y", "flags": "is", "with": "z"},
        {"op": "replace_text", "find": "a", "with": "", "all": True},
        {"op": "prepend", "text": "P"}, {"op": "append", "text": "Q"}]
    assert full["history"] == {"mid_system": "plain", "user_blocks": [
        {"op": "drop_tag", "tag": "system-reminder", "contains": None, "pattern": None, "flags": ""},
        {"op": "drop_tag", "tag": "a", "contains": "k", "pattern": None, "flags": ""},
        {"op": "drop_tag", "tag": "a", "contains": None, "pattern": "p", "flags": "m"},
        {"op": "remove_regex", "pattern": "r", "flags": ""},
        {"op": "replace_text", "find": "f", "with": "w", "all": False}]}
    assert full["tools"]["edits"] == [
        {"tool": "bash", "hide": False, "op": "append", "text": "A"},
        {"tool": "b*", "hide": False, "op": "prepend", "text": "B"},
        {"tool": "c", "hide": False, "op": "set", "text": ""},
        {"tool": "d", "hide": False, "op": "replace_text", "find": "f", "with": "g", "all": True},
        {"tool": "e", "hide": False, "op": "replace_regex", "pattern": "h", "flags": "", "with": "i"},
        {"tool": "gone", "hide": True}]
    assert full["generation"] == {"effort": "max"}
    assert full["publish_as_model"] == ["gpt-*", "claude-*"]


@pytest.mark.parametrize("value", [False, True, [], ["gpt-*"]])
def test_publish_as_model_forms(tmp_path, value):
    doc = document(profiles={"p": {"publish_as_model": value}})
    assert compile_doc(doc, tmp_path).profile("p")["publish_as_model"] == value


@pytest.mark.parametrize("effort", pp.models.EFFORT_ORDER)
def test_every_known_effort_and_verbosity_is_accepted(tmp_path, effort):
    for verbosity in ("low", "medium", "high", None):
        doc = document(profiles={"p": {"generation": {"effort": effort, "verbosity": verbosity}}})
        generation = compile_doc(doc, tmp_path).profile("p")["generation"]
        assert generation == {"effort": effort, **({"verbosity": verbosity} if verbosity else {})}


def test_null_generation_values_change_nothing(tmp_path):
    doc = document(profiles={"p": {"generation": {"effort": None, "verbosity": None}}})
    assert compile_doc(doc, tmp_path).profile("p")["generation"] == {}


def test_edit_lists_are_bounded(tmp_path):
    edits = [{"op": "append", "text": "x"}] * (pp.MAX_EDITS + 1)
    assert rejected(edits_doc(*edits), tmp_path).path == "profiles.p.client_system.edits"


# -- relay texts, codex, rules ----------------------------------------------------------------------------------------


def test_relay_texts_are_the_users_text_or_none(tmp_path):
    doc = document(relay_texts={"role": "MY ROLE", "native_role": None, "structured": "{{default}} + {{schema_name}}",
                                "reminder": "<r>{{text}}</r>"})
    snapshot = compile_doc(doc, tmp_path)
    assert snapshot.relay_text("role") == "MY ROLE"
    assert snapshot.relay_text("native_role") is None
    assert snapshot.relay_text("structured") == "{{default}} + {{schema_name}}"
    assert snapshot.relay_text("reminder") == "<r>{{text}}</r>"
    assert snapshot.relay_text("codex_base") is None
    with pytest.raises(KeyError):
        snapshot.relay_text("nope")
    assert compile_doc(document(), tmp_path).relay_text("role") is None


def test_an_empty_role_is_a_deliberate_text(tmp_path):
    assert compile_doc(document(relay_texts={"role": ""}), tmp_path).relay_text("role") == ""


def test_relay_texts_may_come_from_files(tmp_path):
    (tmp_path / "role.md").write_bytes(b"FILE ROLE\r\n")
    snapshot = compile_doc(document(relay_texts={"role": {"file": "role.md"}}), tmp_path)
    assert snapshot.relay_text("role") == "FILE ROLE\n"


def test_reminder_and_structured_wrappers_accept_escapes(tmp_path):
    doc = document(relay_texts={"reminder": "\\{{raw}} {{text}}", "structured": "\\{{x}} {{schema}}"})
    snapshot = compile_doc(doc, tmp_path)
    assert snapshot.relay_text("reminder") == "\\{{raw}} {{text}}"


def test_codex_base_for_every_model_or_by_glob(tmp_path):
    snapshot = compile_doc(document(relay_texts={"codex_base": "ALL"}), tmp_path)
    assert snapshot.codex_base("gpt-6-sol") == "ALL" and snapshot.codex_base("anything") == "ALL"
    by_glob = compile_doc(document(relay_texts={"codex_base": {"gpt-6*": "SIX", "GPT-*": "ANY GPT", "claude-*": "C"}}),
                          tmp_path)
    assert by_glob.codex_base("gpt-6-sol") == "SIX"      # the first matching glob in document order
    assert by_glob.codex_base("gpt-5.5") == "ANY GPT"    # globs ignore case
    assert by_glob.codex_base("CLAUDE-opus") == "C"
    assert by_glob.codex_base("glm-5") is None
    assert compile_doc(document(), tmp_path).codex_base("gpt-6") is None
    assert compile_doc(document(relay_texts={"codex_base": {}}), tmp_path).codex_base("gpt-6") is None


def test_codex_base_from_a_file_or_a_file_per_model(tmp_path):
    (tmp_path / "base.md").write_text("FROM FILE", encoding="utf-8")
    assert compile_doc(document(relay_texts={"codex_base": {"file": "base.md"}}), tmp_path).codex_base("m") == "FROM FILE"
    per_model = compile_doc(document(relay_texts={"codex_base": {"gpt-*": {"file": "base.md"}}}), tmp_path)
    assert per_model.codex_base("gpt-5") == "FROM FILE"


def test_codex_overrides_come_from_the_first_matching_glob(tmp_path):
    (tmp_path / "persist.md").write_text("PERSIST", encoding="utf-8")
    doc = document(codex={"models": {
        "gpt-6*": {"persistent_instructions": {"file": "persist.md"}, "include_skills_usage_instructions": True},
        "gpt-*": {"include_plugin_usage_instructions": False, "include_apps_usage_instructions": True},
        "never": {}}})
    snapshot = compile_doc(doc, tmp_path)
    assert snapshot.codex_overrides("gpt-6-sol") == {
        "persistent_instructions": "PERSIST", "include_skills_usage_instructions": True}
    assert snapshot.codex_overrides("gpt-5.5") == {
        "include_plugin_usage_instructions": False, "include_apps_usage_instructions": True}
    assert snapshot.codex_overrides("never") == {}
    assert snapshot.codex_overrides("claude-opus-5-5") == {}
    assert compile_doc(document(), tmp_path).codex_overrides("gpt-6") == {}
    snapshot.codex_overrides("gpt-6-sol")["persistent_instructions"] = "MUTATED"
    assert snapshot.codex_overrides("gpt-6-sol")["persistent_instructions"] == "PERSIST"
    assert set(pp.CODEX_OVERRIDE_KEYS) == {"persistent_instructions", "include_skills_usage_instructions",
                                           "include_plugin_usage_instructions", "include_apps_usage_instructions"}


def test_rules_accept_every_condition_and_use(tmp_path):
    doc = document(profiles={"p": {}}, rules=[
        {"name": "everything", "enabled": True, "override_protected": False, "use": "p", "if": {
            "client": ["opencode", "codex"], "agent": "build", "purpose": "client-*", "request_class": "compaction",
            "model": ["claude-opus-*", "gpt-*"], "tools": True, "structured": False, "has_client_system": True,
            "dialect": "chat", "header_profile": "*", "virtual_profile": "*"}},
        {"name": "header", "use": "$header"}, {"name": "virtual", "use": "$virtual", "enabled": False},
        {"name": "legacy", "use": "$legacy"}, {"name": "builtin", "use": "passthrough"}])
    assert compile_doc(doc, tmp_path).warnings == []
    assert set(pp.CONDITIONS) == {"client", "agent", "purpose", "request_class", "model", "tools", "structured",
                                  "has_client_system", "dialect", "header_profile", "virtual_profile"}


def test_default_profile_may_be_null_a_document_profile_or_passthrough(tmp_path):
    for value in (None, "p", "passthrough"):
        compile_doc(document(profiles={"p": {}}, default_profile=value), tmp_path)


# -- warnings the admin shows ---------------------------------------------------------------------------------------


def test_warnings_for_disabled_protection_overrides_hidden_tools_and_bare_wrappers(tmp_path):
    doc = document(
        protected={"enabled": False},
        relay_texts={"structured": "Reply with JSON."},
        profiles={"p": {"tools": {"edits": [{"tool": "x", "hide": True}]}}},
        rules=[{"name": "loud", "use": "p", "override_protected": True},
               {"name": "off", "use": "p", "override_protected": True, "enabled": False},
               {"name": "typo", "use": "p", "if": {"client": "claude", "dialect": ["chat", "soap"],
                                                   "purpose": "titles", "model": "claude"}}])
    warnings = compile_doc(doc, tmp_path).warnings
    assert any("protected.enabled is false" in line for line in warnings)
    assert any("'loud' overrides the protection" in line for line in warnings)
    assert not any("'off'" in line for line in warnings)
    assert any("hides a tool" in line and "profiles.p" in line for line in warnings)
    assert any("relay_texts.structured does not use" in line for line in warnings)
    assert any(line.startswith("rules[2].if.client:") and "'claude'" in line for line in warnings)
    assert any(line.startswith("rules[2].if.dialect[1]:") and "'soap'" in line for line in warnings)
    assert any(line.startswith("rules[2].if.purpose:") for line in warnings)
    assert not any("rules[2].if.model" in line for line in warnings)


def test_snapshot_warnings_are_a_copy(tmp_path):
    snapshot = compile_doc(document(protected={"enabled": False}), tmp_path)
    snapshot.warnings.append("x")
    assert len(snapshot.warnings) == 1


def test_the_document_must_be_plain_json(tmp_path):
    assert "plain JSON" in rejected(document(texts={"a": {1, 2}}), tmp_path).message
    assert "plain JSON" in rejected(document(texts={"a": float("nan")}), tmp_path).message


def test_a_document_round_trips_through_json(tmp_path):
    doc = document(texts={"a": "Я"}, profiles={"p": {"head": "{{text:a}}"}})
    snapshot = compile_doc(json.loads(json.dumps(doc, ensure_ascii=False)), tmp_path)
    assert snapshot.document == doc
