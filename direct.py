"""Direct Retool inference transport (copilotStreamGPT, NDJSON) with prompt caching.

Drop-in alternative to the agent WebSocket path: same normalised event stream, but a
plain HTTP POST. The stable system head is sent as a cached block so a large project
context is billed once and reused, keeping time-to-first-token flat.
"""
import asyncio
import contextlib
import contextvars
import decimal
import functools
import hashlib
import http.client
import json
import math
import pathlib
import random
import re
import socket
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

import apierr
import conversation_state
import credits
import forced_choice
import journal
import client_context
import console_log
import native_history
import models as model_catalog
import summary_guard
import upstream_diagnostics
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


# Retool's "Retool models are rate limited right now. Please try again shortly." (HTTP 429, or an error event inside a 200): a
# limit of the moment on the models, not the account's hourly quota (that one parks the account: apierr.is_account_quota).
# The request is refused before anything is generated, so asking again on the same account costs nothing, and moving to
# another account would start the conversation's history and cache from nothing (79,000 tokens in one measured case).
# These are the pauses before the 1st, 2nd and 3rd retry, on top of the 0.5, 1 and 2 seconds that AttemptBudget.take
# adds before any further POST of a step.
RATE_LIMIT_WAITS = (1.5, 3.0, 6.0)


def _refused_for_now(error):
    return (isinstance(error, apierr.ApiError) and error.status == 429 and error.code == "rate_limit_error"
            and not apierr.is_account_quota(error))


async def _pause(seconds, is_cancelled):
    """Wait `seconds` in short slices; False when the request was cancelled meanwhile."""
    deadline = loop_now() + seconds
    while loop_now() < deadline:
        if is_cancelled():
            return False
        await asyncio.sleep(min(0.25, max(0.0, deadline - loop_now())))
    return not is_cancelled()


_ASSIST_ONLY = {"claude-haiku-4-5-20251001", "claude-haiku", 'gpt-4.1',
                'gpt-5', 'gpt-5-mini', 'gpt-5-nano', 'gpt-5.1', 'gpt-5.2'}
_ASSIST_SEEDS = frozenset(_ASSIST_ONLY)
# Whether upstream refused a body that carried a cache hint on the result of a tool; once it did, no body carries one for
# the rest of the run (see _hints_tool_results and the retry in iter_events).
_TOOL_RESULT_HINTS = {"refused": False}
# Whether upstream refused a body whose branch had lost a call (native_history.deliverable) and answered the same request
# without that branch; once it did, no reply is cut down that way for the rest of the run.
_PRUNING = {"refused": False}


def _unmarked(body):
    """The body without the App Builder marker (metadata.sandpackAgent): a model of the Assist list (_ASSIST_ONLY) is served
    by the same route without it, and the route of the marker answers it with "not available right now"."""
    return {key: value for key, value in body.items() if key != "metadata"}


def _provider_of(model):
    """The provider the catalog gives this model, or None for a model it does not know."""
    try:
        return model_catalog.resolve(model, strict=True)[0].get("provider")
    except apierr.ApiError:
        return None


def _anthropic_route(model):
    """Whether Anthropic's API serves this model: the route with cache breakpoints and signed thinking blocks."""
    return _provider_of(model) == "anthropic"


def _live_thinking(model):
    """Whether this model's reasoning text may reach the client while its attempt can still be retried.

    Claude's thinking carries a signature that belongs to one history and must never be shown twice, so all of it waits
    until the attempt has proved usable. The reasoning of the other routes is display text: shown at once, a retried
    attempt costs the client a second pass of it and nothing else, while held back it leaves the client waiting for the
    first word of the answer (a3, GLM: 9.1 s of silence and then the whole reasoning in one burst, upstream having sent
    it continuously from 1.5 s to 10.6 s). A model the catalog does not know keeps the old behaviour."""
    provider = _provider_of(model)
    return provider is not None and provider != "anthropic"


def _cold_from_the_start(model):
    """Whether a request on this route takes the client's own messages and roles from its first request on.

    GLM's reasoning arrives without a signature, so a reply that reasons is never kept as a branch and the next request
    is rebuilt from the client's transcript. That transcript was taken only once it held a tool result; before, the
    history went flattened (`User: ...`, `Assistant: ...` as user text), so the first tool result re-rendered every earlier
    message and the provider read none of it: 11,094 tokens re-read uncached in a Harness conversation on GLM-5.3
    (72b51698, 1.1 credits) and three more such flips in two hours (journal watch of 2026-11-07); a long chat that starts
    using tools late pays its whole history once. On such a route the history has one form from the first request."""
    return _provider_of(model) == "retool-fast"


def _hints_tool_results(model):
    """Whether the result of a tool may carry a cache hint in a request for this model.

    Only the Anthropic route has breakpoints (the others cache by themselves), and only until upstream has refused a body
    that carried one: the shape of that hint was never seen accepted, which is why it can switch itself off."""
    return not _TOOL_RESULT_HINTS["refused"] and _anthropic_route(model)


def _has_tool_result_hint(messages):
    """Whether a tool result of these upstream messages carries a cache hint."""
    for message in messages:
        content = message.get("content")
        result = content.get("toolResult") if isinstance(content, dict) and message.get("role") == "tool" else None
        if isinstance(result, list) and any(isinstance(block, dict) and block.get("cache") for block in result):
            return True
    return False


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
            try:
                written = _js_number(float(item))
            except OverflowError:  # past the range of a double: JavaScript holds Infinity, and writes null
                written = "null"
            delta += len(written) - len(str(item))
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


def _pointer(root, ref):
    """What a local reference (#/$defs/Name) points at inside `root`; None for one that points elsewhere or nowhere."""
    if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
        return None
    node = root
    for part in ref.split("/")[1:]:
        part = urllib.parse.unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


_LIMIT_KEYS = ("pattern", "format", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
               "exclusiveMaximum", "multipleOf", "minItems", "maxItems")


def _merged(base, extra):
    """Two schemas that both hold, as one: properties and required add up, an enum narrows, anything else `extra` decides.

    A limit both of them set (two patterns, as Claude Code writes them in allOf) is kept beside the first, in an allOf
    that only _limits reads, so that the model is told both."""
    result = dict(base)
    for key, value in extra.items():
        if key == "properties" and isinstance(value, dict) and isinstance(result.get("properties"), dict):
            result["properties"] = {**result["properties"],
                                    **{name: {"allOf": [result["properties"][name], sub]} if name in result["properties"]
                                       else sub for name, sub in value.items()}}
        elif key == "required" and isinstance(value, list) and isinstance(result.get("required"), list):
            result["required"] = list(dict.fromkeys([*result["required"], *value]))
        elif key == "enum" and isinstance(value, list) and isinstance(result.get("enum"), list):
            result["enum"] = [item for item in result["enum"] if item in value]
        elif key == "allOf" and isinstance(value, list):
            result["allOf"] = [*(result["allOf"] if isinstance(result.get("allOf"), list) else []), *value]
        elif key in _LIMIT_KEYS and key in result and result[key] != value:
            result["allOf"] = [*(result["allOf"] if isinstance(result.get("allOf"), list) else []), {key: value}]
        else:
            result[key] = value
    return result


def _deref(schema, root, trail=()):
    """(the schema with its $ref followed and its allOf merged in, the references expanded to make it).

    Pydantic, zod and the MCP SDKs describe a nested object once, under $defs, and point at it, and give such a field a
    description with allOf. The Retool shape of those fields was a plain string, and the model wrote "Name: Ada, Age: 36"
    where the client wanted an object (protocol test run of 2026-11-06; ultracode audit, C6). What a field says itself
    (its description, its default) wins over what the referenced schema says. A reference that points outside the
    schema is dropped, and one that points into itself is a plain object from the second step on, so a schema
    that contains itself ends."""
    if not isinstance(schema, dict):
        return schema, trail
    result, used = {}, trail
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in trail:
            result = {"type": "object"}
        else:
            found = _pointer(root, ref)
            if isinstance(found, dict):
                result, used = _deref(found, root, trail + (ref,))
    for part in schema["allOf"] if isinstance(schema.get("allOf"), list) else ():
        part, more = _deref(part, root, trail)
        if isinstance(part, dict):
            result = _merged(result, part)
            used = used + tuple(item for item in more if item not in used)
    return _merged(result, {key: value for key, value in schema.items() if key not in ("$ref", "allOf")}), used


def _is_map(schema):
    """Whether a schema (its $ref and allOf already followed) is a dictionary with keys of its own: additionalProperties as a
    schema or true, patternProperties, or an object that names no properties at all. An object that must stay empty
    (additionalProperties false) and one with properties of its own are not."""
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        return False
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if not all(item in ("object", "null", None) for item in kinds):
        return False
    extra = schema.get("additionalProperties")
    if schema.get("patternProperties") or isinstance(extra, dict) or extra is True:
        return True
    return "object" in kinds and extra is not False


# What the model reads at the end of the description of a dictionary field that is declared as a string (see _param_schema),
# and what marks such a field for the code that turns the string back into the dictionary (_marked).
MAP_NOTE = "This is a map with any keys: pass it as one string that holds a JSON object, for example {\"name\": \"value\"} (or {} when empty)."


def _marked(spec):
    """Whether a Retool param node is a dictionary written as a string (MAP_NOTE ends its description).

    A strict tool's field that may also be null is that string inside "X or null" (_with_null), whose outer node carries
    the description: it is marked too."""
    if not isinstance(spec, dict) or MAP_NOTE not in str(spec.get("description") or ""):
        return False
    if spec.get("type") == "anyOf":
        branches = [branch for branch in spec.get("anyOf") or [] if isinstance(branch, dict) and branch.get("type") != "null"]
        return len(branches) == 1 and branches[0].get("type") == "string"
    return spec.get("type") == "string"


def _has_marked(spec):
    """Whether a Retool param node is, or holds somewhere inside it, a dictionary written as a string."""
    if not isinstance(spec, dict):
        return False
    if _marked(spec):
        return True
    if isinstance(spec.get("properties"), dict) and any(_has_marked(sub) for sub in spec["properties"].values()):
        return True
    if _has_marked(spec.get("items")):
        return True
    return isinstance(spec.get("anyOf"), list) and any(_has_marked(sub) for sub in spec["anyOf"])


def _refuse_constant(name):
    raise ValueError("%s is not JSON" % name)


def _decode_map(text):
    """The dict a model wrote as a JSON string, an empty dict for an empty string, _JSON_NULL for the text "null"; None when
    the text is no JSON object (the string then stays as it is, and the client's own check says what is wrong with it)."""
    text = text.strip()
    if not text:
        return {}
    if text == "null":
        return _JSON_NULL
    try:
        value = json.loads(text, parse_constant=_refuse_constant)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _decode_in(value, spec):
    """(value with every dictionary written as a string under `spec` turned into the dict, whether anything changed)."""
    if not isinstance(spec, dict):
        return value, False
    if _marked(spec):
        decoded = _decode_map(value) if isinstance(value, str) else None
        return (value, False) if decoded is None else (None if decoded is _JSON_NULL else decoded, True)
    kind = spec.get("type")
    if kind == "object" and isinstance(value, dict) and isinstance(spec.get("properties"), dict):
        found, result = False, dict(value)
        for name, sub in spec["properties"].items():
            if name in result:
                result[name], changed = _decode_in(result[name], sub)
                found = found or changed
        return (result, True) if found else (value, False)
    if kind == "array" and isinstance(value, list):
        found, result = False, []
        for item in value:
            item, changed = _decode_in(item, spec.get("items"))
            result.append(item)
            found = found or changed
        return (result, True) if found else (value, False)
    if kind == "anyOf" and isinstance(spec.get("anyOf"), list):
        for branch in spec["anyOf"]:
            result, changed = _decode_in(value, branch)
            if changed:
                return result, True
    return value, False


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


_STRICT_TOOL = contextvars.ContextVar("proxi_strict_tool", default=False)


def _allows_null(schema):
    """True when a schema says "X or null": a union with a null branch and another, a type list that holds "null" and
    another type, or an enum with null among other values."""
    kind = schema.get("type")
    if isinstance(kind, list):
        return "null" in kind and any(item != "null" for item in kind)
    options = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else schema.get("oneOf")
    if isinstance(options, list):
        branches = [option for option in options if isinstance(option, dict)]
        if any(option.get("type") == "null" for option in branches):
            return any(option.get("type") != "null" for option in branches)
    values = schema.get("enum")
    return isinstance(values, list) and None in values and any(value is not None for value in values)


def _with_null(schema, node):
    """`node`, what _param_schema made of `schema`, as an anyOf that still has a null branch when the schema allows null."""
    if not _allows_null(schema) or node.get("type") == "null":
        return node
    null = {"type": "null", "required": False}
    if node.get("type") == "anyOf":
        if any(isinstance(branch, dict) and branch.get("type") == "null" for branch in node["anyOf"]):
            return node
        return dict(node, anyOf=list(node["anyOf"]) + [null])
    inner = {key: value for key, value in node.items() if key not in ("required", "description")}
    outer = {"type": "anyOf", "anyOf": [dict(inner, required=False), null], "required": node["required"]}
    if "description" in node:
        outer["description"] = node["description"]
    return outer


def _keeps_null(convert):
    """Declare a field that may be null as "X or null" in a strict tool, at every depth.

    Retool's strict mode makes every parameter required, and a field declared as plain X left the model no way to say
    "none": it gave a placeholder (live, 2026-11-06: max_results 10, order "desc", since "1970-01-01" for a search that
    wanted none of them) or refused to call the tool. With the null branch, which Retool accepts, both said null
    (ultracode audit of the relay, C7). The children of a field are converted by the same function, so one wrapper
    covers the properties and items below it; a tool that is not strict keeps the plain type, as its optional field can
    be left out. Whether the field may be null is asked of the schema it stands for, its $ref and allOf followed: a
    reference to a nullable definition is as nullable as the definition."""
    @functools.wraps(convert)
    def wrapper(schema, required, *args, **kwargs):
        node = convert(schema, required, *args, **kwargs)
        if not (_STRICT_TOOL.get() and isinstance(schema, dict)):
            return node
        root = args[0] if args else kwargs.get("root")
        trail = args[1] if len(args) > 1 else kwargs.get("trail", ())
        return _with_null(_deref(schema, schema if root is None else root, trail)[0], node)
    return wrapper


@_keeps_null
def _param_schema(schema, required, root=None, trail=(), maps=False):
    """Convert one JSON-Schema property to Retool's typed param shape.

    Retool accepts only literal/string/number/boolean/null/array/object/enum/anyOf (its enum holds strings), so an
    unsupported type must degrade instead of emitting "any". The values a field is restricted to reach the model:
    strings as an enum, others (numbers, booleans) in the description, which the model reads as well. A client's MCP
    server checks them, and a guessed value fails the call (wrap_up of Claude Desktop: "Invalid option").

    "X or null" (anyOf/oneOf with a null branch, or a type list with "null") is how Pydantic and many MCP servers write
    an optional field: the field keeps the type of X. Before, it became a plain string, and the model sent "" for the
    booleans and lists of tavily_search. Another union of types becomes Retool's anyOf, a type list of several types
    included: "number or string" used to keep the number alone, and the string "007" went to the client as 7.

    `root` is the schema of the whole tool, where a $ref points (see _deref); `trail` the references being expanded.

    `maps` (a strict tool) turns a dictionary with keys of its own (_is_map) into a string that holds a JSON object, and
    says so in the description (MAP_NOTE): Retool's shape has no map, and a strict tool makes the provider write the
    empty object for the one it has, so the model could never fill it (gpt-6.1-sol, real schemas of Claude Code, Exa and
    Codex: `{}`, `null` or a run of blanks in 30 of 30 requests). A string cannot be emptied that way. The code that
    reads the call gives the client the dict back (coerce_arguments, ArgStream). A tool that is not strict keeps the
    empty object: asked for keys, the models wrote them into it in 147 of 171 requests on the real schemas (all three
    dialects, streamed or not); the 24 that did not were a tool the model declined (gpt-6.1-sol, AskUserQuestion) and
    fields that an older converter wrote as strings (allOf, C6), none an empty map.
    """
    if not isinstance(schema, dict):
        return {"type": "string", "required": bool(required)}
    if root is None:
        root = schema
    schema, trail = _deref(schema, root, trail)
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [item for item in kind if item in _KNOWN_TYPES]
        spread = list(dict.fromkeys("number" if item == "integer" else item for item in kinds if item != "null"))
        if len(spread) > 1 and _allowed_values(schema) is None and not schema.get("anyOf") and not schema.get("oneOf"):
            rest = {key: value for key, value in schema.items() if key not in ("type", "description")}
            schema = {"anyOf": [{**rest, "type": item} for item in spread],
                      **({"description": schema["description"]} if schema.get("description") else {})}
            kind = None
        else:
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
            return _param_schema(merged, required, root, trail, maps)
        if branches:
            # A dictionary that is one kind among several stays the object it was: beside a plain string it would be a
            # second string, and the code that reads the call could not tell which of the two the model meant.
            node = {"type": "anyOf", "anyOf": [
                _param_schema(branch, False, root, trail, maps and not _is_map(_deref(branch, root, trail)[0]))
                for branch in branches]}
            if schema.get("description"):
                node["description"] = str(schema["description"])
            node["required"] = bool(required)
            return node
    note, as_text = None, False
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
            node = {"type": "array", "items": _param_schema(schema.get("items") or {}, False, root, trail, maps)}
        elif maps and _is_map(schema):
            as_text = True
        elif kind == "object" or schema.get("properties") or schema.get("additionalProperties"):
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            node = {"type": "object", "properties": {
                name: _param_schema(sub, name in _required_names(schema), root, trail, maps)
                for name, sub in properties.items()}}
    description = " ".join(part for part in (str(schema["description"]) if schema.get("description") else "", note,
                                             _limits(schema), MAP_NOTE if as_text else "") if part)
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
        root = function.get("parameters") if isinstance(function.get("parameters"), dict) else {}
        schema, trail = _deref(root, root)
        required = set(_required_names(schema))
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        maps = bool(function.get("strict"))  # see _param_schema
        token = _STRICT_TOOL.set(function.get("strict") is True)
        try:
            for name in sorted(properties.keys()):
                params[name] = _param_schema(properties[name], name in required, root, trail, maps)
        finally:
            _STRICT_TOOL.reset(token)
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
        elif _has_marked(spec):
            # A dictionary the model wrote as a string inside this array or object.
            data[key], inside = _decode_in(value, spec)
            changed = changed or inside
    return json.dumps(data, ensure_ascii=False) if changed else text


# What _coerce_value returns for a string that stands for JSON null (None already means "leave the string").
_JSON_NULL = object()


def _coerced_kind(spec):
    """The type a string is coerced to for a field. A field that is "X or null" (an anyOf of X and null, how a strict
    tool declares it) is an X, as it was before the null branch; a union of two types is not guessed at."""
    kind = spec.get("type") if isinstance(spec, dict) else None
    if kind == "anyOf":
        kinds = {branch.get("type") for branch in spec.get("anyOf") or []
                 if isinstance(branch, dict) and branch.get("type") != "null"}
        return kinds.pop() if len(kinds) == 1 else None
    return kind


# The text of an integer as JSON writes it: no sign but a minus, no leading zero, no exponent, within the safe integers.
_CANONICAL_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]{0,14})")


def _number_or_text(spec):
    """True for a field that is "number or string" (a client's integer or string, possibly also null)."""
    if not isinstance(spec, dict) or spec.get("type") != "anyOf":
        return False
    kinds = {branch.get("type") for branch in spec.get("anyOf") or [] if isinstance(branch, dict) and branch.get("type") != "null"}
    return kinds == {"number", "string"}


def _coerce_value(value, spec):
    """Coerce one string scalar like coerce_arguments; None when it stays a string."""
    if _marked(spec):
        return _decode_map(value)
    if _number_or_text(spec):
        # The model writes the number of a "number or string" field as text, and a client tool that branches on the type
        # got the string "42" for 42 (an MCP server of the Claude Code test run of 2026-11-07, 72e6f9f0a91a, 993110d098d2,
        # 13dcf0681ac0). The canonical text of an integer is that integer; any other text ("007", "4.0", "x1") stays as
        # the model wrote it, which the schema allows as well.
        return int(value) if _CANONICAL_INTEGER.fullmatch(value) else None
    kind = _coerced_kind(spec)
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

    Only a top-level string value of a number/boolean parameter (or of a dictionary written as a string) is held
    until it closes, then emitted coerced. Everything else (long string bodies, nested
    values) passes through at once, so the emitted text is always a prefix of
    the final arguments and clients can render the call while it is generated.
    Anything unexpected switches to verbatim pass-through.

    A tool with a dictionary written as a string inside an array or object cannot be decoded in passing: all of its
    arguments are held until the call ends and are then given as coerce_arguments makes them.
    """

    def __init__(self, params):
        self.params = params or {}
        self.buffer = [] if any(_has_marked(spec) and not _marked(spec) for spec in self.params.values()) else None
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
        return isinstance(spec, dict) and (_coerced_kind(spec) in ("number", "boolean", "array", "object")
                                           or _marked(spec) or _number_or_text(spec))

    def feed(self, chunk):
        if self.buffer is not None:
            self.buffer.append(chunk or "")
            return ""
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
                    out.append(raw if coerced is None else "null" if coerced is _JSON_NULL
                               else json.dumps(coerced, ensure_ascii=False))
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
        if self.buffer is not None:
            text, self.buffer = "".join(self.buffer), None
            return coerce_arguments(text, self.params) if text else ""
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


_CHOICE_WORDS = ("none", "auto", "required")


def _forced_alone(tools, tool_choice):
    """(tools, tool_choice) with a forced tool whose name is one of Retool's own choices made unambiguous.

    toolChoice is one string, and "none", "auto" and "required" mean what they say whoever a client tool is called: forcing
    a tool named "none" asked for no tool at all, "auto" for nothing in particular and "required" for any tool (ultracode
    audit of the relay, C20). Such a tool is forced the way a lone tool is: the model is offered only that one and the
    choice is "required". The list the caller holds is not changed."""
    function = tool_choice.get("function") if isinstance(tool_choice, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    if name not in _CHOICE_WORDS:
        return tools, tool_choice
    kept = [tool for tool in tools or [] if tool.get("name") == name]
    return (kept, "required") if kept else (tools, tool_choice)


def build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks=None, session=None,
               mark_tail=True):
    """Assemble the request body.

    The stable head becomes a cached system block and the history is sent as one
    message per chunk, preserving prior turn endpoints as cache breakpoints so
    intermediate multi-tool turns do not discard prefix caches.

    mark_tail=False puts no hint on the last chunk: the request of a client's compaction ends in an instruction that
    nobody reads again, and a hint there only makes the provider write it at the price of a write.
    """
    messages = []
    tools, tool_choice = _forced_alone(tools, tool_choice)
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
            if not mark_tail and cacheable_indices and cacheable_indices[-1] == len(history) - 1:
                cacheable_indices.pop()
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
            if not mark_tail:
                # A client's compaction: the client replaces its history by the summary, so nothing written for this
                # request is read again. Only the hint the chat's last request wrote (read now) stays: the flattened dialog
                # of OpenCode 2.2.6's automatic compaction was written to the cache (2,281 and 3,075 tokens, v3 run of
                # 2026-11-07, 3d0b8da709e7, 6a61f23eef9b) and never read.
                cache_indices = {max(previous)} if previous else set()

        for index, part in enumerate(history):
            should_cache = index in cache_indices
            if isinstance(part, list):
                blocks = [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in part]
                for block in blocks:
                    sep = block.pop("sep", None)  # the newline that joined the blocks of one client message (validate._marked_text_blocks)
                    if sep:
                        block["text"] += sep
                if should_cache and not any(b.get("cache") for b in blocks):
                    # Only text blocks carry the cache flag; an image-only chunk has no
                    # cacheable text and upstream may reject the unknown placement.
                    # A message whose client marked a block keeps that breakpoint instead.
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


KEEPWARM_NUDGE = ("Keep-alive check: answer right away with the single word OK, without thinking it over, and call no "
                  "tool.")


# Requests that go upstream at the same moment with one head (tools and system) that no request has cached yet: Claude
# Code starts several sub-agents of a kind in one message, each with the same 23k-token head, and each wrote it to the
# cache (cc6707c87d82, 1a360b011428 in the Claude Code test run of 2026-11-07: about 1.1 credits per extra agent). A cache
# entry can be read only once the response that writes it has begun, so the first of them (the leader) goes at once, and
# the others wait for its first event, then read the head it wrote. A head used within HEAD_WARM is taken as cached and
# nothing waits; a follower never waits longer than HEAD_WAIT, nor for a leader that failed or has not started in
# HEAD_STALE.
HEAD_WARM = 270.0
HEAD_WAIT = 10.0
HEAD_STALE = 30.0
_HEAD_GATES_MAX = 2048
_HEAD_GATES = {}
_HEAD_GATES_LOCK = threading.Lock()


class _HeadGate:
    __slots__ = ("started", "released", "opened_at", "used_at", "owner")

    def __init__(self, now, owner=None):
        self.started, self.released, self.opened_at, self.used_at, self.owner = False, False, now, 0.0, owner


# A head shorter than this (system text and tool definitions together) cannot be a provider cache entry: the smallest
# cacheable prefix is 1,024 tokens, about 2,048 characters of Cyrillic or code. Gating such a head only delays requests:
# three parallel requests with the system "Be brief." waited for the first one's first event, 1.5 s each, for nothing.
HEAD_MIN_CHARS = 2048


def _attribution_only(block):
    """A system block that holds only Claude Code's attribution line, which Anthropic consumes and does not cache."""
    text = block.get("text") if isinstance(block, dict) else None
    return isinstance(text, str) and text.startswith("x-anthropic-billing-header:") and "\n" not in text


def _head_key(body):
    """The digest of what a body shares with every request of its kind: the model, the tools and the system message that
    carries the head's cache hint. None for a body without such a head, or with one too small to be cached.

    Claude Code's attribution line is left out: it carries a fingerprint of each conversation's (each sub-agent's) first
    message, so sub-agents started together with the same head never shared one write, though upstream ignores the line
    and their cached prefix is the same."""
    messages = body.get("messages") or []
    first = messages[0] if messages and isinstance(messages[0], dict) else None
    if first is None or first.get("role") != "system":
        return None
    blocks = [block for block in first.get("content") or [] if not _attribution_only(block)]
    if not any(isinstance(block, dict) and block.get("cache") for block in blocks):
        return None
    tools = body.get("tools")
    size = sum(len(block["text"]) for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str))
    size += len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
    if size < HEAD_MIN_CHARS:
        return None
    payload = json.dumps([body.get("model"), tools, {**first, "content": blocks}], sort_keys=True, ensure_ascii=False,
                         default=str)
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def _claim_head(key, now, owner=None):
    """("cached", gate) when the head was used within HEAD_WARM, ("follow", gate) when another request is writing it now,
    ("lead", gate) when this request is the one that writes it. `owner` is the session: a retry of a turn on the session
    that leads (the only requests a session sends at once) leads again instead of waiting for itself."""
    with _HEAD_GATES_LOCK:
        gate = _HEAD_GATES.get(key)
        if gate is not None and gate.started and now - gate.used_at <= HEAD_WARM:
            return "cached", gate
        if (gate is not None and not gate.started and not gate.released and now - gate.opened_at <= HEAD_STALE
                and (owner is None or gate.owner != owner)):
            return "follow", gate
        gate = _HEAD_GATES[key] = _HeadGate(now, owner)
        if len(_HEAD_GATES) > _HEAD_GATES_MAX:
            for old in sorted(_HEAD_GATES, key=lambda k: max(_HEAD_GATES[k].used_at, _HEAD_GATES[k].opened_at))[
                    :len(_HEAD_GATES) - _HEAD_GATES_MAX]:
                del _HEAD_GATES[old]
        return "lead", gate


def _head_started(gate, key=None):
    """A response that reads or writes the head has begun: from now on the head is cached for HEAD_WARM."""
    with _HEAD_GATES_LOCK:
        gate.started = True
        gate.used_at = time.monotonic()
        current = _HEAD_GATES.get(key) if key is not None else gate
        if key is not None and (current is None or not current.started):
            _HEAD_GATES[key] = gate  # a follower whose leader failed wrote the head itself


def _head_released(key, gate):
    """The leader ended without a response: nobody waits for it, and the next request leads."""
    with _HEAD_GATES_LOCK:
        gate.released = True
        if _HEAD_GATES.get(key) is gate and not gate.started:
            del _HEAD_GATES[key]


async def _wait_for_head(gate, limit=None):
    """Wait until the leader's response has begun (or failed), at most `limit` seconds; the reason it stopped waiting."""
    deadline = time.monotonic() + (HEAD_WAIT if limit is None else limit)
    while time.monotonic() < deadline:
        if gate.started:
            return "leader_started"
        if gate.released:
            return "leader_failed"
        await asyncio.sleep(0.05)
    return "timeout"


def ping_body(body):
    """The request a keep-alive sends: the last body, then one more user message that asks for a one-word answer.

    A ping used to send the last body again, and Retool's Claude route ignores maxCompletionTokens, so the model wrote
    the whole reply again: 1,041 pings of one load cost 467 credits where 194 were expected, up to 14.6k output tokens
    for one (journal of 2026-11-06). An output token costs 50 to 100 times a token read from the cache. The earlier
    messages keep their cache hints, so the ping still reads the same prefix; the new message carries none, because it
    is not worth a cache entry and the request has only a few breakpoints to spare. `rit` is the hint of the new list.
    Measured through the relay (Sonnet 5.5, 13.8k cached tokens): the same body again 715 output tokens and 0.198
    credits, this body 4 output tokens and 0.057, both reading all 13,752 tokens; Opus 5.5 on high 1505 against 50
    output tokens, on max about 150-370 (it thinks a little even about this); after a tool result, with tools declared,
    the model answers OK and calls none. The model and the effort stay the turn's own: the relay does not lower an
    effort or change a model to make a request cheaper, a ping included.

    What the model thinks before its OK is output too: a quarter of the pings on Claude with thinking wrote more than 20
    tokens (16.1 credits in 2.5 hours, journal watch of 2026-11-07). Telling it that this is a keep-alive check to answer
    without thinking it over halves that, measured through the relay on a tool-result tail: Opus 5.5 max 190 -> 91 and
    98 (median of 3 and 4 pings), Opus high 54 -> 36, Sonnet max 79 -> 82, Sonnet high 4 -> 4. A bare "Say OK." made the
    model go on with the task and call a tool."""
    messages = [*(body.get("messages") or []), {"role": "user", "content": [{"type": "text", "text": KEEPWARM_NUDGE}]}]
    payload = dict(body)
    payload["messages"] = messages
    payload["rit"] = make_rit(messages, payload.get("tools"))
    payload["maxCompletionTokens"] = MIN_OUTPUT_TOKENS
    return payload


def _without_inf(value):
    if isinstance(value, dict):
        return {key: _without_inf(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_inf(item) for item in value]
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _wire_json(body):
    """The request body as the ASCII JSON that goes upstream.

    A number past the range of a double (1e400) that the client's own history holds is inf here, and Python writes it as
    Infinity, which is no JSON: the endpoint refused the whole request (ultracode audit of the relay, G3.2). It goes as
    null, which is what a JavaScript client writes for it as well."""
    try:
        return json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except ValueError:
        return json.dumps(_without_inf(body), ensure_ascii=True).encode("utf-8")


def _ping(session, body):
    """Cheap request that re-reads the cached prefix so its TTL does not lapse.

    Returns the credits Retool actually billed for the ping (from the usage
    metadata event), or None when the stream ended without reporting them."""
    payload = ping_body(body)
    # Effort can be part of the provider cache scope. Preserve the exact request
    # settings rather than replacing max with low during a cache refresh.
    headers = session.backend.headers()
    headers["content-type"] = "application/json"
    request = urllib.request.Request(session.backend.cfg["base"] + ROUTE,
                                     data=_wire_json(payload),
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
# healthy pings of a 190k-token prefix (4e-6 per token). Below one cache block it is noise. A route that reports
# no write (GLM, gpt-5.5) bills the missed prefix as plain input instead, held to the same sizes (_rewrite_of).
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


def _hit_the_cap(body, flags):
    """True when the attempt ended on exactly the output cap its request carried and holds no whole tool call.

    GPT and GLM keep the cap and report the tokens they wrote: 60 of 60, 150 of 150, 300 of 300 and 400 of 400 in probes
    through the relay (2026-11-06). Claude's route ignores it (the same caps of 16, 64 and 300 gave 750 to 980 tokens), so
    its replies never end on the cap, and a reply that happens to is not taken for a cut here; a whole tool call in the
    reply says that the end was no cut either."""
    cap, written = body.get("maxCompletionTokens"), (flags.get("last_usage") or {}).get("outputTokens")
    return (type(cap) is int and type(written) in (int, float) and written == cap and not flags.get("whole_calls"))


def _rewrite_of(usage, model=None):
    """The size of a rewrite when a ping wrote the cached prefix again instead of reading it, else None.

    GLM and gpt-5.5 report no cache write (credits.py): a prefix they did not read is billed as plain input, 10 times the
    price of a read (5 times on GLM-5.3-Flash), and a check of the write counter alone let every such ping pass for a healthy
    one until the idle limit. Where the usage has no write counter, or the model is one that never writes (its write is
    priced as its input, credits.rates), the uncached input is the part the ping missed (named input_tokens: nothing was
    reported written), held to the same thresholds against what it read. A ping that reads its prefix leaves only the nudge
    and the tail past the provider's last cached block as input, a few hundred tokens. A route that reports writes is judged
    by its write counter alone, as before."""
    if not isinstance(usage, dict):
        return None
    missed, key = console_log.number(usage.get("promptTokensWrittenToCache")), "written_tokens"
    rate = credits.rates(model)
    if missed is None or (not missed and rate["cache_write"] == rate["input"]):
        missed, key = console_log.number(usage.get("inputTokens")), "input_tokens"
    missed = int(missed or 0)
    if missed < REWRITE_FLOOR:
        return None
    read = int(console_log.number(usage.get("promptTokensReadFromCache")) or 0)
    prefix = read + missed
    if missed < REWRITE_TOKENS and missed < prefix * REWRITE_SHARE:
        return None
    return {key: missed, "read_tokens": read, "prefix_tokens": prefix}


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


def _kept_warm(session):
    """True while the keepwarm cycle of `session` runs: its cache is being kept alive."""
    task = getattr(session, "_warm_task", None)
    return task is not None and not task.done()


def _dependents_live(session, now, turn_timeout, limit):
    """True while a child request that pins this conversation is really alive, or a child is still expected back.

    The pool counts leases, and a release that never comes would pin the count forever.
    Only activity counts here: a child turn busy for at most turn_timeout, or any child
    request within its own idle limit; a request still being set up counts for turn_timeout.

    A child whose request has ended (session._cache_children) is still expected back while it has not given its final
    answer, or its own cycle still warms it, for `limit` from its last request: a subagent waits for a long tool and
    comes back with the result. Pinned by the lease alone, the parent's cycle ended cache_keepwarm_idle after the
    child's request while the child was warmed for cache_keepwarm_subagent_idle, and the parent wrote its whole prefix
    again when it went on. A closed or evicted child does not come back to this session. The limit counts from the
    child's own last request even while its cycle runs, so that conversations that name each other as parents cannot
    keep each other warm forever."""
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
    for child in tuple(getattr(session, "_cache_children", None) or ()):
        if child is session or getattr(child, "_closed", False):
            continue
        started = getattr(child, "_turn_started_at", 0) or 0
        if getattr(child, "_busy", False) and started and now - started > turn_timeout:
            continue  # a stuck turn keeps nothing warm
        if now - (getattr(child, "_last_request_at", 0) or 0) <= limit and (
                _kept_warm(child) or not getattr(child, "_turn_final", False)):
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
    a child request that went quiet keep nothing warm; a child that waits for a tool keeps
    its parent warm while it is expected back (_dependents_live). The reason, the pings and the credits
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
    - a ping reported that it wrote the prefix instead of reading it, or, on a route that reports no write (GLM,
      gpt-5.5), that it paid the prefix as plain input: cache_rewritten. One rewrite is the most a
      session pays for: the session is marked, and no cycle is started for it again (_ensure_keepwarm, which the pool
      calls at every request of a child) until a real turn gives a usable answer;
    - five failed pings in a row: failures (with the default interval the window closes first, see PING_RETRY).
    Inside a running turn only the last three apply. A turn in flight is judged when it ends, not before, so the
    verdict on the turn before it (last_turn_failed, compacted) and the idle limits stop nothing there. A compaction
    in flight gets no ping at all, of its own body or of the chat's last one: the client replaces that history by the
    summary, and the cycle stops with compacted once the summary is back.
    """
    interval = float(cfg.get("cache_keepwarm_seconds", 0) or 0)
    if interval <= 0:
        return
    idle = float(cfg.get("cache_keepwarm_idle", 900) or 900)
    subagent_idle = float(_int_setting(cfg, "cache_keepwarm_subagent_idle", 1800))
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
                    # A compaction pings nothing while it runs: it arms no body of its own (iter_events), and the chat's
                    # last body, which this branch pings for a turn without one, is the history the client replaces.
                    if not during_turns or not started or getattr(session, "_turn_compaction", False):
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
                rewrite = _rewrite_of(usage, body.get("model"))
                touched = any(isinstance(usage.get(key), (int, float)) and usage[key] > 0 for key in (
                    "promptTokensReadFromCache", "promptTokensWrittenToCache"))
                journal.emit("cache.keepwarm",
                             ("Прогрев не прочитал кэш: префикс оплачен как обычный вход" if "input_tokens" in rewrite else
                              "Прогрев перезаписал кэш: префикс записан заново, а не прочитан") if rewrite else
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
                    # (a lapse, an eviction, another shard) can happen again, and every miss pays a full write
                    # (or, where the route reports no write, the whole prefix as plain input).
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
    """Yield normalised relay events from the direct NDJSON stream.

    `session._native_callable_tools` names the tools whose calls reach the client (None: every call does); a call of
    another tool is dropped by the caller and is not part of what the turn delivered, nor of the branch recorded for it."""
    # A route that refuses a forced choice is sent `auto` (forced_choice): the choice is neither sent nor part of the scope
    # of the branch, which the next request continues whatever it forces.
    asked = tools, tool_choice  # what the caller sent: the request that is asked again after a refusal is this one
    required = forced_choice.requirement(model_provider.get("model"), tools, tool_choice)
    if required is not None:
        tool_choice = "auto"
    client = getattr(session, "_native_client_messages", None)
    history = getattr(session, "_native_history", None)
    # The summary of a client's compaction is a side request: the client replaces its history by that summary and
    # never continues this one (see where the branches are recorded below, and what keepwarm does after it). So is a
    # client's own one-shot job (client_context.BACKGROUND: Codex's memory extraction of an old thread).
    task = client_context.current().get("client_task")
    side_request = task in client_context.SIDE_TASKS
    # A hint on the result of a tool is new to upstream: `rebuild` makes the same messages without one, for the retry that
    # a refusal of it gets (see below). It stays None unless a body carries such a hint.
    hint_results = _hints_tool_results(model_provider.get("model"))
    rebuild = None

    def without_result_hints(source):
        return lambda: native_history.cache_messages(source, cfg.get("direct_cache", True), tools,
                                                     mark_tail=not side_request)

    scope = native_scope(getattr(session, "_native_conversation_key", None), model_provider, head, tools, tool_choice)
    tools, tool_choice = _forced_alone(tools, tool_choice)
    parent, messages = (conversation_state.resume(session, history, scope, client)
                        if history is not None and client is not None else (None, None))
    forked = False
    if messages is None and side_request and history is not None and client is not None:
        # A compaction that forks from the last request (History.resume_fork) continues the prefix that request cached.
        parent, messages = history.resume_fork(scope, client)
        forked = messages is not None
    # A branch that lost a call is a shape upstream was never seen to accept: a refusal that blames the blocks of the
    # history sends the same request again from the client's own messages (below).
    pruned_parent = bool(parent is not None and not forked and parent in getattr(history, "pruned", ()))
    if messages is not None:
        body = build_body(cfg, model_provider, head, "", tools, tool_choice, max_tokens)
        body["messages"] = native_history.cache_messages(messages, cfg.get("direct_cache", True), tools,
                                                         mark_tail=not side_request, tool_results=hint_results)
        if _has_tool_result_hint(body["messages"]):
            rebuild = without_result_hints(messages)
        body["rit"] = make_rit(body["messages"], tools)
        # A fork replays none of the last reply: no block of the parent is in this body.
        replayed = [] if forked else parent.assistant
        reminders = 0 if forked else native_history.dropped(parent, client, messages)
        # A turn the client took back (an undo, an edit, a retry) continues the final answer of the turn before it.
        rewound = (not forked and parent is not history.branches[-1]
                   and any(parent is p for p in getattr(history, "points", ())))
        journal.emit("native_history.resume", "Сохранённые assistant/thinking blocks возвращены upstream", "info",
                     blocks=len(replayed), messages=len(body["messages"]),
                     signatures=sum(b["type"] == "thinking" for b in replayed), fork=forked,
                     **({"reminders_kept": reminders} if reminders else {}), **({"rewound": True} if rewound else {}))
        req_id = client_context.current().get("request_id") or getattr(session, "_request_tag", None) or "req"
        console_log.write("[%s] [history] native continuation %s thinking=%d tool_calls=%d" % (
            req_id, "forked from the last request" if forked else "restored",
            sum(b["type"] in ("thinking", "redacted_thinking") for b in replayed),
            sum(b["type"] == "tool_call" for b in replayed)))
    else:
        # A one-shot job continues nothing of the session it runs on, so it changes nothing of it either: Codex can run
        # its memory jobs on the session of a chat, whose signed history, cache points and keepwarm body stay the chat's.
        background = task == client_context.BACKGROUND
        if history is not None and history.branches and client is not None and not background:
            # Never attach an old signature to a rebuilt/edited prefix.
            session._cached_turn_ends = []
            session._warm_body = None
            journal.emit("native_history.reset", "История или настройки изменились; прежняя подпись не переиспользуется",
                         "warn", reason="prefix_or_settings_mismatch",
                         restored=any(getattr(branch, "restored", False) for branch in history.branches),
                         why=history.mismatch(scope, client, getattr(session, "_native_tool_defaults", None)))
        body = build_body(cfg, model_provider, head, prompt, tools, tool_choice, max_tokens, chunks,
                          session=None if background else session, mark_tail=not side_request)
        cold = (native_history.unsigned_tool_transcript(
                    client, without_results=_cold_from_the_start(model_provider.get("model")))
                if client is not None and getattr(session, '_native_allow_cold_replay', False) else None)
        if cold is not None:
            # The opaque signed branch may have expired or belonged to another
            # account. Client-visible calls/results are still valid protocol.
            # Rebuild them without trusting any client-supplied thinking blocks.
            initial = body['messages'][:1] if head else []
            body['messages'] = native_history.cache_messages(initial + cold, cfg.get('direct_cache', True), tools,
                                                             mark_tail=not side_request, tool_results=hint_results)
            if _has_tool_result_hint(body['messages']):
                rebuild = without_result_hints(initial + cold)
            body['rit'] = make_rit(body['messages'], tools)
            journal.emit('native_history.cold', 'История инструментов восстановлена без старых thinking-подписей',
                         'info', messages=len(cold), signatures=0)
    session._direct_reply = None
    param_types = {tool["name"]: tool.get("params") or {} for tool in (tools or [])}
    if model_provider.get("model") in _ASSIST_ONLY:
        body = _unmarked(body)
    identity = client_context.current()
    session._busy = True
    session._turn_identity = identity
    session._turn_started_at = time.time()
    session._turn_pinged = False
    session._turn_final = False
    # A compaction is warmed by nobody while it runs (_keepwarm): the client replaces its history by the summary.
    session._turn_compaction = task == client_context.COMPACTION
    session._turn_seq = getattr(session, "_turn_seq", 0) + 1
    turn_seq = session._turn_seq
    session._last_request_at = time.time()
    rejected = False
    # A completed answer that asked for no tool is final: a subagent that gave one is done.
    asked_tool, ended = False, None
    callable_names = getattr(session, "_native_callable_tools", None)
    callable_names = None if callable_names is None else frozenset(callable_names)
    # What the turn delivered decides what it may become: only a usable answer is a keepwarm body.
    wrote_text = thinking_seen = errored = began = False
    failure = None
    settled = False
    # Requests that go at the same moment with one head nobody has cached: the first writes it, the others wait for its
    # response to begin and then read it (_claim_head). A retry of this turn on the same session never waits for itself.
    head_key = _head_key(body) if cfg.get("direct_cache", True) else None
    head_role, head_gate = _claim_head(head_key, time.monotonic(), id(session)) if head_key else (None, None)
    head_marked = False

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
        session._turn_compaction = False
        session._turn_final = ended == "stop" and not asked_tool and not getattr(session, "_cancelled", False)
        # The consumer may drop the generator right after an error event (a cut stream): that is not a cancel.
        stopped_early = isinstance(failure, (asyncio.CancelledError, GeneratorExit)) and not errored
        # A reply the cap or a cut cut short is no answer to keep warm, whatever it holds.
        outcome = "truncated" if errored or ended == "length" else attempt_outcome(
            cancelled=bool(getattr(session, "_cancelled", False)) or ended == "cancelled" or stopped_early,
            failed=failure is not None, ended=ended is not None, usable=wrote_text or asked_tool,
            thinking_seen=thinking_seen)
        if rejected:
            outcome = "rejected"
        # A one-shot job is judged by nobody's keepwarm and moves no cache key of the session: its verdict and its prefix
        # are its own, and a chat it shares the session with keeps its own.
        own = task != client_context.BACKGROUND
        if body.get("openAICacheKey") and own:
            # Only a usable answer (never an empty, thinking-only, cut off, failed or cancelled turn) may
            # become the keepwarm body; any other verdict stops the cycle until a good turn comes.
            session._turn_outcome = outcome
        if body.get("openAICacheKey") and not rejected and own:
            prior_key = getattr(session, "_last_cache_key", None)
            if prior_key and prior_key != body["openAICacheKey"]:
                # A new key means the upstream prefix cache starts from zero: every
                # history breakpoint misses and the whole prompt is written again.
                journal.emit("cache.key_changed", "Кэш-ключ сменился — префикс будет перезаписан", "info",
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
        nonlocal asked_tool, ended, wrote_text, thinking_seen, errored, began, head_marked
        # Every attempt announces itself with usage_start before upstream has said anything: not an answer. Nor is the mark
        # the relay gives an attempt whose price is unknown before it raises the error of that attempt (_from_upstream): taken
        # for one, a refusal inside an HTTP 200 never got the safety nets below.
        began = began or _from_upstream(event)
        if began and head_gate is not None and not head_marked:
            head_marked = True
            _head_started(head_gate, head_key)
        kind = event.get("type")
        if kind == "tool_call" and not event.get("partial") and not event.get("invalid"):
            asked_tool = asked_tool or callable_names is None or event.get("name") in callable_names
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

    if body.get("openAICacheKey") and not side_request:
        session._turn_body = body
        # Start the scheduler with the turn, not after it: a session's first long
        # turn needs its anchored during-turn ping too. A one-shot job has no next request to keep a prefix for, and
        # neither has a compaction: the client replaces its history by the summary, so a ping of this body (one at 200 s
        # and every 240 s of a long summary, each reading the whole chat) keeps a prefix nobody sends again.
        _ensure_keepwarm(session, cfg)

    # The compaction of a Codex chat, whose tools the relay declared again so that it reads the chat's cache (proxy.
    # _codex_chat_tools): Codex cannot take a call in reply to it, so a reply with one is not passed on (_summary_stream). The
    # request that is sent when the calls do not stop is the one Codex made, from the client's own text and with no tools.
    guarded = (side_request and task == client_context.COMPACTION and required is None and bool(tools)
               and client_context.current().get("compaction_tools") == "restored")

    def text_only():
        # Built anew, this body carried the App Builder marker however the request went: on a model of the Assist list, whose
        # two tries with tools went without it, the route refused this third one ("not available right now") and the
        # compaction failed after two paid tries. It goes the way the request goes, as the retry below sends it too.
        sent = build_body(cfg, model_provider, head, prompt, None, "none", max_tokens, chunks, session=None, mark_tail=False)
        return sent if "metadata" in body else _unmarked(sent)

    def open_stream(sent):
        # A compaction whose tools the relay declared again must end in text (summary_guard); a route that refuses a forced choice
        # has a requirement: its reply is held until it holds the call.
        if guarded:
            return _summary_stream(cfg, session, sent, param_types, text_only)
        return (_forced_stream(cfg, session, sent, param_types, required) if required is not None
                else _stream_body(cfg, session, sent, param_types))

    try:
        try:
            if head_role == "follow":
                waiting_since = time.monotonic()
                outcome = await _wait_for_head(head_gate)
                journal.emit("cache.head_wait",
                             "Запрос ждал начала ответа соседнего запроса с той же головой: голова читается из кэша, "
                             "а не пишется второй раз", "info", outcome=outcome, model=body.get("model"),
                             waited_ms=round((time.monotonic() - waiting_since) * 1000))
            async with contextlib.aclosing(open_stream(body)) as stream:
                async for event in stream:
                    note(event)
                    yield event
        except apierr.ApiError as error:
            assist = (error.status == 400 and "metadata" in body
                      and "not available right now" in (error.message or "").lower())
            # What may be upstream's refusal of a hint on a tool result: a message that names the cache, or the route
            # validator's own words, which name nothing ("Invalid request format", an empty body). One that names thinking,
            # a signature or the blocks of the history is about the signed branch, whose own handling follows, and any
            # other reason it names (a prompt too long, a tool schema, the content filter) is not the hint's.
            said = (error.message or "").lower()
            plausible = "cache" in said or said.strip() in ("", "{}") or "invalid request format" in said
            about_branch = conversation_state.blocks_refused(error) and "cache" not in said
            if pruned_parent and not began and not assist and about_branch and error.status in (400, 422):
                # Upstream refused the history of a branch that had lost a call: forget those branches and ask again from
                # what the client sent (no signatures, like any first request after a reset). If that is answered the
                # shape was the cause, and replies are no longer cut down that way for the rest of the run.
                history.forget_pruned()
                upstream_status = getattr(error, "upstream_status", None) or error.status
                answered = False
                async with contextlib.aclosing(iter_events(cfg, session, model_provider, prompt, head, *asked,
                                                           max_tokens, chunks)) as again:
                    async for event in again:
                        if not answered and _from_upstream(event):
                            answered = True
                            _PRUNING["refused"] = True
                            journal.emit("native_history.prune_refused",
                                         "Upstream отклонил историю без вызова, которого клиент не получил (HTTP %s): "
                                         "запрос повторён с нуля, дальше ответы так не режутся" % upstream_status, "warn",
                                         status=upstream_status, upstream_message=(error.message or "").replace("\n", " ")[:160],
                                         model=model_provider.get("model"))
                        yield event
                return
            if rebuild is not None and not began and not assist and plausible and not about_branch \
                    and error.status in (400, 422):
                # Upstream refused a body that carries a cache hint on the result of a tool, a shape that was never seen
                # accepted: ask again without those hints, and once that is answered stop sending them (this run).
                body = {**body, "messages": rebuild()}
                body["rit"] = make_rit(body["messages"], tools)
                rebuild = None
                upstream_status = getattr(error, "upstream_status", None) or error.status
                if body.get("openAICacheKey") and not side_request:  # what the turn keeps warm, as above
                    session._turn_body = body
                answered = False
                async with contextlib.aclosing(open_stream(body)) as stream:
                    async for event in stream:
                        if not answered and _from_upstream(event):
                            answered = True
                            _TOOL_RESULT_HINTS["refused"] = True
                            journal.emit("upstream.tool_result_hint_refused",
                                         "Upstream отклонил подсказку кэша на результате инструмента (HTTP %s): запрос "
                                         "повторён без неё, дальше такие подсказки не ставятся" % upstream_status, "warn",
                                         status=upstream_status, upstream_message=(error.message or "").replace("\n", " ")[:160],
                                         model=model_provider.get("model"))
                        note(event)
                        yield event
            else:
                # Models absent from the App Builder list (legacy GPT-5, Haiku) live on
                # the Assist list, which is the same route without the agent marker.
                if not assist:
                    raise
                model_name = model_provider.get("model")
                print("[assist] retry without agent marker: %s" % (error.message or "")[:80], flush=True)
                journal.emit("upstream.assist_retry", "Модель только в списке Assist — повтор без маркера", "warn",
                             model=model_name)
                body = _unmarked(body)
                if body.get("openAICacheKey") and not side_request:  # what the turn keeps warm, as above
                    session._turn_body = body
                learned = False
                async with contextlib.aclosing(open_stream(body)) as stream:
                    async for event in stream:
                        if not learned and _from_upstream(event):
                            # Learned only once the request without the marker is answered, as the other fallbacks are:
                            # "not available right now" also comes for a while from a model that is on the App Builder list
                            # (or from one account of a pool), and the retry then fails too. Learned before it, the model was
                            # sent without the marker by every account until a restart, and every request of it failed.
                            learned = True
                            if len(_ASSIST_ONLY) > 200:
                                # Flush only self-learned names: wiping the proven seeds would make
                                # known assist models pay a 400 + retry cycle all over again.
                                _ASSIST_ONLY.intersection_update(_ASSIST_SEEDS)
                            _ASSIST_ONLY.add(model_name)
                        note(event)
                        yield event
        reply = getattr(session, "_direct_reply", None)
        pruned = False
        if (reply is not None and callable_names is not None and not _PRUNING["refused"]
                and _anthropic_route(model_provider.get("model"))):
            # Only on the Anthropic route, where a signature belongs to its thinking block alone: OpenAI wants a reasoning
            # item followed by the item it was made with, so a call cannot be left out there.
            kept = native_history.deliverable(reply, callable_names, param_types, coerce_arguments)
            if kept is not reply:
                journal.emit("native_history.pruned",
                             "В ветку не записаны вызовы, которых клиент не получил: %s" % ", ".join(sorted(
                                 {block["toolName"] for block in reply[0]
                                  if block["type"] == "tool_call" and block["toolName"] not in callable_names})),
                             "info", kept=kept is not None)
                pruned = kept is not None
            reply = kept
        # Recording a compaction's summary would put a branch nobody extends in place of the parent it resumed
        # (a nudged or repeated compaction, a background one beside the chat, or the chat itself after an abandoned
        # compaction would then start from nothing), so the dialog's branches stay as the chat left them.
        if client is not None and history is not None and reply is not None and not session._cancelled and not side_request:
            blocks, expected = reply
            history.record(scope, client, body["messages"], blocks, expected, parent, pruned=pruned)
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
        if head_role == "lead" and not head_gate.started:
            _head_released(head_key, head_gate)  # nobody waits for a leader that got no response


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
    if (getattr(session, "_cancelled", False) or getattr(session, "_closed", False)
            or getattr(asyncio.get_running_loop(), "_relay_closing", False)):
        # A closed session is out of the pool (evicted, released for good, left for another account): nothing would end
        # a cycle started on it, which would ping a prefix nobody continues until its idle limit.
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
        folder = pathlib.Path(cfg.get("upstream_error_dir") or upstream_diagnostics.DEFAULT_DIR)
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


def _asked_for_call(body, text):
    """The body with `text` as one more user turn after its last message.

    Nothing of the body moves: the turn carries no cache mark and is not in the history that gets stored, so a request that
    continues this one finds the prefix the cache holds. Only the integrity hint is made again, as it covers the messages."""
    messages = [*body["messages"], {"role": "user", "content": [{"type": "text", "text": text}]}]
    return {**body, "messages": messages, "rit": make_rit(messages, body.get("tools"))}


async def _forced_stream(cfg, session, body, param_types, required):
    """_stream_body for a route that refuses a forced tool choice: the call is asked for in words, and the reply checked.

    The reply is released once it holds the call (forced_choice.Gate). An attempt that ends without it is asked again once,
    more firmly: nothing of it reaches the client (its thinking, its text and the calls of other tools are dropped) and its
    cost still counts. A second miss is the error a route that ignored a native choice earns in the orchestrator. Only the
    accepted attempt leaves its reply on the session for the history to keep."""
    attempt = 0
    while True:
        attempt += 1
        gate = forced_choice.Gate(required)
        end = None
        sent = _asked_for_call(body, forced_choice.instruction(required, firm=attempt > 1))
        async with contextlib.aclosing(_stream_body(cfg, session, sent, param_types)) as stream:
            async for event in stream:
                if event.get("type") == "end":
                    end = event  # judged below: an attempt that missed the call must not end the turn
                    continue
                for ready in gate.feed(event):
                    yield ready
        if end is None:
            return  # the stream ended with its error event, which the gate let through
        if gate.complied or end.get("reason") == "cancelled":
            yield end
            return
        budget = getattr(session, "_attempt_budget", None) or AttemptBudget()
        final = attempt > 1 or not budget.remaining
        journal.emit("turn.forced_choice_missed",
                     "Модель не вызвала требуемый инструмент%s" % ("" if final else ": повтор с усиленной просьбой"),
                     "warn", model=body.get("model"), tool=required.named, called=gate.called, wrote_text=gate.text,
                     attempt=attempt)
        if final:
            raise budget.error("upstream_tool_choice_ignored" if gate.text or gate.called else "upstream_empty_turn",
                               "upstream did not honor the required client tool choice")


async def _summary_stream(cfg, session, body, param_types, text_only):
    """_stream_body for the compaction of a Codex chat whose tools the relay declared again: the reply is text, or it is asked again.

    The tools are there so that the request shares the chat's cached prefix (compaction_tools), and Codex cannot take a call in
    reply to its compaction (summary_guard). The reply is released once the attempt has ended without a call (summary_guard.Gate).
    An attempt that called a tool is dropped, its cost still counts, and the request is asked once more with one instruction as a
    user turn of its own after the last message (nothing of the body moves: the turn carries no cache mark and is not stored). A
    second call sends the request Codex itself made, with no tools and the history as text (`text_only`), which cannot call
    anything: it writes the history again, as the compaction did before the tools were declared, and that is the worst it costs."""
    attempt = 0
    while True:
        attempt += 1
        fallback = attempt > summary_guard.TRIES_WITH_TOOLS
        if fallback:
            sent = text_only()
        elif attempt == 1:
            sent = body
        else:
            sent = _asked_for_call(body, summary_guard.INSTRUCTION)
        gate = summary_guard.Gate()
        end = None
        async with contextlib.aclosing(_stream_body(cfg, session, sent, None if fallback else param_types)) as stream:
            async for event in stream:
                if event.get("type") == "end":
                    end = event  # judged below: an attempt that called a tool must not end the turn
                    continue
                for ready in gate.feed(event):
                    yield ready
        if end is None:
            return  # the stream ended with its error event, which the gate let through
        if not gate.called or end.get("reason") == "cancelled":
            for ready in gate.release():
                yield ready
            yield end
            return
        journal.emit("turn.compaction_tool_call",
                     "Модель вызвала инструмент вместо сводки сжатия: ответ отброшен, %s" % (
                         "запрос повторён с просьбой ответить текстом" if attempt < summary_guard.TRIES_WITH_TOOLS else
                         "запрос идёт без инструментов, как его прислал Codex"),
                     "warn", model=body.get("model"), called=gate.called, wrote_text=gate.text, attempt=attempt,
                     tools_kept=attempt < summary_guard.TRIES_WITH_TOOLS)


def _unpriced():
    """The usage event the relay gives of its own for an attempt that may have been billed while upstream reported no price for
    it: the client's adapter and the journal count that attempt as one whose price is unknown."""
    return {"type": "usage", "usage": {}, "unpriced": True}


def _from_upstream(event):
    """Whether an event of the stream is something upstream said, and not the relay's own bookkeeping: usage_start, with which
    every attempt starts before upstream has said anything, or the mark of an attempt whose price is unknown (_unpriced). That
    mark comes before the error of an attempt that upstream refused inside an HTTP 200, or that timed out once it was sent."""
    return event.get("type") != "usage_start" and not event.get("unpriced")


async def _stream_body(cfg, session, body, param_types=None, *, payload=None):
    if payload is None:
        payload = _wire_json(body)
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
    live_thinking = _live_thinking(body.get("model"))
    try:
        max_empty_retries = _int_setting(cfg, "upstream_empty_retries", 1)
        empty_retries = 0
        max_limit_retries = _int_setting(cfg, "upstream_rate_limit_retries", 3)
        limit_retries = 0
        while True:
            usable = False
            thinking_seen = False
            spoke = False  # whether this attempt gave the consumer anything but billing and display status
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
                        spoke = True
                        yield event
                    elif kind in _LIVE_EVENTS or not may_retry:
                        spoke = spoke or kind not in _LIVE_EVENTS
                        yield event
                    elif live_thinking and kind in ("thinking", "thinking_start"):
                        # Reasoning text without a signature: the client may read it while the model is still at it.
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
                if _refused_for_now(failure) and not flags.get("usage_seen"):
                    possibly_billed = False  # refused before anything was generated: it cost nothing, as an HTTP 429 never does
                elif (apierr.is_account_quota(failure) and not flags.get("usage_seen")
                      and not (usable or thinking_seen or spoke or held)):
                    # The account's hourly limit as an error event inside a 200, before the model gave anything: the same
                    # refusal as its HTTP 429 form, which costs nothing. Taken for a possible charge, it marked the balance
                    # unreported, told the client an unknown cost and sent the empty usage event that zeroed the
                    # conversation's prefix size, so account_rate_limit_wait moved it at once. One that comes after text or
                    # thinking may have been billed, and stays unknown.
                    possibly_billed = False
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
                yield _unpriced()
            if (_refused_for_now(failure) and not spoke and not usable and limit_retries < max_limit_retries
                    and budget.remaining and not is_cancelled()):
                wait = RATE_LIMIT_WAITS[min(limit_retries, len(RATE_LIMIT_WAITS) - 1)] * random.uniform(0.75, 1.25)
                limit_retries += 1
                journal.emit("upstream.rate_limited",
                             "Retool ограничил частоту запросов к моделям (429): повтор на том же аккаунте через %.1f с "
                             "(%d из %d)" % (wait, limit_retries, max_limit_retries), "warn", model=body.get("model"),
                             attempt=budget.used, wait=round(wait, 2), retry=limit_retries, retries=max_limit_retries,
                             account=account_tag(getattr(session, "backend", None)),
                             thread=journal.tag(getattr(session, "thread_id", None)))
                if not await _pause(wait, is_cancelled):
                    yield {"type": "end", "reason": "cancelled"}
                    return
                text.clear()
                thinking.clear()
                holder["response"] = None
                continue
            if failure is not None:
                raise failure
            saw_end = bool(flags.get("end"))
            if is_cancelled():
                yield {"type": "end", "reason": "cancelled"}
                return
            if saw_end and not flags.get('usage_seen') and not flags.get('cost_unknown'):
                # An empty/no-telemetry attempt can still have been billed. Keep
                # its unknown price visible when a later retry reports a cost.
                yield _unpriced()
            # Where the route keeps the cap, a reply that ends on exactly that many output tokens was cut by it (GPT and
            # GLM, 5 of 5 probes; Claude's route ignores it and writes on). A whole call in the reply says the end was
            # no cut. A reasoning model that spends a small cap on reasoning ends so, with nothing to show for it:
            # asking again pays the same cap for the same nothing (two paid attempts, then a 502).
            capped = _hit_the_cap(body, flags)
            cut = bool(flags.get("cut_calls"))
            if not usable and saw_end and not capped:
                _dump_empty_once(payload, flags, cfg, session, body.get("model"))
            if (usable or not saw_end or service_request or not budget.remaining
                    or empty_retries >= max_empty_retries or capped):
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
        # The reply is cut: by the cap, or inside a tool call. It is told as such (max_tokens, length, incomplete) and
        # never recorded as a branch to continue, nor taken for a whole answer.
        truncated = (capped or cut) and not is_cancelled()
        blocks = flags.get("assistant_blocks")
        if usable and blocks and not is_cancelled() and not truncated:
            session._direct_reply = (blocks, native_history.echo(blocks, param_types or {}, coerce_arguments))
        elif usable and "assistant_blocks" in flags and blocks is None and not is_cancelled() and not truncated:
            journal.emit("native_history.unavailable", "Ответ upstream нельзя сохранить для нативного продолжения",
                         "warn", reason="unsupported_or_incomplete_blocks")
        if truncated:
            journal.emit("upstream.truncated", "Ответ upstream оборван: %s" % (
                            "вызов инструмента не дописан" if cut else "потрачен весь лимит вывода"), "warn",
                         model=body.get("model"), thread=journal.tag(getattr(session, "thread_id", None)),
                         cap=body.get("maxCompletionTokens"), cut_calls=flags.get("cut_calls", 0),
                         output_tokens=(flags.get("last_usage") or {}).get("outputTokens"),
                         empty=not usable, effort=body.get("reasoningEffort"))
        yield {"type": "end", "reason": "length" if truncated else "stop"}
    finally:
        session._active_direct_abort = None


def _delta_target(event, current):
    """The call a tool-call delta belongs to: the one it names, else the newest open one.

    ReplyCapture, which records the signed history, has always read a delta's toolCallId; this read the newest open
    call whatever the delta said, so with two calls open the client and the history disagreed about their arguments
    (ultracode audit of the relay, G1.1). Retool tags no delta today, and then the newest open call is the one."""
    named = event.get("toolCallId")
    return named if isinstance(named, str) and named else current


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
        event = {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": streamed}
        try:
            json.loads(streamed)
            flags["whole_calls"] = flags.get("whole_calls", 0) + 1
        except ValueError:
            if "".join(call["args"]).strip():
                # Arguments that are not JSON at the end: the output stopped inside the call (the cap, a cut). The
                # text is what the client has seen so far and stays that, so that its deltas always add up: this was
                # rebuilt from the raw arguments before, which differ from the streamed text wherever a number or a
                # boolean had been written as a string, and the client got both one after the other (ultracode
                # audit, C13). The event says the call is not whole and no adapter reports it as finished (C5).
                event["invalid"] = True
                flags["cut_calls"] = flags.get("cut_calls", 0) + 1
            else:
                # A call without arguments arrives as one empty partialInputs.
                event["arguments"] = "{}"
                flags["whole_calls"] = flags.get("whole_calls", 0) + 1
        return event
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
            elif block == "tool_call" and (target := _delta_target(event, current)) in calls:
                call = calls[target]
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
