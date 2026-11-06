"""Strict request parsing for every supported dialect."""
import base64
import contextvars
import hashlib
import ipaddress
import json
import native_history
import generation
import math
import re
import socket
import urllib.parse
import urllib.request

from apierr import bad_request, unprocessable, ApiError
import models as model_catalog

MAX_TOKENS_CAP = 1_000_000  # absolute ceiling when a model has no catalog value


def _assert_public_http_url(url, where="image"):
    """SSRF guard: resolve every address for the host and reject non-public ones."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise unprocessable("failed to fetch image attachments: only http(s) URLs are allowed", param=where)
    hostname = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise unprocessable("failed to fetch image attachments: invalid destination host", param=where)
    for info in infos:
        try:
            ip_obj = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            raise unprocessable("failed to fetch image attachments: disallowed destination host", param=where)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target: a public URL must not bounce to an
    internal address (SSRF via redirects)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_public_peer(resp, where="image"):
    """Post-connect check of the socket we ACTUALLY connected to. Closes the
    DNS-rebinding window of check-then-connect: even if the name resolved to a
    public address but the connection landed on an internal one, we bail out."""
    try:
        sock = resp.fp.raw._sock
        peer = ipaddress.ip_address(sock.getpeername()[0])
    except (AttributeError, OSError, ValueError):
        return
    if (peer.is_private or peer.is_loopback or peer.is_link_local
            or peer.is_reserved or peer.is_multicast):
        raise unprocessable("failed to fetch image attachments: disallowed destination host", param=where)


_IMAGE_OPENER = urllib.request.build_opener(_GuardedRedirectHandler())
_IMAGE_FETCHES = contextvars.ContextVar("proxi_image_fetches", default=None)


class ImageFetches:
    """The remote images one request has downloaded, by URL, for the second parse of a body a prompt profile rewrote.

    Used as a context manager it records, in the parses made inside it, what each download gave. Once `reuse` is set,
    a parse made inside it takes those images from here instead of downloading them again: the rewrite leaves images
    alone, so the second parse wants exactly the pictures the first one fetched (and must not fail on a link that has
    stopped answering since). Outside any such block, and while `reuse` is off, every parse downloads as it always did."""

    def __init__(self):
        self.found = {}
        self.reuse = False
        self._tokens = []

    def __enter__(self):
        self._tokens.append(_IMAGE_FETCHES.set(self))
        return self

    def __exit__(self, *exc):
        _IMAGE_FETCHES.reset(self._tokens.pop())
        return False


class Parsed:
    """Normalised request shared by all adapters."""

    def __init__(self):
        self.prompt = ""
        self.message_chunks = []
        self.message_fingerprints = None
        self.native_messages = None
        self.model = ""
        self.entry = {}
        self.effort = None
        self.requested_effort = None
        self.stream = False
        self.max_tokens = None
        self.temperature = None
        self.verbosity = None
        self.structured_title = False
        self.structured_output = None
        self.system = ""
        self.declared_ignored = []
        self.routine_ignored = []
        self.inline_tool_changes = []
        self.system_messages_normalized = False
        self.wants_usage = False
        self.show_thinking = True
        self.tools = []
        self.tool_choice = "auto"
        self.conversation_key = None
        # The client's model name without @profile and #effort, before alias resolution, and the @profile
        # of a virtual model <model>@<profile>; the prompt layer reads both (prompt_policy.PlanInput).
        self.requested_model = ""
        self.virtual_profile = None


def load_json(raw: bytes):
    if not raw:
        raise bad_request("empty request body")
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise bad_request("request body must use valid UTF-8")
    except json.JSONDecodeError as exc:
        raise bad_request(f"invalid JSON body: {exc.msg} at line {exc.lineno}")
    if not isinstance(data, dict):
        raise bad_request(f"request body must be a JSON object, got {type(data).__name__}")
    return data


def _opt_str(data, key, default=None):
    value = data.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise unprocessable(f"'{key}' must be a string", param=key)
    return value


def _opt_bool(data, key, default=False):
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise unprocessable(f"'{key}' must be a boolean", param=key)
    return value


def _client_model(model, cfg):
    """(the client's model name without @profile and #effort, its @profile or None).

    An empty name, or one that is only an effort suffix, stands for the configured default model, as
    models.resolve reads it. The profile is only named here: that it exists is the prompt layer's check.
    """
    named = model_catalog.parse_model_name(model)
    if not named.base:
        named = model_catalog.parse_model_name(cfg.get("model"))
    return named.base, named.profile


def _opt_int(data, key, low=1, high=MAX_TOKENS_CAP):
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise unprocessable(f"'{key}' must be an integer", param=key)
    if value < low:
        raise unprocessable(f"'{key}' must be at least {low}", param=key)
    if value > high:
        return high
    return value


def _reject_images_unsupported(entry, data):
    """The catalog `vision` flag is otherwise declarative: upstream rejects image
    input for non-vision models with a bare 400. Fail fast with a clear 422."""
    if entry.get("vision", True):
        return
    blocks = []
    messages = data.get("messages") if isinstance(data, dict) else None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, dict))
        elif isinstance(content, dict):
            blocks.append(content)
    system = data.get("system") if isinstance(data, dict) else None
    if isinstance(system, list):
        blocks.extend(b for b in system if isinstance(b, dict))
    raw_input = data.get("input") if isinstance(data, dict) else None
    for item in raw_input if isinstance(raw_input, list) else []:
        # Responses API: images arrive as input items or inside item content.
        if isinstance(item, dict):
            blocks.append(item)
            if isinstance(item.get("content"), list):
                blocks.extend(b for b in item["content"] if isinstance(b, dict))
    for block in blocks:
        if block.get("type") in ("image", "image_url", "input_image") or "imageData" in block or "image_url" in block:
            raise unprocessable(f"model {entry['id']} does not support image input", param="messages")


def _cap(entry) -> int:
    """Per-model completion ceiling from the catalog."""
    return int(entry.get("max_output") or MAX_TOKENS_CAP)


def _opt_float(data, key):
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise unprocessable(f"'{key}' must be a number", param=key)
    return float(value)


def _extract_image_data(block, where):
    """Extract Retool-compatible image dict {'type': 'image', 'imageData': str, 'mimeType': str}."""
    btype = block.get("type")
    mime_type = "image/png"
    data_b64 = None
    if btype == "image":
        source = block.get("source")
        if isinstance(source, dict):
            mime_type = source.get("media_type") or "image/png"
            data_b64 = source.get("data")
        elif "imageData" in block:
            mime_type = block.get("mimeType") or "image/png"
            data_b64 = block.get("imageData")
    elif btype in ("image_url", "input_image"):
        iu = block.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else (iu if isinstance(iu, str) else block.get("url"))
        if isinstance(url, str):
            if url.startswith("data:"):
                hdr, sep, rest = url.partition(",")
                if sep and ";base64" in hdr:
                    mime = hdr[5:].split(";")[0].strip()
                    if mime:
                        mime_type = mime
                    data_b64 = rest.strip()
            elif url.startswith("http://") or url.startswith("https://"):
                fetches = _IMAGE_FETCHES.get()
                kept = fetches.found.get(url) if fetches is not None and fetches.reuse else None
                if kept is not None:
                    mime_type, data_b64 = kept
                else:
                    _assert_public_http_url(url, where)
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    max_bytes = 10 * 1024 * 1024
                    try:
                        with _IMAGE_OPENER.open(req, timeout=10) as r:
                            _assert_public_peer(r, where)
                            ct = r.headers.get("content-type", "").split(";")[0].strip()
                            if ct:
                                mime_type = ct
                            buf = r.read(max_bytes + 1)
                            if len(buf) > max_bytes:
                                raise unprocessable("failed to fetch image attachments: image exceeds maximum allowed size (10MB)", param=where)
                            data_b64 = base64.b64encode(buf).decode("ascii")
                    except ApiError:
                        raise
                    except Exception:
                        raise unprocessable(f"failed to fetch image attachments from {url}", param=where)
                    if fetches is not None:
                        fetches.found[url] = (mime_type, data_b64)
    elif "imageData" in block:
        mime_type = block.get("mimeType") or "image/png"
        data_b64 = block.get("imageData")

    if not data_b64:
        raise unprocessable(f"{where}: missing image attachments data", param=where)
    return {"type": "image", "imageData": data_b64, "mimeType": mime_type}


def _parse_content(content, index, role, ignored=None):
    """Render content blocks to text and extract any native image blocks.

    Inline tool changes carry no text: they are skipped and, when `ignored` is
    given, their names are appended to it.
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise unprocessable(f"messages[{index}].content must be a string or an array of blocks",
                            param=f"messages[{index}].content")
    chunks = []
    images = []
    for pos, block in enumerate(content):
        where = f"messages[{index}].content[{pos}]"
        if isinstance(block, str):
            chunks.append(block)
            continue
        if not isinstance(block, dict):
            raise unprocessable(f"{where} must be an object or string", param=where)
        btype = block.get("type")
        if btype in ("text", "input_text", "output_text", "summary_text"):
            text = block.get("text")
            if text is None:
                raise unprocessable(f"{where}.text is required", param=where + ".text")
            if not isinstance(text, str):
                raise unprocessable(f"{where}.text must be a string", param=where + ".text")
            chunks.append(text)
        elif btype in ("image", "image_url", "input_image") or "imageData" in block:
            img = _extract_image_data(block, where)
            images.append(img)
        elif btype == "tool_result":
            call_id = block.get("tool_use_id") or block.get("tool_call_id") or "call"
            sub_text, sub_images = _parse_content(block.get("content"), index, "tool", ignored)
            chunks.append(f"[tool_result id={call_id}: {sub_text}]")
            images.extend(sub_images)
        elif btype in ("tool_use", "tool_call"):
            name = block.get("name") or "tool"
            args = block.get("input", block.get("arguments", {}))
            call_id = block.get("id") or block.get("tool_call_id") or "call"
            chunks.append(f"[tool_call {name} id={call_id}: {json.dumps(args, ensure_ascii=False)}]")
        elif btype in ("thinking", "redacted_thinking"):
            continue
        elif btype in native_history.INLINE_TOOL_BLOCKS:
            # The tool set comes from `tools`; a late change cannot be applied inside the history.
            if ignored is not None:
                by_value = isinstance(block.get("tool"), dict) and block["tool"].get("type") == "tool_definition"
                ignored.append(btype + (".tool_definition" if by_value else ""))
        else:
            chunks.append(f"[{btype or 'block'}: {json.dumps(block, ensure_ascii=False)[:200]}]")
    return "\n".join(c for c in chunks if c), images


def _blocks_to_text(content, index, role):
    """Render content blocks without silently dropping information."""
    text, _ = _parse_content(content, index, role)
    return text


def _instruction_text(*parts):
    """Join instruction texts in order; blank ones (effort-only entries) leave no trace."""
    return "\n\n".join(part for part in parts if part and part.strip())


def _render_chunks(messages, role_key="role", *, allow_system=True, lead=None, converted=None, ignored=None):
    """Return (system_text, [chunk per conversation message]).

    Chunks keep one message per element so the direct transport can place cache
    breakpoints along the history instead of re-sending it uncached. Only the
    leading system/developer messages form the head; a later non-empty one keeps
    its place as a user-turn reminder (its index is appended to `converted`),
    and a blank one, such as an effort-only entry, produces nothing.
    """
    if lead is None:
        lead = native_history.leading_instructions(messages, role_key) if allow_system else 0
    system_parts, convo = [], []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise unprocessable(f"messages[{index}] must be an object", param=f"messages[{index}]")
        role = message.get(role_key) or message.get("role")
        if role is None and message.get("tool_calls"):
            role = "assistant"
        if not isinstance(role, str):
            raise unprocessable(f"messages[{index}].{role_key} must be a string", param=f"messages[{index}].{role_key}")
        text, images = _parse_content(message.get("content"), index, role, ignored)
        if role in ("system", "developer") and allow_system:
            if index < lead:
                system_parts.append(text)
            elif text.strip():
                if converted is not None:
                    converted.append(index)
                convo.append(native_history.reminder(text))
            continue
        if role in ("tool", "function"):
            name = message.get("name") or message.get("tool_call_id") or "tool"
            call_id = message.get("tool_call_id") or message.get("call_id") or name
            convo_text = f"[tool result {name} id={call_id}]: {text}".strip()
            if images:
                blocks = []
                if text:
                    blocks.append({"type": "text", "text": convo_text})
                blocks.extend(images)
                convo.append(blocks)
            else:
                convo.append(convo_text)
            continue
        if role == "assistant":
            calls = message.get("tool_calls") or []
            if isinstance(calls, list) and calls:
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    name = fn.get("name") or call.get("name") or "tool"
                    args = fn.get("arguments") or call.get("arguments") or ""
                    call_id = call.get("id") or call.get("tool_call_id") or "call"
                    asst_text = f"Assistant: {text}\n[tool_call {name} id={call_id}: {str(args)}]".strip()
                    if images:
                        blocks = []
                        if asst_text:
                            blocks.append({"type": "text", "text": asst_text})
                        blocks.extend(images)
                        convo.append(blocks)
                        images = []
                    else:
                        convo.append(asst_text)
                    text = ""
            if text:
                asst_text = f"Assistant: {text}".strip()
                if images:
                    convo.append([{"type": "text", "text": asst_text}] + images)
                else:
                    convo.append(asst_text)
            elif images:
                convo.append(images)
            continue
        user_text = f"{role.capitalize()}: {text}".strip()
        if images:
            blocks = []
            if text:
                blocks.append({"type": "text", "text": user_text})
            blocks.extend(images)
            convo.append(blocks)
        else:
            convo.append(user_text)
    return _instruction_text(*system_parts), convo


def _render_messages(messages, role_key="role", *, allow_system=True, lead=None):
    """Return (system_text, prompt_text)."""
    system, convo = _render_chunks(messages, role_key, allow_system=allow_system, lead=lead)
    text_chunks = []
    for c in convo:
        if isinstance(c, str):
            text_chunks.append(c)
        elif isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "image":
                        img_hash = hashlib.sha256((b.get("imageData") or "").encode("ascii", errors="ignore")).hexdigest()[:16]
                        parts.append(f"[image:{b.get('mimeType', 'unknown')}:{img_hash}]")
            text_chunks.append("\n".join(parts))
        else:
            text_chunks.append(str(c))
    return system, "\n\n".join(text_chunks)


def _message_fingerprints(messages, lead=None):
    """Hash each bridge-visible message, preserving boundaries but not cache markers."""
    if lead is None:
        lead = native_history.leading_instructions(messages)
    return tuple(hashlib.sha256(json.dumps(_render_messages([message], lead=int(index < lead)), ensure_ascii=False,
                                          sort_keys=True, separators=(",", ":")).encode("utf-8")).digest()
                 for index, message in enumerate(messages))


def _responses_item(item):
    """Responses input items are richer than chat messages; normalise the shapes."""
    if not isinstance(item, dict):
        return item
    kind = item.get("type")
    if kind in ("function_call_output", "custom_tool_call_output", "tool_result") and "role" not in item:
        return {"role": "tool", "name": item.get("name") or item.get("call_id") or "tool",
                "tool_call_id": item.get("call_id") or item.get("tool_call_id"),
                "content": item.get("output") if item.get("output") is not None else item.get("content")}
    if kind in ("function_call", "custom_tool_call") and "role" not in item:
        arguments = (json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
                     if kind == "custom_tool_call" else item.get("arguments") or "")
        name = item.get("name") or "tool"
        if item.get("namespace"):
            name = str(item["namespace"]) + "." + name
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": item.get("call_id") or item.get("id") or "call",
                                "function": {"name": name,
                                             "arguments": arguments}}]}
    if kind == "item_reference":
        raise unprocessable("item_reference is not supported; send the full conversation items", param="input")
    if kind == "reasoning" and "role" not in item:
        return {"role": "assistant", "content": item.get("summary") or ""}
    return item


def _messages(data, *, min_items=1):
    messages = data.get("messages")
    if messages is None:
        raise bad_request("'messages' is required", param="messages")
    if not isinstance(messages, list):
        raise bad_request("'messages' must be an array", param="messages")
    if len(messages) < min_items:
        raise unprocessable("'messages' must not be empty", param="messages")
    return messages


def _finish(data, parsed, dialect):
    parsed.requested_effort = _effort(data)
    if parsed.requested_effort is None:
        parsed.requested_effort = model_catalog.parse_model_name(data.get("model")).effort
    parsed.structured_output = generation.structured_output(data, dialect)
    parsed.structured_title = bool(parsed.structured_output and parsed.structured_output['title'])
    parsed.verbosity, parsed.declared_ignored, parsed.routine_ignored = generation.controls(
        data, parsed.entry, dialect)
    parsed.stop_sequences = generation.stop_sequences(data, dialect)
    parsed.declared_ignored += [name for name in parsed.inline_tool_changes if name not in parsed.declared_ignored]
    stream_options = data.get("stream_options")
    if stream_options is None:
        stream_options = data.get("streamOptions")
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise unprocessable("'stream_options' must be an object", param="stream_options")
        inc = _opt_bool(stream_options, "include_usage")
        if inc is None or not inc:
            inc = _opt_bool(stream_options, "includeUsage")
        parsed.wants_usage = bool(inc)
    else:
        parsed.wants_usage = True
    parsed.tools = _tools(data)
    parsed.tool_choice = _tool_choice(data.get("tool_choice"))
    names = [tool["function"]["name"] for tool in parsed.tools]
    if parsed.tool_choice == "required" and not names:
        raise unprocessable("tool_choice requires at least one tool", param="tool_choice")
    if isinstance(parsed.tool_choice, dict) and parsed.tool_choice["function"]["name"] not in names:
        raise unprocessable("tool_choice must name a declared tool", param="tool_choice")
    return parsed


def _tools(data):
    """Validate and retain OpenAI/Anthropic-compatible function definitions."""
    tools = data.get("tools")
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise unprocessable("'tools' must be an array", param="tools")
    result = []
    for index, tool in enumerate(tools):
        where = f"tools[{index}]"
        if not isinstance(tool, dict):
            raise unprocessable(f"{where} must be an object", param=where)
        kind = tool.get("type", "function")
        if kind == "namespace":
            namespace = tool.get("name")
            if not isinstance(namespace, str) or not namespace.strip() or not isinstance(tool.get("tools"), list):
                raise unprocessable("namespace tools require a name and tools array", param=where)
            for nested in _tools({"tools": tool["tools"]}):
                fn = nested["function"]
                client_name = fn["name"]
                fn["x-namespace"] = namespace
                fn["x-client-name"] = client_name
                fn["name"] = namespace + "." + client_name
                if any(item["function"]["name"] == fn["name"] for item in result):
                    raise unprocessable(f"duplicate tool name '{fn['name']}'", param=where + ".name")
                result.append(nested)
            continue
        if kind not in ("function", "custom"):
            if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z_]*_\d{8}", kind):
                # web_search_20250305 and the like: the provider runs these itself, and the Retool route has none of
                # them. Claude Code's WebSearch sends one; naming it lets the model turn to a client tool at once.
                raise unprocessable(f"{where} is a server tool of the provider ('{kind}'), which the Retool route "
                                    "cannot run: it runs client tools (functions) only. Use a client tool instead, "
                                    "such as a search MCP server or WebFetch", param=where + ".type")
            raise unprocessable(f"{where}.type must be 'function' or 'custom'", param=where + ".type")
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            raise unprocessable(f"{where}.function.name must be a non-empty string",
                                param=where + ".function.name")
        if any(item["function"]["name"] == name for item in result):
            raise unprocessable(f"duplicate tool name '{name}'", param=where + ".name")
        parameters = fn.get("parameters", fn.get("input_schema", {}))
        if kind == "custom":
            parameters = {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
        if not isinstance(parameters, dict):
            raise unprocessable(f"{where}.function.parameters must be an object",
                                param=where + ".function.parameters")
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(fn.get("description") or ""),
                "parameters": parameters,
            },
        })
        if kind == "custom":
            result[-1]["function"]["x-client-type"] = "custom"
            if tool.get("format") is not None:
                if not isinstance(tool["format"], dict):
                    raise unprocessable("custom tool format must be an object", param=where + ".format")
                result[-1]["function"]["x-format"] = tool["format"]
        if fn.get("strict") is not None:
            result[-1]["function"]["strict"] = _opt_bool(fn, "strict")
    return result


def _tool_choice(value):
    if value is None:
        return "auto"
    if isinstance(value, str):
        if value not in ("none", "auto", "required"):
            raise unprocessable("'tool_choice' must be 'none', 'auto' or 'required'", param="tool_choice")
        return value
    if isinstance(value, dict):
        fn = value.get("function")
        choice_type = value.get("type")
        if choice_type == "auto":
            return "auto"
        if choice_type == "none":
            return "none"
        if choice_type == "any":
            return "required"
        if choice_type in ("tool", "custom") and isinstance(value.get("name"), str):
            name = value["name"]
            if value.get("namespace"):
                name = str(value["namespace"]) + "." + name
            return {"type": "function", "function": {"name": name}}
        if choice_type == "function" and fn is None and isinstance(value.get("name"), str):
            name = value["name"]
            if value.get("namespace"):
                name = str(value["namespace"]) + "." + name
            return {"type": "function", "function": {"name": name}}
        if value.get("type") != "function" or not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise unprocessable("'tool_choice' function form is invalid", param="tool_choice")
        return {"type": "function", "function": {"name": fn["name"]}}
    raise unprocessable("'tool_choice' must be a string or function object", param="tool_choice")


def conversation_affinity(data, headers, identity=None):
    """Use the calling session's ID before a shared parent affinity header."""
    headers = {str(k).lower(): v.strip() for k, v in headers.items() if isinstance(v, str) and v.strip()}
    explicit = (headers.get("x-proxi-session-id") or headers.get("x-opencode-session-id")
                or headers.get("x-claude-code-session-id"))
    affinity = explicit or next((headers.get(k) for k in (
        "x-session-affinity", "x-session-id", "x-opencode-session", "x-conversation-id",
        "session-id", "conversation-id") if headers.get(k)), None)
    key = conversation_key(data, affinity)
    parent_id = ((identity or {}).get("parent_session_id") or headers.get("x-proxi-parent-session-id")
                 or headers.get("x-opencode-parent-session-id"))
    parent = key.split(":", 1)[0] + ":" + parent_id if key and parent_id else None
    # Claude subagents can share a session ID but advertise a distinct agent ID.
    # Agent display names (build/explore) are deliberately not identities.
    agent = headers.get("x-claude-code-agent-id")
    if key and isinstance(agent, str) and agent.strip():
        parent = parent or key
        digest = hashlib.sha256(json.dumps([key, agent.strip()]).encode()).hexdigest()
        key = "agent:" + digest
    return key, parent if parent != key else None


def conversation_key(data, header_value=None, allow_content_fallback=False):
    """Return a stable affinity key for one client dialogue.

    Checks explicit client headers, data fields, metadata session identifiers.
    If allow_content_fallback is True (used by the HTTP proxy for unidentified clients),
    provides a deterministic content-hash fallback for standard OpenAI/Anthropic
    clients (e.g. OpenCode) across turns.
    """
    if isinstance(header_value, str) and header_value.strip():
        return "header:" + header_value.strip()
    for key in ("conversation", "conversation_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return key + ":" + value.strip()
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        user_id = metadata.get("user_id")
        if isinstance(user_id, str):
            try:
                identity = json.loads(user_id)
            except (ValueError, RecursionError):
                identity = None
            session_id = identity.get("session_id") if isinstance(identity, dict) else None
            if isinstance(session_id, str) and session_id.strip():
                return "claude-metadata:" + session_id.strip()
    if allow_content_fallback:
        messages = data.get("messages")
        if not messages and "input" in data:
            raw_input = data.get("input")
            if isinstance(raw_input, str):
                messages = [{"role": "user", "content": raw_input}]
            elif isinstance(raw_input, list):
                messages = raw_input
            elif isinstance(raw_input, dict):
                messages = [raw_input]
        if isinstance(messages, list) and messages:
            root_msg = None
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "user":
                    root_msg = msg
                    break
            if root_msg is None and isinstance(messages[0], dict):
                root_msg = messages[0]
            elif root_msg is None and messages and isinstance(messages[0], str):
                root_msg = {"role": "user", "content": messages[0]}
            if root_msg is not None:
                raw = json.dumps(root_msg, sort_keys=True, ensure_ascii=False)
                h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
                return f"content:{h}"
    return None


def parse_chat(data, cfg):
    parsed = Parsed()
    entry, effort = model_catalog.resolve_tolerant(
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"),
        allow_downgrade=cfg.get("allow_effort_downgrade", False))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    parsed.requested_model, parsed.virtual_profile = _client_model(_opt_str(data, "model"), cfg)
    _reject_images_unsupported(entry, data)
    messages = _messages(data)
    lead, converted, ignored = native_history.leading_instructions(messages), [], []
    system, chunks = _render_chunks(messages, lead=lead, converted=converted, ignored=ignored)
    text_chunks = [c if isinstance(c, str) else "\n".join(b.get("text", f"[{b.get('type')}]") for b in c if isinstance(b, dict)) for c in chunks]
    parsed.system, parsed.prompt, parsed.message_chunks = system, "\n\n".join(text_chunks), chunks
    parsed.system_messages_normalized = bool(converted)
    parsed.inline_tool_changes = list(dict.fromkeys(ignored))
    parsed.message_fingerprints = _message_fingerprints(messages, lead)
    parsed.native_messages = native_history.client_messages(messages, _extract_image_data, lead=lead)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = (_opt_int(data, "max_tokens", high=_cap(entry))
                         or _opt_int(data, "max_completion_tokens", high=_cap(entry)))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed, 'chat')


def parse_responses(data, cfg):
    parsed = Parsed()
    entry, effort = model_catalog.resolve_tolerant(
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"),
        allow_downgrade=cfg.get("allow_effort_downgrade", False))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    parsed.requested_model, parsed.virtual_profile = _client_model(_opt_str(data, "model"), cfg)
    _reject_images_unsupported(entry, data)

    raw_input = data.get("input")
    if isinstance(raw_input, str):
        messages = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        messages = [_responses_item(item) for item in raw_input]
    elif raw_input is None:
        raise bad_request("'input' is required", param="input")
    else:
        raise bad_request("'input' must be a string or an array", param="input")
    lead, converted, ignored = native_history.leading_instructions(messages), [], []
    system, chunks = _render_chunks(messages, lead=lead, converted=converted, ignored=ignored)
    instructions = _opt_str(data, "instructions") or ""
    parsed.system = _instruction_text(instructions, system)
    text_chunks = [c if isinstance(c, str) else "\n".join(b.get("text", f"[{b.get('type')}]") for b in c if isinstance(b, dict)) for c in chunks]
    parsed.prompt, parsed.message_chunks = "\n\n".join(text_chunks), chunks
    parsed.system_messages_normalized = bool(converted)
    parsed.inline_tool_changes = list(dict.fromkeys(ignored))
    parsed.message_fingerprints = _message_fingerprints(messages, lead)
    native_input = ([message for item, message in zip(raw_input, messages)
                     if item.get("type") != "reasoning" or "role" in item]
                    if isinstance(raw_input, list) else messages)
    parsed.native_messages = native_history.client_messages(native_input, _extract_image_data, lead=lead)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = _opt_int(data, "max_output_tokens", high=_cap(entry))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed, 'responses')


def parse_anthropic(data, cfg):
    parsed = Parsed()
    model = _opt_str(data, "model")
    entry, effort = model_catalog.resolve_tolerant(
        model, _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"),
        allow_downgrade=cfg.get("allow_effort_downgrade", False))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    parsed.requested_model, parsed.virtual_profile = _client_model(model, cfg)
    _reject_images_unsupported(entry, data)
    messages = _messages(data)
    # Retool rejects several system messages and a changed head drops the signed
    # prefix, so only the leading instructions join the top-level system text.
    # Later ones keep their place as user-turn reminders.
    lead, converted, ignored = native_history.leading_instructions(messages), [], []
    system, chunks = _render_chunks(messages, lead=lead, converted=converted, ignored=ignored)
    text_chunks = [c if isinstance(c, str) else "\n".join(b.get("text", f"[{b.get('type')}]") for b in c if isinstance(b, dict)) for c in chunks]
    parsed.prompt, parsed.message_chunks = "\n\n".join(text_chunks), chunks
    declared_system = data.get("system")
    declared_text = _blocks_to_text(declared_system, 0, 'system') if declared_system else ''
    parsed.system = _instruction_text(declared_text, system)
    parsed.system_messages_normalized = bool(converted)
    parsed.inline_tool_changes = list(dict.fromkeys(ignored))
    parsed.message_fingerprints = _message_fingerprints(messages, lead)
    parsed.native_messages = native_history.client_messages(messages, _extract_image_data, lead=lead)
    parsed.stream = _opt_bool(data, "stream")
    if data.get("max_tokens") is None:
        raise bad_request("'max_tokens' is required for the Anthropic dialect", param="max_tokens")
    parsed.max_tokens = _opt_int(data, "max_tokens", high=_cap(entry))
    parsed.temperature = _opt_float(data, "temperature")
    stop = data.get("stop_sequences")
    if stop is not None and not isinstance(stop, list):
        raise unprocessable("'stop_sequences' must be an array", param="stop_sequences")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed, 'anthropic')


def _effort(data):
    """Translate client controls to the actual Retool modelProvider effort field."""
    selected = []
    if data.get("reasoning_effort") is not None:
        selected.append(_opt_str(data, "reasoning_effort"))
    if data.get("reasoningEffort") is not None:
        selected.append(_opt_str(data, "reasoningEffort"))
    if data.get("effort") is not None:
        selected.append(_opt_str(data, "effort"))
    provider_opts = data.get("providerOptions")
    openai_opts = provider_opts.get("openai") if isinstance(provider_opts, dict) else None
    for container in (data.get("settings"), data.get("options"), data.get("body"),
                      data.get("extra_body"), openai_opts):
        if isinstance(container, dict):
            if container.get("reasoning_effort") is not None:
                selected.append(_opt_str(container, "reasoning_effort"))
            if container.get("reasoningEffort") is not None:
                selected.append(_opt_str(container, "reasoningEffort"))
            if container.get("effort") is not None:
                selected.append(_opt_str(container, "effort"))
            if isinstance(container.get("reasoning"), dict) and container["reasoning"].get("effort") is not None:
                selected.append(_opt_str(container["reasoning"], "effort"))
    reasoning = data.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise unprocessable("'reasoning' must be an object", param="reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
        selected.append(_opt_str(reasoning, "effort"))
    output = data.get("output_config")
    if output is not None and not isinstance(output, dict):
        raise unprocessable("'output_config' must be an object", param="output_config")
    if isinstance(output, dict) and output.get("effort") is not None:
        selected.append(_opt_str(output, "effort"))
    thinking = data.get("thinking")
    if thinking is not None and not isinstance(thinking, dict):
        raise unprocessable("'thinking' must be an object", param="thinking")
    if isinstance(thinking, dict):
        kind = thinking.get("type")
        if kind is not None:
            if not isinstance(kind, str) or kind.lower() not in ("enabled", "disabled", "auto", "adaptive"):
                raise unprocessable("thinking.type must be 'enabled', 'disabled', 'adaptive' or 'auto'",
                                    param="thinking.type")
            if kind.lower() == "disabled":
                selected.append("none")
        if thinking.get("effort") is not None:
            selected.append(_opt_str(thinking, "effort"))
        if thinking.get("budget_tokens") is not None:
            if isinstance(kind, str) and kind.lower() in ("disabled", "adaptive"):
                raise unprocessable("budget_tokens requires enabled thinking", param="thinking.budget_tokens")
            selected.append(model_catalog.effort_from_budget(thinking["budget_tokens"]))
    selected = [value.lower() for value in selected]
    if len(set(selected)) > 1:
        raise unprocessable("conflicting reasoning effort controls", param="reasoning_effort")
    # Current Claude clients can place effort updates in system entries, including
    # empty ones. The latest update supersedes the request-wide default. Retool's
    # equivalent control is its top-level reasoningEffort for this response.
    latest = None
    messages = data.get('messages', data.get('input'))
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get('role') not in ('system', 'developer'):
                continue
            if message.get('clear_at') is not None:
                raise unprocessable('turn-scoped system clear_at is not supported by the Retool route',
                                    param=f'messages[{index}].clear_at')
            output = message.get('output_config')
            if output is None:
                continue
            if not isinstance(output, dict):
                raise unprocessable('system output_config must be an object',
                                    param=f'messages[{index}].output_config')
            if output.get('effort') is not None:
                latest = _opt_str(output, 'effort').lower()
    if latest is not None and isinstance(thinking, dict):
        kind = thinking.get('type')
        explicit = thinking.get('effort')
        if thinking.get('budget_tokens') is not None:
            explicit = model_catalog.effort_from_budget(thinking['budget_tokens'])
        if ((isinstance(kind, str) and kind.lower() == 'disabled' and latest != 'none')
                or (explicit is not None and str(explicit).lower() != latest)):
            raise unprocessable('conflicting reasoning effort controls', param='reasoning_effort')
    return latest if latest is not None else (selected[0] if selected else None)


def _show_thinking(data, cfg):
    show = bool(cfg.get("show_thinking", True))
    thinking = data.get("thinking")
    if isinstance(thinking, dict) and thinking.get("display") is not None:
        display = thinking["display"]
        if display not in ("summarized", "omitted", "updates"):
            raise unprocessable("thinking.display must be 'summarized', 'omitted' or 'updates'", param="thinking.display")
        show = show and display != "omitted"
    return show
