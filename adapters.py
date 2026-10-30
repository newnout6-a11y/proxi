"""Dialect adapters: OpenAI Chat, OpenAI Responses, Anthropic Messages."""
import json
import re
import time
import uuid

import apierr


def approx_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


FINISH_MAP = {"stop": "stop", "tool_calls": "tool_calls", "hitl": "stop", "timeout": "length", "cancelled": "stop", "max_exec": "length"}


class _Base:
    def __init__(self, parsed, writer=None):
        self.parsed = parsed
        self.writer = writer
        # Initial and final usage must estimate the same complete request.
        self._prompt_text = getattr(parsed, "system", "") + "\n" + getattr(parsed, "prompt", "")
        self.text = ""
        self.thinking = ""
        self.credits = 0.0
        self.cache_read = 0
        self.cache_write = 0
        self.reasoning_tokens = 0
        self.input_tokens = None
        self.output_tokens = None
        self.exec_lines = 0
        self.tool_calls = []
        self._custom_tools = {
            tool.get("function", {}).get("name")
            for tool in getattr(parsed, "tools", [])
            if tool.get("x-client-type") == "custom"
            or tool.get("function", {}).get("x-client-type") == "custom"
        }

    def feed(self, event):
        kind = event.get("type")
        if kind == "thinking":
            self.thinking += event["delta"]
            if not self.parsed.show_thinking:
                return
            self._think(event["delta"])
            if event.get("signature"):
                self._signature(event["signature"])
        elif kind == "thinking_start":
            if self.parsed.show_thinking:
                self._think_start()
        elif kind == "thinking_signature":
            if self.parsed.show_thinking:
                self._signature(event.get("signature", ""))
        elif kind == "text":
            self.text += event["delta"]
            self._text(event["delta"])
        elif kind == "tool_call":
            self._feed_tool_call(event)
        elif kind == "credits":
            self.credits += event.get("value", 0.0)
            usage = event.get("usage") or {}
            if usage.get("cache_read") is not None:
                self.cache_read = int(usage["cache_read"])
            if usage.get("cache_write") is not None:
                self.cache_write = int(usage["cache_write"])
            if usage.get("input_tokens") is not None:
                self.input_tokens = int(usage["input_tokens"])
            if usage.get("output_tokens") is not None:
                self.output_tokens = int(usage["output_tokens"])
            output_tokens = self.output_tokens
            if self.thinking:
                self.reasoning_tokens = approx_tokens(self.thinking)
            elif output_tokens is not None:
                self.reasoning_tokens = max(0, int(output_tokens) - approx_tokens(self.text))
        elif kind == "hitl":
            block = ("\n\n" if self.text else "") + event["question"]
            self.text += block
            self._text(block)
        elif kind == "exec_start":
            block = f"\n\n$ {event['cmd']}\n"
            self.text += block
            self._text(block)
        elif kind == "exec_result":
            block = f"\x60\x60\x60text\n{event['output']}\n\x60\x60\x60\n"
            self.text += block
            self._text(block)

    def usage(self, prompt_text=None):
        sent = getattr(self.parsed, "_full_prompt_text", None)
        prompt = self.input_tokens if self.input_tokens is not None else approx_tokens(
            sent or (self._prompt_text if prompt_text is None else prompt_text))
        if self.cache_read or self.cache_write:
            prompt = max(prompt, (self.cache_read or 0) + (self.cache_write or 0) + (self.input_tokens or 0))
        tool_text = "".join(c.get("name", "") + c.get("arguments", "") for c in self.tool_calls)
        base_completion = approx_tokens(self.text) + approx_tokens(tool_text)
        reasoning = self.reasoning_tokens or approx_tokens(self.thinking)
        if self.output_tokens is not None:
            completion = max(int(self.output_tokens), base_completion + reasoning)
        else:
            completion = base_completion + reasoning
        return prompt, completion

    def _text(self, delta):
        raise NotImplementedError

    def _think(self, delta):
        pass

    def _think_start(self):
        pass

    def _signature(self, signature):
        pass

    def _feed_tool_call(self, event):
        call_id = str(event.get("id") or "call_0")
        name = str(event.get("name") or "tool")
        arguments = event.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        current = next((call for call in self.tool_calls if call["id"] == call_id), None)
        is_new = current is None
        if current is None:
            current = {"id": call_id, "name": name, "arguments": ""}
            if event.get("tool_type") == "custom" or name in self._custom_tools:
                current["tool_type"] = "custom"
            self.tool_calls.append(current)
        if arguments.startswith(current["arguments"]):
            delta = arguments[len(current["arguments"]):]
        else:
            delta = arguments
        current["arguments"] = arguments
        self._tool_call(self.tool_calls.index(current), current, delta, new=is_new)

    def _tool_call(self, index, call, delta, new=False):
        pass

    def payload(self, reason, prompt_text=None):
        raise NotImplementedError

    def begin(self):
        raise NotImplementedError

    def end(self, reason, prompt_text=None):
        raise NotImplementedError


class ChatAdapter(_Base):
    def __init__(self, parsed, writer=None):
        super().__init__(parsed, writer)
        self.id = "chatcmpl-" + uuid.uuid4().hex[:20]
        self.created = int(time.time())

    def _chunk(self, delta, finish=None):
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                "model": self.parsed.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def begin(self):
        if self.writer:
            self.writer.data(self._chunk({"role": "assistant"}))

    def _text(self, delta):
        if self.writer:
            self.writer.data(self._chunk({"content": delta}))

    def _think(self, delta):
        if self.writer:
            self.writer.data(self._chunk({"reasoning_content": delta}))

    def _think_start(self):
        # Display-only label: it is streamed but never stored in self.thinking.
        label = getattr(self.parsed, "thinking_placeholder", "")
        if label and self.writer:
            self.writer.data(self._chunk({"reasoning_content": label}))

    def _tool_call(self, index, call, delta, new=False):
        if not self.writer:
            return
        item = {"index": index}
        if new:
            item.update({"id": call["id"], "type": "function"})
        item["function"] = {"arguments": delta}
        if new:
            item["function"]["name"] = call["name"]
        self.writer.data(self._chunk({"tool_calls": [item]}))

    def end(self, reason, prompt_text=None):
        finish = FINISH_MAP.get(reason, "stop")
        if self.writer:
            self.writer.data(self._chunk({}, finish))
            prompt, completion = self.usage(prompt_text)
            reasoning = self.reasoning_tokens or approx_tokens(self.thinking)
            usage_dict = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {
                    "cached_tokens": self.cache_read or 0,
                    "cache_read_tokens": self.cache_read or 0,
                    "cache_write_tokens": self.cache_write or 0,
                    "cache_creation_input_tokens": self.cache_write or 0,
                },
                "input_tokens_details": {
                    "cached_tokens": self.cache_read or 0,
                    "cache_read_tokens": self.cache_read or 0,
                    "cache_write_tokens": self.cache_write or 0,
                    "cache_creation_input_tokens": self.cache_write or 0,
                },
                "completion_tokens_details": {
                    "reasoning_tokens": reasoning,
                },
                "output_tokens_details": {
                    "reasoning_tokens": reasoning,
                },
                "credits_used": round(self.credits, 5),
                "estimated": True,
            }
            if self.parsed.wants_usage:
                self.writer.data({"id": self.id, "object": "chat.completion.chunk",
                                  "created": self.created, "model": self.parsed.model,
                                  "choices": [],
                                  "usage": usage_dict})
            self.writer.done()
        return self.payload(reason, prompt_text)

    def payload(self, reason, prompt_text=None):
        prompt, completion = self.usage(prompt_text)
        reasoning = self.reasoning_tokens or approx_tokens(self.thinking)
        return {"id": self.id, "object": "chat.completion", "created": self.created,
                "model": self.parsed.model,
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": self.text or (None if self.tool_calls else ""),
                                                     **({"reasoning_content": self.thinking} if (self.thinking and self.parsed.show_thinking) else {}),
                                                     **({"tool_calls": [{"id": call["id"], "type": "function",
                                                                          "function": {"name": call["name"],
                                                                                       "arguments": call["arguments"]}}
                                                                        for call in self.tool_calls]}
                                                        if self.tool_calls else {})},
                             "finish_reason": FINISH_MAP.get(reason, "tool_calls" if self.tool_calls else "stop")}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                          "total_tokens": prompt + completion,
                          "prompt_tokens_details": {
                              "cached_tokens": self.cache_read or 0,
                              "cache_read_tokens": self.cache_read or 0,
                              "cache_write_tokens": self.cache_write or 0,
                              "cache_creation_input_tokens": self.cache_write or 0,
                          },
                          "input_tokens_details": {
                              "cached_tokens": self.cache_read or 0,
                              "cache_read_tokens": self.cache_read or 0,
                              "cache_write_tokens": self.cache_write or 0,
                              "cache_creation_input_tokens": self.cache_write or 0,
                          },
                          "completion_tokens_details": {"reasoning_tokens": reasoning},
                          "output_tokens_details": {"reasoning_tokens": reasoning},
                          "credits_used": round(self.credits, 5), "estimated": True}}


class ResponsesAdapter(_Base):
    def __init__(self, parsed, writer=None):
        super().__init__(parsed, writer)
        self.id = "resp_" + uuid.uuid4().hex[:20]
        self.created = int(time.time())
        self.seq = 0
        self.item_id = "msg_" + uuid.uuid4().hex[:20]
        self.reasoning_id = "rs_" + uuid.uuid4().hex[:20]
        self._items = []
        self._indices = {}
        self._custom_inputs = {}
        self._tool_definitions = {
            tool.get("function", {}).get("name"): tool.get("function", {})
            for tool in getattr(parsed, "tools", [])
        }

    def _emit(self, name, payload):
        if not self.writer:
            return
        self.seq += 1
        payload = {"type": name, "sequence_number": self.seq, **payload}
        self.writer.event(name, payload)

    def begin(self):
        self._emit("response.created", {"response": {"id": self.id, "object": "response",
                                                     "created_at": self.created, "model": self.parsed.model,
                                                     "status": "in_progress", "output": []}})

    def _ensure_item(self, kind, item_id, call=None):
        if item_id in self._indices:
            return self._indices[item_id]
        index = len(self._items)
        self._indices[item_id] = index
        self._items.append((kind, item_id, call))
        self._emit("response.output_item.added", {"output_index": index,
                                                  "item": self._item(index, started=True)})
        if kind == "message":
            self._emit("response.content_part.added", {"item_id": item_id, "output_index": index,
                                                       "content_index": 0,
                                                       "part": {"type": "output_text", "text": "", "annotations": []}})
        elif kind == "reasoning":
            self._emit("response.reasoning_summary_part.added", {
                "item_id": item_id, "output_index": index, "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            })
        return index

    @staticmethod
    def _custom_input(call):
        arguments = call["arguments"]
        try:
            value = json.loads(arguments)
        except (ValueError, TypeError):
            # Normalized envelopes can arrive in several fragments. Do not send
            # the partially escaped JSON wrapper as the actual custom-tool input.
            return None if re.match(r'^\s*\{\s*"input"\s*:', arguments) else arguments
        if isinstance(value, dict) and isinstance(value.get("input"), str):
            return value["input"]
        return arguments

    def _item(self, index, *, started=False, status="completed"):
        kind, item_id, call = self._items[index]
        status = "in_progress" if started else status
        if kind == "reasoning":
            return {"id": item_id, "type": kind, "status": status,
                    "summary": [] if started else [{"type": "summary_text", "text": self.thinking}]}
        if kind == "message":
            return {"id": item_id, "type": kind, "role": "assistant", "status": status,
                    "content": [] if started else [{"type": "output_text", "text": self.text, "annotations": []}]}
        item = {"id": item_id, "type": kind, "call_id": call["id"], "name": call["name"], "status": status}
        definition = self._tool_definitions.get(call["name"], {})
        if definition.get("x-namespace"):
            item["namespace"] = definition["x-namespace"]
            item["name"] = definition.get("x-client-name") or call["name"]
        if kind == "custom_tool_call":
            value = "" if started else self._custom_input(call)
            if value is None:
                raise apierr.upstream_error("Retool returned incomplete custom tool input")
            item["input"] = value
        else:
            item["arguments"] = "" if started else call["arguments"]
        return item

    def _text(self, delta):
        index = self._ensure_item("message", self.item_id)
        self._emit("response.output_text.delta", {"item_id": self.item_id, "output_index": index,
                                                  "content_index": 0, "delta": delta})

    def _think(self, delta):
        index = self._ensure_item("reasoning", self.reasoning_id)
        self._emit("response.reasoning_summary_text.delta", {"item_id": self.reasoning_id,
                                                             "output_index": index,
                                                             "summary_index": 0, "delta": delta})

    def _think_start(self):
        label = getattr(self.parsed, "thinking_placeholder", "")
        if label and self.writer:
            self._think(label)

    def _tool_call(self, index, call, delta, new=False):
        custom = call.get("tool_type") == "custom"
        kind = "custom_tool_call" if custom else "function_call"
        output_index = self._ensure_item(kind, call["id"], call)
        if custom:
            current = self._custom_input(call)
            if current is None:
                return
            previous = self._custom_inputs.get(call["id"], "")
            delta = current[len(previous):] if current.startswith(previous) else current
            self._custom_inputs[call["id"]] = current
        if delta:
            self._emit("response.custom_tool_call_input.delta" if custom else "response.function_call_arguments.delta", {
                "item_id": call["id"], "output_index": output_index, "delta": delta,
            })

    def end(self, reason, prompt_text=None):
        if not self._items:
            self._ensure_item("message", self.item_id)
        status = "incomplete" if reason == "timeout" else "completed"
        for index, (kind, item_id, call) in enumerate(self._items):
            fields = {"item_id": item_id, "output_index": index}
            if kind == "message":
                fields["content_index"] = 0
                self._emit("response.output_text.done", {**fields, "text": self.text})
                self._emit("response.content_part.done", {
                    **fields, "part": {"type": "output_text", "text": self.text, "annotations": []},
                })
            elif kind == "reasoning":
                fields["summary_index"] = 0
                self._emit("response.reasoning_summary_text.done", {**fields, "text": self.thinking})
                self._emit("response.reasoning_summary_part.done", {
                    **fields, "part": {"type": "summary_text", "text": self.thinking},
                })
            elif kind == "custom_tool_call":
                self._emit("response.custom_tool_call_input.done", {**fields, "input": self._item(index)["input"]})
            else:
                self._emit("response.function_call_arguments.done", {**fields, "arguments": call["arguments"]})
            self._emit("response.output_item.done", {
                "output_index": index, "item": self._item(index, status=status),
            })
        final = self.payload(reason, prompt_text)
        self._emit("response.incomplete" if reason == "timeout" else "response.completed", {"response": final})
        if self.writer:
            self.writer.done()
        return final

    def payload(self, reason, prompt_text=None):
        prompt, completion = self.usage(prompt_text)
        reasoning = self.reasoning_tokens or approx_tokens(self.thinking)
        if not self._items:
            self._ensure_item("message", self.item_id)
        status = "incomplete" if reason == "timeout" else "completed"
        return {"id": self.id, "object": "response", "created_at": self.created,
                "model": self.parsed.model,
                "status": status,
                "output": [self._item(index, status=status) for index in range(len(self._items))],
                "output_text": self.text,
                "usage": {"input_tokens": prompt, "output_tokens": completion,
                          "total_tokens": prompt + completion,
                          "input_token_details": {"cached_tokens": self.cache_read},
                          "output_token_details": {"reasoning_tokens": reasoning},
                          "credits_used": round(self.credits, 5), "estimated": True}}


class AnthropicAdapter(_Base):
    def __init__(self, parsed, writer=None):
        super().__init__(parsed, writer)
        self.id = "msg_" + uuid.uuid4().hex[:20]
        self.open_kind = None
        self.index = 0
        self._blocks = []
        self._open_indices = set()
        self._tool_indices = {}

    def _emit(self, name, payload):
        if self.writer:
            self.writer.event(name, payload)

    def begin(self):
        self._emit("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.parsed.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.usage()[0], "output_tokens": 0}}})
        self._emit("ping", {"type": "ping"})

    def _close_blocks(self):
        for index in sorted(self._open_indices):
            self._emit("content_block_stop", {"type": "content_block_stop", "index": index})
        self._open_indices.clear()
        self.open_kind = None

    def _open(self, kind):
        if self.open_kind == kind and kind != "tool":
            return self._blocks[self.index]
        self._close_blocks()
        self.open_kind = kind
        # Retool does not normally provide an Anthropic signature. An empty
        # schema field is not a fabricated cryptographic signature; genuine
        # signature events, when available, are relayed unchanged below.
        block = ({"type": "thinking", "thinking": "", "signature": ""}
                 if kind == "thinking" else {"type": "text", "text": ""})
        self.index = len(self._blocks)
        self._blocks.append(block)
        self._open_indices.add(self.index)
        self._emit("content_block_start", {"type": "content_block_start", "index": self.index,
                                           "content_block": dict(block)})
        return block

    def _think_start(self):
        # An open thinking block lets the client show "thinking" while the
        # provider reasons silently; the later signature attaches to it.
        self._open("thinking")

    def _think(self, delta):
        block = self._open("thinking")
        block["thinking"] += delta
        self._emit("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "thinking_delta", "thinking": delta}})

    def _signature(self, signature):
        if signature and self.open_kind == "thinking":
            self._blocks[self.index]["signature"] += signature
            self._emit("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                               "delta": {"type": "signature_delta", "signature": signature}})

    def _text(self, delta):
        block = self._open("text")
        block["text"] += delta
        self._emit("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "text_delta", "text": delta}})

    def _tool_call(self, index, call, delta, new=False):
        if new:
            if self.open_kind != "tool":
                self._close_blocks()
            self.open_kind = "tool"
            self.index = len(self._blocks)
            self._blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}})
            self._tool_indices[call["id"]] = self.index
            self._open_indices.add(self.index)
            self._emit("content_block_start", {"type": "content_block_start", "index": self.index,
                                               "content_block": {"type": "tool_use", "id": call["id"],
                                                                 "name": call["name"], "input": {}}})
        block_index = self._tool_indices[call["id"]]
        try:
            self._blocks[block_index]["input"] = json.loads(call["arguments"] or "{}")
        except ValueError:
            pass  # streamed JSON fragments are finalized by a subsequent delta
        if delta:
            self._emit("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                               "delta": {"type": "input_json_delta", "partial_json": delta}})

    def _stop_reason(self, reason):
        if reason == "timeout":
            return "max_tokens"
        if reason == "tool_calls" or self.tool_calls:
            return "tool_use"
        return "end_turn"

    def end(self, reason, prompt_text=None):
        usage = {"input_tokens": self.usage(prompt_text)[0], "output_tokens": self.usage(prompt_text)[1],
                 "cache_read_input_tokens": self.cache_read, "cache_creation_input_tokens": self.cache_write}
        if not self._blocks:
            self._open("text")
        self._close_blocks()
        self._emit("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": self._stop_reason(reason), "stop_sequence": None},
                                     "usage": usage})
        self._emit("message_stop", {"type": "message_stop"})
        if self.writer:
            self.writer.done()
        return self.payload(reason, prompt_text)

    def payload(self, reason, prompt_text=None):
        prompt, completion = self.usage(prompt_text)
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.parsed.model,
                "content": [dict(block) for block in self._blocks] or [{"type": "text", "text": ""}],
                "stop_reason": self._stop_reason(reason), "stop_sequence": None,
                "usage": {"input_tokens": prompt, "output_tokens": completion,
                          "cache_read_input_tokens": self.cache_read,
                          "cache_creation_input_tokens": self.cache_write,
                          "credits_used": round(self.credits, 5), "estimated": True}}


def for_dialect(dialect, parsed, writer=None):
    if dialect == "anthropic":
        return AnthropicAdapter(parsed, writer)
    if dialect == "responses":
        return ResponsesAdapter(parsed, writer)
    return ChatAdapter(parsed, writer)


def error_payload(dialect, err):
    if dialect == "anthropic":
        return err.anthropic_envelope()
    return err.openai_envelope()
