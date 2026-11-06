"""Session-local signed assistant history, bound to the prefix that produced it."""
import base64
import copy
from dataclasses import dataclass
import json
import urllib.parse

INSTRUCTION_ROLES = ("system", "developer")
INLINE_TOOL_BLOCKS = ("tool_addition", "tool_removal")
_TEXT_BLOCKS = ("text", "input_text", "output_text", "summary_text")
_REMINDER_OPEN, _REMINDER_CLOSE = "<system-reminder>", "</system-reminder>"


def leading_instructions(messages, role_key="role"):
    """How many system/developer messages open the conversation.

    Only these form the head. Later ones keep their place in the history, so a
    reminder never rewrites the cached prefix the signed thinking belongs to.
    """
    count = 0
    for message in messages:
        if not isinstance(message, dict) or (message.get(role_key) or message.get("role")) not in INSTRUCTION_ROLES:
            break
        count += 1
    return count


def reminder(text):
    """User-turn text for a system message that arrives after the history started.

    The wrapper is how Claude Code itself carries reminders in a user message
    when an upstream rejects `system` inside `messages`.
    """
    stripped = text.strip()
    if stripped.startswith(_REMINDER_OPEN) and stripped.endswith(_REMINDER_CLOSE):
        return text
    return _REMINDER_OPEN + "\n" + text + "\n" + _REMINDER_CLOSE


def _withheld(where):
    """What a note says of a file whose content cannot be given to the model, so that the model neither asks for it again
    nor goes looking for it.

    The Retool route has no file block, so a PDF never reaches the model (11 of 11 rows in user content, 0 of 8 loops in
    tool results, in every dialect: a4 F12, 2026-11-06). The model got a stub of about 200 characters, took it for data lost
    on the way, called the tool that returned the file again (7 of 8 loops, when the history had been turned into a flat
    transcript) or, when the request had no tools of its own, ran `Get-ChildItem -Recurse` and `dir /s` on the host through
    the relay's own exec feature (60b91554, 6ed0b785). Asked through the relay with a Read tool and a Bash tool, Sonnet 5.5,
    Opus 5.5 and GPT-6.1-sol all reached for pdftotext after the old stub (11 of 11 runs); after the wording for a file that
    came inside the request all three answered in text (9 of 9), and after the wording for a document a tool of the client
    could try, each tried one command and none read the file again (9 of 9). A file that came inside the request exists
    nowhere else, so no tool can find it; one that a local Read returned may well be on disk, and a tool of the client may
    then be tried once."""
    base = "the content of this file cannot be read through this route and was not given to the model"
    if where == "inline":
        return (base + "; it came inside the request and is not on disk, so no tool can find it. Do not look for it: tell "
                "the user that it could not be read, and ask for its text.")
    if where == "elsewhere":
        return base + ". Do not look for it: tell the user that it could not be read, and ask for its text."
    return (base + ". Do not read it again with this tool. If the file is on disk and another tool can extract its text, use "
            "that once; otherwise tell the user that it could not be read.")


def _readable_file(block):
    """Text for a Chat `file` part or a Responses `input_file` item: the decoded text of a text file, else a note that the
    route cannot pass the file. Before, the part became the first 200 characters of its JSON and the conversation lost its
    native history for good, like the Anthropic document blocks (see readable_block)."""
    spec = block.get("file") if isinstance(block.get("file"), dict) else block
    name = spec.get("filename")
    title = " %s" % name if isinstance(name, str) and name else ""
    data = spec.get("file_data")
    if isinstance(data, str) and data.startswith("data:") and "," in data:
        header, _, payload = data.partition(",")
        mime = header[5:].split(";")[0] or "file"
        if mime.startswith("text/") or mime in ("application/json", "application/xml"):
            try:
                text = (base64.b64decode("".join(payload.split()), validate=True) if ";base64" in header
                        else urllib.parse.unquote_to_bytes(payload)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                text = None
            if text is not None:
                return "[file%s, %s]\n%s" % (title, mime, text)
        kilobytes = len(payload) * 3 // 4 // 1024 if ";base64" in header else len(payload) // 1024
        return "[file%s, %s%s: %s]" % (title, mime, ", %d KB" % kilobytes if kilobytes else "", _withheld("inline"))
    if isinstance(spec.get("file_url"), str):
        return "[file%s at %s: %s]" % (title, spec["file_url"], _withheld("elsewhere"))
    uploaded = " (uploaded id %s)" % spec["file_id"] if spec.get("file_id") else ""
    return "[file%s%s: %s]" % (title, uploaded, _withheld("inline" if isinstance(data, str) else "elsewhere"))


def readable_block(block):
    """Text for an Anthropic document or search result, which the Retool route has no block for; None for others.

    Claude Code's Read answers a PDF with a document block, and search tools answer with search_result blocks. The
    model got the first 200 characters of their JSON instead, and the conversation lost its native history for good
    because the block could not be read natively. A Chat `file` part and a Responses `input_file` item are the same case
    (_readable_file)."""
    kind = block.get("type") if isinstance(block, dict) else None
    if kind in ("file", "input_file"):
        return _readable_file(block)
    if kind == "search_result":
        texts = [part.get("text") for part in block.get("content") or []
                 if isinstance(part, dict) and part.get("type") == "text" and part.get("text")]
        head = "[search result: %s, %s]" % (block.get("title") or "untitled", block.get("source") or "no source")
        return "\n".join([head] + texts)
    if kind != "document":
        return None
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    title = " %s" % block["title"] if isinstance(block.get("title"), str) and block["title"] else ""
    if source.get("type") == "text" and isinstance(source.get("data"), str):
        return "[document%s]\n%s" % (title, source["data"])
    if source.get("type") == "content":
        texts = [part.get("text") for part in source.get("content") or []
                 if isinstance(part, dict) and part.get("type") == "text" and part.get("text")]
        return "\n".join(["[document%s]" % title] + texts)
    if source.get("type") == "url":
        return "[document%s at %s: %s]" % (title, source.get("url"), _withheld("elsewhere"))
    kilobytes = len(source.get("data") or "") * 3 // 4 // 1024 if source.get("type") == "base64" else 0
    return "[document%s, %s%s: %s]" % (
        title, source.get("media_type") or source.get("type") or "file", ", %d KB" % kilobytes if kilobytes else "",
        _withheld("document"))


def client_messages(messages, image_converter, *, lead=None):
    """Canonical client echo for matching, never a source of thinking signatures.

    Chat hides signatures; Anthropic and Responses group tool blocks differently.
    Match their visible text, parsed arguments, IDs and results, not display-only
    reasoning labels. Unsupported shapes keep the existing text bridge. Leading
    system messages live in the head; later non-empty ones are user reminders.
    """
    result = []
    if lead is None:
        lead = leading_instructions(messages)

    def instruction_text(value):
        if isinstance(value, str):
            return value
        parts = []
        for block in value or []:
            if isinstance(block, str):
                parts.append(block)
            elif block.get("type") in _TEXT_BLOCKS:
                parts.append(block["text"])
            elif block.get("type") not in INLINE_TOOL_BLOCKS:
                raise ValueError("unsupported instruction block")
        return "\n".join(part for part in parts if part)

    def content(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [{"type": "text", "text": value}] if value else []
        blocks = []
        for block in value:
            if isinstance(block, str):
                if block:
                    blocks.append({"type": "text", "text": block})
            elif block.get("type") in ("text", "input_text", "output_text", "summary_text"):
                # Upstream refuses an empty text block; the text bridge leaves it out as well.
                if block["text"]:
                    blocks.append({"type": "text", "text": block["text"]})
            elif block.get("type") in ("image", "image_url", "input_image") or "imageData" in block:
                blocks.append(image_converter(block, "native history"))
            elif block.get("type") == "tool_reference" and isinstance(block.get("tool_name"), str) and block["tool_name"]:
                # Claude Code's ToolSearch answers with references to the tools it loaded. Upstream has no such block,
                # and the loaded tools are in the request's tool list already, so the result names them.
                blocks.append({"type": "text", "text": "Tool loaded: " + block["tool_name"]})
            elif readable_block(block) is not None:
                blocks.append({"type": "text", "text": readable_block(block)})
            else:
                raise ValueError("unsupported content")
        return blocks

    def call(call_id, name, arguments):
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        if not call_id or not name or not isinstance(arguments, dict):
            raise ValueError("incomplete tool call")
        return {"type": "tool_call", "toolCallId": call_id,
                "toolName": name, "toolInput": arguments}

    def tool_result(call_id, value, is_error=False):
        if not call_id:
            raise ValueError("missing tool result id")
        message = {"role": "tool", "content": {"toolCallId": call_id,
                   "toolResult": value if isinstance(value, str) else content(value)}}
        if is_error:
            message["isError"] = True
        return message

    try:
        for index, message in enumerate(messages):
            role = message.get("role") or ("assistant" if message.get("tool_calls") else None)
            value = message.get("content")
            if role in INSTRUCTION_ROLES:
                text = instruction_text(value) if index >= lead else ""
                if text.strip():
                    result.append({"role": "user", "content": [{"type": "text", "text": reminder(text)}]})
                continue
            if role == "assistant":
                text, calls = [], []
                for block in ([{"type": "text", "text": value}] if isinstance(value, str) else value or []):
                    kind = block.get("type")
                    if kind in ("thinking", "redacted_thinking"):
                        continue
                    if kind in ("text", "output_text", "summary_text"):
                        text.append(block["text"])
                    elif kind in ("tool_use", "tool_call"):
                        calls.append(call(block.get("id") or block.get("tool_call_id"),
                                          block.get("name"), block.get("input", block.get("arguments", {}))))
                    else:
                        raise ValueError("unsupported assistant block")
                for item in message.get("tool_calls") or []:
                    fn = item.get("function") or item
                    calls.append(call(item.get("id") or item.get("tool_call_id"), fn.get("name"),
                                      fn.get("arguments", "{}")))
                blocks = ([{"type": "text", "text": "".join(text)}] if any(text) else []) + calls
                if not blocks:
                    continue
                if result and result[-1]["role"] == "assistant":
                    result[-1]["content"].extend(blocks)
                    result[-1] = canonical_assistant(result[-1]["content"])
                else:
                    result.append({"role": "assistant", "content": blocks})
            elif role in ("tool", "function"):
                result.append(tool_result(message.get("tool_call_id") or message.get("call_id"), value,
                                          message.get("is_error", False)))
            elif role == "user":
                pending = []
                for block in ([{"type": "text", "text": value}] if isinstance(value, str) else value or []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if pending and content(pending):
                            result.append({"role": "user", "content": content(pending)})
                        pending = []
                        result.append(tool_result(block.get("tool_use_id") or block.get("tool_call_id"),
                                                  block.get("content"), block.get("is_error", False)))
                    else:
                        pending.append(block)
                # A user turn of nothing but empty text has nothing to say upstream, which refuses an empty message.
                if pending and content(pending):
                    result.append({"role": "user", "content": content(pending)})
            else:
                raise ValueError("unsupported role")
        return copy.deepcopy(result)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def canonical_assistant(blocks):
    text = "".join(b["text"] for b in blocks if b["type"] == "text")
    calls = [b for b in blocks if b["type"] == "tool_call"]
    return {"role": "assistant", "content": ([{"type": "text", "text": text}] if text else []) + calls}


def unsigned_tool_transcript(client):
    """Keep logical tool roles on a cold context; never invent/replay signatures.

    Only a complete canonical client transcript is usable. Incomplete batches
    keep the existing compatibility path instead of attaching orphan results.
    """
    if not client:
        return None
    pending, seen, has_results = set(), set(), False
    for message in client:
        role = message.get('role')
        if role == 'tool':
            call_id = message['content']['toolCallId']
            if call_id not in pending:
                return None
            pending.remove(call_id)
            has_results = True
        else:
            if pending:
                return None
            if role == 'assistant':
                for block in message['content']:
                    if block['type'] not in ('text', 'tool_call'):
                        return None
                    if block['type'] == 'tool_call':
                        call_id = block['toolCallId']
                        if call_id in seen:
                            return None
                        seen.add(call_id)
                        pending.add(call_id)
            elif role != 'user':
                return None
    return copy.deepcopy(client) if has_results and not pending else None


class ReplyCapture:
    """Keep raw NDJSON block order and opaque signatures before argument coercion."""
    def __init__(self):
        self.blocks = []
        self.current = None
        self.thinking = None
        self.calls = {}
        self.inputs = {}
        self.open_calls = []
        self.valid = True

    def feed(self, event):
        kind, block_type = event.get("event"), event.get("type")
        if kind == "content_block_start":
            if block_type == "thinking":
                self.current = self.thinking = {"type": "thinking", "thinking": "", "signature": ""}
            elif block_type == "text":
                self.current = {"type": "text", "text": ""}
            elif block_type == "tool_call":
                call_id = event.get("toolCallId")
                if not call_id or call_id in self.calls or not event.get("toolName"):
                    self.valid = False
                self.current = {"type": "tool_call", "toolCallId": call_id,
                                "toolName": event.get("toolName"), "toolInput": event.get("toolInput", {})}
                self.calls[call_id] = self.current
                self.inputs[call_id] = []
                self.open_calls.append(call_id)
            elif block_type == "redacted_thinking" and isinstance(event.get("data"), str):
                self.current = {"type": "redacted_thinking", "data": event["data"]}
                if event.get("responseItemId"):
                    self.current["responseItemId"] = event["responseItemId"]
            else:
                self.valid = False
                return
            self.blocks.append(self.current)
        elif kind == "content_block_end":
            if block_type not in (None, "tool_call"):
                self.current = None
            elif self.open_calls:
                call_id = event.get("toolCallId") or self.open_calls[-1]
                if call_id in self.open_calls:
                    self.open_calls.remove(call_id)
                self.current = self.calls[self.open_calls[-1]] if self.open_calls else None
            else:
                self.current = None
        elif kind == "content_block_delta":
            if block_type == "signature":
                if self.thinking is None:
                    self.valid = False
                else:
                    self.thinking["signature"] += event.get("partialSignature") or ""
            elif block_type in ("text", "thinking"):
                if self.current is None or self.current["type"] != block_type:
                    self.feed({"event": "content_block_start", "type": block_type})
                field = "text" if block_type == "text" else "thinking"
                self.current[field] += event.get("partialText" if block_type == "text" else "partialThinking") or ""
            elif block_type == "tool_call":
                call_id = event.get("toolCallId") or (
                    self.current.get("toolCallId") if self.current is not None else None)
                if call_id not in self.calls:
                    self.valid = False
                else:
                    self.inputs[call_id].append(event.get("partialInputs") or "")
            else:
                self.valid = False

    def finish(self):
        if not self.valid:
            return None
        try:
            for call_id, call in self.calls.items():
                raw = "".join(self.inputs[call_id])
                # A call without arguments arrives as one empty partialInputs: the stream gives the client {} for it
                # (coerce_arguments), so the kept reply must say {} too, or its next turn can never continue it.
                if raw:
                    call["toolInput"] = json.loads(raw)
                if not isinstance(call["toolInput"], dict):
                    return None
            if any(b["type"] == "thinking" and not b["signature"] for b in self.blocks):
                return None
            return [b for b in self.blocks if b["type"] != "text" or b["text"]]
        except (TypeError, ValueError):
            return None


def echo(blocks, param_types, coerce):
    visible = copy.deepcopy([b for b in blocks if b["type"] in ("text", "tool_call")])
    for block in visible:
        if block["type"] == "tool_call":
            block["toolInput"] = json.loads(coerce(json.dumps(block["toolInput"], ensure_ascii=False),
                                                  param_types.get(block["toolName"])))
    return canonical_assistant(visible)


@dataclass(frozen=True, eq=False)
class Snapshot:
    scope: str
    client: list
    messages: list
    assistant: list
    echo: dict
    # True for a branch that was read back from disk after a restart (conversation_state), not recorded live.
    restored: bool = False


class History:
    """Bounded live branches; sequential tool turns replace their parent snapshot.

    `revision` counts the recorded snapshots: the stored copy of a conversation is rewritten when it moves."""
    def __init__(self):
        self.branches = []
        self.revision = 0

    def resume(self, scope, client, defaults=None):
        for snapshot in reversed(self.branches):
            offset = len(snapshot.client)
            if (scope != snapshot.scope or len(client) <= offset + 1
                    or client[:offset] != snapshot.client or not same_echo(client[offset], snapshot.echo, defaults)):
                continue
            tail = client[offset + 1:]
            calls = [b["toolCallId"] for b in snapshot.assistant if b["type"] == "tool_call"]
            results = [m["content"]["toolCallId"] for m in tail[:len(calls)] if m["role"] == "tool"]
            if (len(results) != len(calls) or len(set(results)) != len(results) or set(results) != set(calls)
                    or any(m["role"] != "user" for m in tail[len(calls):])):
                continue
            return snapshot, snapshot.messages + [{"role": "assistant", "content": snapshot.assistant}] + tail
        return None, None

    def resume_fork(self, scope, client):
        """(snapshot, upstream messages) for a request that forks from a recorded one, else (None, None).

        Claude Desktop's compaction sends the messages of its last request WITHOUT the reply to that request (the client
        files a reply into the next turn it builds) and its instruction after them. resume() wants the echo of that
        reply and never accepted this shape: the history started again, the whole prefix was written once more although
        the provider still had it (a4, 2026-11-06: 485k tokens, 51.9 credits for one /compact; emulated at 130k tokens
        6.61 credits and 9.2 s to the first token against 2.57 and 2.5 s). Here the client's history must be exactly the
        history the snapshot started from, followed by user messages and nothing else; the upstream messages are the
        snapshot's own input, the prefix its request cached, plus those. No reply of the snapshot is replayed, so no
        signature is attached to anything it does not belong to. The caller records nothing for the fork."""
        for snapshot in reversed(self.branches):
            offset = len(snapshot.client)
            if (scope == snapshot.scope and len(client) > offset and client[:offset] == snapshot.client
                    and all(message.get("role") == "user" for message in client[offset:])):
                return snapshot, snapshot.messages + client[offset:]
        return None, None

    def record(self, scope, client, messages, blocks, expected, parent=None):
        self.branches = [s for s in self.branches if s is not parent]
        self.branches.append(Snapshot(scope, copy.deepcopy(client), messages, blocks, expected))
        self.branches = self.branches[-8:]
        self.revision += 1

    def mismatch(self, scope, client, defaults=None):
        """Which check of resume() the newest branch fails for this request, for the journal of a reset.

        Names only: a part of the scope, a message index and role, a part of the echo; never text."""
        if not self.branches:
            return "no_branch"
        snapshot = self.branches[-1]
        if scope != snapshot.scope:
            return "scope:" + _scope_difference(scope, snapshot.scope)
        offset = len(snapshot.client)
        if len(client) <= offset + 1:
            return "history:%d_of_%d_messages" % (len(client), offset + 2)
        for index, (ours, theirs) in enumerate(zip(client, snapshot.client)):
            if ours != theirs:
                return "message:%d:%s" % (index, ours.get("role"))
        if not same_echo(client[offset], snapshot.echo, defaults):
            return "echo:" + _echo_difference(client[offset], snapshot.echo)
        return "tool_results"


_MISSING = object()


def tool_defaults(tools):
    """{tool name: {argument: default}} from the client's own tool schemas, for the arguments that declare a default."""
    found = {}
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        schema = function.get("parameters") if isinstance(function, dict) else None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            continue
        values = {name: spec["default"] for name, spec in properties.items() if isinstance(spec, dict) and "default" in spec}
        if values and function.get("name"):
            found[function["name"]] = values
    return found


def same_echo(theirs, ours, defaults=None):
    """True when the client's echo of the kept reply is that reply.

    Claude Code fills the default of an optional argument the model left out before it echoes the call ("replace_all":
    false of Edit), so an argument that holds its schema default counts as absent on both sides. Before, such an echo
    failed the check and the next turn went cold, rewriting the whole cached conversation (Claude Desktop test run of
    2026-11-06: 233k tokens, 30.7 credits for one Edit)."""
    if theirs == ours:
        return True
    if not defaults or not isinstance(theirs, dict) or not isinstance(ours, dict):
        return False

    def without_defaults(message):
        blocks = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_call" and isinstance(block.get("toolInput"), dict):
                known = defaults.get(block.get("toolName")) or {}
                block = dict(block, toolInput={key: value for key, value in block["toolInput"].items()
                                               if known.get(key, _MISSING) != value})
            blocks.append(block)
        return dict(message, content=blocks)

    return without_defaults(theirs) == without_defaults(ours)


_SCOPE_PARTS = ("conversation", "provider", "head", "tools", "tool_choice")


def _scope_difference(ours, theirs):
    try:
        ours, theirs = json.loads(ours), json.loads(theirs)
    except (TypeError, ValueError):
        return "unreadable"
    parts = []
    for name, a, b in zip(_SCOPE_PARTS, ours, theirs):
        if a == b:
            continue
        if name == "provider" and isinstance(a, dict) and isinstance(b, dict):
            name += "(" + ",".join(sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))) + ")"
        parts.append(name)
    return ",".join(parts) or "order"


def _echo_difference(ours, theirs):
    if ours.get("role") != theirs.get("role"):
        return "role"
    calls = [[b for b in m.get("content") or [] if b.get("type") == "tool_call"] for m in (ours, theirs)]
    for field, label in (("toolCallId", "call_ids"), ("toolName", "names"), ("toolInput", "arguments")):
        if [b.get(field) for b in calls[0]] != [b.get(field) for b in calls[1]]:
            return label
    return "text"


class _PlainResult:
    """A tool result that is plain text, as a place for a cache hint: it becomes a list of one text block when it gets one."""

    def __init__(self, content):
        self.content = content

    def __setitem__(self, key, value):
        self.content["toolResult"] = [{"type": "text", "text": self.content["toolResult"], key: value}]


def _result_slot(content):
    """(the place of a tool result that can carry a hint, whether it carried one), or (None, False).

    A result that is a list keeps its hint on a text block: every hint it had is taken off, and the last text block takes
    a new one. One that is plain text has none, and is turned into a list of one block only when it is given one."""
    result = content.get("toolResult")
    if isinstance(result, str):
        return (_PlainResult(content), False) if result.strip() else (None, False)
    if not isinstance(result, list):
        return None, False
    texts = [block for block in result if isinstance(block, dict) and block.get("type") == "text"
             and isinstance(block.get("text"), str) and block["text"].strip()]
    had = False
    for block in texts:
        had = bool(block.pop("cache", False)) or had
    return (texts[-1], had) if texts else (None, False)


def cache_messages(messages, enabled, tools, mark_tail=True, tool_results=False):
    """Move only cache hints; signed thinking and the model prefix stay unchanged.

    mark_tail=False leaves the last message without the newest hint: a compaction's own instruction is read by nobody
    (the client replaces its history by the summary), so writing it to the cache only costs the premium of a write.

    tool_results=True lets the result of a tool carry a hint (for a route that has breakpoints: Anthropic). A result
    arrives as plain input, and the next request, whose newest hint stands after it, writes it to the cache: 43,703 tokens
    of input and then a 43,918-token write (a3, 2026-11-06). With the hint where the result arrives it is written once and
    the next request reads it. Without the flag a result carries none, and the hints an earlier body put on one are
    taken off."""
    messages = copy.deepcopy(messages)
    eligible, previous, owner = [], [], []
    for position, message in enumerate(messages):
        value = message.get("content")
        if isinstance(value, dict):
            slot, had = _result_slot(value) if message.get("role") == "tool" else (None, False)
            if slot is not None and tool_results:
                eligible.append(slot)
                owner.append(position)
                if had:
                    previous.append(len(eligible) - 1)
            continue
        if not isinstance(value, list):
            continue
        for block in value:
            if block.get("type") not in ("text", "tool_call"):
                continue
            text = block.get('text', '')
            if (message.get('role') == 'system' and isinstance(text, str)
                    and text.startswith('x-anthropic-billing-header:') and '\n' not in text):
                # An attribution-only SDK block is consumed by Anthropic and
                # must not displace the actual system prefix cache breakpoint.
                block.pop('cache', None)
                continue
            eligible.append(block)
            owner.append(position)
            if block.pop("cache", False):
                previous.append(len(eligible) - 1)
    if enabled and eligible:
        budget = 3 if tools else 4

        def allowed(index):
            return mark_tail or owner[index] != len(messages) - 1

        newest = len(eligible) - 1
        while newest >= 0 and not allowed(newest):
            newest -= 1
        chosen = {newest} if newest >= 0 else set()
        if previous and allowed(previous[-1]):
            chosen.add(previous[-1])
        if allowed(0):
            chosen.add(0)
        for index in sorted(chosen)[-budget:]:
            eligible[index]["cache"] = True
    return messages
