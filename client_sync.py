"""The relay updates OpenCode's model list by itself after each look at its catalogs (Minara's and Retool's).

OpenCode reads its models from its own settings file (~/.config/opencode/opencode.jsonc), and only
scripts/sync_opencode_catalog.py wrote the relay's models there: a model the relay published, or the effort levels it
took from a provider, stayed out of OpenCode until someone ran that script (Gemini 4 Argon had no effort levels in
OpenCode on 2026-11-14, a day after the relay had them). After every look the relay now runs the script with --write,
as a process of its own (the script reads the relay's config and catalog overlays from disk, apart from the running
relay), when OpenCode's settings file exists and `opencode_sync` is on. The script writes nothing when the list is
already current and keeps a backup of every change (catalog-backups beside the settings file); OpenCode reads the
file when it starts.
"""
import json
import os
import pathlib
import subprocess
import sys
import threading

import console_log
import journal

SCRIPT = pathlib.Path(__file__).resolve().parent / "scripts" / "sync_opencode_catalog.py"
TIMEOUT = 120.0
BACKUPS = "catalog-backups"
# Where OpenCode keeps its settings; tests point it at an empty folder (conftest), so no test run writes the real file.
DEFAULT_FOLDER = pathlib.Path.home() / ".config" / "opencode"


def settings_path(cfg):
    """OpenCode's settings file: `opencode_settings` when set, else the first of opencode.jsonc and opencode.json in
    DEFAULT_FOLDER that exists; None when there is none."""
    configured = str(cfg.get("opencode_settings") or "").strip()
    if configured:
        return pathlib.Path(os.path.expanduser(configured))
    for name in ("opencode.jsonc", "opencode.json"):
        if (DEFAULT_FOLDER / name).is_file():
            return DEFAULT_FOLDER / name
    return None


def _summary(output):
    """The script's one-line JSON summary (its last JSON line), None when it printed none."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


class OpenCodeSync:
    """Callable after a look: `sync("minara")` runs the script once; the summary, or None when nothing was run."""

    def __init__(self, cfg, runner=None):
        self.cfg = cfg
        self._run = runner or subprocess.run
        self._lock = threading.Lock()
        self.last = None

    def __call__(self, reason):
        if not self.cfg.get("opencode_sync", True):
            return None
        path = settings_path(self.cfg)
        relay_config = self.cfg.get("_config_path")
        if path is None or not path.is_file() or not relay_config or not SCRIPT.is_file():
            return None
        command = [sys.executable, str(SCRIPT), "--relay-config", str(relay_config), "--settings", str(path),
                   "--write", "--backup-dir", str(path.parent / BACKUPS)]
        with self._lock:  # Minara's and Retool's looks may end together; one write at a time
            try:
                done = self._run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=TIMEOUT, cwd=str(SCRIPT.parent.parent),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console window pops up
            except (OSError, subprocess.SubprocessError) as error:
                summary = {"error": type(error).__name__}
            else:
                summary = _summary(done.stdout)
                if summary is None or done.returncode:
                    detail = (done.stderr or "").strip().splitlines()
                    summary = {"error": (detail[-1] if detail else "exit status %s" % done.returncode)[:300]}
        # Never a credential in what is kept or said: an error can quote a path or a line of the files read.
        if summary.get("error"):
            summary["error"] = journal.scrub(str(summary["error"]))
        self.last = dict(summary, reason=reason, settings=str(path))
        if summary.get("error"):
            journal.emit("client.opencode_sync_failed", "Список моделей OpenCode не обновлён после проверки каталога "
                         "%s: %s" % (reason, summary["error"]), "warn", client="opencode", reason=reason,
                         settings=str(path), error=summary["error"])
            console_log.write("[catalog] OpenCode: список моделей не обновлён: %s" % summary["error"])
        elif summary.get("written"):
            journal.emit("client.opencode_synced", "Список моделей OpenCode обновлён после проверки каталога %s: %s "
                         "моделей; прежний файл сохранён в %s" % (reason, summary.get("models"), summary.get("backup")),
                         "success", client="opencode", reason=reason, settings=str(path), models=summary.get("models"),
                         backup=summary.get("backup"), warnings=summary.get("warnings") or [])
            console_log.write("[catalog] OpenCode: список моделей обновлён (%s моделей); OpenCode увидит его после "
                              "перезапуска" % summary.get("models"))
        return summary
