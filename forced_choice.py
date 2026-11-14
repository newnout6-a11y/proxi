"""A forced tool choice on a route that refuses one: the call is asked for in words, and the reply is checked.

Retool answers 400 to tool_choice any/required/named for every Claude model (`tool_choice: type "tool" and "any" are not
supported for this model`, with and without thinking, at every effort), so a client that forces a call (structured output
through a tool, the Agents SDK, LangChain) could not use Claude at all. The route takes `auto`. The relay sends `auto` and
puts one instruction at the very end of the request, in a user turn of its own after the last message. That turn is never
stored and never carries a cache mark, so the history the next request continues from, the signed thinking in it and the
cache behind it are what the client's own messages made them.

Measured on 2026-11-06 (532 tries, Opus 5.5 and Sonnet 5.5, thinking on and off, one to three small tools): `auto` alone
called the requested tool in 0 of 10 per case; the plain wording "Call the tool X now; do not answer in text." did in 210 of
240, and failed in two ways: the model would not invent an argument it lacked (it asked for the city), and under a named choice
it called the other tool as well. The wording below, which closes both, gave the requested call in 160 of 160, with no extra
call and no text before it. GPT and GLM take both choices natively (71 of 72) and never come here.
"""
import collections

import models

# What a reply has to hold: `named` is the one tool that satisfies it (None: any declared tool does), `names` the tools the
# request declares.
Requirement = collections.namedtuple("Requirement", "named names")

_UNKNOWN_ARGUMENT = "If a required argument is unknown, use a plausible value instead of asking."
# Events that describe the attempt or its billing, not its content: never held back.
PASSING = frozenset({"usage_start", "usage", "credits", "thinking_status", "thinking_heartbeat", "error"})


def _tool_name(tool):
    """The name of a tool in the Retool shape or in the relay's parsed one ({"type": "function", "function": {...}})."""
    if not isinstance(tool, dict):
        return None
    return (tool["function"] if isinstance(tool.get("function"), dict) else tool).get("name")


def requirement(model, tools, choice):
    """The Requirement of a request that forces a tool call on a route that cannot be told to; None for any other.

    `tools` are the declarations in either shape, `choice` the client's normalised tool_choice."""
    if not tools or not models.refuses_forced_tool_choice(model):
        return None
    names = frozenset(name for name in map(_tool_name, tools) if name)
    if choice == "required":
        return Requirement(None, names)
    named = (choice.get("function") or {}).get("name") if isinstance(choice, dict) else None
    return Requirement(named, names) if named in names else None


def instruction(required, firm=False):
    """The text of the user turn that asks for the call; `firm` is the wording of the one retry."""
    if firm:
        target = "one of the available tools" if required.named is None else "the tool %s" % required.named
        return ("IMPORTANT: your next message must be a call to %s%s. Do not write any text, do not ask questions and "
                "do not refuse; call it now. %s" % (target, "" if required.named is None else ", and no other tool",
                                                    _UNKNOWN_ARGUMENT))
    if required.named is None:
        return "Call one of the available tools now; do not answer in text. " + _UNKNOWN_ARGUMENT
    return "Call the tool %s now, and no other tool; do not answer in text. %s" % (required.named, _UNKNOWN_ARGUMENT)


class Gate:
    """The events of one attempt, held back until the attempt holds the call the client forced.

    Billing and display-status events pass at once. Everything else waits. When a call that satisfies the choice starts,
    what waited is released in order and the rest of the attempt passes as it comes. An attempt that ends without one
    releases nothing: a retried attempt leaves no thinking, no text and no half of a call at the client."""

    def __init__(self, required):
        self.required = required
        self.complied = False
        self.held = []
        self.called = []  # the tools the attempt called, the satisfying call or not
        self.text = False  # whether it wrote more than blanks

    def _satisfies(self, name):
        return name == self.required.named if self.required.named is not None else name in self.required.names

    def feed(self, event):
        """The events to pass on now."""
        kind = event.get("type")
        if kind == "tool_call" and event.get("name") not in self.called:
            self.called.append(event.get("name"))
        elif kind == "text" and str(event.get("delta") or "").strip():
            self.text = True
        if self.complied or kind in PASSING:
            return [event]
        if kind == "tool_call" and self._satisfies(event.get("name")):
            self.complied = True
            ready, self.held = [*self.held, event], []
            return ready
        self.held.append(event)
        return []
