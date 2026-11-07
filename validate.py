"""Strict request parsing for every supported dialect."""
import base64
import contextvars
import functools
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
import tool_names
import web_search

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
        if _internal(ip_obj):
            raise unprocessable("failed to fetch image attachments: disallowed destination host", param=where)


def _internal(ip_obj):
    """An address that is not on the public internet. not is_global also covers 100.64.0.0/10 (carrier-grade NAT and every
    Tailscale node), which is neither private nor reserved: an image URL there made the relay fetch from inside the tailnet."""
    return (not ip_obj.is_global or ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
            or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified)


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
    if _internal(peer):
        raise unprocessable("failed to fetch image attachments: disallowed destination host", param=where)


_IMAGE_OPENER = urllib.request.build_opener(_GuardedRedirectHandler())
_IMAGE_FETCHES = contextvars.ContextVar("proxi_image_fetches", default=None)


class ImageFetches:
    """The remote images one request has downloaded, by URL: every parse made inside the block takes them from here.

    One parse reads a picture three times (it renders the text, fingerprints the messages and builds the native ones),
    and a body a prompt profile rewrote is parsed once more. Each read used to download the picture again: three
    downloads of the same link per request, so that one slow answer failed the request with a 422 and a picture that
    changed between two of them made the native history differ from itself. The first download is kept, and the
    other reads, of this parse and of the second one, get the same bytes (and a link that has stopped answering since
    costs nothing). A parse outside any block opens one of its own (_in_fetch_scope)."""

    def __init__(self):
        self.found = {}
        self._tokens = []

    def __enter__(self):
        self._tokens.append(_IMAGE_FETCHES.set(self))
        return self

    def __exit__(self, *exc):
        _IMAGE_FETCHES.reset(self._tokens.pop())
        return False


def _in_fetch_scope(parse):
    """Run a parse inside the fetch scope of its request, or inside one of its own when it is called without."""
    @functools.wraps(parse)
    def scoped(data, cfg):
        if _IMAGE_FETCHES.get() is not None:
            return parse(data, cfg)
        with ImageFetches():
            return parse(data, cfg)
    return scoped


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
        # The cap the relay enforces itself on a route that ignores it (generation.output_cap); None: the route's own.
        self.output_cap = None
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
        # The server tool web_search of an Anthropic request, when the relay runs its searches itself (web_search.spec_of):
        # {"name", "max_uses", "allowed_domains", "blocked_domains", "country"}. parsed.tools then holds it as a function.
        self.web_search = None
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
    except RecursionError:  # a body nested past the decoder's depth was a 500 internal_error
        raise bad_request("request body is nested too deeply")
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
                kept = fetches.found.get(url) if fetches is not None else None
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
            failed = " is_error=true" if block.get("is_error") else ""
            chunks.append(f"[tool_result id={call_id}{failed}: {sub_text}]")
            images.extend(sub_images)
        elif btype in ("tool_use", "tool_call", "server_tool_use"):
            name = block.get("name") or "tool"
            args = block.get("input", block.get("arguments", {}))
            call_id = block.get("id") or block.get("tool_call_id") or "call"
            chunks.append(f"[tool_call {name} id={call_id}: {json.dumps(args, ensure_ascii=False)}]")
        elif btype == "web_search_tool_result":
            # The results of a search the relay ran, echoed inside the assistant message: the model reads them as the
            # result of the call before them, as it did when they arrived, not as 200 characters of JSON.
            chunks.append(f"[tool_result id={block.get('tool_use_id') or 'call'}: "
                          f"{web_search.render_block_content(block.get('content'))}]")
        elif btype in ("thinking", "redacted_thinking"):
            continue
        elif btype in native_history.INLINE_TOOL_BLOCKS:
            # The tool set comes from `tools`; a late change cannot be applied inside the history.
            if ignored is not None:
                by_value = isinstance(block.get("tool"), dict) and block["tool"].get("type") == "tool_definition"
                ignored.append(btype + (".tool_definition" if by_value else ""))
        elif native_history.readable_block(block) is not None:
            chunks.append(native_history.readable_block(block))
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


def _marked_text_blocks(content, role):
    """The blocks of a user message whose client marked a block that is not the last one, else None.

    Claude Code's permission classifier (auto-mode-check) sends the whole transcript as one user message of text blocks
    and marks, with cache_control, the part that stays the same from one request to the next; what follows the mark (the
    pending action, the instruction) is new every time. Joined into one text, that message got one breakpoint at its
    very end, which no later request can read: every check wrote the growing transcript again (a5, 2026-11-06: 35 checks
    read 64.7k tokens of system prompt and nothing of the transcript, and wrote 28k tokens each).

    The blocks keep the text the join made of them (the role before the first one, the trailing whitespace of the whole
    message stripped, empty blocks left out, a block of only whitespace joined to the block before it, as no such block
    can go upstream; the newline of the join is the private "sep" that build_body adds back), so that the text sent
    upstream, parsed.prompt and the fingerprints do not change. The last marked block, and only it, carries `cache`:
    upstream has taken a flag on a block that is not the last of its message, but never two in one. A mark on the last
    block is where build_body flags a message anyway: such a message stays one text.
    """
    if not isinstance(content, list) or len(content) < 2:
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            return None
    kept = []
    for block in content:
        text, mark = block["text"], bool(block.get("cache_control"))
        if not text:
            if mark and kept:
                kept[-1][1] = True  # an empty block cannot carry the mark upstream: the block before it ends in the same place
        elif kept and not text.strip():
            kept[-1][0] += "\n" + text
            kept[-1][1] = kept[-1][1] or mark
        else:
            kept.append([text, mark])
    if len(kept) < 2:
        return None
    kept[-1][0] = kept[-1][0].rstrip()
    marked = [index for index, (_, mark) in enumerate(kept) if mark]
    if not marked or marked[-1] == len(kept) - 1:
        return None
    blocks = [{"type": "text", "text": text} for text, _ in kept]
    blocks[0]["text"] = f"{role.capitalize()}: {blocks[0]['text']}"
    blocks[marked[-1]]["cache"] = True
    for block in blocks[:-1]:
        block["sep"] = "\n"
    return blocks


def _render_chunks(messages, role_key="role", *, allow_system=True, lead=None, converted=None, ignored=None,
                   keep_marked_blocks=False):
    """Return (system_text, [chunk per conversation message]).

    Chunks keep one message per element so the direct transport can place cache
    breakpoints along the history instead of re-sending it uncached. Only the
    leading system/developer messages form the head; a later non-empty one keeps
    its place as a user-turn reminder (its index is appended to `converted`),
    and a blank one, such as an effort-only entry, produces nothing.

    keep_marked_blocks: a user message of text blocks that the client marked with cache_control stays a list of blocks
    where the mark was (_marked_text_blocks), instead of one text with the mark lost.
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
                # The label stays when the result is nothing but the picture: it says which call the picture answers.
                convo.append([{"type": "text", "text": convo_text}, *images])
            else:
                convo.append(convo_text)
            continue
        if role == "assistant":
            calls = message.get("tool_calls") or []
            legacy = message.get("function_call")
            if isinstance(calls, list) and isinstance(legacy, dict) and legacy.get("name"):
                # The deprecated form of one call. Its result is a "function" message that names the function and
                # carries no id, so the name stands for the id here as it does for that result.
                calls = calls + [{"id": legacy["name"], "function": legacy}]
            if isinstance(calls, list) and calls:
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    if fn is not None and not isinstance(fn, dict):
                        # a string or a number here used to raise AttributeError: a 500 instead of the field named
                        raise unprocessable("tool_calls[].function must be an object", param="messages")
                    fn = fn or {}
                    name = fn.get("name") or call.get("name") or "tool"
                    args = fn.get("arguments") or call.get("arguments") or ""
                    if isinstance(call.get("custom"), dict):
                        # A call to a custom tool of Chat: its one string is the input the model was offered the tool as.
                        name = call["custom"].get("name") or "tool"
                        args = json.dumps({"input": call["custom"].get("input", "")}, ensure_ascii=False)
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
        marked = _marked_text_blocks(message.get("content"), role) if keep_marked_blocks and not images else None
        if marked is not None:
            convo.append(marked)
        elif images:
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
    # surrogatepass: half of a surrogate pair that a JavaScript client cut inside an emoji (JSON carries it as an escape) is
    # hashed as it is. Encoded strictly it raised UnicodeEncodeError, and every request of that conversation got a 500.
    return tuple(hashlib.sha256(json.dumps(_render_messages([message], lead=int(index < lead)), ensure_ascii=False,
                                          sort_keys=True, separators=(",", ":")).encode("utf-8", "surrogatepass")).digest()
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
            name = tool_names.namespaced(str(item["namespace"]), name)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": item.get("call_id") or item.get("id") or "call",
                                "function": {"name": name,
                                             "arguments": arguments}}]}
    if kind == "item_reference":
        raise unprocessable("item_reference is not supported; send the full conversation items", param="input")
    if kind == "agent_message" and "role" not in item:
        # Codex's multi-agent v2: a message between the agents of one task (author -> recipient), such as a task or
        # a sub-agent's final answer. Upstream has no such item; to the agent that holds it, it is a turn it received.
        return {"role": "user", "content": item.get("content") if item.get("content") is not None else ""}
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


def _finish(data, parsed, dialect, search=False):
    parsed.requested_effort = _effort(data)
    if parsed.requested_effort is None:
        parsed.requested_effort = model_catalog.parse_model_name(data.get("model")).effort
    parsed.structured_output = generation.structured_output(data, dialect)
    parsed.structured_title = bool(parsed.structured_output and parsed.structured_output['title'])
    parsed.verbosity, parsed.declared_ignored, parsed.routine_ignored = generation.controls(
        data, parsed.entry, dialect)
    parsed.stop_sequences = generation.stop_sequences(data, dialect)
    parsed.output_cap = generation.output_cap(parsed.entry, parsed.max_tokens)
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
    parsed.tools = _tools(data, parsed if search else None)
    parsed.tool_choice = _tool_choice(data.get("tool_choice"))
    names = [tool["function"]["name"] for tool in parsed.tools]
    if parsed.tool_choice == "required" and not names:
        raise unprocessable("tool_choice requires at least one tool", param="tool_choice")
    if isinstance(parsed.tool_choice, dict) and parsed.tool_choice["function"]["name"] not in names:
        raise unprocessable("tool_choice must name a declared tool", param="tool_choice")
    return parsed


def _tools(data, search=None):
    """Validate and retain OpenAI/Anthropic-compatible function definitions.

    `search` is the Parsed of an Anthropic request that the relay may run web searches for (a provider is configured): its
    web_search server tool is declared as a function the model can call, and parsed.web_search keeps what the relay needs
    to run the calls. Without it a server tool of the provider is refused."""
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
        kind = tool.get("type")
        if kind is None or (kind == "custom" and isinstance(tool.get("input_schema"), dict)):
            # The Messages API writes a client tool as "type": "custom", null, or no type at all; only a custom tool
            # of OpenAI has no input_schema, and only that one takes the freeform input.
            kind = "function"
        if kind == "namespace":
            namespace = tool.get("name")
            if not isinstance(namespace, str) or not namespace.strip() or not isinstance(tool.get("tools"), list):
                raise unprocessable("namespace tools require a name and tools array", param=where)
            for nested in _tools({"tools": tool["tools"]}):
                fn = nested["function"]
                client_name = fn["name"]
                fn["x-namespace"] = namespace
                fn["x-client-name"] = client_name
                fn["name"] = tool_names.namespaced(namespace, client_name)
                if any(item["function"]["name"] == fn["name"] for item in result):
                    raise unprocessable(f"duplicate tool name '{fn['name']}'", param=where + ".name")
                result.append(nested)
            continue
        if search is not None and isinstance(kind, str) and web_search.SERVER_TOOL.fullmatch(kind):
            name = tool.get("name")
            if not isinstance(name, str) or not name.strip():
                raise unprocessable(f"{where}.name must be a non-empty string", param=where + ".name")
            if any(item["function"]["name"] == name for item in result):
                raise unprocessable(f"duplicate tool name '{name}'", param=where + ".name")
            search.web_search = dict(web_search.spec_of(tool, where), name=name)
            result.append(web_search.tool_definition(name, search.web_search))
            continue
        if kind not in ("function", "custom"):
            if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z_]*_\d{8}", kind):
                # web_search_20250305 and the like: the provider runs these itself, and the Retool route has none of
                # them. Claude Code's WebSearch sends one; naming it lets the model turn to a client tool at once.
                how = ("; the relay runs web_search itself once web_search_provider and the key it names are configured "
                       "(README)" if web_search.SERVER_TOOL.fullmatch(kind) else "")
                raise unprocessable(f"{where} is a server tool of the provider ('{kind}'), which the Retool route "
                                    "cannot run: it runs client tools (functions) only. Use a client tool instead, "
                                    "such as a search MCP server or WebFetch" + how, param=where + ".type")
            raise unprocessable(f"{where}.type must be 'function' or 'custom'", param=where + ".type")
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if kind == "custom" and isinstance(tool.get("custom"), dict):
            fn = tool["custom"]  # Chat nests a custom tool under "custom", as it nests a function under "function"
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            raise unprocessable(f"{where}.function.name must be a non-empty string",
                                param=where + ".function.name")
        if any(item["function"]["name"] == name for item in result):
            raise unprocessable(f"duplicate tool name '{name}'", param=where + ".name")
        parameters = fn.get("parameters")
        if parameters is None:
            # Responses writes a tool without parameters as null (FunctionTool.parameters is nullable)
            parameters = fn.get("input_schema")
        if parameters is None:
            parameters = {}
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
            if fn.get("format") is not None:
                if not isinstance(fn["format"], dict):
                    raise unprocessable("custom tool format must be an object", param=where + ".format")
                result[-1]["function"]["x-format"] = fn["format"]
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
        if choice_type == "custom" and isinstance(value.get("custom"), dict) and isinstance(value["custom"].get("name"), str):
            return {"type": "function", "function": {"name": value["custom"]["name"]}}  # Chat's own shape
        if choice_type in ("tool", "custom") and isinstance(value.get("name"), str):
            name = value["name"]
            if value.get("namespace"):
                name = tool_names.namespaced(str(value["namespace"]), name)
            return {"type": "function", "function": {"name": name}}
        if choice_type == "function" and fn is None and isinstance(value.get("name"), str):
            name = value["name"]
            if value.get("namespace"):
                name = tool_names.namespaced(str(value["namespace"]), name)
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
    elif key and headers.get("x-codex-parent-thread-id") and headers.get("thread-id"):
        # A Codex sub-agent (spawned, review, guardian) sends its root's session-id, a thread-id of its own and the id of
        # its parent thread: its own conversation, whose prompt and tools are not the parent's.
        parent = parent or key
        digest = hashlib.sha256(json.dumps([key, "codex-thread", headers["thread-id"]]).encode()).hexdigest()
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


def _effort_of(data, cfg):
    """(entry, effort) of a request's model and effort controls under the config's effort rules."""
    return model_catalog.resolve_tolerant(
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"),
        allow_downgrade=cfg.get("allow_effort_downgrade", False), xhigh_as=cfg.get("claude_xhigh_effort", "high"))


@_in_fetch_scope
def parse_chat(data, cfg):
    parsed = Parsed()
    entry, effort = _effort_of(data, cfg)
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


@_in_fetch_scope
def parse_responses(data, cfg):
    parsed = Parsed()
    entry, effort = _effort_of(data, cfg)
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    parsed.requested_model, parsed.virtual_profile = _client_model(_opt_str(data, "model"), cfg)
    _reject_images_unsupported(entry, data)

    raw_input = data.get("input")
    if isinstance(raw_input, str):
        messages = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        # A reasoning item is what the model thought of an earlier turn (a summary, an encrypted copy of it), not a turn
        # of the conversation: the native history leaves it out, and the text bridge did not, so every summary came back
        # to the model as something the assistant had said (ultracode audit, C16).
        kept = [(index, item) for index, item in enumerate(raw_input)
                if not (isinstance(item, dict) and item.get("type") == "reasoning" and "role" not in item)]
        messages = [_responses_item(item) for _, item in kept]
        for (index, _), message in zip(kept, messages):
            if (isinstance(message, dict) and message.get("role") is None and not message.get("tool_calls")
                    and message.get("type") not in (None, "message")):
                # Without this the item failed later as "messages[N].role must be a string", which names no type.
                raise unprocessable("input[%d]: input items of type %r are not supported by the Retool route"
                                    % (index, message.get("type")), param="input[%d].type" % index)
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
    parsed.native_messages = native_history.client_messages(messages, _extract_image_data, lead=lead)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = _opt_int(data, "max_output_tokens", high=_cap(entry))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed, 'responses')


@_in_fetch_scope
def parse_anthropic(data, cfg):
    parsed = Parsed()
    model = _opt_str(data, "model")
    entry, effort = _effort_of(data, cfg)
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    parsed.requested_model, parsed.virtual_profile = _client_model(model, cfg)
    _reject_images_unsupported(entry, data)
    messages = _messages(data)
    # Retool rejects several system messages and a changed head drops the signed
    # prefix, so only the leading instructions join the top-level system text.
    # Later ones keep their place as user-turn reminders.
    lead, converted, ignored = native_history.leading_instructions(messages), [], []
    # A request without tools (the permission classifier of Claude Code) keeps the cache marks of its text blocks.
    system, chunks = _render_chunks(messages, lead=lead, converted=converted, ignored=ignored,
                                    keep_marked_blocks=not data.get("tools"))
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
    return _finish(data, parsed, 'anthropic', search=web_search.provider(cfg) is not None)


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
    # Containers a client copies its model or variant settings into. OpenCode CLI 1.x sends every key of a variant as a
    # body field, and a title request merged two variants: reasoning_effort "high" beside settings/body "low". The
    # request then failed as a conflict and no session ever got a title. The fields of the dialect itself decide; the
    # containers count only when the request has none of them.
    nested = []
    for container in (data.get("settings"), data.get("options"), data.get("body"),
                      data.get("extra_body"), openai_opts):
        if isinstance(container, dict):
            if container.get("reasoning_effort") is not None:
                nested.append(_opt_str(container, "reasoning_effort"))
            if container.get("reasoningEffort") is not None:
                nested.append(_opt_str(container, "reasoningEffort"))
            if container.get("effort") is not None:
                nested.append(_opt_str(container, "effort"))
            if isinstance(container.get("reasoning"), dict) and container["reasoning"].get("effort") is not None:
                nested.append(_opt_str(container["reasoning"], "effort"))
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
    selected = [value.lower() for value in (selected or nested)]
    # Codex's Ultra is maximum reasoning plus delegation it runs itself; it sends max for it, and a client that sends
    # the name instead means the same effort (openai/codex df1199f: "the inference backend expects max").
    selected = ["max" if value == "ultra" else value for value in selected]
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
                latest = 'max' if latest == 'ultra' else latest
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
