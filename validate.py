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
    if not low <= value <= high:
        raise unprocessable(f"'{key}' must be between {low} and {high} (model output cap)", param=key)
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


def _blocks_to_text(content, index, role):
    """Render content blocks without silently dropping information."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise unprocessable(f"messages[{index}].content must be a string or an array of blocks",
                            param=f"messages[{index}].content")
    chunks = []
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
        elif btype in ("image", "image_url", "input_image"):
            raise unprocessable("image attachments are not supported by this relay", param=where)
        elif btype in ("tool_use", "tool_call"):
            name = block.get("name") or "tool"
            args = block.get("input", block.get("arguments", {}))
            call_id = block.get("id") or block.get("tool_call_id") or "call"
            chunks.append(f"[tool_call {name} id={call_id}: {json.dumps(args, ensure_ascii=False)}]")
        elif btype in ("tool_result",):
            call_id = block.get("tool_use_id") or block.get("tool_call_id") or "call"
            result = _blocks_to_text(block.get("content"), index, "tool")
            chunks.append(f"[tool_result id={call_id}: {result}]")
        elif btype in ("thinking", "redacted_thinking"):
            continue
        else:
            chunks.append(f"[{btype or 'block'}: {json.dumps(block, ensure_ascii=False)[:200]}]")
    return "\n".join(c for c in chunks if c)


def _render_messages(messages, role_key="role", *, allow_system=True):
    """Return (system_text, prompt_text)."""
    system_parts, convo = [], []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise unprocessable(f"messages[{index}] must be an object", param=f"messages[{index}]")
        role = message.get(role_key) or message.get("role")
        if role is None and message.get("tool_calls"):
            role = "assistant"
        if not isinstance(role, str):
            raise unprocessable(f"messages[{index}].{role_key} must be a string", param=f"messages[{index}].{role_key}")
        text = _blocks_to_text(message.get("content"), index, role)
        if role in ("system", "developer") and allow_system:
            system_parts.append(text)
            continue
        if role in ("tool", "function"):
            name = message.get("name") or message.get("tool_call_id") or "tool"
            call_id = message.get("tool_call_id") or message.get("call_id") or name
            convo.append(f"[tool result {name} id={call_id}]: {text}")
            continue
        if role == "assistant":
            calls = message.get("tool_calls") or []
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    name = fn.get("name") or call.get("name") or "tool"
                    args = fn.get("arguments") or call.get("arguments") or ""
                    call_id = call.get("id") or call.get("tool_call_id") or "call"
                    convo.append(f"Assistant: {text}\n[tool_call {name} id={call_id}: {str(args)}]".strip())
                    text = ""
            if text:
                convo.append(f"Assistant: {text}")
            continue
        convo.append(f"{role.capitalize()}: {text}")
    return "\n\n".join(system_parts), "\n\n".join(convo)


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
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise unprocessable("'stream_options' must be an object", param="stream_options")
        parsed.wants_usage = _opt_bool(stream_options, "include_usage")
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


def conversation_key(data, header_value=None):
    """Return a stable affinity key for one client dialogue.

    Without an explicit client identity, full request history is sufficient
    and a fresh upstream thread prevents unrelated clients sharing history.
    """
    if isinstance(header_value, str) and header_value.strip():
        return "header:" + header_value.strip()
    for key in ("conversation", "conversation_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return key + ":" + value.strip()
    metadata = data.get("metadata")
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    if isinstance(user_id, str):
        try:
            identity = json.loads(user_id)
        except (ValueError, RecursionError):
            identity = None
        session_id = identity.get("session_id") if isinstance(identity, dict) else None
        if isinstance(session_id, str) and session_id.strip():
            return "claude-metadata:" + session_id.strip()
    return None


def parse_chat(data, cfg):
    parsed = Parsed()
    entry, effort = model_catalog.resolve(
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    messages = _messages(data)
    system, prompt = _render_messages(messages)
    parsed.system, parsed.prompt = system, prompt
    parsed.message_fingerprints = _message_fingerprints(messages)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = (_opt_int(data, "max_tokens", high=_cap(entry))
                         or _opt_int(data, "max_completion_tokens", high=_cap(entry)))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed)


def parse_responses(data, cfg):
    parsed = Parsed()
    entry, effort = model_catalog.resolve(
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
    system, prompt = _render_messages(messages)
    instructions = _opt_str(data, "instructions") or ""
    parsed.system = "\n\n".join(x for x in (instructions, system) if x)
    parsed.prompt = prompt
    parsed.message_fingerprints = _message_fingerprints(messages)
    parsed.stream = _opt_bool(data, "stream")
    parsed.max_tokens = _opt_int(data, "max_output_tokens", high=_cap(entry))
    parsed.temperature = _opt_float(data, "temperature")
    parsed.show_thinking = _show_thinking(data, cfg)
    return _finish(data, parsed)


def parse_anthropic(data, cfg):
    parsed = Parsed()
    model = _opt_str(data, "model")
    entry, effort = model_catalog.resolve(
        model, _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    messages = _messages(data)
    system, prompt = _render_messages(messages, allow_system=False)
    declared_system = data.get("system")
    if declared_system:
        parsed.system = _blocks_to_text(declared_system, 0, "system")
    parsed.prompt = prompt
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
