"""Session-local signed assistant history, bound to the prefix that produced it."""
import copy
from dataclasses import dataclass
import json


def client_messages(messages, image_converter):
    """Canonical client echo for matching, never a source of thinking signatures.

    Chat hides signatures; Anthropic and Responses group tool blocks differently.
    Match their visible text, parsed arguments, IDs and results, not display-only
    reasoning labels. Unsupported shapes keep the existing text bridge.
    """
    result = []

    def content(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [{"type": "text", "text": value}] if value else []
        blocks = []
        for block in value:
            if isinstance(block, str):
                blocks.append({"type": "text", "text": block})
            elif block.get("type") in ("text", "input_text", "output_text", "summary_text"):
                blocks.append({"type": "text", "text": block["text"]})
            elif block.get("type") in ("image", "image_url", "input_image") or "imageData" in block:
                blocks.append(image_converter(block, "native history"))
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
        for message in messages:
            role = message.get("role") or ("assistant" if message.get("tool_calls") else None)
            value = message.get("content")
            if role in ("system", "developer"):
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
                        if pending:
                            result.append({"role": "user", "content": content(pending)})
                            pending = []
                        result.append(tool_result(block.get("tool_use_id") or block.get("tool_call_id"),
                                                  block.get("content"), block.get("is_error", False)))
                    else:
                        pending.append(block)
                if pending:
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
                if self.inputs[call_id]:
                    call["toolInput"] = json.loads("".join(self.inputs[call_id]))
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


class History:
    """Bounded live branches; sequential tool turns replace their parent snapshot."""
    def __init__(self):
        self.branches = []

    def resume(self, scope, client):
        for snapshot in reversed(self.branches):
            offset = len(snapshot.client)
            if (scope != snapshot.scope or len(client) <= offset + 1
                    or client[:offset] != snapshot.client or client[offset] != snapshot.echo):
                continue
            tail = client[offset + 1:]
            calls = [b["toolCallId"] for b in snapshot.assistant if b["type"] == "tool_call"]
            results = [m["content"]["toolCallId"] for m in tail[:len(calls)] if m["role"] == "tool"]
            if (len(results) != len(calls) or len(set(results)) != len(results) or set(results) != set(calls)
                    or any(m["role"] != "user" for m in tail[len(calls):])):
                continue
            return snapshot, snapshot.messages + [{"role": "assistant", "content": snapshot.assistant}] + tail
        return None, None

    def record(self, scope, client, messages, blocks, expected, parent=None):
        self.branches = [s for s in self.branches if s is not parent]
        self.branches.append(Snapshot(scope, copy.deepcopy(client), messages, blocks, expected))
        self.branches = self.branches[-8:]


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
