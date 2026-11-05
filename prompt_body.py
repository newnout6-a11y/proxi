"""Deterministic rewrites of a client request body selected by a prompts profile.

Contract-first skeleton; see docs/prompt-control-plan.md (sections 2.3 and 4). The
rewrite runs on the raw JSON before validate.parse_* so native-history fingerprints,
chunks, cache markers and rit all derive from the rewritten request (plan I4).

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

import copy
import fnmatch
import functools
import json
import re

import native_history

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
    profile always produce the same bytes (plan I2), and rewrite(rewrite(x)) == rewrite(x).
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

    Order: history.mid_system and history.user_blocks go over the messages once, in order (a
    system message that mid_system turns into user text is then a user message for user_blocks,
    which is why a second run changes nothing), then tools.edits in order.

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
    * skipped: True with a short reason when the edit was refused on this request. No change
      entry at all means the edit did not match anything (the preview shows it as unmatched).

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
    bits = 0
    for flag in flags:
        bits |= _FLAG_BITS[flag]
    return re.compile(pattern, bits)


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

    def regex(self, pattern, flags):
        """Compiled once with the revision (snapshot.regex); a local cache when the snapshot cannot."""
        flags = "".join(flags) if isinstance(flags, (list, tuple)) else flags if isinstance(flags, str) else ""
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
# ---------------------------------------------------------------------------------------------

def _remove_regex_fn(rx):
    def apply(text):
        new, count = rx.subn("", text)
        if not count or new == text:
            return None
        return new, len(text) - len(new), 0
    return apply


def _replace_regex_fn(rx, template):
    def apply(text):
        removed = added = 0

        def swap(match):
            nonlocal removed, added
            value = match.expand(template)
            removed += len(match.group(0))
            added += len(value)
            return value

        new = rx.sub(swap, text)
        if new == text:
            return None
        return new, removed, added
    return apply


def _replace_text_fn(find, replacement, replace_all):
    if find in replacement and replacement != find:
        # A plain replace would grow the text on every run; text that already reads `replacement` is left alone.
        both = re.compile(re.escape(replacement) + "|" + re.escape(find))

        def apply(text):
            if find not in text:
                return None
            removed = added = 0
            done = False

            def swap(match):
                nonlocal removed, added, done
                if done:
                    return match.group(0)
                done = not replace_all
                if match.group(0) == replacement:
                    return replacement
                removed += len(find)
                added += len(replacement)
                return replacement

            new = both.sub(swap, text)
            return None if new == text else (new, removed, added)
        return apply

    def apply(text):
        if find not in text:
            return None
        count = text.count(find) if replace_all else 1
        new = text.replace(find, replacement, -1 if replace_all else 1)
        return new, count * len(find), count * len(replacement)
    return apply


def _tag_blocks(text, rx):
    """Spans [start, end) of the outermost <tag ...>...</tag> blocks whose opening tag starts a line.

    Blocks nest, an unclosed opening tag is ignored, and a mention in the middle of a line (a code
    listing, a sentence about the tag) is not a block. start is moved back over the indentation.
    """
    spans, stack = [], []
    for match in rx.finditer(text):
        if match.group(1):
            if stack:
                start = stack.pop()
                if start is not None:
                    spans.append((start, match.end()))
            continue
        line = text.rfind("\n", 0, match.start()) + 1
        stack.append(line if not text[line:match.start()].strip(" \t") else None)
    spans.sort()
    outer, last = [], -1
    for start, end in spans:
        if start >= last:
            outer.append((start, end))
            last = end
    return outer


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
    result, position = "", 0
    for start, end in spans:
        result += text[position:start]
        position = _line_end(text, end)
        if not result.strip():
            result = ""
            while True:
                after = _line_end(text, position)
                if after == position or text[position:after].strip():
                    break
                position = after
        elif result.endswith(("\n\n", "\r\n\r\n")):
            if text.startswith("\r\n", position):
                position += 2
            elif text.startswith("\n", position):
                position += 1
    tail = text[position:]
    if not tail.strip():
        return result.rstrip("\r\n")
    return result + tail


def _drop_tag_fn(tag, contains, pattern):
    rx = _tag_regex(tag)
    opener = "<" + tag

    def apply(text):
        if opener not in text or (contains and contains not in text):
            return None
        spans = _tag_blocks(text, rx)
        if contains:
            spans = [span for span in spans if contains in text[span[0]:span[1]]]
        if pattern is not None:
            spans = [span for span in spans if pattern.search(text[span[0]:span[1]])]
        if not spans:
            return None
        new = _cut(text, spans)
        return (new, len(text) - len(new), 0) if new != text else None
    return apply


def _text_op(context, spec, ops):
    """(fn, None) for one rule, or (None, reason) when the rule cannot be applied."""
    op = spec.get("op")
    if op not in ops:
        return None, "unsupported op"
    try:
        if op == "drop_tag":
            tag = spec.get("tag")
            if not isinstance(tag, str) or not tag:
                return None, "missing tag"
            pattern = spec.get("pattern")
            compiled = context.regex(pattern, spec.get("flags")) if isinstance(pattern, str) and pattern else None
            contains = spec.get("contains")
            return _drop_tag_fn(tag, contains if isinstance(contains, str) and contains else None, compiled), None
        if op in ("remove_regex", "replace_regex"):
            pattern = spec.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                return None, "missing pattern"
            rx = context.regex(pattern, spec.get("flags"))
            if op == "remove_regex":
                return _remove_regex_fn(rx), None
            template = spec.get("with")
            return _replace_regex_fn(rx, template if isinstance(template, str) else ""), None
        find = spec.get("find")
        if not isinstance(find, str) or not find:
            return None, "missing find"
        replacement = spec.get("with")
        return _replace_text_fn(find, replacement if isinstance(replacement, str) else "",
                                spec.get("all", True) is not False), None
    except (re.error, KeyError, ValueError, TypeError):
        return None, "invalid pattern"


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
    """One pass over the conversation: the messages kept so far and what a deletion would break."""

    def __init__(self, context, messages, mode, edits):
        self.context, self.messages, self.mode, self.edits = context, messages, mode, edits
        self.dialect, self.changes = context.dialect, context.changes
        self.lead = native_history.leading_instructions(messages)
        self.result, self.seen, self.rebuilt = [], [], False

    def run(self):
        for index, message in enumerate(self.messages):
            if index < self.lead or not isinstance(message, dict):
                self.keep(message, None if index < self.lead else "U")
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
            self.keep(message, "U" if kind == "X" else kind)
        return self.result

    def keep(self, message, seen):
        self.result.append(message)
        self.seen.append(seen)

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
        if index == self.lead and isinstance(following, dict) and _kind(following, self.dialect) == "S":
            return "would move a later system message into the head"
        return None

    def system(self, index, message):
        """A system message after the head: None when it stays, goes or is a blank entry, else the user message
        that replaces it."""
        text = _instruction_text(message.get("content"))
        if text is not None and not text.strip():
            self.keep(message, None)
            return None
        mode = self.mode
        if mode == "keep" or text is None:
            if text is None and mode != "keep":
                self.context.skipped("history", "mid_system", "unsupported system content", mode=mode,
                                     index=index, target="system")
            self.keep(message, "U")
            return None
        change = {"scope": "history", "op": "mid_system", "mode": mode, "index": index, "target": "system"}
        kept = [name for name in _CONTROL_KEYS if name in message]
        if mode == "drop":
            reason = self.blocker(index)
            if reason is not None:
                self.context.skipped("history", "mid_system", reason, mode=mode, index=index, target="system")
                self.keep(message, "U")
                return None
            change.update(removed_chars=len(text), added_chars=0, deleted=True)
            replacement = None
        else:
            rendered = _render_reminder(self.context.template, text) if mode == "wrap" else text
            change.update(removed_chars=0, added_chars=len(rendered) - len(text))
            replacement = _as_user_message(message, rendered, self.dialect)
        if kept:
            # The effort and the output format of the entry are not text: a blank entry carries them on.
            self.keep(dict(message, content=[]), None)
            change["kept"] = kept
        self.changes.append(change)
        self.rebuilt = True
        return replacement

    def edit(self, index, message, kind):
        """Apply the user_blocks rules to a user message or a tool result; None when the message is deleted."""
        target = "user" if kind == "U" else "tool_result"
        field = "output" if self.dialect == "responses" and kind == "T" and message.get("output") is not None \
            else "content"
        for position, op, fn in self.edits:
            res = _map_content(message.get(field), fn, self.dialect == "anthropic")
            if res is None:
                continue
            value, removed, added, empty = res
            change = {"scope": "history", "op": op, "edit": position, "index": index, "target": target,
                      "removed_chars": removed, "added_chars": added}
            if empty and kind == "U":
                reason = self.blocker(index)
                if reason is not None:
                    self.context.skipped("history", op, reason, edit=position, index=index, target=target)
                    continue
                self.changes.append(dict(change, deleted=True))
                self.rebuilt = True
                return None
            message[field] = "" if empty else value
            self.changes.append(change)
        return message


def _rewrite_history(out, context, history):
    mode = history.get("mid_system") if history.get("mid_system") in ("drop", "wrap", "plain") else "keep"
    edits = []
    for position, spec in enumerate(history.get("user_blocks") or []):
        if not isinstance(spec, dict):
            continue
        fn, reason = _text_op(context, spec, ("drop_tag", "remove_regex", "replace_text"))
        if fn is None:
            context.skipped("history", str(spec.get("op")), reason, edit=position)
        else:
            edits.append((position, spec["op"], fn))
    if mode == "keep" and not edits:
        return
    key, messages, scalar = _history_list(out, context.dialect)
    if messages is None:
        return
    first = len(context.changes)
    walk = _Walk(context, messages, mode, edits)
    result = walk.run()
    if scalar:
        if len(context.changes) > first:
            out[key] = result[0]["content"]
    elif walk.rebuilt:
        out[key] = result


# ---------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------

class _Tool:
    __slots__ = ("top", "holder", "item", "fields", "names")

    def __init__(self, top, holder, item, name, namespace=None):
        self.top, self.holder, self.item = top, holder, item
        function = item.get("function")
        self.fields = function if isinstance(function, dict) else item
        self.names = (namespace + "." + name, name) if namespace else (name,)

    @property
    def name(self):
        return self.names[0]


def _tool_name(item):
    if not isinstance(item, dict):
        return None
    function = item.get("function")
    name = (function if isinstance(function, dict) else item).get("name")
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
        function = choice.get("function")
        for holder in (choice, function if isinstance(function, dict) else {}):
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
                function = call.get("function")
                name = (function if isinstance(function, dict) else call).get("name")
                if isinstance(name, str):
                    names.add(name)
        legacy = message.get("function_call")
        if isinstance(legacy, dict) and isinstance(legacy.get("name"), str):
            names.add(legacy["name"])
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                names.add(block["name"])
    return names


def _description_fn(context, spec):
    op = spec.get("op")
    text = spec.get("text") if isinstance(spec.get("text"), str) else ""
    if op == "append":
        def apply(old):
            if not text.strip() or text.strip() in old:
                return None
            new = old + "\n\n" + text if old.strip() else text
            return new, 0, len(new) - len(old)
        return apply, None
    if op == "prepend":
        def apply(old):
            if not text.strip() or text.strip() in old:
                return None
            new = text + "\n\n" + old if old.strip() else text
            return new, 0, len(new) - len(old)
        return apply, None
    if op == "set":
        def apply(old):
            return None if old == text else (text, len(old), len(text))
        return apply, None
    return _text_op(context, spec, ("replace_text", "replace_regex"))


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
        hiding = spec.get("hide") is True
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
            res = fn(old if isinstance(old, str) else "")
            if res is not None:
                entry.fields["description"] = res[0]
                changes.append({"scope": "tools", "op": op, "edit": position, "index": entry.top, "tool": entry.name,
                                "removed_chars": res[1], "added_chars": res[2]})
    if hide is None or not hide.hidden:
        return
    for holder in {id(e.holder): e.holder for e in entries if id(e.item) in hide.hidden}.values():
        holder[:] = [item for item in holder if id(item) not in hide.hidden]
    out["tools"] = [item for item in tools if not (
        isinstance(item, dict) and item.get("type") == "namespace" and isinstance(item.get("tools"), list)
        and id(item["tools"]) in emptied and not item["tools"])]
