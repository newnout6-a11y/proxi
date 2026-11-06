"""Dialect adapters: OpenAI Chat, OpenAI Responses, Anthropic Messages."""
import json
import math
import re
import time
import uuid

import apierr
import structured
from credits import token_counter as _token_counter


def approx_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


# Characters per token of Claude's tokenizer over what Claude Desktop and Claude Code send (code, JSON, tool results,
# a long system prompt): 2.67 to 2.68 on four saved Desktop requests of 779k to 829k characters that upstream counted at
# 290k to 311k tokens (a5, 2026-11-06). The divisor 4 of approx_tokens said two thirds of that, so the context meter of
# Claude Desktop showed half of the real tokens and nothing compacted on its own up to 492k real tokens.
CHARS_PER_TOKEN = 2.7


def estimate_tokens(chars) -> int:
    return max(0, round(chars / CHARS_PER_TOKEN))


def request_chars(parsed) -> int:
    """The size of a client's request as count_tokens measures it: the system text, the flat messages, the tool schemas."""
    size = len(getattr(parsed, "system", "") or "") + 1 + len(getattr(parsed, "prompt", "") or "")
    tools = getattr(parsed, "tools", None)
    if tools:
        size += 1 + len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return size


def count_input_tokens(chars, anchor=None):
    """(tokens, basis) of a request of `chars` characters.

    `anchor` is (characters, tokens) of the last request of the same conversation that upstream really counted. A request
    that grew from it is that count plus an estimate of what was added: the real part includes what the relay added to the
    prompt and what the estimate cannot see (images, the tokenizer's own habits). One that shrank (a compaction, an edit)
    is scaled by the same ratio. Without an anchor it is the estimate for the whole request. basis says which."""
    if anchor and anchor[0] > 0 and anchor[1] > 0:
        anchor_chars, anchor_tokens = anchor
        if chars >= anchor_chars:
            return anchor_tokens + estimate_tokens(chars - anchor_chars), "last-usage"
        return max(0, round(anchor_tokens * chars / anchor_chars)), "last-usage"
    return estimate_tokens(chars), "estimate"


FINISH_MAP = {"stop": "stop", "tool_calls": "tool_calls", "hitl": "stop", "timeout": "length", "length": "length",
              "cancelled": "stop", "max_exec": "length"}
# The reasons a reply ends before it is whole: the turn ran out of time, or the output stopped (the cap, inside a call).
CUT_REASONS = ("timeout", "length")


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
        self._reasoning_reported = False
        self._attempt_observed = False
        self._attempt_credited = False
        self._prior_cost_unknown = False
        self.input_tokens = None
        self.output_tokens = None
        self.exec_lines = 0
        self.tool_calls = []
        # The client's stop sequence the reply ended at (StopSequenceAdapter); None when it ended otherwise.
        self.stop_sequence = None
        self._usage_offsets = (0, 0, 0)
        self._custom_tools = {
            tool.get("function", {}).get("name")
            for tool in getattr(parsed, "tools", [])
            if tool.get("x-client-type") == "custom"
            or tool.get("function", {}).get("x-client-type") == "custom"
        }

    def feed(self, event):
        kind = event.get("type")
        if kind == "usage_start":
            # One client response may include several paid upstream attempts.
            # Context size describes the latest one; credits cover all of them.
            if self._attempt_observed and not self._attempt_credited:
                self._prior_cost_unknown = True
            self._attempt_observed = self._attempt_credited = False
            self.cache_read = self.cache_write = self.reasoning_tokens = 0
            self._reasoning_reported = False
            self.input_tokens = self.output_tokens = None
            self._usage_offsets = (len(self.text), len(self.thinking), len(self.tool_calls))
            self._usage_boundary()
        elif kind == 'usage':
            self._attempt_observed = True
            self._set_usage(event.get('usage') or {})
        elif kind == "thinking":
            self._attempt_observed = True
            self.thinking += event["delta"]
            if not self.parsed.show_thinking:
                return
            self._think(event["delta"])
            if event.get("signature"):
                self._signature(event["signature"])
        elif kind == "thinking_start":
            if self.parsed.show_thinking:
                self._think_start()
        elif kind == "thinking_status":
            if self.parsed.show_thinking:
                self._think_status()
        elif kind == "thinking_signature":
            if self.parsed.show_thinking:
                self._signature(event.get("signature", ""))
        elif kind == "text":
            self._attempt_observed = True
            self.text += event["delta"]
            self._text(event["delta"])
        elif kind == "tool_call":
            self._attempt_observed = True
            self._feed_tool_call(event)
        elif kind == "credits":
            self._attempt_observed = True
            value = event.get('value')
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                self.credits += value
                self._attempt_credited = True
            usage = event.get("usage") or {}
            self._set_usage(usage)
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

    def _set_usage(self, usage):
        if not usage:
            return
        # Counts are snapshots. A visible summary is not an exact hidden-token count.
        for key in ('cache_read', 'cache_write', 'input_tokens', 'output_tokens'):
            if key in usage:
                counter = _token_counter(usage[key])
                if counter is not None:
                    setattr(self, key, counter)
        if 'reasoning_tokens' in usage:
            reasoning = _token_counter(usage['reasoning_tokens'])
            self._reasoning_reported = reasoning is not None
            self.reasoning_tokens = reasoning if self._reasoning_reported else 0
        if self.output_tokens is not None and self.reasoning_tokens > self.output_tokens:
            self._reasoning_reported = False
            self.reasoning_tokens = 0

    def _reasoning_details(self):
        return {'reasoning_tokens': self.reasoning_tokens} if self._reasoning_reported else {}

    def _price_usage(self):
        complete = self._attempt_credited and not self._prior_cost_unknown
        return {'credits_used': round(self.credits, 5) if complete else None,
                'reported_credits_used': round(self.credits, 5), 'credits_complete': complete}

    def _completion_estimate(self):
        text_at, _, calls_at = self._usage_offsets
        tool_text = "".join(c.get("name", "") + c.get("arguments", "") for c in self.tool_calls[calls_at:])
        return approx_tokens(self.text[text_at:]) + approx_tokens(tool_text)

    def _reasoning_usage(self):
        reasoning = (self.reasoning_tokens if self._reasoning_reported else
                     approx_tokens(self.thinking[self._usage_offsets[1]:]))
        return min(reasoning, self.output_tokens) if self.output_tokens is not None else reasoning

    def usage(self, prompt_text=None):
        sent = getattr(self.parsed, "_full_prompt_text", None)
        prompt = self.input_tokens if self.input_tokens is not None else approx_tokens(
            sent or (self._prompt_text if prompt_text is None else prompt_text))
        if self.cache_read or self.cache_write:
            prompt = max(prompt, (self.cache_read or 0) + (self.cache_write or 0) + (self.input_tokens or 0))
        base_completion = self._completion_estimate()
        reasoning = self._reasoning_usage()
        if self.output_tokens is not None:
            completion = int(self.output_tokens)
        else:
            completion = base_completion + reasoning
        return prompt, completion

    def _text(self, delta):
        raise NotImplementedError

    def _think(self, delta):
        pass

    def _think_start(self):
        pass

    def _think_status(self):
        pass

    def _signature(self, signature):
        pass

    def _usage_boundary(self):
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
        elif self.writer is not None and self._streams_arguments(current):
            # What the client has been sent cannot be taken back, and a delta that does not continue it would break
            # its JSON (the whole new text used to be sent after the old: C13). The call keeps the text the client has.
            delta, arguments = "", current["arguments"]
        else:
            delta = arguments
        current["arguments"] = arguments
        # True once upstream has ended the call: its arguments are complete (the orchestrator says so with "done").
        current["done"] = bool(event.get("done"))
        self._tool_call(self.tool_calls.index(current), current, delta, new=is_new)

    def _tool_call(self, index, call, delta, new=False):
        pass

    @staticmethod
    def _streams_arguments(call):
        """Whether the adapter sends the text of this call's arguments to the client as it arrives."""
        return True

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
        self._thinking_indicated = False

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
        if label and self.writer and not self._thinking_indicated:
            self.writer.data(self._chunk({"reasoning_content": label}))
            self._thinking_indicated = True

    def _think_status(self):
        self._think_start()

    def _usage_boundary(self):
        self._thinking_indicated = False

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
            usage_dict = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "cached_tokens": self.cache_read or 0,
                "cache_read_input_tokens": self.cache_read or 0,
                "cache_creation_input_tokens": self.cache_write or 0,
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
                "completion_tokens_details": self._reasoning_details(),
                "output_tokens_details": self._reasoning_details(),
                **self._price_usage(),
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
                          "cached_tokens": self.cache_read or 0,
                          "cache_read_input_tokens": self.cache_read or 0,
                          "cache_creation_input_tokens": self.cache_write or 0,
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
                          "completion_tokens_details": self._reasoning_details(),
                          "output_tokens_details": self._reasoning_details(),
                          **self._price_usage(), "estimated": True}}


class ResponsesAdapter(_Base):
    def __init__(self, parsed, writer=None):
        super().__init__(parsed, writer)
        self.id = "resp_" + uuid.uuid4().hex[:20]
        self.created = int(time.time())
        self.seq = 0
        self._begun = False
        self.item_id = "msg_" + uuid.uuid4().hex[:20]
        self.reasoning_id = "rs_" + uuid.uuid4().hex[:20]
        self._items = []
        self._indices = {}
        self._custom_inputs = {}
        # Whether the reply being closed was cut (CUT_REASONS): a custom call the output stopped inside has no input.
        self._cut = False
        # Items are closed as soon as nothing can change them, not all at the end (see _flush_ready). `_closed` maps the
        # output index of every item whose done events are out to its status; `_frozen` is the text a closed message or
        # reasoning item ended with; text and thinking that arrive after their item was closed begin a new item, which
        # starts at `_bases` in the text (or the thinking) of the whole reply.
        self._closed = {}
        self._frozen = {}
        self._bases = {}
        self._text_end = self._thinking_end = 0
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
        self._begun = True
        self._emit("response.created", {"response": {"id": self.id, "object": "response",
                                                     "created_at": self.created, "model": self.parsed.model,
                                                     "status": "in_progress", "output": []}})

    def fail(self, error, extra=None):
        """End a stream that failed after it started: an `error` event, then the terminal response.failed.

        codex-rs ignores `error`; without response.failed it sees the stream end before response.completed,
        asks again up to stream_max_retries times (every attempt paid) and never shows the message. Both events
        carry the same code, so a client that stops at the first one, as ai-sdk does, classifies it alike. The
        relay's own code stays in `relay_code`.
        """
        if not self.writer:
            return
        if not self._begun:
            self.begin()  # response.failed needs the id that response.created announces
        failure = error.responses_error()
        self._emit("error", {**error.responses_envelope(), **(extra or {}), **failure, "relay_code": error.code})
        self._emit("response.failed", {"response": {"id": self.id, "object": "response",
                                                    "created_at": self.created, "model": self.parsed.model,
                                                    "status": "failed", "output": [], "error": failure}})

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

    @staticmethod
    def _custom_streamable(arguments):
        """The part of a custom tool's input that no later fragment can change; None while that is not known yet.

        The model writes the input inside a JSON envelope, {"input": "..."}, and the client gets the unwrapped text.
        Text that starts an object (or is still blank) may yet turn out to be that envelope, so nothing of it is sent
        before the JSON is complete: the first fragments, `{` or `{"inp`, used to go out as input and were followed by
        the whole text again (Codex's apply_patch showed pieces of the wrapper while it streamed, C10). Anything
        else is the input as it stands, and streams."""
        try:
            value = json.loads(arguments)
        except (ValueError, TypeError):
            stripped = arguments.lstrip()
            return None if not stripped or stripped.startswith("{") else arguments
        return value["input"] if isinstance(value, dict) and isinstance(value.get("input"), str) else arguments

    def _whole(self, index):
        """Whether the item at `index` is whole: a message or reasoning item always is, a call when upstream ended it."""
        kind, _, call = self._items[index]
        if kind not in ("function_call", "custom_tool_call"):
            return True
        if call.get("done"):
            return True
        try:
            json.loads(call["arguments"] or "{}")
        except ValueError:
            return kind == "custom_tool_call" and self._custom_input(call) is not None
        return True

    def _statuses(self, reason):
        """The status of every item of a reply that ends for `reason`.

        A timeout leaves all of them incomplete. A reply the output stopped (the cap, a cut inside a call) has its last
        item incomplete, and every call that upstream did not end: the items before them were finished."""
        count = len(self._items)
        if reason == "timeout":
            return ["incomplete"] * count
        if reason == "length":
            return ["incomplete" if index == count - 1 or not self._whole(index) else "completed"
                    for index in range(count)]
        return ["completed"] * count

    def _text_of(self, item_id, kind):
        """The text of a message item (or the summary of a reasoning item): what it was closed with, else what it has so far."""
        if item_id in self._frozen:
            return self._frozen[item_id]
        return (self.text if kind == "message" else self.thinking)[self._bases.get(item_id, 0):]

    def _item(self, index, *, started=False, status="completed"):
        kind, item_id, call = self._items[index]
        status = "in_progress" if started else status
        if kind == "reasoning":
            return {"id": item_id, "type": kind, "status": status,
                    "summary": [] if started else [{"type": "summary_text", "text": self._text_of(item_id, kind)}]}
        if kind == "message":
            return {"id": item_id, "type": kind, "role": "assistant", "status": status,
                    "content": [] if started else [{"type": "output_text", "text": self._text_of(item_id, kind),
                                                    "annotations": []}]}
        item = {"id": item_id, "type": kind, "call_id": call["id"], "name": call["name"], "status": status}
        definition = self._tool_definitions.get(call["name"], {})
        if definition.get("x-namespace"):
            item["namespace"] = definition["x-namespace"]
            item["name"] = definition.get("x-client-name") or call["name"]
        if kind == "custom_tool_call":
            value = "" if started else self._custom_input(call)
            if value is None:
                if not self._cut:
                    raise apierr.upstream_error("Retool returned incomplete custom tool input")
                # The output stopped inside the envelope: nothing of the input is final, so it is what was sent.
                value = self._custom_inputs.get(call["id"], "")
            item["input"] = value
        else:
            item["arguments"] = "" if started else call["arguments"]
        return item

    def _text(self, delta):
        if self._indices.get(self.item_id) in self._closed:
            # A call that finished after this item closed it: the text that follows is a message of its own.
            self.item_id = "msg_" + uuid.uuid4().hex[:20]
            self._bases[self.item_id] = self._text_end
        index = self._ensure_item("message", self.item_id)
        self._emit("response.output_text.delta", {"item_id": self.item_id, "output_index": index,
                                                  "content_index": 0, "delta": delta})

    def _think(self, delta):
        if self._indices.get(self.reasoning_id) in self._closed:
            self.reasoning_id = "rs_" + uuid.uuid4().hex[:20]
            self._bases[self.reasoning_id] = self._thinking_end
        index = self._ensure_item("reasoning", self.reasoning_id)
        self._emit("response.reasoning_summary_text.delta", {"item_id": self.reasoning_id,
                                                             "output_index": index,
                                                             "summary_index": 0, "delta": delta})

    def _think_start(self):
        label = getattr(self.parsed, "thinking_placeholder", "")
        if label and self.writer:
            self._think(label)

    @staticmethod
    def _streams_arguments(call):
        # A custom call's input is unwrapped from its envelope and held back until that is known (_custom_streamable),
        # so the text of its arguments never reaches the client as it is.
        return call.get("tool_type") != "custom"

    def _tool_call(self, index, call, delta, new=False):
        custom = call.get("tool_type") == "custom"
        kind = "custom_tool_call" if custom else "function_call"
        output_index = self._ensure_item(kind, call["id"], call)
        if output_index in self._closed:
            return  # its done events are out: nothing can be added to the item
        if custom:
            current = self._custom_streamable(call["arguments"])
            previous = self._custom_inputs.get(call["id"], "")
            if current is None or not current.startswith(previous):
                return
            delta = current[len(previous):]
            self._custom_inputs[call["id"]] = current
        if delta:
            self._emit("response.custom_tool_call_input.delta" if custom else "response.function_call_arguments.delta", {
                "item_id": call["id"], "output_index": output_index, "delta": delta,
            })
        if call.get("done"):
            self._flush_ready()

    def _flush_ready(self):
        """Close the items that nothing can change any more, in output order, up to the last call upstream has ended.

        codex-rs starts a tool the moment the done event of its item arrives and files the item into its history in the
        order the done events arrive. Held to the end of the reply, the first call of a batch waited for all the others to
        be written: 0.45 to 2.8 s for five to eight short calls, 11 to 21 s when each wrote a script (a4 F13, Opus
        5 script-writing calls: the first one's done event came 20.9 s after the call was whole). Messages and reasoning
        before the call are closed first, so that the history keeps the order of the output; a call that is not ended
        holds back every call after it, because closing the later one would close it too, with half its arguments."""
        last = -1
        for index, (kind, _, call) in enumerate(self._items):
            if index in self._closed or kind not in ("function_call", "custom_tool_call"):
                continue
            if not call.get("done"):
                break
            last = index
        if last >= 0:
            self._finish_through(last)

    def _finish_through(self, last, statuses=None):
        for index in range(last + 1):
            if index not in self._closed:
                self._finish_item(index, statuses[index] if statuses else "completed")

    def _finish_item(self, index, status):
        kind, item_id, call = self._items[index]
        fields = {"item_id": item_id, "output_index": index}
        if kind == "message":
            text = self._frozen[item_id] = self._text_of(item_id, kind)
            self._text_end = len(self.text)
            fields["content_index"] = 0
            self._emit("response.output_text.done", {**fields, "text": text})
            self._emit("response.content_part.done", {
                **fields, "part": {"type": "output_text", "text": text, "annotations": []},
            })
        elif kind == "reasoning":
            text = self._frozen[item_id] = self._text_of(item_id, kind)
            self._thinking_end = len(self.thinking)
            fields["summary_index"] = 0
            self._emit("response.reasoning_summary_text.done", {**fields, "text": text})
            self._emit("response.reasoning_summary_part.done", {
                **fields, "part": {"type": "summary_text", "text": text},
            })
        elif kind == "custom_tool_call":
            final = self._item(index, status=status)["input"]
            sent = self._custom_inputs.get(call["id"], "")
            if len(final) > len(sent) and final.startswith(sent):
                # What was held back while the envelope could still change: the deltas add up to the input.
                self._emit("response.custom_tool_call_input.delta", {**fields, "delta": final[len(sent):]})
                self._custom_inputs[call["id"]] = final
            self._emit("response.custom_tool_call_input.done", {**fields, "input": final})
        elif self._whole(index) or not self._cut:
            self._emit("response.function_call_arguments.done", {**fields, "arguments": call["arguments"]})
        self._emit("response.output_item.done", {
            "output_index": index, "item": self._item(index, status=status),
        })
        self._closed[index] = status

    def end(self, reason, prompt_text=None):
        if not self._items:
            self._ensure_item("message", self.item_id)
        cut = self._cut = reason in CUT_REASONS
        self._finish_through(len(self._items) - 1, self._statuses(reason))
        final = self.payload(reason, prompt_text)
        self._emit("response.incomplete" if cut else "response.completed", {"response": final})
        if self.writer:
            self.writer.done()
        return final

    def payload(self, reason, prompt_text=None):
        prompt, completion = self.usage(prompt_text)
        reasoning_details = self._reasoning_details()
        if not self._items:
            self._ensure_item("message", self.item_id)
        cut = self._cut = reason in CUT_REASONS
        statuses = self._statuses(reason)
        return {"id": self.id, "object": "response", "created_at": self.created,
                "model": self.parsed.model,
                "status": "incomplete" if cut else "completed",
                # What Responses clients read as the reason of an incomplete reply (Codex stops at once, without a
                # retry, and prints it): the output stopped at its limit. A timeout names none.
                **({"incomplete_details": {"reason": "max_output_tokens"}} if reason == "length" else {}),
                "output": [self._item(index, status=self._closed.get(index, statuses[index]))
                           for index in range(len(self._items))],
                "output_text": self.text,
                "usage": {"input_tokens": prompt, "output_tokens": completion,
                          "total_tokens": prompt + completion,
                          # openai-python 2.45+ requires cache_write_tokens beside
                          # cached_tokens; Codex's strict parser reads both too.
                          "input_token_details": {"cached_tokens": self.cache_read,
                                                  "cache_write_tokens": self.cache_write},
                          "input_tokens_details": {"cached_tokens": self.cache_read,
                                                   "cache_write_tokens": self.cache_write},
                          # The details object is optional, but reasoning_tokens
                          # is required when it exists in strict Responses clients.
                          # Omit unknown details instead of {} or an invented zero.
                          **({"output_token_details": reasoning_details,
                              "output_tokens_details": reasoning_details} if reasoning_details else {}),
                          **self._price_usage(), "estimated": True}}


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

    def _usage_boundary(self):
        # A new upstream attempt never continues the previous attempt's thinking
        # block: a late signature must not be appended to it.
        if self.open_kind == "thinking":
            self._close_blocks()

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
        if delta and block_index in self._open_indices:
            self._emit("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                               "delta": {"type": "input_json_delta", "partial_json": delta}})
        if call.get("done") and block_index in self._open_indices:
            # Close the block as soon as upstream ends the call. Claude Code starts a tool when its block stops; held
            # to the end of the message, every call of a parallel batch waited for the last one to be written.
            self._emit("content_block_stop", {"type": "content_block_stop", "index": block_index})
            self._open_indices.discard(block_index)

    def _stop_reason(self, reason):
        if reason in CUT_REASONS:
            return "max_tokens"
        if self.stop_sequence is not None:
            return "stop_sequence"
        if reason == "tool_calls" or self.tool_calls:
            return "tool_use"
        return "end_turn"

    def end(self, reason, prompt_text=None):
        raw_prompt, completion = self.usage(prompt_text)
        non_cached = self.input_tokens if self.input_tokens is not None else max(0, raw_prompt - (self.cache_read or 0) - (self.cache_write or 0))
        usage = {"input_tokens": non_cached, "output_tokens": completion,
                 "cache_read_input_tokens": self.cache_read, "cache_creation_input_tokens": self.cache_write,
                 **self._price_usage()}
        if not self._blocks:
            self._open("text")
        self._close_blocks()
        self._emit("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": self._stop_reason(reason),
                                               "stop_sequence": self.stop_sequence},
                                     "usage": usage})
        self._emit("message_stop", {"type": "message_stop"})
        if self.writer:
            self.writer.done()
        return self.payload(reason, prompt_text)

    def payload(self, reason, prompt_text=None):
        raw_prompt, completion = self.usage(prompt_text)
        non_cached = self.input_tokens if self.input_tokens is not None else max(0, raw_prompt - (self.cache_read or 0) - (self.cache_write or 0))
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.parsed.model,
                "content": [dict(block) for block in self._blocks] or [{"type": "text", "text": ""}],
                "stop_reason": self._stop_reason(reason), "stop_sequence": self.stop_sequence,
                "usage": {"input_tokens": non_cached, "output_tokens": completion,
                          "cache_read_input_tokens": self.cache_read,
                          "cache_creation_input_tokens": self.cache_write,
                          **self._price_usage(), "estimated": True}}


_THINKING_EVENTS = ('thinking', 'thinking_start', 'thinking_status', 'thinking_signature')


class StructuredOutputAdapter:
    """Hold the reply back until its JSON contract has been checked.

    Usage/cost events still reach the ordinary adapter; no invalid partial text
    reaches the client. This is post-validation, not native structured output.
    A turn that calls a client tool is no final answer: it is released as it is,
    and only the reply without tool calls is validated.
    """
    def __init__(self, adapter, spec, *, tools=False, hint=''):
        self.adapter = adapter
        self.spec = spec
        self.tools = tools
        self.hint = hint
        self.parts = []
        self.size = 0
        self.released = False

    def begin(self):
        self.adapter.begin()

    def fail(self, error, extra=None):
        self.adapter.fail(error, extra)

    def _release(self):
        self.released = True
        text = ''.join(self.parts)
        self.parts.clear()
        if text:
            self.adapter.feed({'type': 'text', 'delta': text})

    def feed(self, event):
        kind = event.get('type')
        if kind in ('usage_start', 'usage', 'credits') or self.released:
            self.adapter.feed(event)
        elif kind == 'text':
            self.size += len(event['delta'])
            if self.size > structured.MAX_CHARS:
                raise apierr.invalid_structured_output('structured output exceeds the validation buffer')
            self.parts.append(event['delta'])
        elif kind == 'tool_call' and self.tools:
            self._release()
            self.adapter.feed(event)
        elif kind in ('tool_call', 'tool_call_delta', 'tool_start', 'hitl', 'exec_start', 'exec_result'):
            raise apierr.invalid_structured_output('structured output unexpectedly requested an action')
        elif kind in _THINKING_EVENTS and self.tools:
            self.adapter.feed(event)

    def end(self, reason, prompt_text=None):
        if reason == 'cancelled':
            # The client left or the turn was aborted: nothing is validated and nothing buffered is
            # released, so the request ends like any other cancelled one and is no upstream error.
            return self.adapter.end(reason, prompt_text)
        if reason == 'tool_calls' or self.released:
            self._release()
        elif reason == 'timeout':
            raise apierr.upstream_timeout('upstream turn timed out before the structured reply was complete')
        else:
            try:
                if reason != 'stop':
                    raise ValueError('the reply was cut short (%s)' % reason)
                text = structured.verify(''.join(self.parts), self.spec)
            except ValueError as problem:
                what = 'JSON object' if self.spec['kind'] == 'json_object' else 'JSON Schema'
                raise apierr.invalid_structured_output(
                    'upstream reply does not satisfy %s (%s): %s.' % (self.spec['source'], what, problem)
                    + self.hint)
            self.adapter.feed({'type': 'text', 'delta': text})
        return self.adapter.end(reason, prompt_text)


class StopSequenceAdapter:
    """End the reply at the first of the client's stop sequences, the way the provider would: Retool has none.

    Text is held back only while its end could still turn into a stop sequence. The sequence and everything after it
    never reach the client, and the reply ends with stop_reason "stop_sequence" and the sequence (Messages) or
    finish_reason "stop" (Chat). Of the stop sequences that occur, the one that is complete first wins, as when the
    provider generates left to right. The upstream generation runs on: what the model writes after the sequence is paid
    for and dropped, while its usage and cost still arrive.
    """
    _ACCOUNTING = ('usage_start', 'usage', 'credits')

    def __init__(self, adapter, stops):
        self.adapter = adapter
        self.stops = list(stops)
        self.longest = max(len(stop) for stop in self.stops)
        self.pending = ''
        self.matched = None

    def __getattr__(self, name):
        if name == 'adapter':
            raise AttributeError(name)
        return getattr(self.adapter, name)

    def begin(self):
        self.adapter.begin()

    def fail(self, error, extra=None):
        self.adapter.fail(error, extra)

    def _base(self):
        adapter = self.adapter
        while isinstance(adapter, (StructuredOutputAdapter, StopSequenceAdapter)):
            adapter = adapter.adapter
        return adapter

    def _held(self):
        """How much of the end of the pending text is the start of a stop sequence (and must wait)."""
        for size in range(min(len(self.pending), self.longest - 1), 0, -1):
            tail = self.pending[-size:]
            if any(stop.startswith(tail) for stop in self.stops):
                return size
        return 0

    def _flush(self, final=False):
        keep = 0 if final else self._held()
        text, self.pending = self.pending[:len(self.pending) - keep], self.pending[len(self.pending) - keep:]
        if text:
            self.adapter.feed({'type': 'text', 'delta': text})

    def feed(self, event):
        kind = event.get('type')
        if self.matched is not None:
            if kind in self._ACCOUNTING:
                self.adapter.feed(event)
            return
        if kind != 'text':
            if kind not in self._ACCOUNTING:
                self._flush(final=True)  # the text block ended: its held end can no longer become a stop sequence
            self.adapter.feed(event)
            return
        self.pending += event['delta']
        found = None
        for stop in self.stops:
            at = self.pending.find(stop)
            if at >= 0 and (found is None or at + len(stop) < found[0] + len(found[1])):
                found = (at, stop)
        if found is None:
            self._flush()
            return
        self.pending = self.pending[:found[0]]
        self._flush(final=True)
        self.matched = found[1]
        self._base().stop_sequence = found[1]

    def _reason(self, reason):
        if self.matched is None:
            self._flush(final=True)
            return reason
        # The reply ended at the sequence: whatever ended the upstream turn after it is not the client's affair.
        return 'cancelled' if reason == 'cancelled' else 'stop'

    def end(self, reason, prompt_text=None):
        return self.adapter.end(self._reason(reason), prompt_text)

    def payload(self, reason, prompt_text=None):
        return self.adapter.payload(self._reason(reason), prompt_text)


def for_dialect(dialect, parsed, writer=None):
    if dialect == "anthropic":
        adapter = AnthropicAdapter(parsed, writer)
    elif dialect == "responses":
        adapter = ResponsesAdapter(parsed, writer)
    else:
        adapter = ChatAdapter(parsed, writer)
    spec = getattr(parsed, 'structured_output', None)
    if spec:
        adapter = StructuredOutputAdapter(adapter, spec, tools=bool(getattr(parsed, 'tools', None)),
                                          hint=structured.HINT if dialect == "anthropic" else '')
    stops = getattr(parsed, 'stop_sequences', None)
    if stops:
        # Outside the structured check: the reply is cut at its stop sequence first, and what is left is validated.
        adapter = StopSequenceAdapter(adapter, stops)
    return adapter

def error_payload(dialect, err):
    if dialect == "anthropic":
        return err.anthropic_envelope()
    if dialect == "responses":
        return err.responses_envelope()
    return err.openai_envelope()
