"""The name a client tool goes upstream under."""
import hashlib
import re

# OpenAI accepts a function name only within ^[a-zA-Z0-9_-]{1,64}$ (Anthropic's rule is laxer). Codex 0.166 sends its
# MCP servers as namespaces ("mcp__blender" holding "look"), and the dotted "mcp__blender.look" the relay used to send
# failed every turn on a GPT model: "Invalid 'tools[6].name': string does not match pattern".
MAX_LENGTH = 64
SEPARATOR = "__"
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def namespaced(namespace, name):
    """The upstream name of tool `name` of `namespace`: namespace__name, within ^[a-zA-Z0-9_-]{1,64}$.

    A longer name keeps its start and ends in a hash of the whole, so two long names stay apart."""
    joined = _UNSAFE.sub("_", "%s%s%s" % (namespace, SEPARATOR, name))
    if len(joined) > MAX_LENGTH:
        joined = joined[:MAX_LENGTH - 9] + "_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]
    return joined
