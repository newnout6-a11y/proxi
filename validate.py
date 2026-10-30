"""Strict request parsing for every supported dialect."""
import hashlib
import json
import math

from apierr import bad_request, unprocessable
import models as model_catalog

MAX_TOKENS_CAP = 1_000_000  # absolute ceiling when a model has no catalog value


class Parsed:
    """Normalised request shared by all adapters."""

    def __init__(self):
        self.prompt = ""
        self.message_chunks = []
        self.message_fingerprints = None
        self.model = ""
        self.entry = {}
        self.effort = None
        self.stream = False
        self.max_tokens = None
        self.temperature = None
        self.system = ""
        self.declared_ignored = []
        self.wants_usage = False
        self.show_thinking = True
        self.tools = []
        self.tool_choice = "auto"
        self.conversation_key = None


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
                import urllib.request, base64
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        ct = r.headers.get("content-type", "").split(";")[0].strip()
                        if ct:
                            mime_type = ct
                        data_b64 = base64.b64encode(r.read()).decode("ascii")
                except Exception as exc:
                    raise unprocessable(f"failed to fetch image attachments from {url}: {exc}", param=where)
    elif "imageData" in block:
        mime_type = block.get("mimeType") or "image/png"
        data_b64 = block.get("imageData")

    if not data_b64:
        raise unprocessable(f"{where}: missing image attachments data", param=where)
    return {"type": "image", "imageData": data_b64, "mimeType": mime_type}


def _parse_content(content, index, role):
    """Render content blocks to text and extract any native image blocks."""
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
            sub_text, sub_images = _parse_content(block.get("content"), index, "tool")
            chunks.append(f"[tool_result id={call_id}: {sub_text}]")
            images.extend(sub_images)
        elif btype in ("tool_use", "tool_call"):
            name = block.get("name") or "tool"
            args = block.get("input", block.get("arguments", {}))
            call_id = block.get("id") or block.get("tool_call_id") or "call"
            chunks.append(f"[tool_call {name} id={call_id}: {json.dumps(args, ensure_ascii=False)}]")
        elif btype in ("thinking", "redacted_thinking"):
            continue
        else:
            chunks.append(f"[{btype or 'block'}: {json.dumps(block, ensure_ascii=False)[:200]}]")
    return "\n".join(c for c in chunks if c), images


def _blocks_to_text(content, index, role):
    """Render content blocks without silently dropping information."""
    text, _ = _parse_content(content, index, role)
    return text


def _render_chunks(messages, role_key="role", *, allow_system=True):
    """Return (system_text, [chunk per conversation message]).

    Chunks keep one message per element so the direct transport can place cache
    breakpoints along the history instead of re-sending it uncached.
    """
    system_parts, convo = [], []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise unprocessable(f"messages[{index}] must be an object", param=f"messages[{index}]")
        role = message.get(role_key) or message.get("role")
        if role is None and message.get("tool_calls"):
            role = "assistant"
        if not isinstance(role, str):
            raise unprocessable(f"messages[{index}].{role_key} must be a string", param=f"messages[{index}].{role_key}")
        text, images = _parse_content(message.get("content"), index, role)
        if role in ("system", "developer") and allow_system:
            system_parts.append(text)
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
    return "\n\n".join(system_parts), convo


def _render_messages(messages, role_key="role", *, allow_system=True):
    """Return (system_text, prompt_text)."""
    system, convo = _render_chunks(messages, role_key, allow_system=allow_system)
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


def _message_fingerprints(messages):
    """Hash each bridge-visible message, preserving boundaries but not cache markers."""
    return tuple(hashlib.sha256(json.dumps(_render_messages([message]), ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8")).digest()
                 for message in messages)


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


def _finish(data, parsed):
    declared = [k for k in ("n", "seed", "presence_penalty", "frequency_penalty",
                            "logprobs", "top_logprobs", "response_format", "modalities", "metadata")
                if data.get(k) is not None]
    if declared:
        parsed.declared_ignored = declared
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
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    messages = _messages(data)
    system, chunks = _render_chunks(messages)
    text_chunks = [c if isinstance(c, str) else "\n".join(b.get("text", f"[{b.get('type')}]") for b in c if isinstance(b, dict)) for c in chunks]
    parsed.system, parsed.prompt, parsed.message_chunks = system, "\n\n".join(text_chunks), chunks
    parsed.message_fingerprints = _message_fingerprints(messages)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = (_opt_int(data, "max_tokens", high=_cap(entry))
                         or _opt_int(data, "max_completion_tokens", high=_cap(entry)))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed)


def parse_responses(data, cfg):
    parsed = Parsed()
    entry, effort = model_catalog.resolve_tolerant(
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort

    raw_input = data.get("input")
    if isinstance(raw_input, str):
        messages = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        messages = [_responses_item(item) for item in raw_input]
    elif raw_input is None:
        raise bad_request("'input' is required", param="input")
    else:
        raise bad_request("'input' must be a string or an array", param="input")
    system, chunks = _render_chunks(messages)
    instructions = _opt_str(data, "instructions") or ""
    parsed.system = "\n\n".join(x for x in (instructions, system) if x)
    text_chunks = [c if isinstance(c, str) else "\n".join(b.get("text", f"[{b.get('type')}]") for b in c if isinstance(b, dict)) for c in chunks]
    parsed.prompt, parsed.message_chunks = "\n\n".join(text_chunks), chunks
    parsed.message_fingerprints = _message_fingerprints(messages)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = _opt_int(data, "max_output_tokens", high=_cap(entry))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed)


def parse_anthropic(data, cfg):
    parsed = Parsed()
    model = _opt_str(data, "model")
    entry, effort = model_catalog.resolve_tolerant(
        model, _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    messages = _messages(data)
    system, chunks = _render_chunks(messages, allow_system=False)
    text_chunks = [c if isinstance(c, str) else "\n".join(b.get("text", f"[{b.get('type')}]") for b in c if isinstance(b, dict)) for c in chunks]
    parsed.prompt, parsed.message_chunks = "\n\n".join(text_chunks), chunks
    declared_system = data.get("system")
    if declared_system:
        parsed.system = _blocks_to_text(declared_system, 0, "system")
    parsed.message_fingerprints = _message_fingerprints(messages)
    parsed.stream = _opt_bool(data, "stream")
    if data.get("max_tokens") is None:
        raise bad_request("'max_tokens' is required for the Anthropic dialect", param="max_tokens")
    parsed.max_tokens = _opt_int(data, "max_tokens", high=_cap(entry))
    parsed.temperature = _opt_float(data, "temperature")
    stop = data.get("stop_sequences")
    if stop is not None and not isinstance(stop, list):
        raise unprocessable("'stop_sequences' must be an array", param="stop_sequences")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed)


def _effort(data):
    """Translate client controls to the actual Retool modelProvider effort field."""
    selected = []
    if data.get("reasoning_effort") is not None:
        selected.append(_opt_str(data, "reasoning_effort"))
    if data.get("reasoningEffort") is not None:
        selected.append(_opt_str(data, "reasoningEffort"))
    if data.get("effort") is not None:
        selected.append(_opt_str(data, "effort"))
    for container in (data.get("settings"), data.get("options"), data.get("body"),
                      data.get("extra_body"), (data.get("providerOptions") or {}).get("openai")):
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
    return selected[0] if selected else None


def _show_thinking(data, cfg):
    show = bool(cfg.get("show_thinking", True))
    thinking = data.get("thinking")
    if isinstance(thinking, dict) and thinking.get("display") is not None:
        display = thinking["display"]
        if display not in ("summarized", "omitted"):
            raise unprocessable("thinking.display must be 'summarized' or 'omitted'", param="thinking.display")
        show = show and display != "omitted"
    return show