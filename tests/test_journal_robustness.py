"""The journal survives what Windows and clients throw at it: held files, lone surrogates, odd line separators."""
import json
import os

from journal import Journal
from test_journal_rotation import log, numbered, written


def fill(instance, start, count):
    for number in range(start, start + count):
        instance.emit("probe", "event %d" % number)


def retry_now(instance):
    instance._rotate_retry_at = 0.0  # the minute of back-off after a failed rotation, skipped


def hold(monkeypatch, blocked):
    """os.replace fails for `blocked` (a path) as long as the returned flag is set, like a file another program holds."""
    real = os.replace
    state = {"held": True}

    def replace(source, target):
        if state["held"] and os.path.normcase(os.fspath(source)) == os.path.normcase(os.fspath(blocked)):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(source, target)

    monkeypatch.setattr(os, "replace", replace)
    return state


def test_retries_while_journal_1_is_held_delete_no_more_old_files(tmp_path, monkeypatch):
    instance = log(tmp_path, 2000, 5)
    fill(instance, 0, 400)
    assert numbered(tmp_path) == [1, 2, 3, 4, 5]
    hold(monkeypatch, tmp_path / "journal.jsonl.1")
    for round_ in range(6):  # six minutes of retries
        retry_now(instance)
        fill(instance, 1000 + 20 * round_, 20)
    # the first attempt dropped .5, as a whole rotation would; the retries only tried the blocked move again
    assert numbered(tmp_path) == [1, 3, 4, 5]


def test_retries_while_the_live_file_is_held_lose_nothing_and_finish_when_it_is_free(tmp_path, monkeypatch):
    instance = log(tmp_path, 2000, 5)
    fill(instance, 0, 400)
    before = {n: written(tmp_path / ("journal.jsonl.%d" % n)) for n in numbered(tmp_path)}
    state = hold(monkeypatch, tmp_path / "journal.jsonl")
    for round_ in range(6):
        retry_now(instance)
        fill(instance, 1000 + 20 * round_, 20)
    assert numbered(tmp_path) == [2, 3, 4, 5]
    assert [written(tmp_path / ("journal.jsonl.%d" % n)) for n in (2, 3, 4, 5)] == [before[n] for n in (1, 2, 3, 4)]
    state["held"] = False
    retry_now(instance)
    fill(instance, 2000, 5)
    assert numbered(tmp_path) == [1, 2, 3, 4, 5]


def test_an_event_with_a_lone_surrogate_is_written_and_read_back(tmp_path):
    instance = log(tmp_path, 10 ** 7, 2)
    instance.emit("before", "ok")
    event = instance.emit("http.error", "unknown model 'claude@\ud83d'", "warn", model="x\ud83dy")
    instance.emit("after", "ok")
    assert event is not None
    records = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in records] == ["before", "http.error", "after"]
    assert "\\ud83d" in records[1]["message"] and "\\ud83d" in records[1]["data"]["model"]
    # the admin views serialize the buffer the same way: nothing in it is unencodable any more
    json.dumps(instance.query(limit=10), ensure_ascii=False).encode("utf-8")


def test_records_holding_line_separators_survive_a_restart(tmp_path):
    first = log(tmp_path, 10 ** 7, 2)
    first.emit("http.request", "GET /v1/models", user_agent="curl/8 \x85probe")
    first.emit("tool.call", "call", arguments="a b")
    first.emit("web_search.run", "search", query="c d")
    second = Journal()
    second.configure({"journal_max_file_bytes": 10 ** 7, "journal_keep_files": 2}, str(tmp_path / "journal.jsonl"))
    kinds = [event["kind"] for event in second.query(limit=10, order="asc")]
    assert kinds == ["http.request", "tool.call", "web_search.run"]


def test_a_client_error_that_echoes_a_lone_surrogate_still_reaches_the_client(relay):
    # the JSON escape of half a surrogate pair, as a JavaScript client that cut a string by UTF-16 units sends it
    model = "claude-opus-5-5@" + "\\" + "ud83d"
    raw = ('{"model": "%s", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}' % model).encode("ascii")
    status, body = relay.json_any("/v1/messages", raw=raw)
    assert status == 400 and body["type"] == "error"
    assert chr(0xD83D) in body["error"]["message"]  # escaped on the wire, the same half pair after json.loads
