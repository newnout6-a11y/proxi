"""PromptStore: loading, live reload, atomic save, revisions, journal events and the ring of recent heads."""
import json
import os
import pathlib
import threading

import pytest

import apierr
import journal
import prompt_policy as pp
from test_prompt_policy_common import Clock, document, inp, make_store, write_json

SECRET = "SECRET-PROMPT-TEXT-0451"


def doc(head="V1", **extra):
    return document(texts={"secret": SECRET}, profiles={"a": {"head": head}},
                    rules=[{"name": "all", "use": "a"}], **extra)


def mark():
    return journal.JOURNAL.last_seq


def events(since, *kinds):
    found = journal.JOURNAL.query(since=since, limit=5000, order="asc")
    return [event for event in found if event["kind"] in (kinds or ("prompt.reload", "prompt.config_error"))]


def tick(store, clock, seconds=None):
    clock.advance(pp.RELOAD_INTERVAL + 0.01 if seconds is None else seconds)
    store.maybe_reload()


def rewrite(path, document_):
    """Write a document so that the file certainly differs in mtime: a file system stamps two quick writes alike."""
    path = pathlib.Path(path)
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps(document_), encoding="utf-8")
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, max(info.st_mtime_ns, before) + 3_000_000_000))


# -- where the file is and the first load -------------------------------------------------------------------------------


def test_the_file_is_the_explicit_path_then_the_configured_one_then_the_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pp.PromptStore({}).path == pathlib.Path("prompts.json")
    assert pp.PromptStore({"prompts_file": "mine.json"}).path == pathlib.Path("mine.json")
    assert pp.PromptStore({"prompts_file": "mine.json"}, path="other.json").path == pathlib.Path("other.json")
    store = pp.PromptStore({"prompts_file": "mine.json", "_config_path": str(tmp_path / "conf" / "config.json")})
    assert store.path == tmp_path / "conf" / "mine.json"
    store = pp.PromptStore({"_config_path": str(tmp_path / "conf" / "config.json")})
    assert store.path == tmp_path / "conf" / "prompts.json"
    absolute = str(tmp_path / "abs.json")
    assert pp.PromptStore({"prompts_file": absolute, "_config_path": str(tmp_path / "conf" / "c.json")}).path == (
        pathlib.Path(absolute))


def test_a_relative_default_is_read_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_json(tmp_path / "prompts.json", doc())
    store = pp.PromptStore({})
    assert store.snapshot().rev == 1


def test_no_file_means_no_snapshot_no_error_and_no_event(tmp_path):
    since = mark()
    store = make_store(tmp_path)
    assert store.snapshot() is None and store.errors() == []
    assert events(since) == []


def test_a_valid_file_is_loaded_by_the_constructor(tmp_path):
    since = mark()
    store = make_store(tmp_path, doc())
    snapshot = store.snapshot()
    assert snapshot.rev == 1 and snapshot.source_path == str(tmp_path / "prompts.json")
    assert snapshot.profile_names() == ["passthrough", "a"] and store.errors() == []
    (event,) = events(since)
    assert event["kind"] == "prompt.reload" and event["level"] == "info"
    assert event["data"]["rev"] == 1 and event["data"]["source"] == "startup"
    assert event["data"]["profiles"] == 1 and event["data"]["rules"] == 1 and len(event["data"]["sha8"]) == 8


@pytest.mark.parametrize("content, param, fragment", [
    (b"{not json", None, "invalid JSON"),
    (b"[1, 2]", None, "JSON object"),
    (b'{"version": 1, "version": 1}', None, "duplicate key"),
    (b'{"version": 2}', "version", "must be 1"),
    (b'{"version": 1, "profiles": {"p": {"head": "{{nope}}"}}}', "profiles.p.head", "unknown variable"),
    (b"\xff\xfe{\x00}\x00", None, "UTF-8"),
    (b"", None, "invalid JSON"),
])
def test_an_invalid_file_at_startup_leaves_no_snapshot_and_reports_the_error(tmp_path, content, param, fragment):
    (tmp_path / "prompts.json").write_bytes(content)
    since = mark()
    store = make_store(tmp_path)
    assert store.snapshot() is None
    (error,) = store.errors()
    assert error["param"] == param and fragment in error["message"] and error["ts"] > 0
    (event,) = events(since)
    assert event["kind"] == "prompt.config_error" and event["level"] == "error"
    assert event["data"]["param"] == param and fragment in event["data"]["error"]
    assert store.plan(inp()).legacy


def test_a_bom_in_the_file_is_ignored(tmp_path):
    (tmp_path / "prompts.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(doc()).encode("utf-8"))
    assert make_store(tmp_path).snapshot().rev == 1


def test_a_file_over_two_mebibytes_or_a_directory_is_refused(tmp_path):
    (tmp_path / "prompts.json").write_bytes(b" " * (pp.MAX_DOCUMENT_BYTES + 1))
    store = make_store(tmp_path)
    assert store.snapshot() is None and "larger than 2 MiB" in store.errors()[0]["message"]
    other = tmp_path / "dir"
    (other / "prompts.json").mkdir(parents=True)
    store = make_store(other)
    assert store.snapshot() is None and "not a regular file" in store.errors()[0]["message"]


def test_errors_are_copies(tmp_path):
    (tmp_path / "prompts.json").write_text("{", encoding="utf-8")
    store = make_store(tmp_path)
    store.errors()[0]["message"] = "x"
    store.errors().clear()
    assert "invalid JSON" in store.errors()[0]["message"]


# -- live reload -------------------------------------------------------------------------------------------------------


def test_the_file_is_looked_at_once_per_interval_by_the_injected_clock(tmp_path, monkeypatch):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    calls = []
    real = pp._signature
    monkeypatch.setattr(pp, "_signature", lambda path: calls.append(path) or real(path))
    rewrite(tmp_path / "prompts.json", doc("V2"))
    for _ in range(5):
        store.maybe_reload()
    clock.advance(pp.RELOAD_INTERVAL - 0.01)
    store.maybe_reload()
    assert calls == [] and store.snapshot().rev == 1
    clock.advance(0.02)
    store.maybe_reload()
    assert calls and store.snapshot().rev == 2
    count = len(calls)
    store.maybe_reload()
    assert len(calls) == count


def test_an_unchanged_file_is_not_recompiled(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    for _ in range(4):
        tick(store, clock)
    assert store.snapshot().rev == 1 and events(since) == []


def test_a_changed_file_becomes_the_next_revision(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    old = store.snapshot()
    since = mark()
    rewrite(tmp_path / "prompts.json", doc("V2"))
    tick(store, clock)
    new = store.snapshot()
    assert (old.rev, new.rev) == (1, 2) and new is not old
    assert new.document["profiles"]["a"]["head"] == "V2" and old.document["profiles"]["a"]["head"] == "V1"
    (event,) = events(since)
    assert event["data"]["rev"] == 2 and event["data"]["source"] == "file"
    rewrite(tmp_path / "prompts.json", doc("V3"))
    tick(store, clock)
    assert store.snapshot().rev == 3


def test_a_change_of_mtime_alone_is_noticed(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    path = tmp_path / "prompts.json"
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    tick(store, clock)
    assert store.snapshot().rev == 2


def test_a_text_file_that_changes_reloads_the_revision(tmp_path):
    clock = Clock()
    (tmp_path / "t.md").write_text("one", encoding="utf-8")
    document_ = document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}"}},
                         rules=[{"name": "all", "use": "a"}])
    store = make_store(tmp_path, document_, clock=clock)
    assert store.snapshot().rev == 1
    (tmp_path / "t.md").write_text("two!", encoding="utf-8")
    tick(store, clock)
    assert store.snapshot().rev == 2
    plan = store.plan(inp())
    assert plan.snapshot._data.texts["t"] == "two!"
    path, mtime, size = store.snapshot().files[0]
    assert pathlib.Path(path).name == "t.md" and size == 4


def test_a_text_file_touched_without_a_size_change_still_reloads(tmp_path):
    clock = Clock()
    (tmp_path / "t.md").write_text("abc", encoding="utf-8")
    document_ = document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}"}})
    store = make_store(tmp_path, document_, clock=clock)
    info = (tmp_path / "t.md").stat()
    (tmp_path / "t.md").write_text("xyz", encoding="utf-8")
    os.utime(tmp_path / "t.md", ns=(info.st_atime_ns, info.st_mtime_ns + 7_000_000_000))
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.snapshot()._data.texts["t"] == "xyz"


def test_an_invalid_change_keeps_the_last_good_revision_and_says_why(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    rewrite(tmp_path / "prompts.json", document(profiles={"p": {"head": "{{broken}}"}}))
    tick(store, clock)
    assert store.snapshot().rev == 1
    (error,) = store.errors()
    assert error["param"] == "profiles.p.head" and "unknown variable" in error["message"]
    (event,) = events(since)
    assert event["kind"] == "prompt.config_error" and event["data"]["param"] == "profiles.p.head"
    assert store.plan(inp()).profile == "a"                      # the previous revision keeps serving
    for _ in range(3):
        tick(store, clock)
    assert len(events(since)) == 1                               # the same bad file is not judged again
    rewrite(tmp_path / "prompts.json", doc("FIXED"))
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.errors() == []


def test_broken_json_after_a_good_one_is_the_same_story(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    rewrite(tmp_path / "prompts.json", {})
    (tmp_path / "prompts.json").write_text('{"version": 1, ', encoding="utf-8")
    info = (tmp_path / "prompts.json").stat()
    os.utime(tmp_path / "prompts.json", ns=(info.st_atime_ns, info.st_mtime_ns + 9_000_000_000))
    tick(store, clock)
    assert store.snapshot().rev == 1 and "invalid JSON" in store.errors()[0]["message"]


def test_a_removed_file_returns_new_conversations_to_legacy(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    assert store.snapshot() is None and store.errors() == []
    (event,) = events(since)
    assert event["kind"] == "prompt.reload" and event["data"]["source"] == "removed" and event["data"]["rev"] is None
    assert store.plan(inp()).legacy
    tick(store, clock)
    assert len(events(since)) == 1


def test_a_removed_and_restored_file_continues_the_revision_numbers(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    write_json(tmp_path / "prompts.json", doc("AGAIN"))
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.plan(inp()).rev == 2


def test_a_file_that_appears_later_is_loaded(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock=clock)
    write_json(tmp_path / "prompts.json", doc())
    tick(store, clock)
    assert store.snapshot().rev == 1 and store.plan(inp()).profile == "a"


def test_a_broken_file_that_appears_later_is_reported_once(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock=clock)
    since = mark()
    (tmp_path / "prompts.json").write_text("nope", encoding="utf-8")
    tick(store, clock)
    tick(store, clock)
    assert store.snapshot() is None and len(events(since)) == 1 and store.errors()


def test_a_text_file_that_appears_after_the_document_makes_it_valid(tmp_path):
    clock = Clock()
    document_ = document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}"}})
    store = make_store(tmp_path, document_, clock=clock)
    assert store.snapshot() is None and "file not found" in store.errors()[0]["message"]
    (tmp_path / "t.md").write_text("arrived", encoding="utf-8")
    tick(store, clock)
    assert store.snapshot().rev == 1 and store.errors() == []


def test_a_text_file_that_is_removed_keeps_the_last_good_revision(tmp_path):
    clock = Clock()
    (tmp_path / "t.md").write_text("here", encoding="utf-8")
    document_ = document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}"}})
    store = make_store(tmp_path, document_, clock=clock)
    (tmp_path / "t.md").unlink()
    tick(store, clock)
    assert store.snapshot().rev == 1 and "file not found" in store.errors()[0]["message"]


def test_the_history_keeps_the_last_snapshots(tmp_path):
    store = make_store(tmp_path, doc())
    for index in range(pp.SNAPSHOT_HISTORY + 8):
        store.save(doc("V%d" % (index + 2)), store.snapshot().rev)
    newest = pp.SNAPSHOT_HISTORY + 9
    assert sorted(store._history) == list(range(newest - pp.SNAPSHOT_HISTORY + 1, newest + 1))
    assert store.snapshot().rev == newest == max(store._history)


def test_concurrent_reloads_compile_once(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    rewrite(tmp_path / "prompts.json", doc("V2"))
    clock.advance(5)
    since = mark()
    threads = [threading.Thread(target=store.maybe_reload) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.snapshot().rev == 2 and len(events(since)) == 1


# -- save --------------------------------------------------------------------------------------------------------------


def test_save_writes_the_file_and_installs_the_revision(tmp_path):
    store = make_store(tmp_path, doc())
    since = mark()
    snapshot = store.save(doc("Привет"), 1)
    assert snapshot.rev == 2 and store.snapshot() is snapshot and snapshot.source_path == str(store.path)
    on_disk = (tmp_path / "prompts.json").read_text(encoding="utf-8")
    assert json.loads(on_disk) == doc("Привет") and "Привет" in on_disk and on_disk.endswith("\n")
    assert store.plan(inp()).snapshot is snapshot
    (event,) = events(since)
    assert event["data"]["rev"] == 2 and event["data"]["source"] == "admin"


def test_the_first_save_needs_no_base_revision_and_creates_the_folder(tmp_path):
    store = make_store(tmp_path / "deep" / "folder")
    assert store.snapshot() is None
    snapshot = store.save(doc(), None)
    assert snapshot.rev == 1 and (tmp_path / "deep" / "folder" / "prompts.json").exists()


@pytest.mark.parametrize("loaded, base_rev", [(True, None), (True, 2), (True, 0), (False, 1)])
def test_a_stale_base_revision_is_a_409_and_changes_nothing(tmp_path, loaded, base_rev):
    store = make_store(tmp_path, doc() if loaded else None)
    before = (tmp_path / "prompts.json").read_bytes() if loaded else None
    with pytest.raises(apierr.ApiError) as raised:
        store.save(doc("NEW"), base_rev)
    assert (raised.value.status, raised.value.code) == (409, "conflict")
    assert (tmp_path / "prompts.json").read_bytes() == before if loaded else not (tmp_path / "prompts.json").exists()
    assert (store.snapshot().rev if loaded else store.snapshot()) == (1 if loaded else None)


def test_an_invalid_document_is_refused_before_anything_is_written(tmp_path):
    store = make_store(tmp_path, doc())
    before = (tmp_path / "prompts.json").read_bytes()
    with pytest.raises(pp.PromptConfigError) as raised:
        store.save(document(profiles={"p": {"head": "{{nope}}"}}), 1)
    assert raised.value.path == "profiles.p.head"
    assert (tmp_path / "prompts.json").read_bytes() == before and store.snapshot().rev == 1
    assert [name for name in os.listdir(tmp_path) if name != "prompts.json"] == []


def test_a_write_that_fails_leaves_the_old_file_and_revision_and_no_litter(tmp_path, monkeypatch):
    store = make_store(tmp_path, doc())
    before = (tmp_path / "prompts.json").read_bytes()

    attempts = []

    def boom(source, target):
        attempts.append(source)
        raise PermissionError("locked")

    monkeypatch.setattr(pp, "REPLACE_PAUSE", 0)
    monkeypatch.setattr(pp.os, "replace", boom)
    with pytest.raises(apierr.ApiError) as raised:
        store.save(doc("NEW"), 1)
    assert raised.value.status == 503 and len(attempts) == pp.REPLACE_ATTEMPTS
    assert (tmp_path / "prompts.json").read_bytes() == before and store.snapshot().rev == 1
    assert sorted(os.listdir(tmp_path)) == ["prompts.json"] or sorted(os.listdir(tmp_path)) == [
        "prompts.json", "prompts.json.bak"]


def test_save_goes_through_a_temporary_file_in_the_same_folder(tmp_path, monkeypatch):
    store = make_store(tmp_path, doc())
    seen = []
    real = os.replace
    monkeypatch.setattr(pp.os, "replace", lambda source, target: seen.append((source, target)) or real(source, target))
    store.save(doc("V2"), 1)
    (source, target), = seen
    assert pathlib.Path(source).parent == tmp_path and pathlib.Path(target) == tmp_path / "prompts.json"
    assert pathlib.Path(source).name.startswith(".prompts-") and not pathlib.Path(source).exists()


def test_save_keeps_the_previous_file_as_a_backup(tmp_path):
    store = make_store(tmp_path, doc("V1"))
    first = (tmp_path / "prompts.json").read_bytes()
    store.save(doc("V2"), 1)
    assert (tmp_path / "prompts.json.bak").read_bytes() == first
    store.save(doc("V3"), 2)
    assert json.loads((tmp_path / "prompts.json.bak").read_text(encoding="utf-8"))["profiles"]["a"]["head"] == "V2"


def test_a_saved_revision_is_not_loaded_a_second_time(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    store.save(doc("V2"), 1)
    since = mark()
    for _ in range(3):
        tick(store, clock)
    assert store.snapshot().rev == 2 and events(since) == []


def test_save_notices_an_edit_made_outside_before_it_judges_the_base_revision(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    rewrite(tmp_path / "prompts.json", doc("EXTERNAL"))
    with pytest.raises(apierr.ApiError) as raised:
        store.save(doc("MINE"), 1)
    assert raised.value.status == 409 and store.snapshot().rev == 2
    assert json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))["profiles"]["a"]["head"] == "EXTERNAL"
    assert store.save(doc("MINE"), 2).rev == 3


def test_save_over_a_broken_file_is_the_way_to_fix_it(tmp_path):
    (tmp_path / "prompts.json").write_text("{broken", encoding="utf-8")
    clock = Clock()
    store = pp.PromptStore({"prompts_file": str(tmp_path / "prompts.json")}, clock=clock)
    assert store.snapshot() is None and store.errors()
    snapshot = store.save(doc(), None)
    assert snapshot.rev == 1 and store.errors() == []
    assert (tmp_path / "prompts.json.bak").read_text(encoding="utf-8") == "{broken"


def test_save_clears_the_error_of_a_broken_file(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    rewrite(tmp_path / "prompts.json", document(profiles={"p": {"head": "{{x}}"}}))
    tick(store, clock)
    assert store.errors()
    store.save(doc("OK"), 1)
    assert store.errors() == []


def test_a_saved_document_watches_its_text_files(tmp_path):
    clock = Clock()
    (tmp_path / "t.md").write_text("one", encoding="utf-8")
    store = make_store(tmp_path, clock=clock)
    store.save(document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}"}}), None)
    (tmp_path / "t.md").write_text("two!", encoding="utf-8")
    tick(store, clock)
    assert store.snapshot().rev == 2


def test_the_saved_snapshot_is_independent_of_the_dict_that_was_passed(tmp_path):
    store = make_store(tmp_path)
    document_ = doc()
    snapshot = store.save(document_, None)
    document_["profiles"]["a"]["head"] = "CHANGED"
    assert snapshot.document["profiles"]["a"]["head"] == "V1"


def test_two_saves_from_one_revision_cannot_both_win(tmp_path):
    store = make_store(tmp_path, doc())
    results, errors = [], []

    def attempt(head):
        try:
            results.append(store.save(doc(head), 1).rev)
        except apierr.ApiError as exc:
            errors.append(exc.status)

    threads = [threading.Thread(target=attempt, args=("T%d" % index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [2] and errors == [409] * 5


# -- privacy (I7) ------------------------------------------------------------------------------------------------------


def test_the_journal_gets_lengths_and_names_never_texts(tmp_path):
    since = mark()
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    store.save(doc("V2"), 1)
    rewrite(tmp_path / "prompts.json", document(texts={"secret": SECRET}, profiles={"p": {"head": "{{nope}}"}}))
    tick(store, clock)
    (tmp_path / "prompts.json").unlink()
    tick(store, clock)
    found = events(since)
    assert [event["kind"] for event in found] == ["prompt.reload", "prompt.reload", "prompt.config_error",
                                                  "prompt.reload"]
    assert SECRET not in json.dumps(found, ensure_ascii=False)


# -- the ring of recent heads ------------------------------------------------------------------------------------------


def entry(index, **fields):
    return {"request_id": "r%d" % index, "client": "opencode", "model": "m", "head": "H%d" % index, **fields}


def test_record_works_without_a_snapshot(tmp_path):
    store = make_store(tmp_path)
    store.record(entry(1))
    assert [item["request_id"] for item in store.recent()] == ["r1"]


def test_recent_is_newest_first_and_bounded_by_the_limit(tmp_path):
    store = make_store(tmp_path, doc())
    for index in range(5):
        store.record(entry(index))
    assert [item["request_id"] for item in store.recent()] == ["r4", "r3", "r2", "r1", "r0"]
    assert [item["request_id"] for item in store.recent(2)] == ["r4", "r3"]
    assert store.recent(0) == [] and store.recent(-3) == []
    assert len(store.recent(10_000)) == 5


def test_the_ring_holds_the_last_hundred(tmp_path):
    store = make_store(tmp_path)
    assert pp.RECENT_LIMIT == 100
    for index in range(pp.RECENT_LIMIT + 7):
        store.record(entry(index))
    items = store.recent(10_000)
    assert len(items) == 100 and items[0]["request_id"] == "r106" and items[-1]["request_id"] == "r7"


def test_every_entry_gets_a_timestamp_and_keeps_its_fields(tmp_path):
    store = make_store(tmp_path)
    store.record({"request_id": "x", "parts": [{"source": "client", "chars": 3}], "ts": 5.5})
    store.record({"request_id": "y"})
    new, old = store.recent()
    assert old["ts"] == 5.5 and old["parts"] == [{"source": "client", "chars": 3}] and new["ts"] > 1_000_000


def test_recent_hands_out_copies_and_the_record_copies_what_it_was_given(tmp_path):
    store = make_store(tmp_path)
    original = entry(1, parts=[{"source": "client"}])
    store.record(original)
    original["head"] = "CHANGED"
    original["parts"].append("x")
    first = store.recent()[0]
    assert first["head"] == "H1"
    first["head"] = "MUTATED"
    first["parts"][0]["source"] = "MUTATED"
    assert store.recent()[0]["head"] == "H1" and store.recent()[0]["parts"] == [{"source": "client"}]


def request_entry(index):
    return entry(index, request={"body": {"messages": [index]}, "dialect": "chat", "headers": {"user-agent": "x"}})


def test_request_bodies_are_kept_for_the_newest_ten_only(tmp_path):
    store = make_store(tmp_path)
    assert pp.RECENT_REQUESTS_KEPT == 10
    for index in range(25):
        store.record(request_entry(index))
    flags = [item["has_request"] for item in store.recent(100)]
    assert flags == [True] * 10 + [False] * 15
    assert all("request" not in item for item in store.recent(100))
    assert store.request() == {"body": {"messages": [24]}, "dialect": "chat", "headers": {"user-agent": "x"}}
    assert store.request("r15")["body"] == {"messages": [15]}
    assert store.request("r14") is None and store.request("r0") is None and store.request("nope") is None


def test_the_latest_request_is_the_newest_record_that_kept_one(tmp_path):
    store = make_store(tmp_path)
    store.record(request_entry(1))
    store.record(entry(2))
    assert store.request()["body"] == {"messages": [1]}
    assert store.recent()[0]["has_request"] is False and store.recent()[1]["has_request"] is True
    assert make_store(tmp_path / "empty").request() is None


def test_request_returns_a_copy(tmp_path):
    store = make_store(tmp_path)
    store.record(request_entry(1))
    store.request()["body"]["messages"].append("x")
    assert store.request()["body"] == {"messages": [1]}


def test_a_request_that_is_not_a_dict_is_dropped(tmp_path):
    store = make_store(tmp_path)
    store.record(entry(1, request="not a dict"))
    assert store.recent()[0]["has_request"] is False and store.request() is None


def test_recording_from_many_threads_keeps_the_bounds(tmp_path):
    store = make_store(tmp_path)

    def work(base):
        for index in range(200):
            store.record(request_entry(base * 1000 + index))
            store.recent(5)
            store.request()

    threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    items = store.recent(10_000)
    assert len(items) == 100 and sum(item["has_request"] for item in items) == 10

def test_a_limit_that_is_not_a_number_and_an_entry_that_is_not_a_dict_do_no_harm(tmp_path):
    store = make_store(tmp_path)
    for index in range(60):
        store.record(entry(index))
    for bad in (None, "x", 5, [1], ("a",)):
        store.record(bad)
    assert len(store.recent("3")) == 3 and len(store.recent("many")) == 50 and len(store.recent(None)) == 50
    assert len(store.recent(2.9)) == 2 and len(store.recent()) == 50
    assert store.recent(1)[0]["request_id"] == "r59"

# -- many threads at once ------------------------------------------------------------------------------------------------


def test_planning_reloading_saving_and_recording_together_never_break_the_store(tmp_path):
    class Ticking(Clock):
        def __call__(self):
            self.now += 2.0
            return self.now

    store = make_store(tmp_path, doc(), clock=Ticking())
    failures, observed = [], {}
    stop = threading.Event()

    def guard(work):
        def run():
            try:
                work()
            except BaseException as exc:      # noqa: BLE001 - the test reports whatever a thread hit
                failures.append(repr(exc))
                stop.set()
        return run

    def saver():
        for index in range(20):
            if stop.is_set():
                return
            try:
                store.save(doc("S%d" % index), store.snapshot().rev)
            except apierr.ApiError as exc:
                assert exc.status in (409, 503)      # stale, or the file was open in the writer at that instant

    def outside():
        for index in range(8):
            if stop.is_set():
                return
            rewrite(tmp_path / "prompts.json", doc("E%d" % index))

    def planner(name):
        def run():
            while not stop.is_set():
                store.maybe_reload()
                plan = store.plan(inp(conversation_key=name))
                assert plan.snapshot is None or plan.rev == plan.snapshot.rev
                observed.setdefault(name, set()).add(plan.rev)
                store.record({"request_id": name, "rev": plan.rev})
                store.recent(3)
                store.pins()
        return run

    workers = [threading.Thread(target=guard(saver)), threading.Thread(target=guard(outside))] + [
        threading.Thread(target=guard(planner("c%d" % n))) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers[:2]:
        worker.join(timeout=60)
    stop.set()
    for worker in workers[2:]:
        worker.join(timeout=60)
    assert failures == []
    assert all(not worker.is_alive() for worker in workers)
    assert all(len(revs) == 1 for revs in observed.values()), observed       # a pinned conversation keeps its revision
    assert store.snapshot().rev >= 2
    revs = sorted(store._history)
    assert revs == list(range(revs[0], revs[-1] + 1))

def test_a_file_that_is_busy_for_a_moment_is_replaced_on_a_retry(tmp_path, monkeypatch):
    store = make_store(tmp_path, doc())
    real, attempts = os.replace, []

    def flaky(source, target):
        attempts.append(source)
        if len(attempts) < 3:
            raise PermissionError("a scanner has it open")
        return real(source, target)

    monkeypatch.setattr(pp, "REPLACE_PAUSE", 0)
    monkeypatch.setattr(pp.os, "replace", flaky)
    assert store.save(doc("V2"), 1).rev == 2 and len(attempts) == 3
    assert json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))["profiles"]["a"]["head"] == "V2"
    assert [name for name in os.listdir(tmp_path) if name.endswith(".tmp")] == []


# -- a save is the store's own write, not an outside edit ------------------------------------------------------------------


def text_document():
    return document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}"}},
                    rules=[{"name": "all", "use": "a"}])


def test_after_a_save_the_next_reload_finds_nothing_new(tmp_path):
    clock = Clock()
    (tmp_path / "t.md").write_text("one", encoding="utf-8")
    store = make_store(tmp_path, clock=clock)
    saved = store.save(text_document(), None)
    since = mark()
    for _ in range(3):
        tick(store, clock)
    assert store.snapshot() is saved and saved.rev == 1
    assert events(since) == []
    assert not any(event["kind"] == "prompt.config_error" for event in events(since))
    # what is written next from outside is still noticed: the remembered state is the saved one, not a blind spot
    (tmp_path / "t.md").write_text("two!", encoding="utf-8")
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.snapshot()._data.texts["t"] == "two!"


def test_a_save_over_an_existing_revision_is_not_loaded_again_either(tmp_path):
    clock = Clock()
    (tmp_path / "t.md").write_text("one", encoding="utf-8")
    store = make_store(tmp_path, text_document(), clock=clock)
    saved = store.save(document(texts={"t": {"file": "t.md"}}, profiles={"a": {"head": "{{text:t}}!"}}), 1)
    since = mark()
    tick(store, clock)
    tick(store, clock, 30)
    assert store.snapshot() is saved and saved.rev == 2 and events(since) == []


def test_a_file_system_that_stamps_the_renamed_file_differently_changes_nothing(tmp_path, monkeypatch):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    real = os.replace

    def replace_and_restamp(source, target):
        real(source, target)
        info = os.stat(target)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

    monkeypatch.setattr(pp.os, "replace", replace_and_restamp)
    saved = store.save(doc("V2"), 1)
    since = mark()
    tick(store, clock)
    assert store.snapshot() is saved and events(since) == []


def test_a_write_that_lands_on_top_of_a_save_is_picked_up_as_the_outside_edit_it_is(tmp_path, monkeypatch):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    real = os.replace

    def replace_then_overwrite(source, target):
        real(source, target)
        rewrite(target, doc("EXTERNAL"))

    monkeypatch.setattr(pp.os, "replace", replace_then_overwrite)
    saved = store.save(doc("MINE"), 1)
    monkeypatch.undo()
    assert saved.rev == 2
    tick(store, clock)
    assert store.snapshot().rev == 3
    assert store.snapshot().document["profiles"]["a"]["head"] == "EXTERNAL"


def test_the_example_of_the_plan_is_saved_and_served(tmp_path):
    from test_prompt_policy_compile import PLAN_EXAMPLE
    store = make_store(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "opus.md").write_text("Ты — Opus.\n", encoding="utf-8")
    snapshot = store.save(PLAN_EXAMPLE, None)
    assert snapshot.rev == 1 and snapshot.profile_names() == ["passthrough", "mine-first", "exact-opus"]
    assert json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8")) == PLAN_EXAMPLE
    assert store.plan(inp(model="claude-opus-5-5")).profile == "exact-opus"
    sonnet = inp(model="claude-sonnet-5-5", requested_model="claude-sonnet-5-5")
    assert store.plan(sonnet).profile == "mine-first"
    assert store.plan(inp(client="codex", header_profile="mine-first")).profile == "mine-first"
    assert store.plan(inp(client="codex")).legacy
    reloaded = pp.PromptStore({"prompts_file": str(tmp_path / "prompts.json")})
    assert reloaded.snapshot().document == snapshot.document and reloaded.errors() == []


# -- a file that cannot be read at this instant is tried again, not given up on -----------------------------------------------


def test_a_prompts_file_that_is_locked_for_a_moment_is_read_on_a_later_look(tmp_path, monkeypatch):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    rewrite(tmp_path / "prompts.json", doc("V2"))
    real, failures = pathlib.Path.read_bytes, []

    def locked(self):
        if self == store.path and len(failures) < 3:
            failures.append(1)
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", locked)
    since = mark()
    tick(store, clock)
    assert store.snapshot().rev == 1 and "cannot read the prompts file" in store.errors()[0]["message"]
    tick(store, clock)
    tick(store, clock)
    assert len(failures) == 3 and store.snapshot().rev == 1
    assert [event["kind"] for event in events(since)] == ["prompt.config_error"]     # the repeat is journaled once
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.errors() == []
    assert [event["kind"] for event in events(since)] == ["prompt.config_error", "prompt.reload"]


def test_a_text_file_that_is_locked_for_a_moment_is_read_on_a_later_look(tmp_path, monkeypatch):
    clock = Clock()
    (tmp_path / "t.md").write_text("one", encoding="utf-8")
    store = make_store(tmp_path, text_document(), clock=clock)
    (tmp_path / "t.md").write_text("two!", encoding="utf-8")
    real, failures = open, []

    def locked(file, *args, **kwargs):
        if str(file).endswith("t.md") and len(failures) < 2:
            failures.append(1)
            raise PermissionError(13, "in use")
        return real(file, *args, **kwargs)

    monkeypatch.setattr(pp, "open", locked, raising=False)
    since = mark()
    tick(store, clock)
    assert store.snapshot().rev == 1 and "cannot read the file" in store.errors()[0]["message"]
    assert store.errors()[0]["param"] == "texts.t.file"
    tick(store, clock)
    assert len(events(since)) == 1
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.snapshot()._data.texts["t"] == "two!" and store.errors() == []


def test_a_file_rewritten_while_it_is_being_read_is_not_judged_by_its_old_bytes(tmp_path, monkeypatch):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    (tmp_path / "prompts.json").write_text('{"version": 1, "profiles": ', encoding="utf-8")
    info = (tmp_path / "prompts.json").stat()
    os.utime(tmp_path / "prompts.json", ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    real = pp.json.loads

    def loads_while_the_editor_finishes(text, **kwargs):
        monkeypatch.setattr(pp.json, "loads", real)
        rewrite(tmp_path / "prompts.json", doc("DONE"))
        return real(text, **kwargs)

    monkeypatch.setattr(pp.json, "loads", loads_while_the_editor_finishes)
    tick(store, clock)
    assert store.snapshot().rev == 1 and "invalid JSON" in store.errors()[0]["message"]
    tick(store, clock)
    assert store.snapshot().rev == 2 and store.errors() == []
    assert store.snapshot().document["profiles"]["a"]["head"] == "DONE"


def test_every_new_judgement_of_a_bad_file_is_journaled(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    since = mark()
    for index in range(3):
        rewrite(tmp_path / "prompts.json", document(profiles={"p": {"head": "{{nope}}" + " " * index}}))
        tick(store, clock)
    assert [event["kind"] for event in events(since)] == ["prompt.config_error"] * 3
