"""Direct Retool inference transport (copilotStreamGPT, NDJSON) with prompt caching.

Drop-in alternative to the agent WebSocket path: same normalised event stream, but a
plain HTTP POST. The stable system head is sent as a cached block so a large project
context is billed once and reused, keeping time-to-first-token flat.
"""
import asyncio
import contextlib
import contextvars
import decimal
import hashlib
import http.client
import json
import pathlib
import re
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request

import apierr
import conversation_state
import credits
import journal
import client_context
import console_log
import native_history
from accounts import account_tag, idle_block
from upstream_attempts import AttemptBudget
from upstream_diagnostics import AttemptTrace, prune_diagnostics

ROUTE = "/api/gpt/copilotStreamGPT?origin=frontend"
MIN_OUTPUT_TOKENS = 16
# Billing and keep-alive events describe the attempt, not its content: never held back.
_LIVE_EVENTS = frozenset({"credits", "usage", "thinking_heartbeat", "end"})
# What an attempt that produced only reasoning looks like on the event stream (thinking_only, not content).
_THINKING_EVENTS = frozenset({"thinking", "thinking_start", "thinking_signature", "thinking_heartbeat"})
_EMPTY_DUMPED = set()  # (request_purpose, model) pairs whose empty answer is already saved this run


def _int_setting(cfg, key, default):
    try:
        return max(0, int(cfg.get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return default
_ASSIST_ONLY = {"claude-haiku-4-5-20251001", "claude-haiku", 'gpt-4.1',
                'gpt-5', 'gpt-5-mini', 'gpt-5-nano', 'gpt-5.1', 'gpt-5.2'}
_ASSIST_SEEDS = frozenset(_ASSIST_ONLY)


def _owning_socket(resp):
    """Best-effort walk to the socket OBJECT that owns the descriptor (a real
    http.client response is resp.fp.raw._sock). Closing through the owner makes
    the owner forget the number, so no second close can hit a reused fd."""
    seen = set()
    cur = resp
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        raw = getattr(cur, "raw", None)
        if raw is not None:
            sock = getattr(raw, "_sock", None)
            if sock is not None and hasattr(sock, "shutdown"):
                return sock
        cur = getattr(cur, "fp", None)
    return None


def _force_close_response(resp):
    """Instantaneously unblock threads waiting in WinSock recv() or HTTPResponse read().

    Windows needs a real closesocket() to abort a blocked recv(); shutdown() alone
    does not wake it. The fd is closed through its OWNING socket object, which then
    remembers -1: a worker thread that still holds the buffered reader (its own
    reference survives resp.fp = None) can never close the number a second time
    after it has been reused by an unrelated connection."""
    if resp is None:
        return
    owner = None
    try:
        fd = resp.fileno() if hasattr(resp, "fileno") else None
        owner = _owning_socket(resp) if fd is not None and fd != -1 else None
    except Exception:
        owner = None
    if owner is not None:
        try:
            owner.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            # Bypass the makefile reference count: the blocked worker holds a
            # reader, so a plain close() would only defer the real closesocket.
            owner._real_close()
        except Exception:
            try:
                owner.close()
            except Exception:
                pass
        try:
            if getattr(resp, "fp", None) is not None:
                resp.fp = None
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
        return
    # Fallback for response shapes without a walkable socket chain. Borrow the
    # handle only to shut down reads; never steal ownership from an unseen
    # Python socket. Closing an alias leaves that original object holding a
    # stale number, whose later finalizer can kill a reused, unrelated socket.
    # This is best effort: Winsock may require a real owning close to unblock a
    # pending recv. Real HTTPResponse objects use the owning path above.
    try:
        fd = resp.fileno() if hasattr(resp, "fileno") else None
        if fd is not None and fd != -1:
            try:
                if getattr(resp, "fp", None) is not None:
                    resp.fp = None
            except Exception:
                pass
            try:
                s = socket.socket(fileno=fd)
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                finally:
                    s.detach()
            except Exception:
                pass
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass

_CACHE_MIN_CHARS = 0


def loop_now():
    return time.time()


_SURROGATE = re.compile("[\ud800-\udfff]")


def _js_number(value):
    """A double as JSON.stringify writes it (Number::toString): 3 for 3.0, 1e-7 for 1e-07, null for NaN and inf."""
    if value != value or value in (float("inf"), float("-inf")):
        return "null"
    if value == 0:
        return "0"
    negative, digits, exponent = decimal.Decimal(repr(float(value))).normalize().as_tuple()
    digits = "".join(map(str, digits))
    k, n = len(digits), len(digits) + exponent
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if n > 0 else "-") + str(abs(n - 1))
    return ("-" if negative else "") + text


def _js_delta(value):
    """How much longer JSON.stringify(value) is than Python's compact dump of the same value, in UTF-16 units.

    Python writes 3.0, 1e-07 and 1e+16 where JavaScript writes 3, 1e-7 and 10000000000000000, an integer past 2**53 in
    full where JavaScript keeps a double, inf and nan as Infinity and NaN where JavaScript writes null, and a lone
    surrogate as itself where JavaScript escapes it (\\udXXX)."""
    stack, delta = [value], 0
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
            stack.extend(key for key in item if isinstance(key, str))
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            delta += 5 * len(_SURROGATE.findall(item))
        elif isinstance(item, bool) or item is None:
            continue
        elif isinstance(item, float):
            delta += len(_js_number(item)) - len(json.dumps(item))
        elif isinstance(item, int) and abs(item) >= 2 ** 53:
            delta += len(_js_number(float(item))) - len(str(item))
    return delta


def _js_length(value):
    """JSON.stringify(value).length in UTF-16 code units."""
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2 + _js_delta(value)


def _hash32(length):
    # Match JavaScript Number multiplication before >>> 0. The product exceeds
    # 2**53 around 3.4M UTF-16 units; exact Python integers then produce a different
    # hint for some lengths, so otherwise valid large requests are rejected.
    return format(int(length * 2654435761.0) & 0xFFFFFFFF, "08x")


def make_rit(messages, tools=None):
    """Non-cryptographic integrity hint the endpoint validates."""
    return _hash32(_js_length(messages)) + _hash32(_js_length(tools or []))


_KNOWN_TYPES = ("string", "number", "integer", "boolean", "null", "array", "object")


def _allowed_values(schema):
    """The values a schema restricts a field to (enum, const, or an anyOf/oneOf of those); None when it does not."""
    if "const" in schema:
        return [schema["const"]]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return list(schema["enum"])
    options = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(options, list) and options:
        values = []
        for option in options:
            if isinstance(option, dict) and option.get("type") == "null" and len(option) <= 2:
                values.append(None)
                continue
            found = _allowed_values(option) if isinstance(option, dict) else None
            if found is None:
                return None
            values.extend(found)
        return values
    return None


def _param_schema(schema, required):
    """Convert one JSON-Schema property to Retool's typed param shape.

    Retool accepts only literal/string/number/boolean/null/array/object/enum/anyOf (its enum holds strings), so an
    unsupported type must degrade instead of emitting "any". The values a field is restricted to reach the model:
    strings as an enum, others (numbers, booleans) in the description, which the model reads as well. A client's MCP
    server checks them, and a guessed value fails the call (wrap_up of Claude Desktop: "Invalid option").

    "X or null" (anyOf/oneOf with a null branch, or a type list with "null") is how Pydantic and many MCP servers write
    an optional field: the field keeps the type of X. Before, it became a plain string, and the model sent "" for the
    booleans and lists of tavily_search. Another union of types becomes Retool's anyOf.
    """
    if not isinstance(schema, dict):
        return {"type": "string", "required": bool(required)}
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [item for item in kind if item in _KNOWN_TYPES]
        kind = next((item for item in kinds if item != "null"), kinds[0] if kinds else None)
    allowed = _allowed_values(schema)
    allowed = [value for value in allowed if value is not None] if allowed is not None else None
    options = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else schema.get("oneOf")
    if allowed is None and kind is None and not schema.get("properties") and isinstance(options, list):
        branches = [option for option in options if isinstance(option, dict) and option.get("type") != "null"]
        if len(branches) == 1:
            merged = dict(branches[0])
            if schema.get("description"):
                merged["description"] = schema["description"]
            return _param_schema(merged, required)
        if branches:
            node = {"type": "anyOf", "anyOf": [_param_schema(branch, False) for branch in branches]}
            if schema.get("description"):
                node["description"] = str(schema["description"])
            node["required"] = bool(required)
            return node
    note = None
    node = {"type": "string"}
    if allowed and all(isinstance(value, str) for value in allowed):
        node = {"type": "enum", "enum": list(dict.fromkeys(allowed))}
    else:
        if allowed:
            note = "Allowed values: %s." % ", ".join(json.dumps(value, ensure_ascii=False) for value in allowed)
            if kind is None:
                kinds = {"number" if isinstance(value, (int, float)) and not isinstance(value, bool) else
                         "boolean" if isinstance(value, bool) else None for value in allowed}
                kind = kinds.pop() if len(kinds) == 1 and None not in kinds else None
        if kind in ("string", "number", "integer", "boolean", "null"):
            node = {"type": "string" if kind == "string" else
                    ("number" if kind in ("number", "integer") else kind)}
        elif kind == "array":
            node = {"type": "array", "items": _param_schema(schema.get("items") or {}, False)}
        elif kind == "object" or schema.get("properties") or schema.get("additionalProperties"):
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            node = {"type": "object", "properties": {
                name: _param_schema(sub, name in _required_names(schema)) for name, sub in properties.items()}}
    description = " ".join(part for part in (str(schema["description"]) if schema.get("description") else "", note,
                                             _limits(schema)) if part)
    if description:
        node["description"] = description
    node["required"] = bool(required)
    return node


def _required_names(schema):
    """The required property names of an object schema; a draft-3 boolean or another odd value counts as none.

    Before, `required: true` or a list of properties raised TypeError while the tools were converted, and the client
    got a 500 instead of its answer (ultracode audit of the relay, C15)."""
    names = schema.get("required")
    return [name for name in names if isinstance(name, str)] if isinstance(names, list) else []


_LIMITS = (("minLength", "at least %s character"), ("maxLength", "at most %s character"),
           ("minimum", ">= %s"), ("maximum", "<= %s"), ("exclusiveMinimum", "> %s"), ("exclusiveMaximum", "< %s"),
           ("multipleOf", "a multiple of %s"), ("minItems", "at least %s item"), ("maxItems", "at most %s item"))


def _limits(schema):
    """The validation limits of a field, in words for its description; None when it has none.

    Retool's param shape has no place for them, so the model never saw them and broke them, and the client's tool or
    its MCP server refused the call: a wait above its 60-second maximum, a SendMessage recipient on several lines.
    Limits written inside allOf (as Claude Code writes its patterns) count as well. The safe-integer bounds a schema
    library adds to every integer (+-9007199254740991) say nothing and are left out."""
    parts = []
    for source in [schema] + [item for item in schema.get("allOf") or [] if isinstance(item, dict)]:
        for key, words in _LIMITS:
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) < 2 ** 53 - 1:
                number = int(value) if float(value).is_integer() else value
                parts.append(words % number + ("s" if words.endswith(("character", "item")) and number != 1 else ""))
        if source.get("uniqueItems") is True:
            parts.append("no duplicate items")
        if isinstance(source.get("pattern"), str) and 0 < len(source["pattern"]) <= 160:
            parts.append("matching the regular expression " + source["pattern"])
        if isinstance(source.get("format"), str) and source["format"]:
            parts.append("in %s format" % source["format"])
    parts = list(dict.fromkeys(parts))
    return "Limits: %s." % "; ".join(parts) if parts else None


def tools_to_retool(tools):
    """Translate client function tools to the Retool tool declaration shape with canonical ordering.

    A tool is strict only when the client asks for it. In strict mode Retool makes every parameter required, so the
    model filled each optional one with a placeholder ("", 0, [], false or the first enum value): show_pane got
    path "" and tab_id "", update_plan items [], tavily_search a country and an end date nobody asked for. Neither
    Anthropic's API nor Chat Completions makes a tool strict by default, and none of the clients asks for it."""
    converted = []
    sorted_tools = sorted(
        tools or [],
        key=lambda item: (item.get("function", {}).get("name") if isinstance(item, dict) and isinstance(item.get("function"), dict)
                          else (item.get("name") if isinstance(item, dict) else "")) or ""
    )
    for tool in sorted_tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        params = {}
        schema = function.get("parameters") if isinstance(function.get("parameters"), dict) else {}
        required = set(_required_names(schema))
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name in sorted(properties.keys()):
            params[name] = _param_schema(properties[name], name in required)
        converted.append({"name": function["name"],
                          "description": function.get("description") or "",
                          "params": params, "strict": function.get("strict", False)})
    return converted


def coerce_arguments(raw, params):
    """Repair scalar types the provider emits as strings (e.g. "1" for a number)."""
    text = raw or "{}"
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    changed = False
    for key, spec in (params or {}).items():
        if key not in data or not isinstance(spec, dict):
            continue
        value = data[key]
        # _coerce_value rejects inf/nan, so the batch path cannot emit a bare
        # Infinity that would make the arguments invalid JSON.
        coerced = _coerce_value(value, spec) if isinstance(value, str) else None
        if coerced is not None:
            data[key] = None if coerced is _JSON_NULL else coerced
            changed = True
    return json.dumps(data, ensure_ascii=False) if changed else text


# What _coerce_value returns for a string that stands for JSON null (None already means "leave the string").
_JSON_NULL = object()


def _coerce_value(value, spec):
    """Coerce one string scalar like coerce_arguments; None when it stays a string."""
    kind = spec.get("type") if isinstance(spec, dict) else None
    if value == "null" and kind in ("number", "boolean", "array", "object"):
        # Retool has no way to declare "X or null", so an optional field of such a type is declared as X, and Claude
        # writes the string "null" when it means null: the client's tool refused middle="null", active="null" (protocol
        # test run of 2026-11-06). A field of these types never holds that text.
        return _JSON_NULL
    if kind == "number":
        try:
            number = float(value) if "." in value else int(value)
        except ValueError:
            return None
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    if kind == "boolean" and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


class ArgStream:
    """Stream tool-call JSON as it arrives while keeping coerce_arguments' repairs.

    Only a top-level string value of a number/boolean parameter is held until it
    closes, then emitted coerced. Everything else (long string bodies, nested
    values) passes through at once, so the emitted text is always a prefix of
    the final arguments and clients can render the call while it is generated.
    Anything unexpected switches to verbatim pass-through.
    """

    def __init__(self, params):
        self.params = params or {}
        self.state = "pre"
        self.depth = 0
        self.in_string = False
        self.escape = False
        self.key = []
        self.held = None

    def _coercible(self, raw_key):
        try:
            name = json.loads('"' + raw_key + '"')
        except ValueError:
            return False
        spec = self.params.get(name)
        return isinstance(spec, dict) and spec.get("type") in ("number", "boolean", "array", "object")

    def feed(self, chunk):
        out = []
        for char in chunk or "":
            state = self.state
            if state == "pass":
                out.append(char)
            elif state == "pre":
                out.append(char)
                if char == "{":
                    self.depth, self.state = 1, "key"
                elif not char.isspace():
                    self.state = "pass"
            elif state == "hold":
                self.held.append(char)
                if self.escape:
                    self.escape = False
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    raw = "".join(self.held)
                    self.held = None
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = None
                    coerced = _coerce_value(value, self.params.get(self.key_name)) if isinstance(value, str) else None
                    out.append(raw if coerced is None else "null" if coerced is _JSON_NULL else json.dumps(coerced))
                    self.state = "after"
            elif state == "keystr":
                out.append(char)
                if self.escape:
                    self.escape = False
                    self.key.append(char)
                elif char == "\\":
                    self.escape = True
                    self.key.append(char)
                elif char == '"':
                    self.state = "colon"
                else:
                    self.key.append(char)
            elif state == "key":
                out.append(char)
                if char == '"':
                    self.key, self.state = [], "keystr"
                elif char == "}":
                    self.state = "done"
                elif not char.isspace() and char != ",":
                    self.state = "pass"
            elif state == "colon":
                out.append(char)
                if char == ":":
                    self.state = "value"
                elif not char.isspace():
                    self.state = "pass"
            elif state == "value":
                if char.isspace():
                    out.append(char)
                elif char == '"' and self._coercible("".join(self.key)):
                    self.key_name = json.loads('"' + "".join(self.key) + '"')
                    self.held, self.escape, self.state = ['"'], False, "hold"
                else:
                    out.append(char)
                    self.state = "inval"
                    self._track(char)
            elif state == "inval":
                out.append(char)
                if not self.in_string and self.depth == 1 and char in ",}":
                    self.state = "key" if char == "," else "done"
                else:
                    self._track(char)
            elif state == "after":
                out.append(char)
                if char == ",":
                    self.state = "key"
                elif char == "}":
                    self.state = "done"
                elif not char.isspace():
                    self.state = "pass"
            else:  # done: trailing characters are relayed verbatim
                out.append(char)
        return "".join(out)

    def _track(self, char):
        if self.in_string:
            if self.escape:
                self.escape = False
            elif char == "\\":
                self.escape = True
            elif char == '"':
                self.in_string = False
        elif char == '"':
            self.in_string = True
        elif char in "{[":
            self.depth += 1
        elif char in "}]":
            self.depth -= 1

    def finish(self):
        """Flush a value left open by a truncated stream verbatim."""
        if self.held is not None:
            raw, self.held = "".join(self.held), None
            return raw
        return ""


def _extract_chunk_text(chunk):
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, dict):
        return str(chunk.get("text") or chunk.get("content") or "")
    if isinstance(chunk, list):
        parts = []
        for b in chunk:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return str(chunk or "")


def _is_user_chunk(chunk):
    if isinstance(chunk, dict):
        role = chunk.get("role")
        if role in ("user", "human", "client"):
            return True
    elif isinstance(chunk, list):
        if any(isinstance(b, dict) and b.get("role") in ("user", "human", "client") for b in chunk):
            return True
    text = _extract_chunk_text(chunk).lstrip()
    lowered = text.lower()
    return (lowered.startswith(("user:", "human:", "client:", "user task:"))
            or text.startswith(("User:", "Human:", "Client:", "USER TASK:")))


def _is_tool_result_chunk(chunk):
    if isinstance(chunk, dict):
        role = chunk.get("role")
        ctype = chunk.get("type")
        if role in ("tool", "function") or ctype in ("tool_result", "exec_result"):
            return True
    elif isinstance(chunk, list):
        if any(isinstance(b, dict) and (b.get("role") in ("tool", "function") or b.get("type") in ("tool_result", "exec_result")) for b in chunk):
            return True
    text = _extract_chunk_text(chunk).lstrip()
    lowered = text.lower()
    return (lowered.startswith(("[tool result", "[tool_result", "[command result", "[exec_result", "tool:", "tool result"))
            or "[tool result" in lowered or "[tool_result" in lowered or "[command result" in lowered)


def cache_key_for(head, model="", tools=None):
    """Prefix cache key. The model and the toolset are part of the upstream prompt
    prefix, so a change to either must not reuse the previous cache entry."""
    digest = hashlib.sha256()
    digest.update((model or "").encode("utf-8"))
    digest.update(b"\x00")
    if tools:
        digest.update(json.dumps(tools, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    digest.update(b"\x00")
    digest.update((head or "").encode("utf-8"))
    return digest.hexdigest()[:24]


def _has_cacheable_text(chunk):
    blocks = chunk if isinstance(chunk, list) else [chunk]
    # Non-dict blocks become text in build_body; image dicts cannot carry cache.
    return any(not isinstance(block, dict) or block.get("type") == "text" for block in blocks)


def build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks=None, session=None):
    """Assemble the request body.

    The stable head becomes a cached system block and the history is sent as one
    message per chunk, preserving prior turn endpoints as cache breakpoints so
    intermediate multi-tool turns do not discard prefix caches.
    """
    messages = []
    cache = cfg.get("direct_cache", True) and len(head or "") >= _CACHE_MIN_CHARS
    prefix_key = cache_key_for(head, model_provider.get("model"), tools) if cache and head else None
    if head:
        block = {"type": "text", "text": head}
        if cache:
            block["cache"] = True
        blocks = [block]
        first, separator, rest = head.partition('\n')
        if first.startswith('x-anthropic-billing-header:') and separator and rest:
            # Anthropic consumes this SDK attribution block. Combining real
            # instructions with it makes those instructions disappear upstream.
            # This split is deterministic from head, including on signed replay.
            block['text'] = rest
            blocks.insert(0, {'type': 'text', 'text': first})
        messages.append({"role": "system", "content": blocks})
    history = [part for part in (chunks or []) if part]
    if not history:
        if prompt:
            messages.append({"role": "user", "content": prompt})
    else:
        cache_indices = set()
        turn_boundaries = set()
        if cfg.get("direct_cache", True):
            session_ends = [idx for idx in getattr(session, "_cached_turn_ends", []) if 0 <= idx < len(history) - 1] if session else []

            # The initial prompt is useful when the marker budget leaves room.
            turn_boundaries.add(0)

            for i, chunk in enumerate(history):
                if _is_user_chunk(chunk):
                    turn_boundaries.add(i)
                if i + 1 < len(history) and _is_user_chunk(history[i + 1]):
                    turn_boundaries.add(i)
                if _is_tool_result_chunk(chunk):
                    turn_boundaries.add(i)

            known_boundaries = sorted(set(session_ends) | turn_boundaries)
            # Anthropic enforces a strict maximum of 4 blocks with cache_control across the request.
            # If head is cached, that leaves at most 3 blocks for history. Otherwise at most 4.
            # The upstream backend adds its own cache_control breakpoint on the tool
            # definitions, so with tools present one block stays reserved for it.
            budget = 4 - (1 if tools else 0)
            max_history_cache = budget - 1 if (head and cache) else budget

            # An image-only tail cannot carry cache: selecting its index silently
            # lost the recent history breakpoint and re-billed the whole prefix.
            cacheable_indices = [i for i, part in enumerate(history) if _has_cacheable_text(part)]
            cacheable = set(cacheable_indices)
            chosen = {cacheable_indices[-1]} if cacheable_indices else set()
            previous = [i for i in session_ends if i in cacheable]
            warm_body = getattr(session, "_warm_body", None) if session is not None else None
            if isinstance(warm_body, dict) and prefix_key and warm_body.get("openAICacheKey") == prefix_key:
                warm_messages = warm_body.get("messages") or []
                offset = int(bool(warm_messages and warm_messages[0].get("role") == "system"))
                previous += [i - offset for i, message in enumerate(warm_messages)
                             if i - offset in cacheable and i - offset < len(history) - 1
                             and isinstance(message.get("content"), list)
                             and any(isinstance(block, dict) and block.get("cache")
                                     for block in message["content"])]
            if previous:
                # Provider lookup only scans a limited number of blocks before a
                # breakpoint. Keep the old endpoint when a tool batch adds >20.
                chosen.add(max(previous))
            if cacheable_indices and len(chosen) < max_history_cache:
                chosen.add(cacheable_indices[0])

            slots_left = max_history_cache - len(chosen)
            intermediates = [i for i in known_boundaries if i in cacheable and i not in chosen]
            if slots_left > 0 and intermediates:
                for idx in intermediates[-slots_left:]:
                    chosen.add(idx)

            if len(chosen) < max_history_cache:
                for idx in reversed(cacheable_indices):
                    if len(chosen) >= max_history_cache:
                        break
                    chosen.add(idx)

            cache_indices = chosen

        for index, part in enumerate(history):
            should_cache = index in cache_indices
            if isinstance(part, list):
                blocks = [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in part]
                if should_cache:
                    # Only text blocks carry the cache flag; an image-only chunk has no
                    # cacheable text and upstream may reject the unknown placement.
                    target = next((b for b in reversed(blocks) if b.get("type") == "text"), None)
                    if target is not None:
                        target["cache"] = True
                messages.append({"role": "user", "content": blocks})
            elif isinstance(part, dict):
                block = dict(part)
                if should_cache and block.get("type") == "text":
                    block["cache"] = True
                messages.append({"role": "user", "content": [block]})
            else:
                block = {"type": "text", "text": str(part)}
                if should_cache:
                    block["cache"] = True
                messages.append({"role": "user", "content": [block]})

    # Hard safety check: ensure total blocks with cache: True NEVER exceeds the Anthropic
    # limit. With tools present one block is reserved for the upstream tool breakpoint.
    max_cache_blocks = 3 if tools else 4
    cached_blocks = [
        b
        for m in messages
        for b in m.get("content", [])
        if isinstance(b, dict) and b.get("cache")
    ]
    if len(cached_blocks) > max_cache_blocks:
        for b in cached_blocks[1:-1]:
            if len(cached_blocks) <= max_cache_blocks:
                break
            b.pop("cache", None)
            cached_blocks = [
                blk for m in messages for blk in m.get("content", [])
                if isinstance(blk, dict) and blk.get("cache")
            ]

    if session is not None and history and cfg.get("direct_cache", True):
        prior = [e for e in getattr(session, "_cached_turn_ends", []) if e < len(history) - 1]
        all_ends = set(prior) | {e for e in turn_boundaries if e < len(history) - 1}
        all_ends.update(cache_indices)
        all_ends.add(0)
        all_ends.add(len(history) - 1)
        session._cached_turn_ends = sorted(all_ends)

    body = {"model": model_provider.get("model"),
            "messages": messages,
            "rit": make_rit(messages, tools),
            "metadata": {"sandpackAgent": True}}
    resource = model_provider.get("providerResourceName")
    if resource:
        body["providerResourceName"] = resource
    if model_provider.get("reasoningEffort"):
        body["reasoningEffort"] = model_provider["reasoningEffort"]
    if model_provider.get('verbosity') is not None:
        body['verbosity'] = model_provider['verbosity']
    if max_tokens:
        body["maxCompletionTokens"] = max(int(max_tokens), MIN_OUTPUT_TOKENS)
    if tools:
        body["tools"] = tools
        choice = tool_choice if isinstance(tool_choice, str) else (
            (tool_choice or {}).get("function", {}).get("name") if isinstance(tool_choice, dict) else None)
        if choice and choice != "auto":
            body["toolChoice"] = choice
    if prefix_key:
        body["openAICacheKey"] = prefix_key
    return body


def _bounded_put(loop, queue, item, is_cancelled, max_wait=120.0):
    """One owner-loop callback retries a full queue; the producer waits for it.

    Cancellation disables that callback before returning, preventing duplicates
    or late enqueueing. No Queue.put coroutine is left behind on loop shutdown.
    """
    if is_cancelled() or loop.is_closed() or not loop.is_running():
        return False
    delivered = threading.Event()
    lock = threading.Lock()
    state = {"sent": False, "cancelled": False}

    def put_once():
        with lock:
            if state["cancelled"]:
                return
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                loop.call_later(0.05, put_once)
                return
            state["sent"] = True
            delivered.set()

    try:
        loop.call_soon_threadsafe(put_once)
    except RuntimeError:  # the loop closed between the check and the hand-off
        return False
    deadline = time.monotonic() + max_wait if max_wait else None
    while not delivered.wait(0.1):
        expired = deadline is not None and time.monotonic() >= deadline
        if is_cancelled() or expired or loop.is_closed() or not loop.is_running():
            with lock:
                if state["sent"]:
                    return True
                state["cancelled"] = True
            if expired:
                journal.emit("direct.queue_drop",
                             "Кадр отброшен: потребитель не отвечал дольше %d с — ход прерван" % int(max_wait),
                             "warn")
            return False
    return True


# How providers word a refusal of an input past the model's context window (Retool passes the provider's sentence on).
_CONTEXT_EXCEEDED = re.compile(r"(?i)exceeds? the context window|context[ _]length[ _]exceeded|maximum context length"
                               r"|prompt is too long|input is too long for")


def _error_from_event(event):
    message = event.get("error")
    if isinstance(message, dict):
        message = message.get("message")
    status = event.get("statusCode")
    text = str(message or "upstream stream error")
    if status in (400, 413, None) and _CONTEXT_EXCEEDED.search(text):
        return apierr.context_length_exceeded(text)
    if status in (429, None) and apierr.is_hourly_limit(text):
        # The hourly managed-key quota belongs to the account, not to this one request.
        return apierr.account_rate_limited(text)
    if status == 429:
        return apierr.too_many_requests(text)
    if status in (401, 403):
        return apierr.upstream_auth_failed(text)
    if status in (400, 404, 422):
        return apierr.bad_request(text)
    return apierr.upstream_error(text)


def _ping(session, body):
    """Cheap request that re-reads the cached prefix so its TTL does not lapse.

    Returns the credits Retool actually billed for the ping (from the usage
    metadata event), or None when the stream ended without reporting them."""
    payload = dict(body)
    payload["maxCompletionTokens"] = MIN_OUTPUT_TOKENS
    # Effort can be part of the provider cache scope. Preserve the exact request
    # settings rather than replacing max with low during a cache refresh.
    headers = session.backend.headers()
    headers["content-type"] = "application/json"
    request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                     data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                                     headers=headers, method="POST")
    response = urllib.request.urlopen(request, timeout=120)
    abort = lambda: _force_close_response(response)
    session._active_warm_abort = abort
    actual_credits = None
    complete = False
    billing_id = "keepwarm:" + uuid.uuid4().hex
    warm_usage = {}
    try:
        for raw in response:
            if getattr(session, "_cancelled", False):
                raise InterruptedError("cache refresh cancelled")
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            warm_usage.update(usage)
            if usage.get("creditsUsed") is not None:
                reported = console_log.number(usage["creditsUsed"])
                if reported is not None:
                    actual_credits = reported
            if event.get("event") == "error":
                # Preserve any billed usage attached to the error, without claiming a refreshed TTL.
                raise _error_from_event(event)
            if event.get("event") == "end_of_stream" or event.get("type") == "end_of_stream":
                complete = True
                break
    finally:
        if getattr(session, "_active_warm_abort", None) is abort:
            session._active_warm_abort = None
        try:
            response.close()
        finally:
            session._last_warm_usage = warm_usage
            # Run in the worker, which can outlive a cancelled scheduler/lease.
            # Multiple metadata snapshots belong to this ONE billed ping.
            manager = getattr(session, "_credits_manager", None)
            if actual_credits is not None:
                session._keepwarm_cost = getattr(session, "_keepwarm_cost", 0.0) + actual_credits
                if manager is not None:
                    manager.record_turn(billing_id, actual_credits)
            else:
                mark_unknown = getattr(manager, "mark_usage_unknown", None)
                if mark_unknown:
                    mark_unknown()
            try:
                request_id = client_context.current().get("request_id") or getattr(session, "_request_tag", None) or "keepwarm"
                monitor = getattr(session, "_console_warm_cache_monitor", None)
                if monitor is None:
                    monitor = session._console_warm_cache_monitor = console_log.CacheMonitor()
                console_log.write(monitor.line(request_id, warm_usage, sample_id=billing_id,
                                              scope=(body.get("model"), body.get("openAICacheKey")))
                                  + " [keepwarm] " + client_context.label())
                snapshot = getattr(manager, "balance_snapshot", None)
                if snapshot:
                    console_log.write(console_log.balance_line(request_id, snapshot(), actual_credits) + " [keepwarm]")
                journal.emit("cache.keepwarm_usage", "Стоимость запроса прогрева", "info",
                             billing_id=billing_id, model=body.get("model"), source="relay",
                             account=account_tag(session.backend),
                             request_purpose="cache-keepwarm", actual_credits=actual_credits,
                             complete=complete, usage=warm_usage)
            except Exception:  # noqa: BLE001 - reporting must not mask the ping's own outcome
                pass
    if not complete:
        raise apierr.upstream_error("cache refresh ended without end_of_stream")
    return actual_credits


# The provider keeps a cache entry for 5 minutes counted from the START of the request that last wrote or
# read it (Anthropic prompt caching); only a read renews it. A ping that arrives later finds nothing to read
# and pays to write the whole prefix again, 25 times the price of a read, for a conversation nobody asked
# to keep warm.
PROMPT_CACHE_TTL = 300.0
# The scheduler's tick, the wait for a worker thread and the trip to the provider all come out of the TTL:
# a ping may start at most TTL - margin after the last confirmed touch of the entry.
PROMPT_CACHE_MARGIN = 20.0
PING_DEADLINE = PROMPT_CACHE_TTL - PROMPT_CACHE_MARGIN
# No setting may schedule a ping later than this: a few seconds of tick before the deadline.
PING_LATEST = PING_DEADLINE - 5.0
# A failed ping is retried this long after the failure (doubled for every further one), and only if the retry
# still starts inside the window above. Five failed pings in a row end the cycle all the same (`failures`), but the
# window usually closes first: the retries come 10, 20, 40 and 80 s after the failures, so all five attempts fit only
# when the interval is at most about 130 s. With the default 240 s the retry after the third failure would start too
# late and the cycle ends with `cache_expired` (`after_failure`) instead.
PING_RETRY = 10.0
# A ping that WROTE the prefix instead of reading it found the entry gone. It counts as a rewrite when it wrote
# half of the cached prefix (a miss on a small prompt) or REWRITE_TOKENS tokens: the console flags the same
# size as [WRITE HIGH], and at the write price (1e-4 credits per token) it is 5 credits, as much as six
# healthy pings of a 190k-token prefix (4e-6 per token). Below one cache block it is noise.
REWRITE_SHARE = 0.5
REWRITE_TOKENS = 50_000
REWRITE_FLOOR = 1_024

_STOP_TEXT = {
    "idle_limit": "Прогрев остановлен: диалог не запрашивали дольше лимита простоя",
    "subagent_done": "Прогрев остановлен: субагент ответил без вызова инструмента",
    "account_unavailable": "Прогрев остановлен: аккаунт недоступен для фоновых запросов",
    "evicted": "Прогрев остановлен: диалог вытеснен из пула",
    "failures": "Прогрев остановлен: пять неудачных пингов подряд",
    "closed": "Прогрев остановлен: сессия закрыта",
    "last_turn_failed": "Прогрев остановлен: последний ход не дал пригодного ответа",
    "cache_expired": "Прогрев остановлен: кэш префикса уже истёк, пинг оплатил бы его полную перезапись",
    "cache_rewritten": "Прогрев остановлен: пинг перезаписал префикс вместо чтения из кэша",
    "compacted": "Прогрев остановлен: клиент сжал историю диалога и больше не продолжит прежний префикс",
}


def attempt_outcome(*, cancelled, failed, ended, usable, thinking_seen):
    """The class of one finished upstream attempt: cancelled, error, truncated, content, thinking_only or empty.

    One definition for the attempt trace (_stream_body) and for the verdict on a whole turn (iter_events).
    Only `content`, a non-blank text or a finished tool call delivered by a stream that reached its end, is an
    answer; every other class is a paid attempt that gave the client nothing."""
    return ("cancelled" if cancelled else "error" if failed else "truncated" if not ended else
            "content" if usable else "thinking_only" if thinking_seen else "empty")


def _rewrite_of(usage):
    """The size of a rewrite when a ping wrote the cached prefix again instead of reading it, else None."""
    if not isinstance(usage, dict):
        return None
    written = int(console_log.number(usage.get("promptTokensWrittenToCache")) or 0)
    if written < REWRITE_FLOOR:
        return None
    read = int(console_log.number(usage.get("promptTokensReadFromCache")) or 0)
    prefix = read + written
    if written < REWRITE_TOKENS and written < prefix * REWRITE_SHARE:
        return None
    return {"written_tokens": written, "read_tokens": read, "prefix_tokens": prefix}


class _WarmCycle:
    """Pings and cost of one keepwarm task; reported once, as cache.keepwarm_stop, when it ends."""
    __slots__ = ("session", "account", "pings", "idle_pings", "cost0", "reason", "detail")

    def __init__(self, session, account):
        self.session, self.account = session, account
        self.pings = self.idle_pings = 0
        self.cost0 = float(getattr(session, "_keepwarm_cost", 0.0) or 0.0)
        # Cancelled by whoever closed the session; the pool names its own reasons (eviction).
        self.reason, self.detail = "closed", {}

    def stop(self, reason, **detail):
        self.reason, self.detail = reason, detail

    def cancelled(self):
        self.stop(getattr(self.session, "_warm_stop_reason", None) or "closed")

    def announce(self):
        session, reason = self.session, self.reason
        if reason == "closed" and not self.pings:
            return  # a session closed without a single ping has nothing to account for
        try:
            last = getattr(session, "_last_request_at", None)
            spent = float(getattr(session, "_keepwarm_cost", 0.0) or 0.0) - self.cost0
            level = ("warn" if reason in ("failures", "cache_rewritten") else
                     "debug" if reason == "account_unavailable" and not self.pings else "info")
            owner = getattr(session, "_turn_identity", None) or getattr(session, "_warm_identity", None) or {}
            token = client_context.bind(owner)
            try:
                journal.emit("cache.keepwarm_stop", _STOP_TEXT.get(reason, reason), level, reason=reason,
                             pings=self.pings, idle_pings=self.idle_pings, credits=round(max(0.0, spent), 5),
                             idle_seconds=round(max(0.0, time.time() - last)) if isinstance(last, (int, float)) else None,
                             subagent=bool(getattr(session, "_parent_key", None)),
                             model=(getattr(session, "_warm_body", None) or {}).get("model"),
                             thread=journal.tag(getattr(session, "thread_id", None)), account=self.account,
                             **self.detail)
            finally:
                client_context.reset(token)
        except Exception:  # noqa: BLE001 - reporting must not break the session's own shutdown
            pass


def _dependents_live(session, now, turn_timeout, limit):
    """True while a child request that pins this conversation is really alive.

    The pool counts leases, and a release that never comes would pin the count forever.
    Only activity counts here: a child turn busy for at most turn_timeout, or any child
    request within its own idle limit; a request still being set up counts for turn_timeout."""
    for record in tuple(getattr(session, "_cache_dependent_records", None) or ()):
        child, since = getattr(record, "session", None), getattr(record, "since", 0) or 0
        if child is None:
            if now - since <= turn_timeout:
                return True
            continue
        started = getattr(child, "_turn_started_at", 0) or 0
        if getattr(child, "_busy", False) and started:
            if now - started <= turn_timeout:
                return True
            continue
        if now - max(getattr(child, "_last_request_at", 0) or 0, since) <= limit:
            return True
    return False


async def _keepwarm(session, cfg):
    """Refresh active and idle prefixes, scheduling from request starts.

    Only one ping runs per session. Active turns get their first refresh at
    the configured offset, then at most every 240s (inside the 5-minute TTL).
    Failures retry soon, but only while the retry still starts inside the cache window;
    an unknown cache result is never reported as a hit.

    Idle warming ends by limits that hold whatever the pool's bookkeeping says: after
    cache_keepwarm_idle seconds without a real request (pings do not count), after
    cache_keepwarm_subagent_idle for a subagent whose answer asked for a tool, at once
    for a subagent that answered without one. A turn busy for longer than turn_timeout and
    a child request that went quiet keep nothing warm. The reason, the pings and the credits
    of the cycle are journaled as cache.keepwarm_stop.

    A ping is paid for, so it is only sent while it can still read the cache. The guards:
    - the last turn gave no usable answer (empty, thinking only, cut off, an error): last_turn_failed;
    - the last turn was a client's compaction: the client replaces its history by the summary, so the prefix kept
      warm so far is not continued and the summary itself is no body to keep warm: compacted, until a real turn of
      the chat gives a usable answer;
    - the last confirmed touch of the cache, the start of a real request or of a successful ping
      (a failed ping touches nothing: its result is unknown), is older than
      PROMPT_CACHE_TTL - PROMPT_CACHE_MARGIN: cache_expired. This also ends a retry that no longer fits (the touch
      is read again after the failure: a request that began while the ping hung read the cache itself);
    - a ping reported that it wrote the prefix instead of reading it: cache_rewritten. One rewrite is the most a
      session pays for: the session is marked, and no cycle is started for it again (_ensure_keepwarm, which the pool
      calls at every request of a child) until a real turn gives a usable answer;
    - five failed pings in a row: failures (with the default interval the window closes first, see PING_RETRY).
    Inside a running turn only the last three apply. A turn in flight is judged when it ends, not before, so the
    verdict on the turn before it (last_turn_failed, compacted) and the idle limits stop nothing there.
    """
    interval = float(cfg.get("cache_keepwarm_seconds", 0) or 0)
    if interval <= 0:
        return
    idle = float(cfg.get("cache_keepwarm_idle", 900) or 900)
    subagent_idle = float(_int_setting(cfg, "cache_keepwarm_subagent_idle", 600))
    turn_timeout = float(_int_setting(cfg, "turn_timeout", 900) or 900)
    during_turns = bool(cfg.get("cache_keepwarm_during_turns", True))
    turn_offset = min(max(float(cfg.get("cache_keepwarm_turn_offset", 200) or 200), 30.0), PING_LATEST)
    active_interval = min(interval, 240.0)
    idle_interval = min(interval, PING_LATEST)
    account = account_tag(getattr(session, "backend", None))
    cycle = _WarmCycle(session, account)
    scheduled_seq = None
    next_turn_ping = 0.0
    retry_at = 0.0
    try:
        while True:
            busy, seq, touch_at = False, None, None
            context_token = client_context.bind({})
            try:
                await asyncio.sleep(0.05)
                now = time.time()
                started = getattr(session, "_turn_started_at", 0) or 0
                # A turn busy for longer than turn_timeout is stuck, not working: it keeps nothing warm.
                busy = bool(getattr(session, "_busy", False)) and not (started and now - started > turn_timeout)
                seq = getattr(session, "_turn_seq", 0)
                if seq != scheduled_seq:
                    scheduled_seq = seq
                    next_turn_ping = started + turn_offset
                last_req = getattr(session, "_last_request_at", getattr(session, "_warm_at", 0))
                if not busy:
                    refused = _warm_refused(session, False)
                    if refused is not None:
                        # Nothing usable came back (last_turn_failed), so there is nothing a client returns to and the
                        # prefix behind it is not worth a paid refresh; or the client replaced its history by the
                        # summary of a compaction (compacted), or a ping found the prefix gone (cache_rewritten). The
                        # next real turn that gives a usable answer lifts it.
                        cycle.stop(refused[0], **refused[1])
                        return
                    subagent = bool(getattr(session, "_parent_key", None))
                    limit = subagent_idle if subagent else idle
                    if subagent and getattr(session, "_turn_final", False):
                        cycle.stop("subagent_done")
                        return
                    pinned = _dependents_live(session, now, turn_timeout, subagent_idle)
                    if not pinned and now - last_req > limit:
                        cycle.stop("idle_limit", limit=limit)
                        return
                    unavailable = idle_block(getattr(session, "backend", None))
                    if unavailable:
                        # Switched off or marked unavailable, whatever the reason: no idle warmups on this account.
                        cycle.stop("account_unavailable", account_state=unavailable)
                        return
                body = getattr(session, "_warm_body", None)
                identity = getattr(session, "_warm_identity", None) or {}
                touch_at = getattr(session, "_warm_at", 0) or 0
                if busy:
                    if not during_turns or not started:
                        await asyncio.sleep(0.95)
                        continue
                    turn_body = getattr(session, "_turn_body", None)
                    if turn_body:
                        body = turn_body
                        identity = getattr(session, "_turn_identity", None) or {}
                        # The turn's own request read or wrote this prefix when it began.
                        touch_at = max(touch_at, started)
                    due = max(next_turn_ping, retry_at)
                else:
                    due = max(touch_at + idle_interval, retry_at)
                if not body:
                    await asyncio.sleep(0.95)
                    continue
                if now - touch_at > PING_DEADLINE:
                    # The entry is gone or about to be: a ping now writes the whole prefix again.
                    cycle.stop("cache_expired", touch_age_seconds=round(now - touch_at), limit=PING_DEADLINE,
                               during_turn=busy)
                    return
                if now < due:
                    await asyncio.sleep(min(0.95, max(0.05, due - now)))
                    continue
                # The scheduler spans turns: bind the owner of this exact body,
                # snapshotting before to_thread so a following turn cannot relabel it.
                client_context.bind(identity)
                ping_started = now
                cycle.pings += 1
                cycle.idle_pings += 0 if busy else 1
                loop = asyncio.get_running_loop()
                io_executor = getattr(loop, "_relay_io_executor", None)
                if io_executor is None:
                    actual = await asyncio.to_thread(_ping, session, body)
                else:
                    actual = await loop.run_in_executor(io_executor, contextvars.copy_context().run,
                                                        _ping, session, body)
                if busy and getattr(session, "_turn_seq", 0) == seq:
                    session._turn_pinged = True
                    next_turn_ping = ping_started + active_interval
                # A touch never moves back: a turn that began (or ended) while this ping was out has touched later.
                session._warm_at = max(ping_started, getattr(session, "_warm_at", 0) or 0)
                session._warm_failures = 0
                retry_at = 0.0
                prefix_tokens = int(getattr(session, "_last_prefix_tokens", 0) or 0)
                estimate = prefix_tokens * credits.rates(body.get("model"))["cache_read"]
                if actual is None:
                    session._keepwarm_estimated_cost = round(
                        getattr(session, "_keepwarm_estimated_cost", 0.0) + estimate, 4)
                usage = getattr(session, "_last_warm_usage", {})
                rewrite = _rewrite_of(usage)
                touched = any(isinstance(usage.get(key), (int, float)) and usage[key] > 0 for key in (
                    "promptTokensReadFromCache", "promptTokensWrittenToCache"))
                journal.emit("cache.keepwarm",
                             "Прогрев перезаписал кэш: префикс записан заново, а не прочитан" if rewrite else
                             "Кэш прочитан или записан при прогреве" if touched else
                             "Запрос прогрева завершён; использование кэша не подтверждено",
                             "warn" if rewrite else "info" if busy else "debug",
                             model=body.get("model"), thread=journal.tag(getattr(session, "thread_id", None)),
                             account=account,
                             during_turn=busy, turn_age_seconds=round(ping_started - started) if busy else None,
                             touch_age_seconds=round(ping_started - touch_at),
                             prefix_tokens=prefix_tokens, est_credits=round(estimate, 4),
                             actual_credits=actual, credits_total=getattr(session, "_keepwarm_cost", 0.0),
                             cache_verified=touched, usage=usage)
                if rewrite:
                    # The cache the cycle was keeping alive was gone: whatever kept it from being read
                    # (a lapse, an eviction, another shard) can happen again, and every miss pays a full write.
                    # The session is marked, not just this cycle: the pool starts a new cycle for a parent at every
                    # request of its child, and each would pay for one more rewrite.
                    session._warm_blocked = "cache_rewritten"
                    cycle.stop("cache_rewritten", touch_age_seconds=round(ping_started - touch_at),
                               ping_credits=actual, **rewrite)
                    return
            except asyncio.CancelledError:
                cycle.cancelled()
                return
            except Exception as exc:
                if busy and getattr(session, "_turn_seq", 0) == seq:
                    session._turn_pinged = True
                failures = getattr(session, "_warm_failures", 0) + 1
                session._warm_failures = failures
                if failures >= 5:
                    journal.emit("cache.keepwarm", _STOP_TEXT["failures"], "warn", account=account)
                    console_log.write("[err] cache keepwarm stopped after five failures; cached prefix may expire")
                    cycle.stop("failures", failures=failures)
                    return
                failed_at = time.time()
                retry_at = failed_at + PING_RETRY * 2 ** (failures - 1)
                next_turn_ping = retry_at
                # A failed ping touched nothing (its result is unknown): the retry has to start inside the
                # window of the last confirmed touch, or it would pay for a cache that is gone. That touch is read
                # again now, not taken from before the ping: a request that began while the ping hung (or one that
                # began and ended) read the cache itself, and it is the newest touch the window counts from.
                if touch_at is not None:
                    touch_at = max(touch_at, getattr(session, "_warm_at", 0) or 0,
                                   (getattr(session, "_turn_started_at", 0) or 0) if getattr(session, "_busy", False)
                                   else 0)
                fits = touch_at is None or retry_at - touch_at <= PING_DEADLINE
                journal.emit("cache.keepwarm",
                             ("Прогрев не удался; повтор через %d с: %s" % (round(retry_at - failed_at), str(exc)[:200]))
                             if fits else
                             "Прогрев не удался; повтор не успевает до истечения кэша: %s" % str(exc)[:200],
                             "warn", account=account)
                console_log.write("[err] cache keepwarm failed; cached prefix may expire: %s" % str(exc)[:200])
                if not fits:
                    cycle.stop("cache_expired", touch_age_seconds=round(failed_at - touch_at), limit=PING_DEADLINE,
                               during_turn=busy, after_failure=True, failures=failures)
                    return
            finally:
                client_context.reset(context_token)
    finally:
        cycle.announce()


def native_scope(conversation_key, model_provider, head, tools, tool_choice):
    """What a signed branch is keyed by: it continues only a request that has all of these as it had them.

    iter_events keys the branches it records and resumes by this, and the compaction check of the orchestrator asks
    the same question before the turn begins. Output caps and verbosity control generation, not the signed input
    prefix, and clients legitimately omit or change them between tool follow-ups, so verbosity is left out. The effort
    is in: the provider renders it into the prompt, so another effort is another prefix."""
    prefix_provider = {key: value for key, value in model_provider.items() if key != 'verbosity'}
    return json.dumps([conversation_key, prefix_provider, head, tools, tool_choice], sort_keys=True, ensure_ascii=True)


def scope_effort(scope):
    """The reasoning effort a signed branch was made with, read from its scope; None when it has none.

    The provider is the second part of the scope (native_scope): a request that asks for another effort starts a new
    history instead of continuing the branch."""
    try:
        effort = json.loads(scope)[1].get("reasoningEffort")
    except (TypeError, ValueError, IndexError, KeyError, AttributeError):
        return None
    return effort if isinstance(effort, str) and effort else None


async def iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens,
                      chunks=None):
    """Yield normalised relay events from the direct NDJSON stream."""
    client = getattr(session, "_native_client_messages", None)
    history = getattr(session, "_native_history", None)
    scope = native_scope(getattr(session, "_native_conversation_key", None), model_provider, head, tools, tool_choice)
    parent, messages = (conversation_state.resume(session, history, scope, client)
                        if history is not None and client is not None else (None, None))
    if messages is not None:
        body = build_body(cfg, model_provider, head, "", tools, tool_choice, max_tokens)
        body["messages"] = native_history.cache_messages(messages, cfg.get("direct_cache", True), tools)
        body["rit"] = make_rit(body["messages"], tools)
        journal.emit("native_history.resume", "Сохранённые assistant/thinking blocks возвращены upstream", "info",
                     blocks=len(parent.assistant), messages=len(body["messages"]),
                     signatures=sum(b["type"] == "thinking" for b in parent.assistant))
        req_id = client_context.current().get("request_id") or getattr(session, "_request_tag", None) or "req"
        console_log.write("[%s] [history] native continuation restored thinking=%d tool_calls=%d" % (
            req_id, sum(b["type"] in ("thinking", "redacted_thinking") for b in parent.assistant),
            sum(b["type"] == "tool_call" for b in parent.assistant)))
    else:
        if history is not None and history.branches and client is not None:
            # Never attach an old signature to a rebuilt/edited prefix.
            session._cached_turn_ends = []
            session._warm_body = None
            journal.emit("native_history.reset", "История или настройки изменились; прежняя подпись не переиспользуется",
                         "warn", reason="prefix_or_settings_mismatch",
                         restored=any(getattr(branch, "restored", False) for branch in history.branches),
                         why=history.mismatch(scope, client, getattr(session, "_native_tool_defaults", None)))
        body = build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks, session=session)
        cold = (native_history.unsigned_tool_transcript(client)
                if client is not None and getattr(session, '_native_allow_cold_replay', False) else None)
        if cold is not None:
            # The opaque signed branch may have expired or belonged to another
            # account. Client-visible calls/results are still valid protocol.
            # Rebuild them without trusting any client-supplied thinking blocks.
            initial = body['messages'][:1] if head else []
            body['messages'] = native_history.cache_messages(initial + cold, cfg.get('direct_cache', True), tools)
            body['rit'] = make_rit(body['messages'], tools)
            journal.emit('native_history.cold', 'История инструментов восстановлена без старых thinking-подписей',
                         'info', messages=len(cold), signatures=0)
    session._direct_reply = None
    param_types = {tool["name"]: tool.get("params") or {} for tool in (tools or [])}
    if model_provider.get("model") in _ASSIST_ONLY:
        body = {key: value for key, value in body.items() if key != "metadata"}
    identity = client_context.current()
    session._busy = True
    session._turn_identity = identity
    session._turn_started_at = time.time()
    session._turn_pinged = False
    session._turn_final = False
    session._turn_seq = getattr(session, "_turn_seq", 0) + 1
    turn_seq = session._turn_seq
    session._last_request_at = time.time()
    # The summary of a client's compaction is a side request: the client replaces its history by that summary and
    # never continues this one (see where the branches are recorded below, and what keepwarm does after it).
    side_request = identity.get("client_task") == client_context.COMPACTION
    rejected = False
    # A completed answer that asked for no tool is final: a subagent that gave one is done.
    asked_tool, ended = False, None
    # What the turn delivered decides what it may become: only a usable answer is a keepwarm body.
    wrote_text = thinking_seen = errored = False
    failure = None
    settled = False

    def settle():
        """Record what this turn came to on the session: once, and only while it is still the session's turn.

        The verdict is known as soon as the stream says it was cut (an error event: the consumer raises on it and may
        leave this generator suspended until the garbage collector closes it, and until then the session would look
        busy and its cycle would warm a turn that is over). A turn that a newer one has replaced on the session leaves
        the state alone: a generator that is closed late must neither turn the newer turn's `content` into
        `truncated` nor mark the session idle while the newer turn still runs."""
        nonlocal settled
        if settled:
            return
        settled = True
        if getattr(session, "_turn_seq", None) != turn_seq:
            return
        session._busy = False
        session._turn_body = None
        session._turn_identity = None
        session._turn_final = ended == "stop" and not asked_tool and not getattr(session, "_cancelled", False)
        # The consumer may drop the generator right after an error event (a cut stream): that is not a cancel.
        stopped_early = isinstance(failure, (asyncio.CancelledError, GeneratorExit)) and not errored
        outcome = "truncated" if errored else attempt_outcome(
            cancelled=bool(getattr(session, "_cancelled", False)) or ended == "cancelled" or stopped_early,
            failed=failure is not None, ended=ended is not None, usable=wrote_text or asked_tool,
            thinking_seen=thinking_seen)
        if rejected:
            outcome = "rejected"
        if body.get("openAICacheKey"):
            # Only a usable answer (never an empty, thinking-only, cut off, failed or cancelled turn) may
            # become the keepwarm body; any other verdict stops the cycle until a good turn comes.
            session._turn_outcome = outcome
        if body.get("openAICacheKey") and not rejected:
            prior_key = getattr(session, "_last_cache_key", None)
            if prior_key and prior_key != body["openAICacheKey"]:
                # A new key means the upstream prefix cache starts from zero: every
                # history breakpoint misses and the whole prompt is written again.
                journal.emit("cache.key_changed", "╨Ъ╤Н╤И-╨║╨╗╤О╤З ╤Б╨╝╨╡╨╜╨╕╨╗╤Б╤П тАФ ╨┐╤А╨╡╤Д╨╕╨║╤Б ╨▒╤Г╨┤╨╡╤В ╨┐╨╡╤А╨╡╨╖╨░╨┐╨╕╤Б╨░╨╜", "info",
                             old=prior_key, new=body["openAICacheKey"], model=body.get("model"),
                             tools=len(body.get("tools") or []),
                             thread=journal.tag(getattr(session, "thread_id", None)))
            session._last_cache_key = body["openAICacheKey"]
            if outcome == "content" and side_request:
                # The summary of a compaction is no body to keep warm, and the history it summarizes is the one the
                # client now replaces: warming either keeps a prefix nobody continues. The cycle ends with the
                # reason `compacted`, and no new one starts until a real turn of the chat gives a usable answer
                # (a stronger mark, cache_rewritten, stays).
                if not getattr(session, "_warm_blocked", None):
                    session._warm_blocked = "compacted"
            elif outcome == "content":
                session._warm_body = body
                session._warm_identity = identity
                session._warm_blocked = None  # a usable answer of the chat lifts what stopped the cycles before it
                # The TTL counts from the last cache touch: this request's start or a
                # later during-turn ping, NOT the turn's end; otherwise a pause after
                # a long turn outlives the 5-minute entry before the next idle ping.
                session._warm_at = max(session._turn_started_at, getattr(session, "_warm_at", 0) or 0)
            session._last_request_at = time.time()
            if outcome == "content" and not side_request:
                _ensure_keepwarm(session, cfg)

    def note(event):
        nonlocal asked_tool, ended, wrote_text, thinking_seen, errored
        kind = event.get("type")
        if kind == "tool_call" and not event.get("partial"):
            asked_tool = True
        elif kind == "end":
            ended = event.get("reason")
        elif kind == "text":
            wrote_text = wrote_text or bool(str(event.get("delta") or "").strip())
        elif kind in _THINKING_EVENTS:
            thinking_seen = True
        elif kind == "error":
            # The stream ended without end_of_stream, and nothing follows this event: the turn is over as it is told
            # to the consumer, which may raise on it without ever resuming this generator.
            errored = True
            settle()

    if body.get("openAICacheKey"):
        session._turn_body = body
        # Start the scheduler with the turn, not after it: a session's first long
        # turn needs its anchored during-turn ping too.
        _ensure_keepwarm(session, cfg)
    try:
        try:
            async with contextlib.aclosing(_stream_body(cfg, session, body, param_types)) as stream:
                async for event in stream:
                    note(event)
                    yield event
        except apierr.ApiError as error:
            # Models absent from the App Builder list (legacy GPT-5, Haiku) live on
            # the Assist list, which is the same route without the agent marker.
            if (error.status != 400 or "metadata" not in body
                    or "not available right now" not in (error.message or "").lower()):
                raise
            if len(_ASSIST_ONLY) > 200:
                # Flush only self-learned names: wiping the proven seeds would make
                # known assist models pay a 400 + retry cycle all over again.
                _ASSIST_ONLY.intersection_update(_ASSIST_SEEDS)
            _ASSIST_ONLY.add(model_provider.get("model"))
            print("[assist] retry without agent marker: %s" % (error.message or "")[:80], flush=True)
            journal.emit("upstream.assist_retry", "╨Ь╨╛╨┤╨╡╨╗╤М ╤В╨╛╨╗╤М╨║╨╛ ╨▓ ╤Б╨┐╨╕╤Б╨║╨╡ Assist тАФ ╨┐╨╛╨▓╤В╨╛╤А ╨▒╨╡╨╖ ╨╝╨░╤А╨║╨╡╤А╨░", "warn",
                         model=model_provider.get("model"))
            body = {key: value for key, value in body.items() if key != "metadata"}
            if body.get("openAICacheKey"):
                session._turn_body = body
            async with contextlib.aclosing(_stream_body(cfg, session, body, param_types)) as stream:
                async for event in stream:
                    note(event)
                    yield event
        reply = getattr(session, "_direct_reply", None)
        # Recording a compaction's summary would put a branch nobody extends in place of the parent it resumed
        # (a nudged or repeated compaction, a background one beside the chat, or the chat itself after an abandoned
        # compaction would then start from nothing), so the dialog's branches stay as the chat left them.
        if client is not None and history is not None and reply is not None and not session._cancelled and not side_request:
            blocks, expected = reply
            history.record(scope, client, body["messages"], blocks, expected, parent)
    except apierr.ApiError as error:
        if error.status in (400, 422) and parent is not None:
            # A branch read back from disk is not replayed again once upstream has refused its blocks: the next
            # try takes the unsigned path instead of repeating the refusal until the stored copy expires.
            conversation_state.refuse(session, parent, error)
        # A body upstream rejected (400/413/422) must not become the keepwarm body.
        rejected = 400 <= error.status < 500 and error.status not in (401, 403, 408, 429)
        failure = error
        raise
    except BaseException as error:  # noqa: BLE001 - noted for the verdict on the turn, then raised again
        failure = error
        raise
    finally:
        settle()


def _warm_refused(session, busy):
    """(reason, detail) when no cycle may warm this session now, else None; reason is what a cycle stops with.

    last_turn_failed and compacted describe the turn that ended: they refuse a cycle between turns, and a turn in
    flight is judged when it ends, not before. cache_rewritten is the cap on what a session pays when a ping finds
    its prefix gone: it holds through a turn in flight as well and whoever asks again (the pool restarts the cycle of
    a parent at every request of its child), until a real turn gives a usable answer."""
    blocked = getattr(session, "_warm_blocked", None)
    if blocked == "cache_rewritten":
        return blocked, {}
    if busy:
        return None
    outcome = getattr(session, "_turn_outcome", None)
    if outcome not in (None, "content"):
        return "last_turn_failed", {"turn_outcome": outcome}
    return (blocked, {}) if blocked else None


def _ensure_keepwarm(session, cfg):
    if getattr(session, "_cancelled", False) or getattr(asyncio.get_running_loop(), "_relay_closing", False):
        return
    busy = bool(getattr(session, "_busy", False))
    if _warm_refused(session, busy) is not None or (
            not busy and time.time() - (getattr(session, "_warm_at", 0) or 0) > PING_DEADLINE):
        # Between turns a cycle is worth starting only for a usable answer whose cache is still alive, from a real turn of
        # the chat. Any other would stop at its first look, and say so again at every lease of a child that pins this
        # conversation.
        return
    task = getattr(session, "_warm_task", None)
    if task is None or task.done():
        # A fresh cycle gets a fresh failure budget.
        session._warm_failures = 0
        session._warm_task = asyncio.create_task(_keepwarm(session, cfg), context=contextvars.Context())


def _dump_rejected_body(payload, detail, *, cfg=None, status=400, request_id=None):
    """Land the exact upstream-rejected body on disk for offline bisection.

    A 400 from the route validator ("Invalid request format") carries no field
    detail, and client history only grows — every later retry re-sends the same
    poison. The dump is local-only and holds the user's own conversation text,
    so a repeated identical body reuses its saved set and only the newest
    `upstream_error_max_dumps` sets are kept.
    """
    try:
        cfg = cfg or {}
        folder = pathlib.Path(cfg.get("upstream_error_dir") or "logs/upstream-400")
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        key = (str(folder.resolve()), digest)
        with _DUMP_LOCK:
            known = _DUMP_RECENT.get(key)
            if known and (folder / ("%s-body.json" % known)).exists():
                print("[debug] upstream %s diagnostic: %s (same body already saved)" % (status, known), flush=True)
                return known
            request_tag = "".join(c for c in str(request_id or "unknown") if c.isalnum())[:64]
            stamp = "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), request_tag, uuid.uuid4().hex[:12])
            (folder / ("%s-body.json" % stamp)).write_bytes(payload)
            (folder / ("%s-error.txt" % stamp)).write_text(detail, encoding="utf-8")
            metadata = {**client_context.current(), "request_id": request_id,
                        "upstream_status": status, "diagnostic_id": stamp,
                        "body_sha256": digest}
            (folder / ("%s-metadata.json" % stamp)).write_text(
                json.dumps(journal.scrub(metadata), ensure_ascii=False, indent=2), encoding="utf-8")
            _DUMP_RECENT.pop(key, None)
            _DUMP_RECENT[key] = stamp
            while len(_DUMP_RECENT) > 256:
                del _DUMP_RECENT[next(iter(_DUMP_RECENT))]
            prune_diagnostics(cfg, folder, keep=stamp)
        print("[debug] upstream %s diagnostic: %s" % (status, stamp), flush=True)
        return stamp
    except Exception:  # noqa: BLE001 - diagnostics must never break the turn
        pass


_DUMP_RECENT = {}
_DUMP_LOCK = threading.Lock()


def _dump_empty_once(payload, flags, cfg, session, model=None):
    """Keep one sample per request kind and model per relay run: each dump holds
    conversation text, so repeated empty answers must not pile up on disk."""
    budget = flags["budget"]
    if budget.empty_dump_attempted:
        return budget.empty_dump_id
    kind = (client_context.current().get("request_purpose") or "generation", model)
    if kind in _EMPTY_DUMPED:
        return None
    budget.empty_dump_attempted = True
    detail = json.dumps({"reason": "empty turn: end_of_stream without content blocks",
                         "usage": budget.last.get("usage")}, ensure_ascii=False)
    stamp = _dump_rejected_body(payload, detail, cfg=cfg, status="empty",
                                request_id=client_context.current().get("request_id")
                                or getattr(session, "_request_tag", None))
    if stamp:
        # Marked only after a successful write, so a failed write is retried later.
        _EMPTY_DUMPED.add(kind)
    journal.emit("upstream.empty_turn", "Пустой ответ upstream сохранён для диагностики", "warn",
                 diagnostic_id=stamp)
    budget.empty_dump_id = stamp
    return stamp


async def _stream_body(cfg, session, body, param_types=None, *, payload=None):
    if payload is None:
        payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
    elif json.loads(payload) != body:
        raise ValueError("replay payload does not match the supplied body")
    session._direct_reply = None
    if cfg.get("direct_debug"):
        body_summary = {k: v for k, v in body.items() if k not in ("messages", "tools")}
        if "tools" in body:
            body_summary["tools_count"] = len(body["tools"])
        print("[body]", json.dumps(body_summary, ensure_ascii=False), flush=True)
    headers = session.backend.headers()
    headers["content-type"] = "application/json"
    headers["accept"] = "application/json"
    timeout = cfg.get("turn_timeout", 900)
    loop = asyncio.get_running_loop()
    budget = getattr(session, "_attempt_budget", None) or AttemptBudget()
    request_id = client_context.current().get("request_id") or getattr(session, "_request_tag", None)

    def is_cancelled():
        return session._cancelled or bool(budget.cancel_event and budget.cancel_event.is_set())
    # Bounded so a stalled consumer (slow or gone client) cannot balloon memory:
    # the upstream worker blocks on hand-off, which backpressures the TCP read.
    # One queue per attempt: a retried attempt must never read the previous
    # worker's terminal sentinel (it made every retry end empty at once).
    holder = {"response": None, "queue": None, "trace": None}

    def hand_off_final(item, queue=None):
        """Land a terminal item (sentinel/error/cancel) without blocking. Safe from
        any thread; sacrifices one buffered data line if the queue is full."""
        queue = queue if queue is not None else holder["queue"]
        if queue is None:
            return

        def _put():
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                pass
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:  # a cancelled daemon reader can outlive the loop
            pass

    def abort_direct():
        _force_close_response(holder.get("response"))
        hand_off_final(asyncio.CancelledError())

    session._active_direct_abort = abort_direct

    def worker(queue):
        try:
            attempt = 0
            while True:
                if is_cancelled():
                    return
                budget.take(is_cancelled, session.backend)
                trace = AttemptTrace(cfg, budget, request_id, body.get("model"), payload)
                holder["trace"] = trace
                request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                                 data=payload, headers=headers, method="POST")
                trace.wire.request(headers)
                try:
                    response = urllib.request.urlopen(request, timeout=timeout)
                    break
                except urllib.error.HTTPError as exc:
                    trace.response(exc, exc.code)
                    holder["response"] = exc
                    detail_bytes = exc.read(16384)
                    trace.wire.raw(detail_bytes)
                    trace.wire.event("http_error_body_read", limit_bytes=16384,
                                     bytes_read=len(detail_bytes),
                                     may_be_incomplete=len(detail_bytes) == 16384)
                    detail = detail_bytes.decode("utf-8", "replace")
                    diagnostic = None
                    if exc.code in (400, 422):
                        diagnostic = _dump_rejected_body(
                            payload, detail, cfg=cfg, status=exc.code,
                            request_id=client_context.current().get("request_id") or getattr(session, "_request_tag", None))
                        journal.emit("upstream.rejected", detail[:1000], "warn", upstream_status=exc.code,
                                     diagnostic_id=diagnostic, model=body.get("model"),
                                     effort=body.get("reasoningEffort"),
                                     upstream_request_id=exc.headers.get("request-id") or exc.headers.get("x-request-id"))
                    if exc.code in (401, 403) and attempt == 0:
                        attempt = 1
                        # The 401 may come from a header snapshot taken just before
                        # a keepalive rotation: pass the rejected cookie so the
                        # refresher adopts already-rotated credentials despite the
                        # MIN_INTERVAL gate.
                        if session.backend.refresher.refresh(rejected_cookie=headers.get("cookie")):
                            trace.finish("auth_retry")
                            exc.close()
                            headers.update(session.backend.headers())
                            continue
                        error = apierr.upstream_auth_failed(detail[:400])
                        error.upstream_status = exc.code
                        raise error
                    holder["response"] = exc
                    error = _api_error(exc.code, detail, exc.headers)
                    error.upstream_status = exc.code
                    error.diagnostic_id = diagnostic
                    raise error
            holder["response"] = response
            trace.response(response)
            if is_cancelled():
                _force_close_response(response)
                return
            for raw in response:
                trace.wire.raw(raw)
                if is_cancelled():
                    break
                line = raw.strip()
                if not line:
                    continue
                trace.observe(line)
                # Bounded hand-off: block this worker when the consumer is slower
                # than upstream instead of buffering the whole stream in memory.
                if not _bounded_put(loop, queue, line, is_cancelled):
                    # Dropped or cancelled: stop reading. The missing end_of_stream
                    # surfaces as a truncated-stream error, never a silent hole.
                    break
            trace.wire.event("reader_end", cancelled=is_cancelled())
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            trace = holder.get("trace")
            if trace:
                trace.wire.event("reader_error", exception_type=type(exc).__name__)
            if not is_cancelled():
                # urlopen wraps DNS, refused, TLS and send failures in URLError: the request never
                # reached upstream. Any other failure (a read timeout, a closed connection)
                # happens after it was sent and may still be billed.
                unsent = isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError)
                if isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError, http.client.HTTPException,
                                    OSError)):  # OSError covers ssl.SSLError (TLS drops mid-stream)
                    timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
                    exc = (apierr.upstream_timeout("Retool response timed out") if timed_out else
                           apierr.upstream_error("Retool connection failed: %s" % type(exc).__name__))
                if unsent:
                    exc.request_sent = False
                hand_off_final(exc, queue)
        finally:
            _force_close_response(holder.get("response"))
            hand_off_final(None, queue)

    text, thinking = [], []
    saw_end = False
    # Client service checks (Auto mode, titles) fail deterministically when empty:
    # re-sending the same body only pays for the same empty answer again.
    service_request = client_context.current().get("request_purpose") in client_context.SERVICE_KINDS
    try:
        max_empty_retries = _int_setting(cfg, "upstream_empty_retries", 1)
        empty_retries = 0
        while True:
            usable = False
            thinking_seen = False
            # Events of an attempt are held back until it proves usable, so a retried
            # attempt can never leak thinking blocks, signatures or blank text.
            held = []
            may_retry = (not service_request and empty_retries < max_empty_retries and budget.remaining > 1)
            yield {"type": "usage_start"}
            queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
            holder["queue"] = queue
            previous_trace = holder.get("trace")
            task = loop.run_in_executor(getattr(loop, "_relay_io_executor", None),
                                        contextvars.copy_context().run, worker, queue)
            flags = {"budget": budget}
            failure = None
            try:
                async for event in _drain(cfg, session, queue, text, thinking, param_types, flags,
                                          cache_scope=(body.get("model"), body.get("openAICacheKey"))):
                    trace = holder.get("trace")
                    if trace:
                        trace.wire.event("parser_event", event=event)
                    kind = event.get("type")
                    if kind == "thinking_start":
                        # Display status is live even while this attempt's content
                        # is buffered. Never expose a discarded signature or actual
                        # thinking just to start the client's waiting indicator.
                        yield {"type": "thinking_status", "source": "upstream"}
                    if kind == "tool_call" or (kind == "text" and event.get("delta", "").strip()):
                        usable = True
                    if kind in ("thinking", "thinking_start", "thinking_signature", "thinking_heartbeat"):
                        thinking_seen = True
                    if usable:
                        for pending in held:
                            yield pending
                        held.clear()
                        yield event
                    elif kind in _LIVE_EVENTS or not may_retry:
                        yield event
                    else:
                        held.append(event)
            except BaseException as exc:
                failure = exc
                if not isinstance(exc, Exception):
                    raise
            finally:
                current_trace = holder.get("trace")
                if current_trace is previous_trace:
                    current_trace = None  # this attempt stopped before creating a trace of its own
                status = getattr(current_trace, "http_status", None)
                # Without a status the request may still be in flight, unless it provably never left.
                possibly_billed = (current_trace is not None and (
                    200 <= status < 300 if status is not None else getattr(failure, "request_sent", True)))
                if not flags.get("credits_reported") and (flags.get("usage_seen") or possibly_billed):
                    flags['cost_unknown'] = True
                    mark_unknown = getattr(getattr(session, "_credits_manager", None), "mark_usage_unknown", None)
                    if mark_unknown:
                        mark_unknown()
                if not task.done():
                    _force_close_response(holder.get("response"))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except Exception:
                    pass
                trace = holder.get("trace")
                if trace is not None:
                    outcome = attempt_outcome(
                        cancelled=is_cancelled() or isinstance(failure, asyncio.CancelledError),
                        failed=bool(failure), ended=flags.get("end"), usable=usable, thinking_seen=thinking_seen)
                    trace.finish(outcome)
                if isinstance(failure, apierr.ApiError):
                    budget.attach(failure)
            if flags.get('cost_unknown') and not flags.get('usage_seen'):
                # Include possibly billed failures/truncations before failover.
                yield {'type': 'usage', 'usage': {}}
            if failure is not None:
                raise failure
            saw_end = bool(flags.get("end"))
            if is_cancelled():
                yield {"type": "end", "reason": "cancelled"}
                return
            if saw_end and not flags.get('usage_seen') and not flags.get('cost_unknown'):
                # An empty/no-telemetry attempt can still have been billed. Keep
                # its unknown price visible when a later retry reports a cost.
                yield {'type': 'usage', 'usage': {}}
            if not usable and saw_end:
                _dump_empty_once(payload, flags, cfg, session, body.get("model"))
            if (usable or not saw_end or service_request or not budget.remaining
                    or empty_retries >= max_empty_retries):
                # This attempt is the final one: whatever it produced is the answer.
                for pending in held:
                    yield pending
                held.clear()
                break
            empty_retries += 1
            # Retry the same request, including instructions, tool choice and
            # full history. Never turn the user's task into a status-only job.
            text.clear()
            thinking.clear()
            holder["response"] = None
        if text:
            yield {"type": "final_text", "text": "".join(text)}
        if not saw_end and not is_cancelled():
            journal.emit("upstream.stream_truncated",
                         "Поток upstream оборвался без end_of_stream — ход завершён ошибкой", "warn",
                         model=body.get("model"), thread=journal.tag(getattr(session, "thread_id", None)))
            # An aborted stream must not masquerade as a clean stop: surface the
            # failure instead of a successful end. No auto-retry — tool calls may
            # already have been streamed to the client.
            yield {"type": "error", "error": budget.attach(apierr.upstream_error(
                "upstream stream ended without end_of_stream; the response may be incomplete"))}
            return
        blocks = flags.get("assistant_blocks")
        if usable and blocks and not is_cancelled():
            session._direct_reply = (blocks, native_history.echo(blocks, param_types or {}, coerce_arguments))
        elif usable and "assistant_blocks" in flags and blocks is None and not is_cancelled():
            journal.emit("native_history.unavailable", "Ответ upstream нельзя сохранить для нативного продолжения",
                         "warn", reason="unsupported_or_incomplete_blocks")
        yield {"type": "end", "reason": "stop"}
    finally:
        session._active_direct_abort = None


async def _drain(cfg, session, queue, text, thinking, param_types=None, flags=None, *, cache_scope=None):
    started = loop_now()
    think_started = None
    flags = {} if flags is None else flags
    """Consume one attempt's NDJSON lines, yielding normalised events.

    Calls are keyed by id: a provider may interleave several tool calls in one
    turn, and a single slot would merge their arguments.
    """
    stack = []  # open calls, newest last; deltas belong to the newest open block
    calls = {}
    current = None
    call_seq = 0  # monotonic: a finished call leaves `calls`, len() would repeat ids
    capture = native_history.ReplyCapture() if getattr(session, "_native_client_messages", None) is not None else None
    # The relay request id matches the `relay <id> POST ...` console lines:
    # concurrent requests of one conversation interleave in the same console.
    req_id = getattr(session, "_request_tag", None) or getattr(session, "thread_id", None) or "req"
    monitor = getattr(session, "_console_cache_monitor", None)
    if monitor is None:
        monitor = session._console_cache_monitor = console_log.CacheMonitor()

    def _final_call_event(key):
        call = calls.pop(key, None)
        if call is None:
            return None
        streamed = call["out"] + call["stream"].finish()
        raw = "".join(call["args"])
        # The streamed text is already coerced; fall back for non-JSON or empty input.
        try:
            json.loads(streamed)
            final = streamed
        except ValueError:
            final = coerce_arguments(raw, (param_types or {}).get(call["name"]))
        return {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": final}
    while True:
        if session._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        try:
            item = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            if think_started is not None and not text and not stack and not calls:
                elapsed = loop_now() - think_started
                console_log.write(f"[{req_id}] Waiting for upstream reasoning output... ({round(elapsed)}s elapsed)")
                journal.emit("upstream.thinking_heartbeat",
                             f"Ожидаем ответ upstream после начала thinking ({round(elapsed)} с)",
                             "debug", elapsed=round(elapsed, 1), thread=journal.tag(req_id))
                yield {"type": "thinking_heartbeat", "elapsed": round(elapsed, 1)}
            continue
        if item is None:
            return
        if isinstance(item, asyncio.CancelledError) or session._cancelled:
            yield {"type": "end", "reason": "cancelled"}
            return
        if isinstance(item, BaseException):
            raise item

        if cfg.get("direct_raw"):
            print("[raw]", str(item)[:200], flush=True)
        try:
            event = json.loads(item)
        except Exception:
            continue
        if not isinstance(event, dict):
            # A valid JSON line that is not an object (a number, a list) must be
            # skipped, not kill the turn with an AttributeError.
            continue
        if capture is not None:
            try:
                capture.feed(event)
            except (TypeError, ValueError, AttributeError):
                # Unknown provider extensions must not corrupt signed history or
                # change the existing stream parser's error handling.
                capture.valid = False
        if event.get("event") == "end_of_stream" or event.get("type") == "end_of_stream":
            flags["end"] = True
            if capture is not None:
                flags["assistant_blocks"] = capture.finish()
            # A stream can end with calls still open: finalize them exactly like
            # content_block_end would, or the client keeps a dangling partial.
            while stack:
                final_event = _final_call_event(stack.pop())
                if final_event is not None:
                    yield final_event
            current = None
            return
        kind = event.get("event")
        if kind == "content_block_start":
            if event.get("type") == "thinking":
                # Opus 5.5 streams no thinking text, only this early start and a final
                # signature; surfacing the start replaces silence with "thinking".
                think_started = loop_now()
                console_log.write(f"[{req_id}] [thinking] started source=upstream event=content_block_start")
                journal.emit("upstream.thinking_signal", "Upstream сообщил начало блока thinking", "info",
                             source="upstream", signal="content_block_start", thread=journal.tag(req_id))
                yield {"type": "thinking_start"}
            elif event.get("type") == "tool_call":
                think_started = None
                if event.get("toolCallId"):
                    current = event["toolCallId"]
                else:
                    current = "call_%d" % call_seq
                    call_seq += 1
                # Store the resolved key as the id: upstream may omit toolCallId,
                # and adapters merge calls by id — an absent id would glue every
                # id-less call into one "call_0".
                calls[current] = {"id": current, "name": event.get("toolName"), "args": [],
                                  "out": "", "stream": ArgStream((param_types or {}).get(event.get("toolName")))}
                stack.append(current)
                # Announce the call immediately so clients render it while arguments generate.
                yield {"type": "tool_call", "id": current, "name": event.get("toolName"),
                       "arguments": "", "partial": True}
        elif kind == "content_block_delta":
            block = event.get("type")
            if block == "text":
                piece = event.get("partialText") or ""
                if piece and cfg.get("direct_debug") and not text:
                    print("[ttft] %.2fs" % (time.time() - started), flush=True)
                if piece:
                    text.append(piece)
                    yield {"type": "text", "delta": piece}
            elif block == "thinking":
                if think_started is None:
                    think_started = loop_now()
                piece = event.get("partialThinking") or ""
                if piece:
                    thinking.append(piece)
                    yield {"type": "thinking", "delta": piece}
            elif block == "signature":
                think_started = None
                if event.get("partialSignature"):
                    yield {"type": "thinking_signature", "signature": event["partialSignature"]}
            elif block == "tool_call" and current in calls:
                call = calls[current]
                piece = event.get("partialInputs") or ""
                call["args"].append(piece)
                emitted = call["stream"].feed(piece)
                if emitted:
                    call["out"] += emitted
                    yield {"type": "tool_call", "id": call["id"], "name": call["name"],
                           "arguments": call["out"], "partial": True}
        elif kind == "content_block_end":
            btype = event.get("type")
            if btype is not None and btype != "tool_call":
                # Ends of text/thinking blocks must not finalize an open tool call:
                # the streamed arguments would be cut short and later deltas lost.
                continue
            ended = event.get("toolCallId")
            if ended is not None and ended not in calls:
                # An end for an unknown (already finished) call must not close
                # whichever other call happens to be on top of the stack.
                continue
            if ended is not None and ended in calls:
                key = ended
                if ended in stack:
                    stack.remove(ended)
            else:
                key = stack.pop() if stack else current
            final_event = _final_call_event(key)
            if final_event is not None:
                yield final_event
            current = stack[-1] if stack else None
        elif kind == "metadata":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if usage:
                flags["usage_seen"] = True
                flags.setdefault("last_usage", {}).update(usage)
            if any(k in usage for k in ("promptTokensReadFromCache", "promptTokensWrittenToCache", "creditsUsed")):
                console_log.write(monitor.line(req_id, usage, scope=cache_scope) +
                                  (" " + client_context.label() if client_context.label() else ""))
            credits_value = console_log.number(usage.get("creditsUsed"))
            normalized = {target: usage[source] for source, target in (
                ('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens'),
                ('promptTokensReadFromCache', 'cache_read'), ('promptTokensWrittenToCache', 'cache_write'))
                if source in usage}
            # Preserve a genuine provider counter if a route explicitly supplies
            # it; current Retool captures normally omit this optional information.
            reported_reasoning = usage.get('reasoningTokens')
            details = usage.get('output_tokens_details')
            if reported_reasoning is None and isinstance(details, dict):
                reported_reasoning = details.get('reasoning_tokens', details.get('thinking_tokens'))
            if reported_reasoning is not None:
                normalized['reasoning_tokens'] = reported_reasoning
            if credits_value is not None:
                # Metadata reports cumulative snapshots for ONE HTTP attempt.
                # Emit only new cost, keeping distinct retries separately billed.
                previous = flags.get('credits_snapshot')
                flags['credits_snapshot'] = max(previous or 0, credits_value)
                delta = credits_value - (previous or 0)
                flags["credits_reported"] = True
                if previous is None or delta > 0:
                    yield {"type": "credits", "value": max(0, delta), "usage": normalized}
                elif normalized:
                    yield {"type": "usage", "usage": normalized, "credits_reported": True}
            elif usage:
                yield {"type": "usage", "usage": normalized}
        elif kind == "error":
            raise _error_from_event(event)


def _http_error(status, detail):
    text = detail[:300]
    try:
        parsed = json.loads(detail)
        text = str(parsed.get("message") or text)[:300]
    except Exception:
        pass
    if status == 429:
        return (429, "rate_limit_error", text)
    if status in (401, 403):
        return (502, "upstream_auth_error", text)
    if status in (400, 422):
        return (400, "invalid_request_error", text)
    return (502, "upstream_error", text)


def _api_error(status, detail, headers=None):
    error = apierr.ApiError(*_http_error(status, detail))
    if status == 429 and apierr.is_hourly_limit(detail):
        # The hourly managed-key quota is spent: the orchestrator parks this account and moves the turn.
        error.account_quota = True
        error.retry_after = apierr.retry_after_seconds((headers or {}).get("retry-after"))
    return error
