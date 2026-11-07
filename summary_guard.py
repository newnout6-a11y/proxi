"""The reply to a Codex compaction must be text: a call of one of the chat's tools never reaches Codex.

The relay declares the chat's tools again on a compaction (proxy._codex_chat_tools, compaction_tools) only so that the request
shares the chat's cached prefix. Codex's summary is the last assistant message of its history after the reply
(codex-rs core/src/compact.rs get_last_assistant_message_from_turn): a function call in the reply is recorded and not run, and the
summary becomes the previous assistant message, or "(no summary available)". The route cannot be told to answer in text (Retool
takes tool_choice none on GPT, but on Claude it answers an empty turn), so the reply is checked here, as forced_choice checks a
forced call, and the attempt is asked again in words.

Measured on 2026-11-07 (live relay, the chat's tools declared, tool_choice auto, Codex's own prompt): gpt-6.1-sol wrote the summary
in 36 of 36 compactions (6 after a finished task, 30 in the middle of one, 22 of them Codex's own mid-turn requests), claude-opus-5-5
in 8 of 8 and claude-sonnet-5-5 in 6 of 6, with no tool call. The guard is for the call that does come.
"""
import forced_choice

# Attempts that keep the chat's prefix (the request as it is, then once more with the instruction below); a third goes without
# tools, from the client's own text, which is the request Codex sent: it cannot call anything.
TRIES_WITH_TOOLS = 2
# Events that describe the attempt or its billing, not its content: never held back.
PASSING = forced_choice.PASSING
INSTRUCTION = ("IMPORTANT: your next message must be plain text: the checkpoint summary that the previous instruction asks for. "
               "Do not call any tool, do not continue the work and do not ask questions; write the summary now.")


class Gate:
    """The events of one attempt, held until it ends, and released only if it called no tool.

    Billing and display-status events pass at once. A call is never passed on: it is only noted. An attempt that ends with a
    call releases nothing, so a retried attempt leaves no thinking, no text and no half of a call at the client; the summary
    the client receives is the whole reply of one attempt."""

    def __init__(self):
        self.held = []
        self.called = []  # the tools the attempt called, in order, once each
        self.text = False  # whether it wrote more than blanks

    def feed(self, event):
        """The events to pass on now."""
        kind = event.get("type")
        if kind == "tool_call":
            if event.get("name") not in self.called:
                self.called.append(event.get("name"))
            return []
        if kind == "text" and str(event.get("delta") or "").strip():
            self.text = True
        if kind in PASSING:
            return [event]
        self.held.append(event)
        return []

    def release(self):
        """What was held, in order; nothing is held afterwards."""
        ready, self.held = self.held, []
        return ready
