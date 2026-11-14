"""R1-02: a {"file": ...} text is read only from prompts/ next to the prompts file, and only as a .md or .txt file.

The folder of the prompts file holds config.json (the cookies, the xsrf token and relay_api_key) and the journal: a document
that could name them would put them into the head and send them to Retool. Reviewer 1 read config.json this way, both from a
saved document and from a draft of the preview.
"""
import json
import os
import subprocess
import sys

import pytest

import prompt_policy as pp
from test_prompt_policy_common import document, make_store
from test_prompt_policy_preview import chat

SECRETS = {"relay_api_key": "private-relay-key", "cookie": "accessToken=private-cookie-value", "xsrf": "private-xsrf"}


def relay_folder(tmp_path):
    """A relay folder: config.json and the journal beside the prompts file, and one real text in prompts/."""
    folder = tmp_path / "relay"
    (folder / "prompts").mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps(SECRETS), encoding="utf-8")
    (folder / "journal.jsonl").write_text('{"secret": "in the journal"}\n', encoding="utf-8")
    (folder / "prompts" / "ok.md").write_text("FINE", encoding="utf-8")
    return folder


def draft(name):
    return document(texts={"x": {"file": name}}, profiles={"p": {"head": "{{text:x}}"}}, default_profile="p")


def refusal(folder, name):
    with pytest.raises(pp.PromptConfigError) as raised:
        pp.compile_document(draft(name), folder, rev=1)
    return raised.value


def leaked(text):
    return any(secret in text for secret in SECRETS.values()) or "in the journal" in text


def link_to_folder(link, target):
    """A directory link (a symlink, else a junction on Windows); False when none can be made here."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        return made.returncode == 0
    return False


@pytest.mark.parametrize("name", [
    "config.json", "./config.json", "CONFIG.JSON", "journal.jsonl", "prompts.json", "prompts/../config.json",
    "../relay/config.json", "sub/../config.json", "config.json::$DATA", "config.json:stream", ".", "prompts/.."])
def test_the_relay_config_and_the_journal_cannot_be_read_as_texts(tmp_path, name):
    folder = relay_folder(tmp_path)
    error = refusal(folder, name)
    assert error.path == "texts.x.file"
    assert not leaked(str(error))


@pytest.mark.parametrize("name", [
    "prompts/data.json", "prompts/config.json", "prompts/ok", "prompts/ok.md.json", "prompts/ok.mdx", "prompts/ok.md ",
    "prompts/ok.md.", "prompts/ok.md:stream", "prompts/ok.md::$DATA", "prompts/.md.exe", "prompts/ok.markdown"])
def test_only_md_and_txt_files_are_texts(tmp_path, name):
    folder = relay_folder(tmp_path)
    for other in ("data.json", "config.json", "ok.md.json", "ok.mdx", "ok", "ok.markdown"):
        (folder / "prompts" / other).write_text("OTHER", encoding="utf-8")
    error = refusal(folder, name)
    assert error.path == "texts.x.file"
    assert error.message.startswith(("must name a .md or .txt file", "must not contain ':'"))


@pytest.mark.parametrize("name", ["prompts/ok.md", "prompts/ok.txt", "prompts/sub/deep.md", "prompts/Upper.MD",
                                  "./prompts/ok.md", "prompts//ok.md", "prompts\\ok.md"])
def test_a_md_or_txt_file_inside_prompts_is_a_text(tmp_path, name):
    folder = relay_folder(tmp_path)
    (folder / "prompts" / "sub").mkdir()
    for fresh in ("ok.txt", "sub/deep.md", "Upper.MD"):
        (folder / "prompts" / fresh).write_text("FINE", encoding="utf-8")
    snapshot = pp.compile_document(draft(name), folder, rev=1)
    assert snapshot.profile("p")["head"] == "{{text:x}}"
    assert snapshot._data.texts["x"] == "FINE"


def test_the_refusal_says_where_texts_live(tmp_path):
    folder = relay_folder(tmp_path)
    assert "inside the prompts/ folder next to the prompts file" in refusal(folder, "config.json").message
    assert pp.TEXT_DIR == "prompts" and pp.TEXT_SUFFIXES == (".md", ".txt")


def test_every_kind_of_text_goes_through_the_same_check(tmp_path):
    folder = relay_folder(tmp_path)
    where = {
        "texts.x": document(texts={"x": {"file": "config.json"}}),
        "profiles.p.head": document(profiles={"p": {"head": {"file": "config.json"}}}),
        "relay_texts.role": document(relay_texts={"role": {"file": "config.json"}}),
        "relay_texts.native_role": document(relay_texts={"native_role": {"file": "config.json"}}),
        "relay_texts.codex_base": document(relay_texts={"codex_base": {"file": "config.json"}}),
        "relay_texts.codex_base.gpt-*": document(relay_texts={"codex_base": {"gpt-*": {"file": "config.json"}}}),
        "codex.models.gpt-*.persistent_instructions": document(
            codex={"models": {"gpt-*": {"persistent_instructions": {"file": "config.json"}}}})}
    for field, doc in where.items():
        with pytest.raises(pp.PromptConfigError) as raised:
            pp.compile_document(doc, folder, rev=1)
        assert raised.value.path == field + ".file"
        assert not leaked(str(raised.value))


def test_a_stored_document_that_names_config_json_is_not_loaded(tmp_path):
    folder = relay_folder(tmp_path)
    (folder / "prompts.json").write_text(json.dumps(draft("config.json")), encoding="utf-8")
    store = pp.PromptStore({}, str(folder / "prompts.json"))
    assert store.snapshot() is None
    assert store.errors()[0]["param"] == "texts.x.file" and not leaked(json.dumps(store.errors()))


def test_saving_a_document_that_names_config_json_is_refused_and_writes_nothing(tmp_path):
    folder = relay_folder(tmp_path)
    store = pp.PromptStore({}, str(folder / "prompts.json"))
    with pytest.raises(pp.PromptConfigError) as raised:
        store.save(draft("config.json"), None)
    assert raised.value.path == "texts.x.file"
    assert not (folder / "prompts.json").exists() and store.snapshot() is None


def test_a_draft_of_the_preview_cannot_read_config_json_into_the_head(tmp_path):
    folder = relay_folder(tmp_path)
    store = make_store(folder)
    with pytest.raises(pp.PromptConfigError) as raised:
        store.preview(chat(), "chat", {}, document=draft("config.json"))
    assert raised.value.path == "texts.x.file" and not leaked(str(raised.value))
    # the same draft pointing at a real text works, so the refusal is about the place and not about drafts
    assert store.preview(chat(), "chat", {}, document=draft("prompts/ok.md"))["head"] == "FINE"


def test_a_link_inside_prompts_that_leads_out_is_refused(tmp_path):
    folder = relay_folder(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("OUTSIDE", encoding="utf-8")
    if not link_to_folder(folder / "prompts" / "link", outside):
        pytest.skip("no directory link can be made here")
    error = refusal(folder, "prompts/link/secret.md")
    assert error.path == "texts.x.file" and "OUTSIDE" not in str(error)


def test_a_prompts_folder_that_is_itself_a_link_to_somewhere_else_is_refused(tmp_path):
    folder = tmp_path / "relay2"
    folder.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ok.md").write_text("ELSEWHERE", encoding="utf-8")
    if not link_to_folder(folder / "prompts", elsewhere):
        pytest.skip("no directory link can be made here")
    assert refusal(folder, "prompts/ok.md").path == "texts.x.file"


def test_a_file_link_with_a_md_name_that_points_at_another_kind_of_file_is_refused(tmp_path):
    folder = relay_folder(tmp_path)
    (folder / "prompts" / "data.json").write_text("{}", encoding="utf-8")
    try:
        os.symlink(folder / "prompts" / "data.json", folder / "prompts" / "disguised.md")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert refusal(folder, "prompts/disguised.md").message.startswith("must name a .md or .txt file")


def test_the_example_layout_of_the_documentation_still_works(tmp_path):
    folder = relay_folder(tmp_path)
    (folder / "prompts" / "opus.md").write_text("You are Opus.\n", encoding="utf-8")
    doc = document(texts={"opus_role": {"file": "prompts/opus.md"}}, profiles={"exact-opus": {"head": "{{text:opus_role}}"}})
    assert pp.compile_document(doc, folder, rev=1)._data.texts["opus_role"] == "You are Opus.\n"
