"""The relay updates OpenCode's model list by itself after each look at its catalogs (Minara's and Retool's).

OpenCode reads its models from its own settings file (~/.config/opencode/opencode.jsonc), and only
scripts/sync_opencode_catalog.py wrote the relay's models there: a model the relay published, or the effort levels it
took from a provider, stayed out of OpenCode until someone ran that script (Gemini 4 Argon had no effort levels in
OpenCode on 2026-11-14, a day after the relay had them). After every look the relay now runs the script with --write,
as a process of its own (the script reads the relay's config and catalog overlays from disk, apart from the running
relay), when OpenCode's settings file exists and `opencode_sync` is on. The script writes nothing when the list is
already current and keeps a backup of every change (catalog-backups beside the settings file). OpenCode 2.x's service
(`opencode-cli serve --service`) reloads the file by itself; a window already open may need a reload.
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
HARNESS_SCRIPT = pathlib.Path(__file__).resolve().parent / "scripts" / "sync_harness_catalog.py"
TIMEOUT = 120.0
BACKUPS = "catalog-backups"
# Where OpenCode and DeepSeek Harness keep their settings; tests point both at empty folders (conftest),
# so no test run writes the real files.
DEFAULT_FOLDER = pathlib.Path.home() / ".config" / "opencode"
HARNESS_DEFAULT_FOLDER = pathlib.Path.home() / ".dsh" / "profiles"
HARNESS_PROFILES = ("desktop", "web")


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


def harness_settings_paths(cfg):
    """DeepSeek Harness profile patches holding a Proxi (`retool`) provider."""
    configured = cfg.get("harness_settings")
    if isinstance(configured, str) and configured.strip():
        candidate = pathlib.Path(os.path.expanduser(configured.strip()))
        return [candidate] if candidate.is_file() else []
    if isinstance(configured, list):
        paths = [pathlib.Path(os.path.expanduser(item.strip()))
                 for item in configured if isinstance(item, str) and item.strip()]
        return [item for item in paths if item.is_file()]
    found = []
    for name in HARNESS_PROFILES:
        candidate = HARNESS_DEFAULT_FOLDER / name / "cordis.patch.yml"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "llm-pi-ai" in text and "retool" in text:
            found.append(candidate)
    return found


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
        self.last_harness = []

    def _exec_script(self, script, relay_config, target):
        command = [sys.executable, str(script), "--relay-config", str(relay_config), "--settings", str(target),
                   "--write", "--backup-dir", str(target.parent / BACKUPS)]
        try:
            done = self._run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=TIMEOUT, cwd=str(SCRIPT.parent.parent),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console window pops up
        except (OSError, subprocess.SubprocessError) as error:
            summary = {"error": type(error).__name__}
        else:
            summary = _summary(done.stdout)
            if summary is None or done.returncode:
                detail = (done.stderr or "").strip().splitlines() or (done.stdout or "").strip().splitlines()
                summary = {"error": (detail[-1] if detail else "exit status %s" % done.returncode)[:300]}
        # Never a credential in what is kept or said: an error can quote a path or a line of the files read.
        if summary.get("error"):
            summary["error"] = journal.scrub(str(summary["error"]))
        return summary

    def __call__(self, reason):
        relay_config = self.cfg.get("_config_path")
        if not relay_config:
            return None
        path = settings_path(self.cfg) if self.cfg.get("opencode_sync", True) else None
        opencode_ready = bool(self.cfg.get("opencode_sync", True)) and path is not None and path.is_file() and SCRIPT.is_file()
        harness_paths = harness_settings_paths(self.cfg) if (self.cfg.get("harness_sync", True) and HARNESS_SCRIPT.is_file()) else []
        if not opencode_ready and not harness_paths:
            return None
        summary = None
        harness_results = []
        with self._lock:  # Minara's, Retool's and universal providers' looks may end together; one write at a time
            if opencode_ready:
                summary = self._exec_script(SCRIPT, relay_config, path)
            for hpath in harness_paths:
                hsum = self._exec_script(HARNESS_SCRIPT, relay_config, hpath)
                harness_results.append(dict(hsum, reason=reason, settings=str(hpath)))
        if summary is not None:
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
                console_log.write("[catalog] OpenCode: список моделей обновлён (%s моделей); открытое окно OpenCode может "
                                  "понадобиться перезагрузить" % summary.get("models"))
        self.last_harness = harness_results
        for item in harness_results:
            label = pathlib.Path(item["settings"]).parent.name
            if item.get("error"):
                journal.emit("client.harness_sync_failed", "Список моделей DeepSeek Harness (%s) не обновлён после проверки каталога "
                             "%s: %s" % (label, reason, item["error"]), "warn", client="harness", profile=label,
                             reason=reason, settings=item["settings"], error=item["error"])
                console_log.write("[catalog] DeepSeek Harness (%s): список моделей не обновлён: %s" % (label, item["error"]))
            elif item.get("written"):
                journal.emit("client.harness_synced", "Список моделей DeepSeek Harness (%s) обновлён после проверки каталога %s: %s "
                             "моделей; прежний файл сохранён в %s" % (label, reason, item.get("models"), item.get("backup")),
                             "success", client="harness", profile=label, reason=reason, settings=item["settings"],
                             models=item.get("models"), backup=item.get("backup"))
                console_log.write("[catalog] DeepSeek Harness (%s): список моделей обновлён (%s моделей)" % (label, item.get("models")))
        if summary is not None:
            return dict(summary, harness=harness_results) if harness_results else summary
        return {"harness": harness_results, "written": any(item.get("written") for item in harness_results),
                "changed": any(item.get("changed") for item in harness_results)}
