"""Session-local signed assistant history, bound to the prefix that produced it."""
import copy
from dataclasses import dataclass
import json

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


def readable_block(block):
    """Text for an Anthropic document or search result, which the Retool route has no block for; None for others.

    Claude Code's Read answers a PDF with a document block, and search tools answer with search_result blocks. The
    model got the first 200 characters of their JSON instead, and the conversation lost its native history for good
    because the block could not be read natively."""
    kind = block.get("type") if isinstance(block, dict) else None
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
        return "[document%s at %s: this route does not pass the file to the model]" % (title, source.get("url"))
    kilobytes = len(source.get("data") or "") * 3 // 4 // 1024 if source.get("type") == "base64" else 0
    return "[document%s, %s%s: this route does not pass the file to the model]" % (
        title, source.get("media_type") or source.get("type") or "file", ", %d KB" % kilobytes if kilobytes else "")


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
                    or not same_json(client[:offset], snapshot.client)
                    or not same_echo(client[offset], snapshot.echo, defaults)):
                continue
            tail = client[offset + 1:]
            calls = [b["toolCallId"] for b in snapshot.assistant if b["type"] == "tool_call"]
            results = [m["content"]["toolCallId"] for m in tail[:len(calls)] if m["role"] == "tool"]
            if (len(results) != len(calls) or len(set(results)) != len(results) or set(results) != set(calls)
                    or not _complete_turns(tail[len(calls):], set(calls))):
                continue
            return snapshot, snapshot.messages + [{"role": "assistant", "content": snapshot.assistant}] + tail
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
            if not same_json(ours, theirs):
                return "message:%d:%s" % (index, ours.get("role"))
        if not same_echo(client[offset], snapshot.echo, defaults):
            return "echo:" + _echo_difference(client[offset], snapshot.echo)
        return "tool_results"


_MISSING = object()


def _complete_turns(messages, seen):
    """True when what follows a kept reply's tool results is whole turns in the client's own words.

    A reply the relay could not keep (a stream cut by a network error, a reply the user stopped) stays in the client's
    history as an assistant message of its own, usually with a tool call answered by an error, before the client's next
    message. Before, any assistant message there sent the next request cold, and the whole cached conversation was
    written again: 281k and 617k tokens (14 and 31 credits) after a few seconds without internet on 2026-11-06. Such
    turns now follow the kept reply as they are, unsigned, the way a cold replay sends every turn: text and tool calls,
    each call answered before the next turn, no call id used twice, and no call left open at the end."""
    pending = set()
    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = message["content"]["toolCallId"]
            if call_id not in pending:
                return False
            pending.discard(call_id)
            continue
        if pending:
            return False
        if role == "assistant":
            for block in message["content"]:
                if block["type"] not in ("text", "tool_call"):
                    return False
                if block["type"] == "tool_call":
                    if block["toolCallId"] in seen:
                        return False
                    seen.add(block["toolCallId"])
                    pending.add(block["toolCallId"])
        elif role != "user":
            return False
    return not pending and (not messages or messages[-1].get("role") != "assistant")


def same_json(a, b):
    """True when two parsed JSON values are the same values.

    Python's == also says True == 1 and False == 0, so a history that held 1 where the model wrote true was taken for the
    conversation the signed reply belongs to (bug summary of 2026-11-06, section 3, item 14)."""
    try:
        return _same_json(a, b)
    except RecursionError:  # nested past what is worth comparing: not the same, and the turn goes the unsigned way
        return False


def _same_json(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, dict) and isinstance(b, dict):
        return len(a) == len(b) and all(key in b and _same_json(value, b[key]) for key, value in a.items())
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(map(_same_json, a, b))
    return a == b


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
    if same_json(theirs, ours):
        return True
    if not defaults or not isinstance(theirs, dict) or not isinstance(ours, dict):
        return False

    def without_defaults(message):
        blocks = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_call" and isinstance(block.get("toolInput"), dict):
                known = defaults.get(block.get("toolName")) or {}
                block = dict(block, toolInput={key: value for key, value in block["toolInput"].items()
                                               if not same_json(known.get(key, _MISSING), value)})
            blocks.append(block)
        return dict(message, content=blocks)

    return same_json(without_defaults(theirs), without_defaults(ours))


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
        if not same_json([b.get(field) for b in calls[0]], [b.get(field) for b in calls[1]]):
            return label
    return "text"


def cache_messages(messages, enabled, tools):
    """Move only cache hints; signed thinking and the model prefix stay unchanged."""
    messages = copy.deepcopy(messages)
    eligible, previous = [], []
    for message in messages:
        value = message.get("content")
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
            if block.pop("cache", False):
                previous.append(len(eligible) - 1)
    if enabled and eligible:
        budget = 3 if tools else 4
        chosen = {len(eligible) - 1}
        if previous:
            chosen.add(previous[-1])
        chosen.add(0)
        for index in sorted(chosen)[-budget:]:
            eligible[index]["cache"] = True
    return messages
