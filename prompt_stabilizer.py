"""Parts of a client's system prompt that change between its requests though nothing in the conversation did.

The head is the start of the cached prefix: a changed character in it rewrites the whole cache and drops the signed
history (cache.head_changed, native_history.reset, 7-21 credits per rewrite in the OpenCode dialog of 2026-11-06, 47% of
what the dialog cost). Two such parts are known.

Skill locations. OpenCode 1.x lists each skill in <available_skills> with a <location>. A skill that exists in two
scanned folders (four of the user's do: ~/.agents/skills and ~/.claude/skills) is listed from whichever folder the process
met first, and that differs from process to process: six fresh `opencode run` processes in one folder sent five different
system prompts. The name and the description are the skill; the location only says where its file is. SkillLocations
remembers the first location seen for a skill (by working folder, name and description) and writes that one into every
later prompt, so the head stays put. A remembered file that is gone while the new one exists is dropped for the new
one, so the model is never pointed at a file that does not exist.

The date. OpenCode writes "Today's date: ..." into the system prompt of every request, so a conversation that crosses
midnight rewrites its whole cache once (30 credits at 300k tokens). PinnedDate keeps the first date a conversation was
shown for as long as the conversation is active. The model is then told yesterday's date for the rest of that stretch of
work, which is a choice, not a repair: it is off unless stable_prompt_date is set.
"""
import collections
import hashlib
import json
import os
import re
import threading
import time

import journal

_SKILL = re.compile(r"<skill>(.*?)</skill>", re.S)
_NAME = re.compile(r"<name>(.*?)</name>", re.S)
_DESCRIPTION = re.compile(r"<description>(.*?)</description>", re.S)
_LOCATION = re.compile(r"<location>(.*?)</location>", re.S)
_FOLDER = re.compile(r"^[ \t]*Working directory:[ \t]*(.+?)[ \t]*$", re.M)
_DATE = re.compile(r"(Today's date: )([A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{4})")
FILE_NAME = "client-prompts.json"
# A skill in use has its time written to the file at most this late (seconds). Only a new skill wrote the file before, so the
# times in it stayed those of the day the skills were first seen: the first restart a week later dropped every skill in daily
# use, and each conversation going on got a new head (native_history.reset, the whole prefix written to the cache again).
TOUCH_SAVE_EVERY = 3600


def _is_path(text):
    # "<built-in>" (escaped or not) is the location of a skill that has no file
    return bool(re.search(r"[\\/]", text)) and not text.lstrip().startswith(("<", "&lt;"))


class SkillLocations:
    """(working folder, skill name, description) -> the location the model is told, the first one seen."""

    def __init__(self, path=None, ttl=7 * 24 * 3600, max_entries=2048, clock=time.time, exists=os.path.isfile):
        self._path, self._ttl, self._max = path, ttl, max_entries
        self._clock, self._exists = clock, exists
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()  # held by the one thread writing the file (see _save)
        self._memory = collections.OrderedDict()
        self._dirty = False  # the memory holds a location the file does not
        self._touched = False  # the memory holds a later time of use than the file (written every TOUCH_SAVE_EVERY)
        self._saved_at = float("-inf")
        self._warned = False
        self._load()

    def stabilize(self, system):
        """(system text with the remembered locations, names of the skills whose location was replaced)."""
        if "<available_skills>" not in system:
            return system, []
        folder = _FOLDER.search(system)
        folder = folder.group(1) if folder else ""
        replaced = []

        def skill(match):
            body = match.group(1)
            name, description, location = _NAME.search(body), _DESCRIPTION.search(body), _LOCATION.search(body)
            if not (name and location):
                return match.group(0)
            raw = location.group(1)
            seen = raw.strip()
            if not _is_path(seen):
                return match.group(0)
            # surrogatepass: half of a surrogate pair (a client that cut a description inside an emoji sends it as a JSON
            # escape) made encode raise, and the whole prompt of every request of that client went unstabilized
            digest = hashlib.sha256((description.group(1).strip() if description else "").encode(
                "utf-8", "surrogatepass")).hexdigest()[:16]
            chosen = self._choose((folder, name.group(1).strip(), digest), seen)
            if chosen == seen:
                return match.group(0)
            replaced.append(name.group(1).strip())
            fixed = raw[:len(raw) - len(raw.lstrip())] + chosen + raw[len(raw.rstrip()):]
            return "<skill>" + body[:location.start(1)] + fixed + body[location.end(1):] + "</skill>"

        text = _SKILL.sub(skill, system)
        self._save()
        return text, replaced

    def _choose(self, key, seen):
        with self._lock:
            now = self._clock()
            entry = self._memory.get(key)
            if entry is not None and now - entry[1] > self._ttl:
                entry = None
            if entry is None:
                self._remember(key, seen, now)
                return seen
            entry[1] = now
            self._touched = True
            self._memory.move_to_end(key)
            if entry[0] == seen:
                return seen
            if not self._exists(entry[0]) and self._exists(seen):
                self._remember(key, seen, now)
                return seen
            return entry[0]

    def _remember(self, key, location, now):
        self._memory[key] = [location, now]
        self._memory.move_to_end(key)
        while len(self._memory) > self._max:
            self._memory.popitem(last=False)
        self._dirty = True

    def _load(self):
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                document = json.load(handle)
            now = self._clock()
            for folder, name, digest, location, seen in document["skills"]:
                if all(isinstance(item, str) for item in (folder, name, digest, location)) and now - seen < self._ttl:
                    self._memory[(folder, name, digest)] = [location, float(seen)]
        except Exception:  # noqa: BLE001 - any file it cannot use (a hand edit, another layout, AttributeError, RecursionError,
            # OverflowError) is no reason to keep the relay from starting: it is overwritten by the next save
            self._memory.clear()  # nothing to go on: the next prompt seen is the first

    def _save(self):
        """Writes the memory if it changed. One thread writes at a time, and it writes until nothing is left unsaved.

        Requests run in threads of their own. Two of them that each learned a skill wrote one temporary file (named by the
        process) at the same time: on Windows the rename of one failed while the other held the file open, the other
        renamed a mix of both documents into place, and a write that took the older snapshot last put it over the newer.
        A thread that finds the file being written leaves its change to the writer, which looks again before it lets go."""
        if not ((self._dirty or self._touched) and self._path):
            return
        if not self._save_lock.acquire(blocking=False):
            return
        while True:
            with self._lock:
                now = self._clock()
                if not (self._dirty or (self._touched and now - self._saved_at >= TOUCH_SAVE_EVERY)):
                    self._save_lock.release()  # under self._lock: a change made after this look finds the file free
                    return
                self._dirty = self._touched = False
                self._saved_at = now
                document = {"version": 1, "skills": [[*key, value[0], value[1]] for key, value in self._memory.items()]}
            if not self._write(document):
                with self._lock:
                    self._dirty = True  # the next request tries again
                    self._save_lock.release()
                return

    def _write(self, document):
        temporary = "%s.%d.tmp" % (self._path, os.getpid())
        try:
            text = json.dumps(document, ensure_ascii=False)
            try:
                data = text.encode("utf-8")
            except UnicodeEncodeError:  # half of a surrogate pair in a skill: the file keeps it as a JSON escape
                data = json.dumps(document).encode("ascii")
            with open(temporary, "wb") as handle:
                handle.write(data)
            os.replace(temporary, self._path)
            return True
        except Exception as exc:  # noqa: BLE001 - the memory is a convenience: no failure to keep it may fail a request
            try:
                os.remove(temporary)
            except OSError:
                pass
            if not self._warned:
                self._warned = True
                journal.emit("prompt.stabilizer_save_failed", "Расположения навыков клиента не сохранены на диск: %s"
                             % type(exc).__name__, "warn")
            return False


class PinnedDate:
    """conversation -> the date it was first shown, while it stays active (a gap longer than `ttl` ends the pin)."""

    def __init__(self, ttl=1200, max_entries=4096, clock=time.time):
        self._ttl, self._max, self._clock = ttl, max_entries, clock
        self._lock = threading.Lock()
        self._seen = collections.OrderedDict()

    def pin(self, system, conversation):
        """(system text with the pinned date, whether it was changed)."""
        match = _DATE.search(system) if conversation else None
        if match is None:
            return system, False
        with self._lock:
            now = self._clock()
            entry = self._seen.get(conversation)
            if entry is None or now - entry[1] > self._ttl:
                entry = self._seen[conversation] = [match.group(2), now]
            entry[1] = now
            self._seen.move_to_end(conversation)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            date = entry[0]
        if date == match.group(2):
            return system, False
        return system[:match.start(2)] + date + system[match.end(2):], True


class Stabilizer:
    """What proxy runs over the final system prompt of a request."""

    def __init__(self, cfg, path=None):
        self.skills = SkillLocations(path) if cfg.get("stabilize_client_prompts", True) else None
        ttl = max(int(cfg.get("conversation_idle_timeout", 900) or 900), 900) + 300
        self.dates = PinnedDate(ttl) if cfg.get("stable_prompt_date", False) else None

    def apply(self, system, conversation):
        """(the system text, [what was done]) for a request of `conversation`."""
        done = []
        if self.skills is not None:
            system, names = self.skills.stabilize(system)
            if names:
                done.append("skill_locations:" + ",".join(dict.fromkeys(names)))
        if self.dates is not None:
            system, changed = self.dates.pin(system, conversation)
            if changed:
                done.append("date")
        return system, done
