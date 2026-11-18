"""What the admin saves in the settings: a value the next start refuses is never saved, a setting that says it applies at
once does, and the model lists follow the catalog as it is now."""
import json

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
