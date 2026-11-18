"""The admin's "Синхр. OpenCode": one write at a time with the relay's own sync, and an answer that says what it did."""
import json
import subprocess
import threading
import time
import types

import client_sync
from catalog import admin_ops


def _admin(tmp_path, answers):
    """An admin whose relay keeps a shared OpenCodeSync; `answers` maps a script name to (exit status, stdout, stderr)."""
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    settings = tmp_path / "opencode.jsonc"
    settings.write_text("{}", encoding="utf-8")
    harness = tmp_path / "cordis.patch.yml"
    harness.write_text("providers: {}\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        script = [part for part in command if str(part).endswith(".py")][-1]
        calls.append(script)
        status, out, err = answers[client_sync.pathlib.Path(script).name]
        return subprocess.CompletedProcess(command, status, out, err)

    cfg = {"_config_path": str(config), "opencode_settings": str(settings), "harness_settings": str(harness)}
    shared = client_sync.OpenCodeSync(cfg, runner=runner)
    admin = types.SimpleNamespace(cfg=cfg, runtime=types.SimpleNamespace(client_sync=shared), _safe=lambda value: value)
    return admin, shared, calls


WRITTEN = (0, json.dumps({"written": True, "changed": True, "models": 7}) + "\n", "")
CURRENT = (0, json.dumps({"written": False, "changed": False, "models": 7}) + "\n", "")
FAILED = (1, "", "error: no Proxi provider in the profile\n")


# A14-07: the button ran a sync of its own (its own lock: it could write OpenCode's file while the relay's sync did), also
# rewrote the DeepSeek Harness profiles, and its "ok" looked at OpenCode alone: a failed Harness sync, or a list already
# current (nothing written), was reported as "Список моделей обновлён".
def test_a_failed_harness_profile_is_not_reported_as_a_success(tmp_path):
    admin, _shared, _calls = _admin(tmp_path, {"sync_opencode_catalog.py": WRITTEN, "sync_harness_catalog.py": FAILED})
    answer = admin_ops.sync_opencode_now(admin)
    assert answer["ok"] is False
    assert answer["written"] is True
    assert answer["result"]["harness"][0]["error"] == "error: no Proxi provider in the profile"


def test_the_answer_says_whether_anything_was_written(tmp_path):
    admin, _shared, _calls = _admin(tmp_path, {"sync_opencode_catalog.py": CURRENT, "sync_harness_catalog.py": CURRENT})
    answer = admin_ops.sync_opencode_now(admin)
    assert answer["ok"] is True and answer["written"] is False and answer["changed"] is False


def test_the_button_waits_for_a_sync_of_the_relay_under_way(tmp_path):
    admin, shared, calls = _admin(tmp_path, {"sync_opencode_catalog.py": WRITTEN, "sync_harness_catalog.py": WRITTEN})
    shared._lock.acquire()  # the relay's own sync is writing
    done = []
    worker = threading.Thread(target=lambda: done.append(admin_ops.sync_opencode_now(admin)), daemon=True)
    worker.start()
    time.sleep(0.3)
    assert calls == [] and not done
    shared._lock.release()
    worker.join(5)
    assert done and done[0]["ok"] is True and len(calls) == 2
