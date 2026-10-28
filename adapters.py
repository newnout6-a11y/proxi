"""Dialect adapters: OpenAI Chat, OpenAI Responses, Anthropic Messages."""
import json
import time
import uuid


def approx_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


FINISH_MAP = {"stop": "stop", "tool_calls": "tool_calls", "hitl": "stop", "timeout": "length", "cancelled": "stop", "max_exec": "length"}


class _Base:
    def __init__(self, parsed, writer=None):
        self.parsed = parsed
        self.writer = writer
        self.text = ""
        self.thinking = ""
        self.credits = 0.0
        self.exec_lines = 0
        self.tool_calls = []

    def feed(self, event):
        kind = event.get("type")
        if kind == "thinking":
            self.thinking += event["delta"]
            if not self.parsed.show_thinking:
                return
            self._think(event["delta"])
        elif kind == "text":
            self.text += event["delta"]
            self._text(event["delta"])
        elif kind == "tool_call":
            self._feed_tool_call(event)
        elif kind == "credits":
            self.credits += event["value"]
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

    def usage(self, prompt_text=""):
        prompt = approx_tokens(prompt_text) + approx_tokens(self.parsed.system)
        completion = approx_tokens(self.text)
        return prompt, completion

    def _text(self, delta):
        raise NotImplementedError

    def _think(self, delta):
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
            self.tool_calls.append(current)
        if arguments.startswith(current["arguments"]):
            delta = arguments[len(current["arguments"]):]
        else:
            delta = arguments
        current["arguments"] = arguments
        self._tool_call(len(self.tool_calls) - 1, current, delta, new=is_new)

    def _tool_call(self, index, call, delta, new=False):
        pass

    def payload(self, reason, prompt_text=""):
        raise NotImplementedError

    def begin(self):
        raise NotImplementedError

    def end(self, reason, prompt_text=""):
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

    def end(self, reason, prompt_text=""):
        finish = FINISH_MAP.get(reason, "stop")
        if self.writer:
            self.writer.data(self._chunk({}, finish))
            if self.parsed.wants_usage:
                prompt, completion = self.usage(prompt_text)
                self.writer.data({"id": self.id, "object": "chat.completion.chunk",
                                  "created": self.created, "model": self.parsed.model,
                                  "choices": [],
                                  "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                            "total_tokens": prompt + completion,
                                            "credits_used": round(self.credits, 5), "estimated": True}})
            self.writer.done()
        return self.payload(reason, prompt_text)

    def payload(self, reason, prompt_text=""):
        prompt, completion = self.usage(prompt_text)
        return {"id": self.id, "object": "chat.completion", "created": self.created,
                "model": self.parsed.model,
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": None if self.tool_calls else self.text,
                                                     **({"reasoning_content": self.thinking} if self.thinking else {}),
                                                     **({"tool_calls": [{"id": call["id"], "type": "function",
                                                                          "function": {"name": call["name"],
                                                                                       "arguments": call["arguments"]}}
                                                                        for call in self.tool_calls]}
                                                        if self.tool_calls else {})},
                             "finish_reason": FINISH_MAP.get(reason, "tool_calls" if self.tool_calls else "stop")}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                          "total_tokens": prompt + completion,
                          "credits_used": round(self.credits, 5), "estimated": True}}


class ResponsesAdapter(_Base):
    def __init__(self, parsed, writer=None):
        super().__init__(parsed, writer)
        self.id = "resp_" + uuid.uuid4().hex[:20]
        self.created = int(time.time())
        self.seq = 0
        self.item_id = "msg_" + uuid.uuid4().hex[:20]
        self.reasoning_id = "rs_" + uuid.uuid4().hex[:20]

    def _emit(self, name, payload):
        if not self.writer:
            return
        self.seq += 1
        payload = {"type": name, "sequence_number": self.seq, **payload}
        self.writer.event(name, payload)

    def begin(self):
        self._emit("response.created", {"response": {"id": self.id, "object": "response",
                                                     "created_at": self.created, "model": self.parsed.model,
                                                     "status": "in_progress"}})
        self._emit("response.output_item.added", {"output_index": 0,
                                                  "item": {"id": self.item_id, "type": "message",
                                                           "role": "assistant", "content": []}})
        self._emit("response.content_part.added", {"item_id": self.item_id, "output_index": 0,
                                                   "content_index": 0,
                                                   "part": {"type": "output_text", "text": ""}})

    def _text(self, delta):
        self._emit("response.output_text.delta", {"item_id": self.item_id, "output_index": 0,
                                                  "content_index": 0, "delta": delta})

    def _think(self, delta):
        self._emit("response.reasoning_summary_text.delta", {"item_id": self.reasoning_id,
                                                             "output_index": 0,
                                                             "summary_index": 0, "delta": delta})

    def _tool_call(self, index, call, delta, new=False):
        output_index = index + 1
        if new:
            self._emit("response.output_item.added", {
                "output_index": output_index,
                "item": {"id": call["id"], "type": "function_call", "call_id": call["id"],
                         "name": call["name"], "arguments": ""},
            })
        if delta:
            self._emit("response.function_call_arguments.delta", {
                "item_id": call["id"], "output_index": output_index, "delta": delta,
            })

    def end(self, reason, prompt_text=""):
        prompt, completion = self.usage(prompt_text)
        self._emit("response.output_text.done", {"item_id": self.item_id, "output_index": 0,
                                                 "content_index": 0, "text": self.text})
        for index, call in enumerate(self.tool_calls):
            output_index = index + 1
            self._emit("response.function_call_arguments.done", {
                "item_id": call["id"], "output_index": output_index, "arguments": call["arguments"]
            })
            self._emit("response.output_item.done", {
                "output_index": output_index,
                "item": {"id": call["id"], "type": "function_call", "call_id": call["id"],
                         "name": call["name"], "arguments": call["arguments"], "status": "completed"},
            })
        final = self.payload(reason, prompt_text)
        self._emit("response.completed", {"response": final})
        if self.writer:
            self.writer.done()
        return final

    def payload(self, reason, prompt_text=""):
        prompt, completion = self.usage(prompt_text)
        return {"id": self.id, "object": "response", "created_at": self.created,
                "model": self.parsed.model,
                "status": "incomplete" if reason == "timeout" else "completed",
                "output": ([{"id": self.reasoning_id, "type": "reasoning",
                             "summary": [{"type": "summary_text", "text": self.thinking}]}]
                           if self.thinking else [])
                + [{"id": call["id"], "type": "function_call", "call_id": call["id"],
                   "name": call["name"], "arguments": call["arguments"], "status": "completed"}
                   for call in self.tool_calls]
                + [{"id": self.item_id, "type": "message", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": self.text}]}],
                "output_text": self.text,
                "usage": {"input_tokens": prompt, "output_tokens": completion,
                          "total_tokens": prompt + completion,
                          "credits_used": round(self.credits, 5), "estimated": True}}


class AnthropicAdapter(_Base):
    def __init__(self, parsed, writer=None):
        super().__init__(parsed, writer)
        self.id = "msg_" + uuid.uuid4().hex[:20]
        self.open_kind = None
        self.index = 0

    def _emit(self, name, payload):
        if self.writer:
            self.writer.event(name, payload)

    def begin(self):
        self._emit("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.parsed.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.usage()[0], "output_tokens": 0}}})
        self._emit("ping", {"type": "ping"})

    def _open(self, kind):
        if self.open_kind == kind:
            return
        if self.open_kind is not None:
            self._emit("content_block_stop", {"type": "content_block_stop", "index": self.index})
            self.index += 1
        self.open_kind = kind
        block = {"type": "thinking", "thinking": ""} if kind == "thinking" else {"type": "text", "text": ""}
        self._emit("content_block_start", {"type": "content_block_start", "index": self.index,
                                           "content_block": block})

    def _think(self, delta):
        self._open("thinking")
        self._emit("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "thinking_delta", "thinking": delta}})

    def _text(self, delta):
        self._open("text")
        self._emit("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "text_delta", "text": delta}})

    def _tool_call(self, index, call, delta, new=False):
        if new:
            if self.open_kind is not None:
                self._emit("content_block_stop", {"type": "content_block_stop", "index": self.index})
                self.index += 1
            self.open_kind = "tool"
            self._emit("content_block_start", {"type": "content_block_start", "index": self.index,
                                               "content_block": {"type": "tool_use", "id": call["id"],
                                                                 "name": call["name"], "input": {}}})
        if delta:
            self._emit("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                               "delta": {"type": "input_json_delta", "partial_json": delta}})

    def _stop_reason(self, reason):
        if reason == "timeout":
            return "max_tokens"
        if reason == "tool_calls" or self.tool_calls:
            return "tool_use"
        return "end_turn"

    def end(self, reason, prompt_text=""):
        usage = {"input_tokens": self.usage(prompt_text)[0], "output_tokens": self.usage(prompt_text)[1]}
        if self.open_kind is None:
            self._open("text")
        self._emit("content_block_stop", {"type": "content_block_stop", "index": self.index})
        self._emit("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": self._stop_reason(reason), "stop_sequence": None},
                                     "usage": usage})
        self._emit("message_stop", {"type": "message_stop"})
        if self.writer:
            self.writer.done()
        return self.payload(reason, prompt_text)

    def payload(self, reason, prompt_text=""):
        prompt, completion = self.usage(prompt_text)
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.parsed.model,
                "content": ([{"type": "thinking", "thinking": self.thinking}] if self.thinking else [])
                + [{"type": "tool_use", "id": call["id"], "name": call["name"],
                    "input": json.loads(call["arguments"] or "{}")}
                   for call in self.tool_calls]
                + [{"type": "text", "text": self.text}],
                "stop_reason": self._stop_reason(reason), "stop_sequence": None,
                "usage": {"input_tokens": prompt, "output_tokens": completion,
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
