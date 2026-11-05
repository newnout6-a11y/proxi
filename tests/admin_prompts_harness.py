"""Shared setup of the prompt control admin API tests: a relay over HTTP whose prompts store is a fake that follows the
PromptStore contract (docs/prompt-control-plan.md 4 and 4.3), plus request helpers and the checks every route shares."""
import copy
import http.client
import json

import pytest

import apierr
import journal
import prompt_policy
from prompt_policy import Snapshot

KEY = "private-relay-key"  # the relay key of test_admin.admin_relay
MARKER = "PRIVATE-PROMPT-TEXT-MARKER"
DOC = {"version": 1,
       "texts": {"my_rules": MARKER + " Отвечай по-русски."},
       "profiles": {"mine": {"head": "{{text:my_rules}}\n\n{{client_system}}"}},
       "rules": [{"name": "opencode", "if": {"client": "opencode"}, "use": "mine"}]}
REQUEST = {"model": "claude-opus-5-5",
           "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]}


class FakeStore:
    """A PromptStore by contract, scripted; every call is recorded in `calls`."""

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.snap = None
        self.problems = []
        self.save_error = None
        self.snapshot_warnings = []
        self.heads = []
        self.pinned = []
        self.kept = {}  # request id (None: the newest) -> {"body", "dialect", "headers"}
        self.unpin_counts = {}  # what unpin(argument) reports
        self.preview_answer = {"rule": "opencode", "profile": "mine", "head": "H"}
        self.preview_error = None

    def called(self, name):
        return [call[1:] for call in self.calls if call[0] == name]

    def hold(self, document, rev=1):
        """Make `document` the active revision without a save: the state a file loaded at start leaves."""
        self.snap = Snapshot(rev, copy.deepcopy(document), str(self.path))

    def snapshot(self):
        return self.snap

    def maybe_reload(self):
        self.calls.append(("maybe_reload",))

    def save(self, document, base_rev):
        self.calls.append(("save", copy.deepcopy(document), base_rev))
        if self.save_error is not None:
            raise self.save_error
        current = None if self.snap is None else self.snap.rev
        if base_rev != current:
            raise apierr.conflict("the revision is stale")
        self.snap = Snapshot((current or 0) + 1, copy.deepcopy(document), str(self.path))
        if self.snapshot_warnings:
            self.snap.warnings = list(self.snapshot_warnings)
        return self.snap

    def errors(self):
        return copy.deepcopy(self.problems)

    def pins(self):
        return copy.deepcopy(self.pinned)

    def unpin(self, conversation_key=None):
        self.calls.append(("unpin", conversation_key))
        return self.unpin_counts.get(conversation_key, 0)

    def recent(self, limit=50):
        self.calls.append(("recent", limit))
        return copy.deepcopy(self.heads[:limit])

    def request(self, request_id=None):
        self.calls.append(("request", request_id))
        return copy.deepcopy(self.kept.get(request_id))

    def preview(self, body, dialect, headers, document=None):
        self.calls.append(("preview", copy.deepcopy(body), dialect, copy.deepcopy(headers), copy.deepcopy(document)))
        if self.preview_error is not None:
            raise self.preview_error
        return copy.deepcopy(self.preview_answer)


@pytest.fixture
def api(admin_relay, tmp_path):
    """admin_relay (test_admin) with the fake store behind runtime.prompts. Test modules import admin_relay too."""
    admin_relay.store = FakeStore(tmp_path / "prompts.json")
    admin_relay.runtime.prompts = admin_relay.store
    journal.JOURNAL.reset()
    return admin_relay


def guard(api):
    """The headers of a same-origin admin mutation."""
    return {"x-admin-request": "1", "origin": api.url}


def send(api, route, payload=None, **options):
    """api.request. With attempts > 1 it repeats a request whose connection dropped: the relay refuses (401, 403, 405,
    413, 415) without reading the body and closes the socket, and Windows may then reset it under the answer. Only
    requests that are refused anyway are sent this way, so a repeat can change nothing."""
    attempts = options.pop("attempts", 1)
    while True:
        try:
            return api.request(route, payload, **options)
        except (OSError, http.client.HTTPException):
            attempts -= 1
            if attempts <= 0:
                raise


def get(api, route="", **options):
    return send(api, "/admin/api/prompts" + route, **options)


def put(api, document, base_rev=None, **options):
    options.setdefault("headers", guard(api))
    return send(api, "/admin/api/prompts", {"document": document, "base_rev": base_rev}, method="PUT", **options)


def post(api, route, payload, **options):
    options.setdefault("headers", guard(api))
    return send(api, "/admin/api/prompts/" + route, payload, method="POST", **options)


def raw(api, method, route, body, headers=None):
    """A request with exactly these bytes: what a client that is not the admin UI may send."""
    host, port = api.url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.request(method, route, body, {"authorization": "Bearer " + KEY, "content-type": "application/json",
                                                 **guard(api), **(headers or {})})
        reply = connection.getresponse()
        data = reply.read()
        return reply.status, (json.loads(data) if data else None)
    finally:
        connection.close()


def events(kind):
    return [event for event in journal.JOURNAL.query(order="asc") if event["kind"] == kind]


def deep(levels):
    """A document nested `levels` objects deep around {"head": "x"}."""
    node = {"head": "x"}
    for _ in range(levels):
        node = {"n": node}
    return node


def real_engine_or_skip():
    """The tests of the real PromptStore wait for the engine of prompt_policy (another work package) to be in the branch."""
    try:
        prompt_policy.compile_document({"version": 1}, prompt_policy.pathlib.Path("."), rev=1)
    except NotImplementedError:
        pytest.skip("the prompts engine (prompt_policy) is not part of this branch")
    except prompt_policy.PromptConfigError:
        pass  # implemented: this probe document is simply not valid there


# -- checks every route shares ------------------------------------------------------------------
def check_needs_the_key(api, method, route, payload, key):
    status, body, headers = send(api, route, payload, method=method, key=key, attempts=4,
                                 headers=guard(api) if method != "GET" else None)
    assert status == 401 and body["error"]["code"] == "unauthorized"
    assert headers["cache-control"] == "no-store" and "access-control-allow-origin" not in headers
    assert api.store.calls == []


def check_csrf(api, method, route, payload, headers, status):
    before = api.store.snapshot()
    assert send(api, route, payload, method=method, headers=headers, attempts=4)[0] == status
    assert api.store.calls == [] and api.store.snapshot() is before
    assert events("admin.prompts") == [] and events("admin.prompt_pins") == []


CSRF_CASES = [
    ({}, 403),
    ({"x-admin-request": "1", "origin": "http://attacker.invalid"}, 403),
    ({"x-admin-request": "1", "origin": "null"}, 403),
    ({"x-admin-request": "1", "sec-fetch-site": "cross-site"}, 403),
    ({"x-admin-request": "0"}, 403),
    ({"x-admin-request": "1", "content-type": "text/plain"}, 415),
]
