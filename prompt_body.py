"""Deterministic rewrites of a client request body selected by a prompts profile.

See docs/prompt-control-plan.md (sections 2.3 and 4). The rewrite runs on the raw JSON before
validate.parse_* so native-history fingerprints, chunks, cache markers and rit all derive from the
rewritten request (plan I4).

What is edited (everything else, including the head, assistant turns, thinking blocks and
signatures, images, tool calls and tool_choice, is copied untouched):

* history.mid_system: a non-blank system/developer message AFTER the leading ones
  (native_history.leading_instructions is the head boundary) is kept, wrapped, turned
  into plain user text or dropped. Blank entries (effort-only updates) are never touched.
* history.user_blocks: the text of user messages and of tool results (chat role tool,
  Messages tool_result blocks, Responses function_call_output).
* tools.edits: descriptions of the tools, and hide.

Every message is rewritten from its own content and the profile alone, so a conversation that
grows by appending keeps its earlier messages byte for byte and the signed native history
(fingerprints, resume) continues. The only context a decision looks at is its neighbours when a
message would be deleted (see rewrite).
"""
from __future__ import annotations

import collections
import copy
import fnmatch
import functools
import hashlib
import json
import re
import threading

import native_history
import prompt_policy as policy

DEFAULT_REMINDER = "<system-reminder>\n{{text}}\n</system-reminder>"
SUMMARY_INDEXES = 8

_ATOMS = frozenset((str, int, float, bool, type(None)))
_DIALECTS = ("chat", "responses", "anthropic")
_EDITABLE_PARTS = ("text", "input_text", "output_text")
_INSTRUCTION_PARTS = ("text", "input_text", "output_text", "summary_text")
_RESULT_ITEMS = ("function_call_output", "custom_tool_call_output", "tool_result")
_CALL_ITEMS = ("function_call", "custom_tool_call", "reasoning")
# The keys validate reads from a system message besides its text: the effort and the structured
# output format (output_config) and the unsupported turn lifetime (clear_at, a 422 today).
_CONTROL_KEYS = ("output_config", "clear_at")
_FLAG_BITS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
_PLACEHOLDER = re.compile(r"\\\{\{|\{\{\s*text\s*\}\}")
_KEEP, _DROP = object(), object()


def rewrite(data: dict, dialect: str, profile: dict, snapshot) -> tuple[dict, list[dict]]:
    """Return (new body, changes) for a profile's history and tools edits.

    The input body is never mutated: the result is a deep copy. The same input and the same
    profile always produce the same bytes (plan I2), and rewrite(rewrite(x)) == rewrite(x) except where a deletion joins
    its neighbours into a new copy of the text it deletes (replace_text find="ab" with "" turns "aabb" into "ab", and a
    second run into ""): the relay rewrites the history the client sent, once per request, never its own output.
    Each change is a dict such as {"scope": "history", "op": "drop_tag", "edit": 0, "index": 12,
    "target": "tool_result", "removed_chars": 431}.

    profile is Snapshot.profile(name): normalized, texts already literal (plan 4.3). Patterns
    come from snapshot.regex(pattern, flags) and the mid_system "wrap" template from
    snapshot.relay_text("reminder") (None = the built-in "<system-reminder>\\n{{text}}\\n</system-reminder>").
    A snapshot without those methods (or None) falls back to a local compile with a cache.

    The rules it reads: history.mid_system (keep, wrap, plain, drop), history.user_blocks[] (drop_tag
    with tag and optional contains / pattern / flags, remove_regex with pattern / flags, replace_text
    with find / with / all, all defaulting to true) and tools.edits[] (tool: a glob or a list of globs,
    matched case-insensitively against the name, and either hide: true or op append, prepend, set with
    text, replace_text with find / with / all, replace_regex with pattern / flags / with). A rule that
    cannot run (unknown op, missing field, a pattern that does not compile) is recorded as skipped.

    drop_tag removes the outermost <tag ...>...</tag> blocks whose opening tag starts a line, with the
    blank lines they leave: a mention inside a line (code, prose) is not a block. contains / pattern
    keep only the blocks that hold that text / match that regex. Blocks nest; an unclosed opening tag
    is ignored. replace_text whose replacement holds the searched text leaves text that already reads
    like the replacement alone, so running it twice changes nothing. append and prepend add nothing
    that the description already contains.

    Order: history.mid_system goes over every message first, then history.user_blocks over the result
    (a system message that mid_system turned into user text is edited like any user message, and a
    deletion sees the conversation as it will be, which is why a second run changes nothing), then
    tools.edits in order. The history entries of changes are listed by the position of the message in
    the body the client sent.

    Deleting a message (a user message every edit emptied, a dropped system message) is refused,
    and recorded as skipped, when it would leave two assistant messages next to each other, start
    the conversation with an assistant or tool message, end it with an assistant message, empty
    it, or pull a later system message into the head. A tool result is never deleted (its text
    becomes empty) so tool_use / tool_result pairs stay whole. hide is refused for a tool that
    tool_choice names, that the history already called, or when tool_choice demands a call and
    no tool would be left.

    changes is the list the journal and the admin preview show; it carries no text (plan I7):

    * scope: "history" or "tools".
    * op: the rule's op (history: drop_tag, remove_regex, replace_text; tools: append, prepend,
      replace_text, replace_regex, set, hide) or "mid_system" (then mode: drop, wrap or plain).
    * edit: the zero-based position of the rule in user_blocks / tools.edits; absent for mid_system.
    * index: history, the position of the message in the original messages / input (0 for a
      string input); tools, the position of the tool in the original tools.
    * target: history only, "user", "tool_result" or "system".
    * tool: tools only, the tool name (namespace.name for a tool inside a Responses namespace).
    * removed_chars / added_chars: characters taken out of and put into the text by this change.
    * deleted: True when the whole message was removed.
    * kept: a dropped or converted system message that carried output_config / clear_at leaves a
      blank entry with exactly those fields, so effort and structured output do not change; kept
      names them.
    * skipped: True with a short reason when the edit was refused on this request: why a deletion would break the
      conversation, or "timeout" (a pattern did not finish in policy.REGEX_TIMEOUT, or what all the regular expressions of
      the call may use, policy.REGEX_WORK characters to scan and policy.REGEX_BUDGET seconds, was spent) or "too_large" (the
      edit would add more than policy.MAX_EDIT_GROWTH characters to
      one text, or the edits of the section more than policy.MAX_BODY_GROWTH in all: history and tools count apart, in the
      order of the messages and tools, so a conversation that grows keeps the decisions it had). The text is then left as
      it was. No change entry at all means the edit did not match anything (the preview shows it as unmatched).

    summarize_changes() condenses the list when the journal should not carry one entry per message.
    """
    out = _clone(data)
    changes: list[dict] = []
    if not isinstance(out, dict) or dialect not in _DIALECTS or not isinstance(profile, dict):
        return out, changes
    context = _Context(snapshot, dialect, changes)
    history, tools = profile.get("history"), profile.get("tools")
    _rewrite_history(out, context, history if isinstance(history, dict) else {})
    _rewrite_tools(out, context, tools if isinstance(tools, dict) else {})
    return out, changes


def summarize_changes(changes: list[dict], indexes: int = SUMMARY_INDEXES) -> list[dict]:
    """One entry per (scope, op, mode, edit, tool, skipped, reason) with counts, for compact journal events.

    {"scope", "op", ["mode"], ["edit"], ["tool"], "count", "removed_chars", "added_chars",
    "indexes": first positions, ["skipped": True, "reason"]}; the order of first appearance is kept.
    """
    groups: dict[tuple, dict] = {}
    for change in changes or []:
        skipped = bool(change.get("skipped"))
        key = (change.get("scope"), change.get("op"), change.get("mode"), change.get("edit"), change.get("tool"),
               skipped, change.get("reason") if skipped else None)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"scope": change.get("scope"), "op": change.get("op")}
            for name in ("mode", "edit", "tool"):
                if change.get(name) is not None:
                    group[name] = change[name]
            group.update(count=0, removed_chars=0, added_chars=0, indexes=[])
            if skipped:
                group.update(skipped=True, reason=change.get("reason"))
        group["count"] += 1
        group["removed_chars"] += int(change.get("removed_chars") or 0)
        group["added_chars"] += int(change.get("added_chars") or 0)
        if change.get("index") is not None and len(group["indexes"]) < indexes:
            group["indexes"].append(change["index"])
    return list(groups.values())


def unmatched_edits(profile: dict, changes: list[dict]) -> list[dict]:
    """The rules of a profile's history and tools that left no entry in changes: they found nothing to do.

    [{"scope": "history" | "tools", "op", "edit": position in user_blocks / tools.edits}], and the
    mid_system mode as {"scope": "history", "op": "mid_system", "mode"} when it is not "keep" and no system
    message was touched. A rule that was refused or could not run has an entry (skipped), so it is not listed.
    """
    seen = {(c.get("scope"), c.get("edit")) for c in changes or [] if c.get("edit") is not None}
    profile = profile if isinstance(profile, dict) else {}
    history = profile["history"] if isinstance(profile.get("history"), dict) else {}
    tools = profile["tools"] if isinstance(profile.get("tools"), dict) else {}
    found = []
    mode = history.get("mid_system")
    if mode in ("drop", "wrap", "plain") and not any(c.get("op") == "mid_system" for c in changes or []):
        found.append({"scope": "history", "op": "mid_system", "mode": mode})
    for scope, rules in (("history", history.get("user_blocks")), ("tools", tools.get("edits"))):
        for position, spec in enumerate(rules if isinstance(rules, list) else []):
            if isinstance(spec, dict) and (scope, position) not in seen:
                hiding = spec.get("hide") is True or spec.get("op") == "hide"
                found.append({"scope": scope, "op": "hide" if hiding else str(spec.get("op")), "edit": position})
    return found


def _clone(value):
    """A deep copy of JSON-shaped data that shares the immutable leaves (several times faster than copy.deepcopy)."""
    kind = type(value)
    if kind is dict:
        return {key: (item if type(item) in _ATOMS else _clone(item)) for key, item in value.items()}
    if kind is list:
        return [item if type(item) in _ATOMS else _clone(item) for item in value]
    if kind in _ATOMS:
        return value
    return copy.deepcopy(value)


@functools.lru_cache(maxsize=512)
def _compile_local(pattern, flags):
    return policy.compile_pattern(pattern, flags)


@functools.lru_cache(maxsize=256)
def _tag_regex(tag):
    return re.compile(r"<(/)?" + re.escape(tag) + r"(?:\s[^<>]*?)?(?<!/)>")


@functools.lru_cache(maxsize=256)
def _glob(pattern):
    return re.compile(fnmatch.translate(pattern.lower()))


class _Context:
    def __init__(self, snapshot, dialect, changes):
        self.snapshot, self.dialect, self.changes = snapshot, dialect, changes
        self._template = _KEEP
        # What all the regular expressions of this rewrite may use together: characters to scan, and time as a last stop (see
        # policy.Deadline).
        self.deadline = policy.Deadline()
        # How many characters the edits of the history, and apart from them those of the tools, may still add in all.
        self.room = {"history": policy.MAX_BODY_GROWTH, "tools": policy.MAX_BODY_GROWTH}

    def allowance(self, scope):
        """How many characters one edit may add to a text now: its own limit, and what is left of the section's."""
        return max(0, min(policy.MAX_EDIT_GROWTH, self.room[scope]))

    def grew(self, scope, characters):
        """An edit of `scope` made a text `characters` longer (or shorter, then nothing is used up)."""
        if characters > 0:
            self.room[scope] -= characters

    @property
    def template(self):
        """relay_texts.reminder: the user's wrapper, None for the built-in one."""
        if self._template is _KEEP:
            self._template = None
            getter = getattr(self.snapshot, "relay_text", None)
            if getter is not None:
                try:
                    value = getter("reminder")
                except (NotImplementedError, KeyError):
                    value = None
                self._template = value if isinstance(value, str) and value else None
        return self._template

    @staticmethod
    def flags(flags):
        return "".join(flags) if isinstance(flags, (list, tuple)) else flags if isinstance(flags, str) else ""

    def regex(self, pattern, flags):
        """Compiled once with the revision (snapshot.regex); a local cache when the snapshot cannot."""
        flags = self.flags(flags)
        getter = getattr(self.snapshot, "regex", None)
        if getter is not None:
            try:
                return getter(pattern, flags)
            except NotImplementedError:
                pass
        return _compile_local(pattern, flags)

    def skipped(self, scope, op, reason, **fields):
        self.changes.append({"scope": scope, "op": op, **fields, "skipped": True, "reason": reason})


# ---------------------------------------------------------------------------------------------
# text edits shared by history.user_blocks and tools.edits: fn(text) -> (new, removed, added) | None
# A fn may raise policy.EditRefused (a pattern out of time): the rule is then skipped for that text.
# ---------------------------------------------------------------------------------------------

def _remove_regex_fn(rx, context):
    def run(text):
        new, count = policy.regex_remove(rx, text, context.deadline)
        if not count or new == text:
            return None
        return new, len(text) - len(new), 0

    def apply(text):
        return _remembered(context, ("remove", rx.pattern, rx.flags), text, run)
    return apply


def _replace_regex_fn(rx, template, context, scope):
    def apply(text):
        room = context.allowance(scope)

        def run(text):
            new, _count, removed, added = policy.regex_replace(rx, template, text, context.deadline, room)
            if new == text:
                return None
            return new, removed, added
        return _remembered(context, ("replace", rx.pattern, rx.flags, template, room), text, run)
    return apply


# What a regular expression edit made of a text, by (revision, edit, sha256 of the text). The history comes back whole with
# every request, and every turn ran each pattern over every message again under the time limit of one operation: a pattern
# that is slow on a text (`[ \t]+$` on a table padded with blanks) finished on a quiet machine and ran out of time on a busy
# one, so the body sent upstream changed from turn to turn and its cache was written again (audit prompt-a-1; the owner's
# decision of 2026-11-08). A text keeps the result it got the first time, a refusal for its own time included, for as long
# as the revision and the edit are the same. Nothing is learned: it is the same result, without running the pattern again.
_EDIT_MEMO = collections.OrderedDict()
_EDIT_MEMO_LOCK = threading.Lock()
_EDIT_MEMO_ENTRIES = 8192
_EDIT_MEMO_CHARS = 32 * 1024 * 1024
_EDIT_MEMO_TEXT = 4 * 1024 * 1024  # a longer text is edited every time, as before
_EDIT_MEMO_HELD = [0]


def _remembered(context, edit, text, run):
    """run(text), or what it gave for this text before. The work is charged either way (Deadline.charge), so a request whose
    budget runs out is refused the same edits whatever is remembered; a budget refusal is never remembered."""
    if len(text) > _EDIT_MEMO_TEXT:
        return run(text)
    key = (getattr(context.snapshot, "_token", id(context.snapshot)), edit,
           hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest())
    with _EDIT_MEMO_LOCK:
        entry = _EDIT_MEMO.get(key)
        if entry is not None:
            _EDIT_MEMO.move_to_end(key)
    if entry is not None:
        context.deadline.charge(len(text))
        kind, value = entry[0]
        if kind == "refused":
            raise value[0](value[1])
        return value
    try:
        result = run(text)
    except (policy.OperationTimeout, policy.EditTooLarge) as refused:
        _remember(key, ("refused", (type(refused), str(refused))), 0)
        raise
    _remember(key, ("ok", result), len(result[0]) if result else 0)
    return result


def _remember(key, found, held):
    with _EDIT_MEMO_LOCK:
        if key in _EDIT_MEMO:
            return
        _EDIT_MEMO[key] = (found, held)
        _EDIT_MEMO_HELD[0] += held
        while _EDIT_MEMO and (len(_EDIT_MEMO) > _EDIT_MEMO_ENTRIES or _EDIT_MEMO_HELD[0] > _EDIT_MEMO_CHARS):
            _dropped, (_found, size) = _EDIT_MEMO.popitem(last=False)
            _EDIT_MEMO_HELD[0] -= size


def _replace_text_fn(find, replacement, replace_all, context, scope):
    if find in replacement and replacement != find:
        # A plain replace would grow the text on every run; text that already reads `replacement` is left alone. The
        # first place that reads `find` or already reads `replacement` is the one that counts when only one is replaced.
        # (Found with str.find hops: a pattern made of two long literals is slow on a text full of their prefixes, and
        # `re` would hold the GIL for all of it.)
        def apply(text):
            if find not in text:
                return None
            room = context.allowance(scope)
            pieces, position, removed, added = [], 0, 0, 0
            copy = text.find(replacement)                       # the next copy of the replacement, or -1
            while True:
                at = text.find(find, position)
                if at < 0:
                    break
                if copy != -1 and copy < position:
                    copy = text.find(replacement, position)
                if copy != -1 and copy <= at:                  # a copy stands first: it stays as it is
                    if not replace_all:
                        break
                    pieces.append(text[position:copy + len(replacement)])
                    position = copy + len(replacement)
                    continue
                pieces.append(text[position:at])
                pieces.append(replacement)
                position = at + len(find)
                removed += len(find)
                added += len(replacement)
                if added - removed > room:
                    raise policy.EditTooLarge("the replacement would make the text more than %d characters longer" % room)
                if not replace_all:
                    break
            if not removed:
                return None
            pieces.append(text[position:])
            return "".join(pieces), removed, added
        return apply

    def apply(text):
        if find not in text:
            return None
        count = text.count(find) if replace_all else 1
        if count * (len(replacement) - len(find)) > context.allowance(scope):
            raise policy.EditTooLarge("the replacement would make the text more than %d characters longer"
                                      % context.allowance(scope))
        new = text.replace(find, replacement, -1 if replace_all else 1)
        return new, count * len(find), count * len(replacement)
    return apply


def _tag_pairs(text, rx):
    """Every matched <tag ...>...</tag> pair as (start, end, line_start | None), sorted by start.

    line_start is where the opening tag's line begins when only blanks stand in front of the tag on that
    line, None for a mention in the middle of a line. An unclosed opening tag pairs with nothing and
    nested tags pair like brackets. The scan is linear: the line start is tracked from one tag to the next.
    """
    pairs, stack = [], []
    line, seen = 0, 0
    for match in rx.finditer(text):
        begin = match.start()
        newline = text.rfind("\n", seen, begin)
        if newline >= 0:
            line = newline + 1
        seen = begin
        if match.group(1):
            if stack:
                start, anchor = stack.pop()
                pairs.append((start, match.end(), anchor))
            continue
        indent = begin
        while indent > line and text[indent - 1] in " \t":
            indent -= 1
        stack.append((begin, line if indent == line else None))
    pairs.sort()
    return pairs


def _select_blocks(text, pairs, accept):
    """Spans [start, end) of the outermost blocks to cut: they start a line (or follow a cut block on its
    line, since that is where they will stand once it is gone) and accept(block text) holds.

    Whether only blanks lie between the last cut block and a block in the middle of a line is read once per cut block
    (blanks_end), not once per block: a long run of blanks behind a cut would otherwise be read again for every block after it.
    """
    spans, covered, blanks_end = [], -1, None
    for start, end, anchor in pairs:
        if start < covered:
            continue
        if anchor is not None:
            begin = anchor
        else:
            if covered < 0:
                continue
            if blanks_end is None:
                blanks_end = covered
                while blanks_end < len(text) and text[blanks_end] in " \t":
                    blanks_end += 1
            if blanks_end != start:
                continue
            begin = covered
        if accept(text[start:end]):
            spans.append((begin, end))
            covered, blanks_end = end, None
    return spans


def _line_end(text, position):
    """The offset after the line terminator that follows position (past trailing blanks), or position."""
    scan = position
    size = len(text)
    while scan < size and text[scan] in " \t":
        scan += 1
    if text.startswith("\r\n", scan):
        return scan + 2
    if scan >= size or text[scan] == "\n":
        return min(size, scan + 1)
    return position


def _cut(text, spans):
    """text without the spans and without the blank lines the removal would leave behind."""
    parts, tail, blank, position = [], "", True, 0
    for start, end in spans:
        piece = text[position:start]
        parts.append(piece)
        tail, blank = (tail + piece)[-4:], blank and not piece.strip()
        position = _line_end(text, end)
        if blank:
            # The block opened the text: no blank lines stay in front of what follows it.
            parts, tail = [], ""
            while True:
                after = _line_end(text, position)
                if after == position or text[position:after].strip():
                    break
                position = after
        elif tail.endswith(("\n\n", "\r\n\r\n")):
            if text.startswith("\r\n", position):
                position += 2
            elif text.startswith("\n", position):
                position += 1
    rest = text[position:]
    if not rest.strip():
        return "".join(parts).rstrip("\r\n")
    return "".join(parts) + rest


def _drop_tag_fn(tag, contains, pattern, context):
    rx = _tag_regex(tag)
    opener = "<" + tag

    def accept(block):
        return ((not contains or contains in block)
                and (pattern is None or policy.regex_search(pattern, block, context.deadline) is not None))

    def apply(text):
        if opener not in text or (contains and contains not in text):
            return None
        current = text
        for _ in range(3):
            spans = _select_blocks(current, _tag_pairs(current, rx), accept)
            if not spans:
                break
            current = _cut(current, spans)
            if opener not in current:
                break
        return (current, len(text) - len(current), 0) if current != text else None
    return apply


def _text_op(context, spec, ops, scope):
    """(fn, None) for one rule of `scope` ("history" or "tools"), or (None, reason) when the rule cannot be applied."""
    op = spec.get("op")
    if op not in ops:
        return None, "unsupported op"
    try:
        if op == "drop_tag":
            tag = spec.get("tag")
            tag = tag.strip().strip("<>/").strip() if isinstance(tag, str) else None
            if not tag:
                return None, "missing tag"
            pattern, contains = spec.get("pattern"), spec.get("contains")
            if pattern not in (None, "") and not isinstance(pattern, str):
                return None, "invalid pattern"
            if contains not in (None, "") and not isinstance(contains, str):
                # A filter this module cannot read must not widen the rule to every block.
                return None, "invalid contains"
            compiled = context.regex(pattern, spec.get("flags")) if pattern else None
            return _drop_tag_fn(tag, contains or None, compiled, context), None
        if op in ("remove_regex", "replace_regex"):
            pattern = spec.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                return None, "missing pattern"
            rx = context.regex(pattern, spec.get("flags"))
            if op == "remove_regex":
                return _remove_regex_fn(rx, context), None
            template = spec.get("with")
            template = template if isinstance(template, str) else ""
            _check_template(pattern, context.flags(spec.get("flags")), template)
            return _replace_regex_fn(rx, template, context, scope), None
        find = spec.get("find")
        if not isinstance(find, str) or not find:
            return None, "missing find"
        replacement = spec.get("with")
        return _replace_text_fn(find, replacement if isinstance(replacement, str) else "",
                                spec.get("all", True) is not False, context, scope), None
    except (*policy.REGEX_ERRORS, KeyError, ValueError, TypeError, IndexError):
        return None, "invalid pattern"


def _check_template(pattern, flags, template):
    """A replacement that cannot be expanded fails here, not on some message: the stdlib parser judges the template, since
    the regex module looks at one only when something matches. A template it takes, the regex module expands the same way."""
    bits = 0
    for flag in flags:
        bits |= _FLAG_BITS[flag]
    re.compile(pattern, bits).sub(template, "")


# ---------------------------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------------------------

def _kind(message, dialect):
    """S system/developer, U user, T tool result, A assistant side, X anything else."""
    role = message.get("role")
    if role is None and dialect == "chat" and message.get("tool_calls"):
        role = "assistant"
    if isinstance(role, str):
        if role in native_history.INSTRUCTION_ROLES:
            return "S"
        if role == "user":
            if dialect == "anthropic" and _has_tool_result(message.get("content")):
                return "T"
            return "U"
        if role == "assistant":
            return "A"
        if role in ("tool", "function"):
            return "T"
        return "X"
    if dialect == "responses":
        item = message.get("type")
        if item in _RESULT_ITEMS:
            return "T"
        if item in _CALL_ITEMS:
            return "A"
    return "X"


def _has_tool_result(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _instruction_text(content):
    """The text validate gives a system message ("" when blank), None for a shape this module leaves alone."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get("type")
            if kind in _INSTRUCTION_PARTS and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif kind not in native_history.INLINE_TOOL_BLOCKS:
                return None
        else:
            return None
    return "\n".join(part for part in parts if part)


def _visible_kind(message, dialect):
    """What a message is in the conversation: None for the blank system entries that leave no trace."""
    if not isinstance(message, dict):
        return "U"
    kind = _kind(message, dialect)
    if kind == "S":
        text = _instruction_text(message.get("content"))
        return None if text is not None and not text.strip() else "U"
    return "U" if kind == "X" else kind


def _map_content(value, fn, tool_results):
    """Apply fn to the editable text of a content value: None when nothing changes, else
    (new value, removed, added, empty). A text part that ends up blank leaves the list; a tool_result
    block (Messages) is edited inside and never leaves."""
    if isinstance(value, str):
        res = fn(value)
        if res is None:
            return None
        return res[0], res[1], res[2], not res[0].strip()
    if not isinstance(value, list):
        return None
    out, removed, added = None, 0, 0
    for position, part in enumerate(value):
        replacement = _KEEP
        if isinstance(part, str):
            res = fn(part)
            if res is not None:
                replacement = res[0] if res[0].strip() else _DROP
                removed, added = removed + res[1], added + res[2]
        elif isinstance(part, dict):
            kind, text = part.get("type"), part.get("text")
            if kind in _EDITABLE_PARTS and isinstance(text, str):
                res = fn(text)
                if res is not None:
                    replacement = dict(part, text=res[0]) if res[0].strip() else _DROP
                    removed, added = removed + res[1], added + res[2]
            elif kind == "tool_result" and tool_results:
                inner = _map_content(part.get("content"), fn, False)
                if inner is not None:
                    replacement = dict(part, content=inner[0] if not inner[3] else "")
                    removed, added = removed + inner[1], added + inner[2]
        if replacement is _KEEP:
            if out is not None:
                out.append(part)
            continue
        if out is None:
            out = list(value[:position])
        if replacement is not _DROP:
            out.append(replacement)
    if out is None:
        return None
    return out, removed, added, not out


def _render_reminder(template, text):
    live = [m for m in _PLACEHOLDER.finditer(template or "") if m.group(0)[0] != "\\"]
    if not live or template == DEFAULT_REMINDER:
        # No user template, or one without {{text}}, which would swallow the system text: the built-in wrapper.
        return native_history.reminder(text)
    if len(live) == 1:
        prefix = _PLACEHOLDER.sub("{{", template[:live[0].start()]).strip()
        suffix = _PLACEHOLDER.sub("{{", template[live[0].end():]).strip()
        body = text.strip()
        if (prefix or suffix) and len(body) >= len(prefix) + len(suffix) \
                and body.startswith(prefix) and body.endswith(suffix):
            return text
    return _PLACEHOLDER.sub(lambda m: "{{" if m.group(0)[0] == "\\" else text, template)


def _as_user_message(message, text, dialect):
    converted = {key: value for key, value in message.items() if key not in _CONTROL_KEYS}
    converted["role"] = "user"
    if isinstance(message.get("content"), list):
        converted["content"] = [{"type": "input_text" if dialect == "responses" else "text", "text": text}]
    else:
        converted["content"] = text
    return converted


def _history_list(out, dialect):
    """(key, messages, scalar): the list the conversation lives in; a Responses string input is one user message."""
    if dialect == "responses":
        value = out.get("input")
        if isinstance(value, str):
            return "input", [{"role": "user", "content": value}], True
        return ("input", value, False) if isinstance(value, list) else (None, None, False)
    value = out.get("messages")
    return ("messages", value, False) if isinstance(value, list) else (None, None, False)


class _Walk:
    """One pass over the conversation: the messages kept so far and what a deletion would break.

    origins[i] is the position message i had in the body the client sent; changes report that one."""

    def __init__(self, context, messages, origins, mode, edits):
        self.context, self.messages, self.origins, self.mode, self.edits = context, messages, origins, mode, edits
        self.dialect, self.changes = context.dialect, context.changes
        self.lead = native_history.leading_instructions(messages)
        self.result, self.seen, self.origin_out, self.rebuilt = [], [], [], False

    def run(self):
        for index, message in enumerate(self.messages):
            if index < self.lead or not isinstance(message, dict):
                self.keep(message, None if index < self.lead else "U", index)
                continue
            kind = _kind(message, self.dialect)
            if kind == "S":
                message = self.system(index, message)
                if message is None:
                    continue
                kind = "U"
            if self.edits and kind in ("U", "T"):
                message = self.edit(index, message, kind)
                if message is None:
                    continue
            self.keep(message, "U" if kind == "X" else kind, index)
        return self.result

    def keep(self, message, seen, index):
        self.result.append(message)
        self.seen.append(seen)
        self.origin_out.append(self.origins[index])

    def blocker(self, index):
        """Why message `index` cannot leave the conversation, or None when it can."""
        before = next((k for k in reversed(self.seen) if k is not None), None)
        after, scan, size = None, index + 1, len(self.messages)
        while scan < size:
            after = _visible_kind(self.messages[scan], self.dialect)
            if after is not None:
                break
            scan += 1
        if before is None and after is None:
            return "would leave the conversation empty"
        if before is None and after in ("A", "T"):
            return "would start the conversation with an assistant or tool message"
        if after is None and before == "A":
            return "would end the conversation with an assistant message"
        if before == "A" and after == "A":
            return "would put two assistant messages next to each other"
        following = self.messages[index + 1] if index + 1 < size else None
        # Judged by what is kept, not by the position: when two leading user messages both go, the second is no longer at
        # self.lead, and the later system message used to be pulled into the head anyway.
        if before is None and isinstance(following, dict) and _kind(following, self.dialect) == "S":
            return "would move a later system message into the head"
        return None

    def system(self, index, message):
        """A system message after the head: None when it stays, goes or is a blank entry, else the user message
        that replaces it."""
        origin, mode = self.origins[index], self.mode
        text = _instruction_text(message.get("content"))
        if text is not None and not text.strip():
            self.keep(message, None, index)
            return None
        if mode == "keep" or text is None:
            if text is None and mode != "keep":
                self.context.skipped("history", "mid_system", "unsupported system content", mode=mode,
                                     index=origin, target="system")
            self.keep(message, "U", index)
            return None
        change = {"scope": "history", "op": "mid_system", "mode": mode, "index": origin, "target": "system"}
        kept = [name for name in _CONTROL_KEYS if name in message]
        if mode == "drop":
            reason = self.blocker(index)
            if reason is not None:
                self.context.skipped("history", "mid_system", reason, mode=mode, index=origin, target="system")
                self.keep(message, "U", index)
                return None
            change.update(removed_chars=len(text), added_chars=0, deleted=True)
            replacement = None
        else:
            rendered = _render_reminder(self.context.template, text) if mode == "wrap" else text
            if len(rendered) - len(text) > self.context.room["history"]:
                self.context.skipped("history", "mid_system", "too_large", mode=mode, index=origin, target="system")
                self.keep(message, "U", index)
                return None
            self.context.grew("history", len(rendered) - len(text))
            change.update(removed_chars=0, added_chars=len(rendered) - len(text))
            replacement = _as_user_message(message, rendered, self.dialect)
        if kept:
            # The effort and the output format of the entry are not text: a blank entry carries them on.
            self.keep(dict(message, content=[]), None, index)
            change["kept"] = kept
        self.changes.append(change)
        self.rebuilt = True
        return replacement

    def edit(self, index, message, kind):
        """Apply the user_blocks rules to a user message or a tool result; None when the message is deleted."""
        origin = self.origins[index]
        target = "user" if kind == "U" else "tool_result"
        field = "output" if self.dialect == "responses" and kind == "T" and message.get("output") is not None \
            else "content"
        for position, op, fn in self.edits:
            try:
                res = _map_content(message.get(field), fn, self.dialect == "anthropic")
            except policy.EditRefused as refused:
                self.context.skipped("history", op, refused.reason, edit=position, index=origin, target=target)
                continue
            if res is None:
                continue
            value, removed, added, empty = res
            change = {"scope": "history", "op": op, "edit": position, "index": origin, "target": target,
                      "removed_chars": removed, "added_chars": added}
            if empty and kind == "U":
                reason = self.blocker(index)
                if reason is not None:
                    self.context.skipped("history", op, reason, edit=position, index=origin, target=target)
                    continue
                self.changes.append(dict(change, deleted=True))
                self.rebuilt = True
                return None
            message[field] = "" if empty else value
            self.context.grew("history", added - removed)
            self.changes.append(change)
        return message


def _rewrite_history(out, context, history):
    mode = history.get("mid_system") if history.get("mid_system") in ("drop", "wrap", "plain") else "keep"
    edits = []
    for position, spec in enumerate(history.get("user_blocks") or []):
        if not isinstance(spec, dict):
            continue
        fn, reason = _text_op(context, spec, ("drop_tag", "remove_regex", "replace_text"), "history")
        if fn is None:
            context.skipped("history", str(spec.get("op")), reason, edit=position)
        else:
            edits.append((position, spec["op"], fn))
    if mode == "keep" and not edits:
        return
    key, messages, scalar = _history_list(out, context.dialect)
    if messages is None:
        return
    first, origins, rebuilt = len(context.changes), list(range(len(messages))), False
    # The system messages go first, so that the text rules and the deletion rules see the conversation as it
    # will be: a system message turned into user text is edited like one, and is not a head to protect.
    for walk_mode, walk_edits in ((mode, []), ("keep", edits)):
        if walk_mode == "keep" and not walk_edits:
            continue
        walk = _Walk(context, messages, origins, walk_mode, walk_edits)
        messages, origins, rebuilt = walk.run(), walk.origin_out, rebuilt or walk.rebuilt
    context.changes[first:] = sorted(context.changes[first:], key=lambda c: (c.get("index") is not None,
                                                                              c.get("index") or 0))
    if scalar:
        if len(context.changes) > first:
            out[key] = messages[0]["content"]
    elif rebuilt:
        out[key] = messages


# ---------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------

class _Tool:
    __slots__ = ("top", "holder", "item", "fields", "names")

    def __init__(self, top, holder, item, name, namespace=None):
        self.top, self.holder, self.item = top, holder, item
        self.fields = _tool_fields(item)
        self.names = (namespace + "." + name, name) if namespace else (name,)

    @property
    def name(self):
        return self.names[0]


def _tool_fields(item):
    """Where a tool's name and description live: "function" of a function tool, "custom" of a Chat custom tool in its nested
    form ({type: custom, custom: {name}}, which validate accepts; the edits used to miss it), else the tool itself."""
    for field in ("function", "custom"):
        nested = item.get(field)
        if isinstance(nested, dict) and (field == "function" or item.get("type") == "custom"):
            return nested
    return item


def _tool_name(item):
    if not isinstance(item, dict):
        return None
    name = _tool_fields(item).get("name")
    return name if isinstance(name, str) and name else None


def _collect_tools(tools):
    entries = []
    for top, item in enumerate(tools):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "namespace" and isinstance(item.get("tools"), list):
            namespace = item.get("name")
            for nested in item["tools"]:
                name = _tool_name(nested)
                if name is not None:
                    entries.append(_Tool(top, item["tools"], nested, name,
                                         namespace if isinstance(namespace, str) and namespace else None))
            continue
        name = _tool_name(item)
        if name is not None:
            entries.append(_Tool(top, tools, item, name))
    return entries


def _choice_names(choice):
    names = set()
    if isinstance(choice, dict):
        function, custom = choice.get("function"), choice.get("custom")
        for holder in (choice, function if isinstance(function, dict) else {}, custom if isinstance(custom, dict) else {}):
            name = holder.get("name")
            if isinstance(name, str) and name:
                names.add(name)
                if isinstance(choice.get("namespace"), str) and choice["namespace"]:
                    names.add(choice["namespace"] + "." + name)
    return names


def _forces_a_call(choice):
    return choice.get("type") == "any" if isinstance(choice, dict) else choice == "required"


def _called_names(out, dialect):
    names = set()
    for message in _history_list(out, dialect)[1] or []:
        if not isinstance(message, dict):
            continue
        if message.get("type") in ("function_call", "custom_tool_call"):
            name = message.get("name")
            if isinstance(name, str):
                names.add(name)
                if message.get("namespace"):
                    names.add(str(message["namespace"]) + "." + name)
            continue
        calls = message.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            if isinstance(call, dict):
                name = _tool_fields(call).get("name")  # a function call, or a Chat custom call {type: custom, custom: {name}}
                if isinstance(name, str):
                    names.add(name)
        legacy = message.get("function_call")
        if isinstance(legacy, dict) and isinstance(legacy.get("name"), str):
            names.add(legacy["name"])
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            # server_tool_use: a server tool (web_search) the conversation already used is a called tool too
            if (isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use")
                    and isinstance(block.get("name"), str)):
                names.add(block["name"])
    return names


def _description_fn(context, spec):
    op = spec.get("op")
    text = spec.get("text") if isinstance(spec.get("text"), str) else ""

    def check(growth):
        if growth > context.allowance("tools"):
            raise policy.EditTooLarge("the text would make the description more than %d characters longer"
                                      % context.allowance("tools"))

    if op == "append":
        def apply(old):
            if not text.strip() or text.strip() in old:
                return None
            new = old + "\n\n" + text if old.strip() else text
            check(len(new) - len(old))
            return new, 0, len(new) - len(old)
        return apply, None
    if op == "prepend":
        def apply(old):
            if not text.strip() or text.strip() in old:
                return None
            new = text + "\n\n" + old if old.strip() else text
            check(len(new) - len(old))
            return new, 0, len(new) - len(old)
        return apply, None
    if op == "set":
        def apply(old):
            if old == text:
                return None
            check(len(text) - len(old))
            return text, len(old), len(text)
        return apply, None
    return _text_op(context, spec, ("replace_text", "replace_regex"), "tools")


class _Hide:
    """What hide must not do to this request: the tools it may never remove."""

    def __init__(self, out, dialect, entries):
        choice = out.get("tool_choice")
        self.named, self.forced = _choice_names(choice), _forces_a_call(choice)
        self.called, self.entries, self.hidden = _called_names(out, dialect), entries, set()

    def refusal(self, entry):
        if self.named.intersection(entry.names):
            return "tool_choice names the tool"
        if self.called.intersection(entry.names):
            return "the conversation already called the tool"
        if self.forced and not any(e is not entry and id(e.item) not in self.hidden for e in self.entries):
            return "tool_choice demands a call and no tool would be left"
        return None


def _rewrite_tools(out, context, tools_config):
    specs = tools_config.get("edits") or []
    tools = out.get("tools")
    if not specs or not isinstance(tools, list) or not tools:
        return
    entries, changes = _collect_tools(tools), context.changes
    hide, emptied = None, set()
    for position, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        globs = spec.get("tool")
        patterns = [_glob(g) for g in ([globs] if isinstance(globs, str) else globs or []) if isinstance(g, str) and g]
        if not patterns:
            continue
        hiding = spec.get("hide") is True or spec.get("op") == "hide"
        op, fn = "hide" if hiding else spec.get("op"), None
        if not hiding:
            fn, reason = _description_fn(context, spec)
            if fn is None:
                context.skipped("tools", str(op), reason, edit=position)
                continue
        elif hide is None:
            hide = _Hide(out, context.dialect, entries)
        for entry in entries:
            if hide is not None and id(entry.item) in hide.hidden:
                continue
            if not any(p.fullmatch(n.lower()) for p in patterns for n in entry.names):
                continue
            if hiding:
                reason = hide.refusal(entry)
                if reason is not None:
                    context.skipped("tools", "hide", reason, edit=position, index=entry.top, tool=entry.name)
                    continue
                hide.hidden.add(id(entry.item))
                emptied.add(id(entry.holder))
                changes.append({"scope": "tools", "op": "hide", "edit": position, "index": entry.top,
                                "tool": entry.name, "added_chars": 0,
                                "removed_chars": len(json.dumps(entry.item, ensure_ascii=False, default=str))})
                continue
            old = entry.fields.get("description")
            try:
                res = fn(old if isinstance(old, str) else "")
            except policy.EditRefused as refused:
                context.skipped("tools", op, refused.reason, edit=position, index=entry.top, tool=entry.name)
                continue
            if res is not None:
                entry.fields["description"] = res[0]
                context.grew("tools", res[2] - res[1])
                changes.append({"scope": "tools", "op": op, "edit": position, "index": entry.top, "tool": entry.name,
                                "removed_chars": res[1], "added_chars": res[2]})
    if hide is None or not hide.hidden:
        return
    for holder in {id(e.holder): e.holder for e in entries if id(e.item) in hide.hidden}.values():
        holder[:] = [item for item in holder if id(item) not in hide.hidden]
    out["tools"] = [item for item in tools if not (
        isinstance(item, dict) and item.get("type") == "namespace" and isinstance(item.get("tools"), list)
        and id(item["tools"]) in emptied and not item["tools"])]
