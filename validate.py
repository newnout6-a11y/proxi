"""Strict request parsing for every supported dialect."""
import json

from apierr import bad_request, unprocessable
import models as model_catalog

MAX_TOKENS_CAP = 200000


class Parsed:
    """Normalised request shared by all adapters."""

    def __init__(self):
        self.prompt = ""
        self.model = ""
        self.entry = {}
        self.effort = None
        self.stream = False
        self.max_tokens = None
        self.temperature = None
        self.system = ""
        self.declared_ignored = []
        self.wants_usage = False


def load_json(raw: bytes):
    if not raw:
        raise bad_request("empty request body")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
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


def _opt_int(data, key, low=1, high=MAX_TOKENS_CAP):
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise unprocessable(f"'{key}' must be an integer", param=key)
    if not low <= value <= high:
        raise unprocessable(f"'{key}' must be between {low} and {high}", param=key)
    return value


def _opt_float(data, key):
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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
            source = block.get("source") if isinstance(block.get("source"), dict) else None
            if source:
                url = source.get("url") or source.get("data") or ""
            else:
                image = block.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
            label = "image"
            if isinstance(source, dict) and source.get("media_type"):
                label = str(source["media_type"])
            chunks.append(f"[{label}: {str(url)[:120] or 'inline'}]")
        elif btype in ("tool_use", "tool_call"):
            name = block.get("name") or "tool"
            args = block.get("input") or block.get("arguments") or {}
            chunks.append(f"[tool_call {name}: {json.dumps(args, ensure_ascii=False)[:400]}]")
        elif btype in ("tool_result",):
            chunks.append(f"[tool_result: {str(block.get('content'))[:400]}]")
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
        if not isinstance(role, str):
            raise unprocessable(f"messages[{index}].{role_key} must be a string", param=f"messages[{index}].{role_key}")
        text = _blocks_to_text(message.get("content"), index, role)
        if role in ("system", "developer") and allow_system:
            system_parts.append(text)
            continue
        if role in ("tool", "function"):
            name = message.get("name") or message.get("tool_call_id") or "tool"
            convo.append(f"[tool result {name}]: {text}")
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
                    convo.append(f"Assistant: {text}\n[tool_call {name}: {str(args)[:400]}]".strip())
                    text = ""
            if text:
                convo.append(f"Assistant: {text}")
            continue
        convo.append(f"{role.capitalize()}: {text}")
    return "\n\n".join(system_parts), "\n\n".join(convo)


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
    if isinstance(stream_options, dict) and stream_options.get("include_usage"):
        parsed.wants_usage = True
    return parsed


def parse_chat(data, cfg):
    parsed = Parsed()
    entry, effort = model_catalog.resolve(
        _opt_str(data, "model"), _effort(data), strict=cfg["strict_models"], default_model=cfg.get("model"))
    parsed.entry, parsed.model, parsed.effort = entry, entry["id"], effort
    messages = _messages(data)
    system, prompt = _render_messages(messages)
    parsed.system, parsed.prompt = system, prompt
    parsed.stream = bool(data.get("stream"))
    parsed.max_tokens = _opt_int(data, "max_tokens") or _opt_int(data, "max_completion_tokens")
    parsed.temperature = _opt_float(data, "temperature")
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
        messages = raw_input
    elif raw_input is None:
        raise bad_request("'input' is required", param="input")
    else:
        raise bad_request("'input' must be a string or an array", param="input")
    system, prompt = _render_messages(messages)
    instructions = _opt_str(data, "instructions") or ""
    parsed.system = "\n\n".join(x for x in (instructions, system) if x)
    parsed.prompt = prompt
    parsed.stream = bool(data.get("stream"))
    parsed.max_tokens = _opt_int(data, "max_output_tokens")
    parsed.temperature = _opt_float(data, "temperature")
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
    parsed.stream = bool(data.get("stream"))
    if data.get("max_tokens") is None:
        raise bad_request("'max_tokens' is required for the Anthropic dialect", param="max_tokens")
    parsed.max_tokens = _opt_int(data, "max_tokens")
    parsed.temperature = _opt_float(data, "temperature")
    stop = data.get("stop_sequences")
    if stop is not None and not isinstance(stop, list):
        raise unprocessable("'stop_sequences' must be an array", param="stop_sequences")
    return _finish(data, parsed)


def _effort(data):
    """Accept reasoning_effort, reasoning.effort and thinking.budget_tokens."""
    if data.get("reasoning_effort") is not None:
        return _opt_str(data, "reasoning_effort")
    reasoning = data.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
        return _opt_str(reasoning, "effort")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise unprocessable("'reasoning' must be an object", param="reasoning")
    thinking = data.get("thinking")
    if thinking is not None and not isinstance(thinking, dict):
        raise unprocessable("'thinking' must be an object", param="thinking")
    if isinstance(thinking, dict):
        if thinking.get("effort") is not None:
            return _opt_str(thinking, "effort")
        if thinking.get("budget_tokens") is not None:
            return model_catalog.effort_from_budget(thinking["budget_tokens"])
    return None
