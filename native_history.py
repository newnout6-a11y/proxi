"""Session-local signed assistant history, bound to the prefix that produced it."""
import base64
import copy
from dataclasses import dataclass
import json
import math
import urllib.parse
import weakref

import web_search
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

                def flush():
                    """The assistant message so far (its text, then its calls), merged into the one before it if any."""
                    nonlocal text, calls
                    blocks = ([{"type": "text", "text": "".join(text)}] if any(text) else []) + calls
                    text, calls = [], []
                    if not blocks:
                        return
                    if result and result[-1]["role"] == "assistant":
                        result[-1]["content"].extend(blocks)
                        result[-1] = canonical_assistant(result[-1]["content"])
                    else:
                        result.append({"role": "assistant", "content": blocks})

                for block in ([{"type": "text", "text": value}] if isinstance(value, str) else value or []):
                    kind = block.get("type")
                    if kind in ("thinking", "redacted_thinking"):
                        continue
                    if kind in ("text", "output_text", "summary_text"):
                        text.append(block["text"])
                    elif kind in ("tool_use", "tool_call", "server_tool_use"):
                        calls.append(call(block.get("id") or block.get("tool_call_id"),
                                          block.get("name"), block.get("input", block.get("arguments", {}))))
                    elif kind == "web_search_tool_result":
                        # A search the relay ran for the client: its result sits inside the assistant message the client
                        # echoes, and upstream saw it as the result of the call before the reply went on.
                        flush()
                        result.append(tool_result(block.get("tool_use_id"), web_search.render_block_content(
                            block.get("content"))))
                    else:
                        raise ValueError("unsupported assistant block")
                for item in message.get("tool_calls") or []:
                    if isinstance(item.get("custom"), dict):
                        # A call to a custom tool of Chat: its one string is the input the model was offered the tool as.
                        calls.append(call(item.get("id"), item["custom"].get("name"),
                                          {"input": item["custom"].get("input", "")}))
                        continue
                    fn = item.get("function") or item
                    calls.append(call(item.get("id") or item.get("tool_call_id"), fn.get("name"),
                                      fn.get("arguments", "{}")))
                legacy = message.get("function_call")
                if isinstance(legacy, dict) and legacy.get("name"):
                    # The deprecated form of one call; its "function" message carries the name, not an id (see below).
                    calls.append(call(legacy["name"], legacy["name"], legacy.get("arguments", "{}")))
                flush()
            elif role in ("tool", "function"):
                result.append(tool_result(message.get("tool_call_id") or message.get("call_id")
                                          or (message.get("name") if role == "function" else None), value,
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


def unsigned_tool_transcript(client, without_results=False):
    """Keep logical tool roles on a cold context; never invent/replay signatures.

    Only a complete canonical client transcript is usable. Incomplete batches
    keep the existing compatibility path instead of attaching orphan results.
    A transcript without a tool result is taken as well when `without_results`: a route that keeps no branch (see
    direct._cold_from_the_start) must render its history in one form from the first request on.
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
    return copy.deepcopy(client) if (has_results or without_results) and not pending else None


def _finite_float(text):
    value = float(text)
    return value if math.isfinite(value) else None


def _no_constant(name):
    return None


def loads_finite(text):
    """json.loads for the arguments of a tool call, with a number past the range of a double (1e400) read as null.

    Python reads 1e400 as inf and writes it back as Infinity, which is no JSON: the next request upstream carried it
    and was refused, and so was the Messages reply that held it (ultracode audit of the relay, G3.2). A JavaScript
    client reads it as Infinity and writes null when it echoes the call, so null is also what its echo is compared
    with. NaN and Infinity written out as words are read as null as well."""
    return json.loads(text, parse_float=_finite_float, parse_constant=_no_constant)


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
                    call["toolInput"] = loads_finite(raw)
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


def deliverable(reply, names, param_types, coerce):
    """The reply `(blocks, echo)` as the client has it when only the calls of the tools `names` reached it, or None.

    The relay passes on a call only when the client can run it: one of a tool that was not declared, or, under a named
    choice, of another tool than the named one, is dropped (orchestrator, `tool.call_dropped`). The model made that call
    all the same, so the reply kept as a branch had a call the client never echoes, and its next request could not
    continue the branch: history reset and the prefix written again (a5, C4). The branch is the reply without the
    dropped calls: its thinking blocks, which carry the signatures, stay as they are, and the message the client
    echoes is the message the branch holds. None when nothing is left that the client received (no text and no call),
    so that nothing is recorded."""
    blocks, _ = reply
    kept = [block for block in blocks if block["type"] != "tool_call" or block["toolName"] in names]
    if len(kept) == len(blocks):
        return reply
    if not any(block["type"] in ("text", "tool_call") for block in kept):
        return None
    return kept, echo(kept, param_types, coerce)


@dataclass(frozen=True, eq=False)
class Snapshot:
    scope: str
    client: list
    messages: list
    assistant: list
    echo: dict
    # True for a branch that was read back from disk after a restart (conversation_state), not recorded live.
    restored: bool = False


LIVE_BRANCHES = 8
# The turns a user can take back and still continue: the newest final answers whose conversation went on.
REWIND_POINTS = 3


class History:
    """Bounded live branches; sequential tool turns replace their parent snapshot.

    A final answer (a reply without a tool call) is kept when the next turn continues it, for REWIND_POINTS turns: it is
    where a client's undo goes back to (OpenCode's /undo and Revert, Claude Code's /rewind and double Esc, Codex's
    double Esc, Claude Desktop's edit and retry). The request after an undo is the conversation up to the end of an
    earlier turn plus a new message; with the final answer of that turn gone, it was sent cold, every message written
    to the cache again (30 credits at 300k tokens) though the provider still held the prefix it had cached minutes
    before. Points give way to other branches when the live bound is reached.

    `revision` counts the recorded snapshots: the stored copy of a conversation is rewritten when it moves. `pruned`
    holds the branches that were recorded without a call the model made (deliverable): a shape upstream may refuse."""
    def __init__(self):
        self.branches = []
        self.points = []
        self.revision = 0
        self.pruned = weakref.WeakSet()

    def resume(self, scope, client, defaults=None):
        for snapshot in reversed(self.branches):
            at = _echo_index(snapshot.client, client) if scope == snapshot.scope else None
            if at is None or len(client) <= at + 1 or not same_echo(client[at], snapshot.echo, defaults):
                continue
            tail = client[at + 1:]
            calls = [b["toolCallId"] for b in snapshot.assistant if b["type"] == "tool_call"]
            results = [m["content"]["toolCallId"] for m in tail[:len(calls)] if m["role"] == "tool"]
            if (len(results) != len(calls) or len(set(results)) != len(results) or set(results) != set(calls)
                    or not _complete_turns(tail[len(calls):], set(calls))):
                continue
            return snapshot, snapshot.messages + [{"role": "assistant", "content": snapshot.assistant}] + tail
        return self._resume_cut(scope, client)

    def _resume_cut(self, scope, client):
        """(snapshot, upstream messages) when the client's echo is a cut version of a kept reply, else (None, None).

        The relay ends a reply at the output cap the client set (a cap below 16,384 on Claude, OutputCapAdapter) while
        upstream writes it whole, so the kept reply held more text, or a call, than the client got and its echo never
        matched: a reset and a cold replay, the whole conversation written again (6.1k of a 27k prompt in all three
        dialects; about 14 credits at 300k tokens on Sonnet; v4 protocol audit of 2026-11-07, 121bb5f5, a51006ce,
        6bac31da). The echo is accepted when its text begins the kept text and its calls begin the kept calls, one of them
        shorter; upstream gets the kept signed thinking and what the client was given, its delivered calls as the model
        wrote them."""
        for snapshot in reversed(self.branches):
            at = _echo_index(snapshot.client, client) if scope == snapshot.scope else None
            if at is None or len(client) <= at + 1 or not isinstance(client[at], dict) or client[at].get("role") != "assistant":
                continue
            theirs, kept = client[at].get("content") or [], snapshot.echo.get("content") or []
            text = "".join(b.get("text", "") for b in theirs if isinstance(b, dict) and b.get("type") == "text")
            kept_text = "".join(b.get("text", "") for b in kept if isinstance(b, dict) and b.get("type") == "text")
            delivered = [b for b in theirs if isinstance(b, dict) and b.get("type") == "tool_call"]
            kept_calls = [b for b in kept if isinstance(b, dict) and b.get("type") == "tool_call"]
            if (not kept_text.startswith(text) or not same_json(delivered, kept_calls[:len(delivered)])
                    or not (text or delivered) or (len(text), len(delivered)) == (len(kept_text), len(kept_calls))):
                continue
            tail = client[at + 1:]
            calls = [b["toolCallId"] for b in delivered]
            results = [m["content"]["toolCallId"] for m in tail[:len(calls)] if m.get("role") == "tool"]
            if (len(results) != len(calls) or set(results) != set(calls)
                    or not _complete_turns(tail[len(calls):], set(calls))):
                continue
            raw = {b["toolCallId"]: b for b in snapshot.assistant if b.get("type") == "tool_call"}
            given = ([b for b in snapshot.assistant if b.get("type") in ("thinking", "redacted_thinking")]
                     + ([{"type": "text", "text": text}] if text else []) + [raw[call_id] for call_id in calls])
            return snapshot, snapshot.messages + [{"role": "assistant", "content": given}] + tail
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
            if (scope == snapshot.scope and len(client) > offset and same_json(client[:offset], snapshot.client)
                    and all(message.get("role") == "user" for message in client[offset:])):
                return snapshot, snapshot.messages + client[offset:]
        return None, None

    def record(self, scope, client, messages, blocks, expected, parent=None, pruned=False):
        points = [p for p in getattr(self, "points", []) if p is not parent and any(p is s for s in self.branches)]
        if parent is not None and any(parent is s for s in self.branches) and _final_answer(parent):
            points.append(parent)
        else:
            self.branches = [s for s in self.branches if s is not parent]
        while len(points) > REWIND_POINTS:
            oldest = points.pop(0)
            self.branches = [s for s in self.branches if s is not oldest]
        snapshot = Snapshot(scope, copy.deepcopy(client), messages, blocks, expected)
        self.branches.append(snapshot)
        if pruned:
            self.pruned.add(snapshot)
        while len(self.branches) > LIVE_BRANCHES:
            # Other branches (a fork, a subagent on the same key, the chat beside a compaction) outrank a rewind point.
            victim = next((s for s in self.branches[:-1] if any(s is p for p in points)), self.branches[0])
            self.branches = [s for s in self.branches if s is not victim]
            points = [p for p in points if p is not victim]
        self.points = points
        self.revision += 1

    def forget_pruned(self):
        """Drop the branches that were recorded without a call; the next request starts that history again."""
        self.branches = [s for s in self.branches if s not in self.pruned]
        self.pruned.clear()
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


def _final_answer(snapshot):
    """True for a kept reply that called no tool: the end of a turn, where a client's undo can go back to."""
    return not any(isinstance(block, dict) and block.get("type") == "tool_call" for block in snapshot.assistant or ())


def transient(message):
    """True for a user message of nothing but <system-reminder> text: what a client adds to one request without keeping it."""
    content = message.get("content") if isinstance(message, dict) and message.get("role") == "user" else None
    if not isinstance(content, list) or not content:
        return False
    for block in content:
        text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
        if not isinstance(text, str) or not (text.strip().startswith(_REMINDER_OPEN)
                                             and text.strip().endswith(_REMINDER_CLOSE)):
            return False
    return True


def _echo_index(kept, client):
    """Where the client's echo of a kept reply stands in its next request, or None when the request does not extend it.

    The request extends the kept one when it holds all of its messages, with one exception: a reminder the client added
    to the kept request only, in the user messages that close it. OpenCode's goal plugin adds "<system-reminder>A goal
    is active for this session...</system-reminder>" to every request without keeping it (at the end, or just before a
    trailing user message), so in the next request the reminder is gone and the reply stands where it was. Every step
    of such a conversation went cold: 89 requests in a row on 2026-11-06 (9b839275847f, ffb84e3c975b, 0aff4f28a7eb),
    cache reads stuck at the 11.5k tokens of the system prompt, up to 122k tokens written and 4 to 18 credits a step,
    the first token after 60 to 180 s. The reminder stays in what upstream gets, where the model saw it when it made the
    signed reply, so that reply is continued in its own context and the cached prefix is read as it was written."""
    if same_json(client[:len(kept)], kept):
        return len(kept)
    start = len(kept)
    while start and isinstance(kept[start - 1], dict) and kept[start - 1].get("role") == "user":
        start -= 1
    if not same_json(client[:start], kept[:start]):
        return None
    at = start
    for message in kept[start:]:
        if at < len(client) and same_json(client[at], message):
            at += 1
        elif not transient(message):
            return None
    return at


def dropped(snapshot, client, messages):
    """How many reminders of the kept request the client left out of this one (resume() keeps them upstream)."""
    return len(snapshot.client) - (len(client) - (len(messages) - len(snapshot.messages) - 1) - 1)


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
    """True when two parsed JSON values are the same values, as a JSON client holds them.

    Python's == also says True == 1 and False == 0, so a history that held 1 where the model wrote true was taken for the
    conversation the signed reply belongs to (bug summary of 2026-11-06, section 3, item 14). And Python keeps an integer
    exact where a JavaScript client (Claude Code, the AI SDK) holds a double: the id 9007199254740993 the model wrote
    comes back from it as 9007199254740992, which was taken for another reply, and the whole conversation was sent
    again, uncached (ultracode audit of the relay, G3.1). Numbers are compared as doubles."""
    try:
        return _same_json(a, b)
    except RecursionError:  # nested past what is worth comparing: not the same, and the turn goes the unsigned way
        return False


def _double(number):
    try:
        return float(number)
    except OverflowError:  # an integer past the range of a double is infinity to JavaScript
        return float("inf") if number > 0 else float("-inf")


def _same_json(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return _double(a) == _double(b)
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
        # A client's compaction (mark_tail=False): the client replaces its history by the summary, so nothing this request
        # writes is read again. Only the hint the chat's last request wrote, which this one reads, and the head stay;
        # content the chat never cached is input, not a write (OpenCode 2.2.6's automatic compaction wrote the newest tool
        # result, 27,171 tokens and 1.62 credits, v3 run of 2026-11-07, 532118816fc1).
        chosen = {newest} if newest >= 0 and mark_tail else set()
        if previous and allowed(previous[-1]):
            chosen.add(previous[-1])
        if allowed(0):
            chosen.add(0)
        for index in sorted(chosen)[-budget:]:
            eligible[index]["cache"] = True
    return messages
