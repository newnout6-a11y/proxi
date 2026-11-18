"""What the admin saves in the settings: a value the next start refuses is never saved, a setting that says it applies at
once does, and the model lists follow the catalog as it is now."""
import json
import threading
import time

import pytest

import apierr
import config
import stream_watchdog
from test_admin_pool import clean_environment, pool  # noqa: F401 - the environment guard and the two-account relay

STREAM_KEYS = ("stream_first_token_timeout", "stream_idle_timeout", "stream_tool_timeout", "stream_thinking_timeout")


def settings(pool):
    status, body = pool.call("/admin/api/settings")
    assert status == 200
    return body


def patch(pool, changes):
    return pool.call("/admin/api/settings", {"settings": changes}, method="PATCH")


# A07-01: the three stream timeouts were "number" fields, so the admin saved 7.5 and answered "applied", and the next start
# refused config.json ("must be an integer"): the relay stayed down until the file was fixed by hand.
def test_every_number_the_admin_saves_lets_the_relay_start_again(pool):
    schema = settings(pool)["schema"]
    original = pool.path.read_text(encoding="utf-8")
    refused = []
    for key, field in sorted(schema.items()):
        if field["type"] not in ("integer", "number"):
            continue
        value = field.get("min", 0) + 0.5
        if "max" in field and value > field["max"]:
            continue
        status, _ = patch(pool, {key: value})
        if status != 200:
            continue  # refused by the admin: nothing reached the file
        try:
            config.load(str(pool.path))
        except apierr.ApiError as error:
            refused.append((key, error.message))
        pool.path.write_text(original, encoding="utf-8")
    assert refused == []


def test_the_stream_timeouts_are_whole_seconds_in_the_admin_and_at_start(pool, tmp_path):
    schema = settings(pool)["schema"]
    for key in STREAM_KEYS:
        assert schema[key]["type"] == "integer", key
        assert schema[key]["restart_required"] is False, key
        status, body = patch(pool, {key: 7.5})
        assert status == 400 and body["error"]["param"] == key
    # stream_thinking_timeout (the thinking budget of 0d205e9) is checked at start like the others.
    saved = json.loads(pool.path.read_text(encoding="utf-8"))
    for bad in (1.5, -1, True, config.STREAM_TIMEOUT_MAX + 1):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(dict(saved, stream_thinking_timeout=bad)), encoding="utf-8")
        with pytest.raises(apierr.ApiError, match="stream_thinking_timeout"):
            config.load(str(path))
    path.write_text(json.dumps(dict(saved, stream_thinking_timeout=0)), encoding="utf-8")
    assert config.load(str(path))["stream_thinking_timeout"] == 0
    assert config.DEFAULTS["stream_thinking_timeout"] == stream_watchdog.DEFAULT_THINKING_TIMEOUT


# A07-03: every account's session reads the stream timeouts from its own copy of the configuration (account_config), and
# the admin changed only the root: the 300 s first-token cut stayed in force on every account until a restart.
def test_a_stream_timeout_saved_in_the_admin_reaches_every_account_at_once(pool):
    backends = pool.runtime.pool.backends
    assert len(backends) == 2 and all(backend.cfg is not pool.runtime.cfg for backend in backends)
    status, body = patch(pool, {"stream_first_token_timeout": 900, "stream_idle_timeout": 200,
                                "stream_thinking_timeout": 1200})
    assert status == 200 and set(body["applied"]) == {"stream_first_token_timeout", "stream_idle_timeout",
                                                       "stream_thinking_timeout"}
    for backend in backends:
        assert stream_watchdog.timeouts(backend.cfg) == (900.0, 200.0)
        assert stream_watchdog.thinking_timeout(backend.cfg) == 1200.0


def test_an_account_that_sets_a_value_of_its_own_keeps_it(pool):
    first, second = pool.runtime.pool.backends
    first.cfg["_account_ref"]["stream_idle_timeout"] = 45  # the account's own record names it
    first.cfg["stream_idle_timeout"] = 45
    assert patch(pool, {"stream_idle_timeout": 200})[0] == 200
    assert first.cfg["stream_idle_timeout"] == 45 and second.cfg["stream_idle_timeout"] == 200


class FakeRefresher:
    def __init__(self, thread=None):
        self.calls, self._thread = [], thread

    def stop(self):
        self.calls.append("stop")
        self._thread = None

    def start(self):
        self.calls.append(("start", self.cfg.get("catalog_refresh_hours")))


# A07-03: the provider catalog's refresher fixed its interval when it started, so a new catalog_refresh_hours ("applies
# at once") changed nothing: 0 -> 6 started nothing, 6 -> 1 kept 6 hours, 6 -> 0 kept refreshing.
def test_a_new_catalog_refresh_interval_restarts_the_provider_refresher(pool):
    refresher = FakeRefresher()
    refresher.cfg = pool.runtime.cfg
    pool.runtime.provider_catalog = refresher
    status, body = patch(pool, {"catalog_refresh_hours": 1})
    assert status == 200 and "catalog_refresh_hours" in body["applied"]
    assert refresher.calls == ["stop", ("start", 1)]
    refresher.calls.clear()
    assert patch(pool, {"catalog_refresh_hours": 1})[0] == 200  # unchanged: nothing to restart
    assert refresher.calls == []


def test_a_refresher_still_reading_starts_again_only_after_its_read_ends(pool):
    release = threading.Event()
    busy = threading.Thread(target=release.wait, daemon=True)
    busy.start()
    refresher = FakeRefresher(thread=busy)
    refresher.cfg = pool.runtime.cfg
    pool.runtime.provider_catalog = refresher
    assert patch(pool, {"catalog_refresh_hours": 2})[0] == 200
    assert refresher.calls == ["stop"]  # a second loop beside the one still reading would run both
    release.set()
    deadline = time.monotonic() + 5
    while refresher.calls == ["stop"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert refresher.calls == ["stop", ("start", 2)]
