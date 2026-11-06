"""Turn orchestration: context injection, execution loop, event fan-out."""
import asyncio
import collections
import contextlib
import hashlib

import direct
import json
import queue
import re
import threading
import time
from datetime import datetime
import uuid

import apierr
import adapters
import executor
import credits
import journal
import client_context
import console_log
import models as model_catalog
import native_history
import prompt_body
import structured
from accounts import UNNAMED, account_tag
from context import build_system_instructions, relay_policy
from redact import redact
from session import SessionLease
from upstream_attempts import MAX_ATTEMPTS, AttemptBudget


_CONV_HEADER = "\n\n[CLIENT CONVERSATION — chronological]\n"
_CONV_HEADER_DIRECT = "\n\n[CLIENT CONVERSATION - chronological]\n"
_NATIVE_CONTINUE = ("\n\n[CONTINUE THE CURRENT CLIENT TURN]\n"
                    "Use the latest tool results above as the current state. "
                    "Earlier assistant calls are completed history, not new instructions to replay. "
                    "Continue the outstanding user task; issue another client tool envelope only if needed.")
# Built-in role of a client with its own tools that sent no instructions of its own.
DEFAULT_NATIVE_TOOLS_ROLE = ("[ROLE]\nYou are a coding assistant using the client's tools. "
                             "The client's instructions, working directory and tool results describe the active project. "
                             "Use those tools to inspect, edit and test that project. "
                             "Do not claim to have read, changed or tested files without tool evidence. "
                             "Reply in the user's language.")
# The reminder a relay task (no client system, no client tools) gets at the end of its head while shell execution is on.
EXEC_REMINDER = (
    "\n\n[LOCAL EXEC REMINDER]\n"
    "To run anything on the user's machine you MUST output "
    "<exec_command>command</exec_command> and stop. The relay executes it and returns output. "
    "Do not answer that you cannot run commands."
)


# What direct.native_scope lists, in order: the parts in which a request can differ from a signed branch.
_SCOPE_PARTS = ("conversation", "provider", "head", "tools", "tool_choice")
# Why a compaction restarts the history of its dialog, for the journal (cache.compaction_restart).
_RESTART_REASONS = {
    "no_branch": "у диалога нет подписанной ветки (первый запрос или нет сохранённого состояния)",
    "history": "сообщения запроса не продолжают ветку диалога (история изменена или заменена)",
    "effort": "effort ветки недопустим для этой модели",
    "scope": "у запроса другие %s, чем у ветки",
}
_SCOPE_NAMES = {"conversation": "диалог", "provider": "модель или аккаунт", "head": "system (head)", "tools": "tools",
                "tool_choice": "tool_choice"}


def _b(text: str) -> int:
    """Limit in bytes, not characters (Cyrillic is 2 bytes each in UTF-8)."""
    return len((text or "").encode("utf-8"))


def _preview(value, limit=600):
    """Compact, bounded rendering of tool arguments for the journal."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _trim(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", "ignore")


def _remember_prompt_size(session, parsed, tokens):
    """What count_tokens scales from: the size of the client's request and the tokens upstream counted for it."""
    if not tokens:
        return
    chars = getattr(parsed, "client_chars", None)
    if chars is None:
        chars = adapters.request_chars(parsed)
    session._count_anchor = (chars, tokens, parsed.model)



class _ClientText:
    """Stream ordinary native-client text while suppressing relay tool markup."""
    TAGS = ("<tool_call", "<exec_command")


    def __init__(self):
        self.pending = ""
        self.closing = None
        self.suppressed = ""
        self.leading_space = ""
        self.visible = False

    def feed(self, delta, *, final=False):
        self.pending += delta
        visible = []
        while self.pending:
            lowered = self.pending.lower()
            if self.closing:
                end = lowered.find(self.closing)
                if end < 0:
                    keep = len(self.closing) - 1
                    if len(self.pending) > keep:
                        self.suppressed += self.pending[:-keep]
                        self.pending = self.pending[-keep:]
                    break
                self.pending = self.pending[end + len(self.closing):]
                self.closing = None
                self.suppressed = ""
                continue
            starts = [(lowered.find(tag), tag) for tag in self.TAGS if tag in lowered]
            if starts:
                start, tag = min(starts)
                visible.append(self.pending[:start])
                self.closing = "</" + tag[1:] + ">"
                self.suppressed = self.pending[start:start + len(tag)]
                self.pending = self.pending[start + len(tag):]
                continue
            keep = max((length for tag in self.TAGS for length in range(1, len(tag))
                        if lowered.endswith(tag[:length])), default=0)
            visible.append(self.pending[:-keep] if keep else self.pending)
            self.pending = self.pending[-keep:] if keep else ""
            break
        if final and self.closing:
            total = self.suppressed + self.pending
            tag_name = self.closing.replace("</", "<").rstrip(">")
            rem = total
            if rem.lower().startswith(tag_name.lower()):
                rem = rem[len(tag_name):].lstrip(">").strip()
            is_quoted_markup = rem.startswith("`") or rem.startswith('"') or rem.startswith("'")
            if any(tok in rem for tok in ("{", "```", '"name"', '"arguments"')):
                is_quoted_markup = False
            if is_quoted_markup:
                visible.append(total)
            self.pending = ""
            self.closing = None
            self.suppressed = ""
        text = "".join(visible)
        if not self.visible:
            text = self.leading_space + text
            self.leading_space = ""
            if not text.strip():
                if not final:
                    self.leading_space = text
                return ""
            self.visible = True
        return text


class _FailoverOut:
    """Forwards turn events, holding back the error and end sentinel so the
    runner can retry on another account before the client sees anything."""

    def __init__(self, out):
        self.out = out
        self.error = None
        self.started = False

    def put(self, event):
        if event is None:
            return
        if event.get("type") == "error":
            self.error = event["error"]
            return
        if event.get("type") not in ("credits", "usage", "usage_start"):
            self.started = True
        self.out.put(event)


def _may_be_balance_related(error):
    """Rate limits, timeouts and client errors never justify a balance fetch."""
    status = getattr(error, "status", 0) or 0
    code = getattr(error, "code", None)
    if status == 402 or (status == 429 and code == "insufficient_credits"):
        return True
    return status >= 500 and code != "upstream_timeout"


def _can_exec(cfg, steps: int) -> bool:
    """No limits by default; an explicit max_exec_turns > 0 caps the steps."""
    if cfg["exec_policy"] == "off":
        return False
    max_steps = int(cfg.get("max_exec_turns", 0) or 0)
    return max_steps <= 0 or steps < max_steps


def _purpose(parsed):
    return getattr(parsed, "request_purpose", None) or client_context.request_kind(parsed)


def _client_system(parsed):
    """The client's own system text; a blank one (an effort-only entry, a lone newline) is no instruction at all."""
    return parsed.system if (parsed.system or "").strip() else ""


# What of the request as the client sent it decides who owns it, whether anything may be executed for it and which
# pool session it takes (plan I9). A profile may rewrite the body so that its second parse no longer shows these (it hides
# every tool): proxy hands the first parse's answers on as `prompt_origin`.
ClientOrigin = collections.namedtuple("ClientOrigin", "tools system structured")


def client_origin(parsed):
    """The ClientOrigin of `parsed`, the parse of the body the client sent."""
    return ClientOrigin(tools=bool(getattr(parsed, "tools", None)), system=bool(_client_system(parsed)),
                        structured=bool(getattr(parsed, "structured_output", None)))


def _origin(parsed):
    origin = getattr(parsed, "prompt_origin", None)
    return origin if isinstance(origin, ClientOrigin) else None


def client_tools(parsed):
    """Whether the client sent tools of its own, as it sent them: a profile may hide all of them from the model."""
    origin = _origin(parsed)
    return origin.tools if origin is not None else bool(getattr(parsed, "tools", None))


def _client_owns_instructions(parsed, purpose, native_tools):
    """True when the client brings the instructions of this job, so the relay adds no project context or shell protocol.

    A JSON-only reply has no room for the relay's workspace or shell protocol;
    a client that supplies tools keeps the relay's native-tools role as before.
    A request a profile rewrote is judged by what the client sent (plan I9), not by the body it was turned into."""
    origin = _origin(parsed)
    if origin is not None:
        native_tools, system, structured_output = origin.tools, origin.system, origin.structured
    else:
        system, structured_output = bool(_client_system(parsed)), bool(getattr(parsed, "structured_output", None))
    return purpose in client_context.CLIENT_TEXT_KINDS or system or (structured_output and not native_tools)


def _relay_task_args(cfg):
    """What build_system_instructions takes besides the workspace and the role.

    The legacy head and the blocks of a prompt template read the same settings the same way."""
    return dict(enable_context=cfg["project_context"], enable_local_exec=cfg["exec_policy"] != "off",
                limit=cfg["context_max_bytes"], cookie_values=cfg.get("cookie_values", ()),
                exec_policy=cfg["exec_policy"])


class RelaySources:
    """The relay's own texts a prompt template may name (prompt_policy.HeadSources), each built once, and only when asked.

    The blocks of a relay task come from the same build_system_instructions call as the legacy head, so
    `{{relay_task}}{{exec_reminder}}` repeats that head byte for byte (with the built-in role). They stay empty for a
    request the client owns and for one that brings client tools (plan I9): no project context and no shell protocol
    reach a client's job.
    The role texts are the file's relay_texts, else the built-in text. config.json's relay_system_prompt belongs to the
    legacy head alone (no file, $legacy, no rule matched): a request a profile serves never reads it, so the preview
    shows the head the relay sends, and saving a role in the settings leaves the heads of conversations that a profile
    holds as they are (plan I3). Only cfg is read, so the preview can build the same object for a pasted request.
    """

    def __init__(self, cfg, snapshot, *, client_owned, native_tools, output_spec=None, tool_choice="auto",
                 client_tools=None):
        self.cfg, self.snapshot = cfg, snapshot
        self.client_owned = client_owned
        self.native_tools, self.output_spec, self.tool_choice = bool(native_tools), output_spec, tool_choice
        # native_tools says what the model is offered; client_tools whether the client sent any (a profile can hide them all)
        self.client_tools = self.native_tools if client_tools is None else bool(client_tools)
        self._built = {}

    def _once(self, name, build):
        if name not in self._built:
            self._built[name] = build()
        return self._built[name]

    def _text(self, name):
        return self.snapshot.relay_text(name) if self.snapshot is not None else None

    @property
    def _is_relay_task(self):
        return not self.client_owned and not self.client_tools

    def relay_role(self):
        # None is the built-in role, a blank text means no role of the relay's own: as relay_policy reads settings.
        return self._once("role", lambda: relay_policy(self._text("role")))

    def native_role(self):
        def build():
            text = self._text("native_role")
            return DEFAULT_NATIVE_TOOLS_ROLE if text is None else (text if text.strip() else "")
        return self._once("native_role", build)

    def _blocks(self):
        def build():
            if not self._is_relay_task:
                return "", ""
            args, root = _relay_task_args(self.cfg), self.cfg["workspace_root"]
            return (build_system_instructions(root, system_prompt="", **{**args, "enable_context": False}),
                    build_system_instructions(root, system_prompt="", **{**args, "enable_local_exec": False}))
        return self._once("blocks", build)

    def local_exec(self):
        return self._blocks()[0]

    def project_context(self):
        return self._blocks()[1]

    def relay_task(self):
        return self._once("task", lambda: "\n\n".join(
            part for part in (self.relay_role(), *self._blocks()) if part) if self._is_relay_task else "")

    def exec_reminder(self):
        return EXEC_REMINDER if self._is_relay_task and self.cfg["exec_policy"] != "off" else ""

    def structured(self):
        if not self.output_spec:
            return ""
        # Imported here: prompt_policy may need this module for the built-in texts.
        import prompt_policy
        tools = bool(self.native_tools)
        try:
            return prompt_policy.structured_contract(self.snapshot, self.output_spec, tools=tools,
                                                     tool_choice=self.tool_choice)
        except NotImplementedError:  # until the engine lands, the contract is the function's own text
            return structured.instructions(self.output_spec, tools=tools, tool_choice=self.tool_choice)


def split_changes(changes):
    """(history, tools) from the one list prompt_body.rewrite returns, by the scope each change names.

    A change without a scope (a rewrite that does not name one) edits tools when it names a tool."""
    history, tools = [], []
    for change in changes or ():
        scope = change.get("scope")
        if scope not in ("history", "tools"):
            scope = "tools" if "tool" in change else "history"
        (tools if scope == "tools" else history).append(change)
    return history, tools


# What prompt.plan and the ring say when a profile leaves a client that sent tools (and no system prompt) with no head
# at all: without the profile it would have had the relay's native-tools role.
EMPTY_HEAD_WARNING = ("the head is empty: the client sent tools but no system prompt, and the profile's template adds "
                      "nothing; the model works from the tools alone (add {{native_role}} to give it the relay's role "
                      "for a client with tools)")


# What a recorded request keeps of the changes a rewrite made: the ring is in memory, so a body with thousands of
# edited messages is cut here, and the summary (one entry per kind of edit) still names every kind.
RING_CHANGES = 200
# The journal takes prompt_body.summarize_changes; a rewrite without one gets its first entries only.
JOURNAL_CHANGES = 20


def journal_changes(changes):
    """The changes of a rewrite in the form a journal event takes: one entry per kind of edit, never one per message."""
    summarize = getattr(prompt_body, "summarize_changes", None)
    if summarize is None:
        return list(changes or ())[:JOURNAL_CHANGES]
    return summarize(list(changes or ()))


# kind: "text" (the persistent text chat), "isolated" (a standalone job) or "session" (the conversation's own one).
AcquirePlan = collections.namedtuple("AcquirePlan", "kind kwargs")


def acquire_plan(cfg, parsed):
    """Which pool session a turn asks for, as SessionPool.acquire()'s `isolated` / `context_scope` keywords.

    TurnRunner acquires with exactly this plan, and the HTTP layer puts the same keywords to the pool
    (model_refusal, unavailable_error) before a response starts: the check and the acquire read the same accounts.
    The plan follows the request as the client sent it (plan I9): a profile that hides every tool leaves the
    conversation in the session its tools made, and the plan is the same before and after the body is rewritten.
    """
    native_tools = client_tools(parsed)
    direct_mode = cfg.get("transport") == "direct"
    mode = cfg.get("session_mode")
    purpose = _purpose(parsed)
    key = getattr(parsed, "conversation_key", None)
    parent_key = getattr(parsed, "parent_conversation_key", None)
    can_exec = (_can_exec(cfg, 0) and not native_tools
                and not _client_owns_instructions(parsed, purpose, native_tools))
    persistent_text = (direct_mode and not native_tools and mode != "per_request"
                       and purpose not in client_context.SERVICE_KINDS and (key or mode == "shared"))
    isolated = (not native_tools and not persistent_text
                and (direct_mode or purpose in client_context.CLIENT_TEXT_KINDS or (key and not can_exec)))
    child_scope = ("subagent", key) if parent_key and key and mode == "shared" else None
    if persistent_text:
        # Ordinary text chats retain idle keepwarm, with state separate from native
        # tools even when their conversation ID is the same.
        return AcquirePlan("text", {"context_scope": (child_scope, "text") if child_scope else "text"})
    if isolated:
        # A standalone job can share account affinity, never the main chat's tools, head,
        # implicit history, cache breakpoints or idle keepwarm.
        return AcquirePlan("isolated", {"isolated": True})
    return AcquirePlan("session", {"context_scope": child_scope} if child_scope else {})


class TurnRunner:
    def __init__(self, cfg, credits, pool):
        self.cfg = cfg
        self.credits = credits
        self.pool = pool
        # AccountDispatcher; set by Runtime when it manages account health.
        self.accounts = None
        # PromptStore; set by Runtime. None keeps every head on the legacy path.
        self.prompts = None

    def _can_exec(self, steps: int) -> bool:
        return _can_exec(self.cfg, steps)

    def _looks_looping(self, command: str, history) -> bool:
        """True when this command already ran three times in the recent window."""
        key = " ".join(command.strip().lower().split())
        recent = [" ".join(c.strip().lower().split()) for c, _ in history]
        return recent.count(key) >= 3

    def _remember(self, command: str, history) -> None:
        history.append((command, 0))
        if len(history) > 8:
            del history[0]

    def _dialog_effort(self, parsed, head, choice, exclude):
        """(the effort of the dialog, why the history restarts anyway) for a compaction.

        The first is the effort of the dialog when asking for it makes the signed history of the dialog resume, else None
        to leave the client's own effort alone. The second is None, or, when the request does not continue the dialog's
        signed history on any effort, what to tell the journal: {"reason": "no_branch" | "history" | "effort"} or
        {"reason": "scope", "differs": [...]} naming what differs from the newest branch (the parts of native_scope).

        A signed branch is made under one reasoning effort and continues only a request that asks for that one: with
        another the history starts again, and the provider drops its cached messages as well (Anthropic: a changed
        output_config.effort always invalidates them). The compaction agent of a client can have a variant of its own, so
        its request, the chat's own history and one more message, comes on another effort than the chat.

        This asks the transport's own question, History.resume under the scope the request would have with the effort of
        each live branch, so it says yes only when the history really continues: another head, tools, model or account, or a
        history that is not the branch's own, are all a no. The effort must be valid for the model under the rules the
        client's own obey (a nearest level is another prefix). A signature never comes from the client: the branches are
        the relay's own, kept on the session of the dialog.
        """
        client, key = getattr(parsed, "native_messages", None), getattr(parsed, "conversation_key", None)
        peek = getattr(self.pool, "peek", None)
        if not client or not key or peek is None:
            return None, None
        session = peek(key, model_entry=parsed.entry, exclude=exclude, **acquire_plan(self.cfg, parsed).kwargs)
        history = getattr(session, "_native_history", None)
        if history is None or not history.branches:
            return None, {"reason": "no_branch"}
        try:
            resource = model_catalog.provider_resource(
                getattr(getattr(session, "backend", None), "cfg", None) or self.cfg, parsed.entry)
        except apierr.ApiError:
            return None, None  # acquire says why
        wanted = model_catalog.effective_effort(parsed.entry, parsed.effort, self.cfg.get("reasoning_effort"))
        tools = direct.tools_to_retool(parsed.tools)

        def scope_of(effort):
            provider = {"model": parsed.model}
            if effort is not None:
                provider["reasoningEffort"] = effort
            if resource:
                provider["providerResourceName"] = resource
            return direct.native_scope(key, provider, head or "", tools, choice)

        refused = False
        defaults = native_history.tool_defaults(parsed.tools)
        for effort in dict.fromkeys(direct.scope_effort(branch.scope) for branch in reversed(list(history.branches))):
            if effort is None:
                continue
            found, _ = history.resume(scope_of(effort), client, defaults)
            if found is None:
                continue
            if effort == wanted:
                return None, None  # the request resumes as it is
            try:
                _, resolved = model_catalog.resolve_tolerant(
                    parsed.model, effort, strict=self.cfg.get("strict_models", True), default_model=self.cfg.get("model"),
                    allow_downgrade=self.cfg.get("allow_effort_downgrade", False))
            except apierr.ApiError:
                resolved = None
            if resolved == effort:
                return effort, None
            refused = True
        # Nothing continues: say why when the newest branch is the one it should have been (a text for the journal).
        newest = history.branches[-1]
        try:
            differs = [name for name, before, after in zip(_SCOPE_PARTS, json.loads(newest.scope),
                                                           json.loads(scope_of(direct.scope_effort(newest.scope))))
                       if before != after]
        except ValueError:
            differs = []
        if differs:
            return None, {"reason": "scope", "differs": differs}
        if history.resume(newest.scope, client, defaults)[0] is None:
            return None, {"reason": "history"}
        return None, ({"reason": "effort"} if refused else None)

    def _check_prompt(self, prompt):
        limit = int(self.cfg.get("max_prompt_bytes", 0) or 0)
        if limit > 0 and _b(prompt) > limit:
            raise apierr.ApiError(413, "prompt_too_large",
                                 f"complete upstream prompt exceeds configured max_prompt_bytes={limit}; "
                                 "increase the limit or set it to 0 to preserve full history")
        return prompt

    async def _native_prompt(self, session, parsed, instructions, reminder, full_prompt, cancelled):
        # Leases are request-specific; history belongs to their actual upstream session.
        target = session.session if isinstance(session, SessionLease) else session
        key = getattr(parsed, "conversation_key", None)
        fingerprint = hashlib.sha256(json.dumps(
            [instructions, reminder, parsed.tools], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).digest()
        previous = getattr(target, "_client_context", None)
        pending = getattr(target, "_client_context_pending", False)
        messages = getattr(parsed, "message_fingerprints", None)
        old_messages = previous.get("message_fingerprints") if previous else None
        messages_appended = (isinstance(messages, tuple) and isinstance(old_messages, tuple)
                             and len(messages) > len(old_messages)
                             and messages[:len(old_messages)] == old_messages)
        suffix = ""
        if previous and parsed.prompt.startswith(previous["history"]):
            suffix = parsed.prompt[len(previous["history"]):]
        reuse = (key and previous and previous["key"] == key
                 and previous["fingerprint"] == fingerprint
                 and previous["thread_id"] == target.thread_id
                 and not pending and not getattr(target, "_needs_drain", False)
                 and messages_appended
                 and suffix.startswith("\n") and suffix.strip())
        # Failed or cancelled attempts may already have changed Retool history.
        target._client_context_pending = True
        if cancelled.is_set():
            raise asyncio.CancelledError()
        if reuse:
            update = ("[CLIENT CONVERSATION UPDATE - chronological]\n"
                      if self.cfg.get("transport") == "direct"
                      else "[CLIENT CONVERSATION UPDATE — chronological]\n")
            prompt = update + suffix.lstrip("\r\n") + _NATIVE_CONTINUE
        else:
            if self.cfg.get("transport") != "direct":
                if (previous or pending or getattr(target, "ws", None) is not None
                        or getattr(target, "_needs_drain", False) or self.cfg.get("session_mode") == "shared"):
                    await target.aclose()
                    thread_id = await target.backend.create_thread()
                    if cancelled.is_set() or getattr(target, "_cancelled", False):
                        raise asyncio.CancelledError()
                    target.thread_id = thread_id
            prompt = full_prompt
        context = {"key": key, "history": parsed.prompt, "fingerprint": fingerprint,
                   "thread_id": target.thread_id, "message_fingerprints": messages} if key else None
        return prompt, target, context

    @staticmethod
    def _commit_native_context(target, context, completed, cancelled):
        if (context is not None and completed and not cancelled.is_set()
                and not getattr(target, "_cancelled", False)
                and not getattr(target, "_needs_drain", False)):
            target._client_context = context
            target._client_context_pending = False

    @staticmethod
    def _direct_text(instructions, chunks):
        """Text actually sent in the system head and each retained content block."""
        parts = [instructions] if instructions else []
        def append(value):
            if isinstance(value, str):
                if value:
                    parts.append(value)
            elif isinstance(value, list):
                for block in value:
                    append(block)
            elif isinstance(value, dict) and value.get('type') == 'text':
                append(value.get('text'))
        for chunk in chunks:
            append(chunk)
        return '\n'.join(parts)

    def _followup(self, instructions, reminder, command, code, output, header, *, direct_chunks=None):
        def tail(body):
            return executor.format_command_followup(command, code, body, header) + reminder

        def compose(body):
            text = tail(body)
            return instructions + "\n\n" + text if instructions else text

        def full(body):
            return (compose(body) if direct_chunks is None else
                    self._direct_text(instructions, [*direct_chunks, tail(body)]))

        limit = int(self.cfg.get("max_prompt_bytes", 0) or 0)
        if limit > 0:
            fixed = self._check_prompt(full(""))
            budget = limit - _b(fixed)
            if _b(output) > budget:
                notice = "\n[command output truncated to configured prompt byte limit]"
                if budget < _b(notice):
                    raise apierr.ApiError(413, "prompt_too_large",
                                         "configured max_prompt_bytes leaves no room for command output")
                output = _trim(output, budget - _b(notice)) + notice
        self._check_prompt(full(output))
        return compose(output)

    def _instructions(self, native_tools=False):
        # relay_system_prompt is a live setting: read it once for this head.
        role = self.cfg.get('relay_system_prompt')
        if native_tools:
            if role is not None:
                return relay_policy(role)
            return DEFAULT_NATIVE_TOOLS_ROLE
        return build_system_instructions(
            self.cfg["workspace_root"], system_prompt=role, **_relay_task_args(self.cfg))

    def _client_instructions(self, instructions):
        """Compose only policy text, never relay workspace or shell capabilities."""
        # Mode and role are live settings: read together, so one head never mixes an old mode with a new role.
        mode, role = self.cfg.get('relay_system_prompt_mode', 'standalone'), self.cfg.get('relay_system_prompt')
        if mode == 'standalone':
            return instructions
        policy = relay_policy(role)
        if mode == 'replace':
            return policy
        return '\n\n'.join(part for part in (instructions, policy) if part)

    async def _compose_head(self, parsed, client_system, client_owned, native_tools, output_spec, choice, log):
        """(head, parts, warnings) when a prompt profile owns this head (plan section 3); None sends it down the legacy path."""
        plan = getattr(parsed, "prompt_plan", None)
        if plan is None or plan.legacy:
            return None
        sources = RelaySources(self.cfg, plan.snapshot, client_owned=client_owned, native_tools=native_tools,
                               output_spec=output_spec, tool_choice=choice, client_tools=client_tools(parsed))
        try:
            # Off the event loop, as the legacy head is: a relay task reads its project from disk.
            composed = await asyncio.get_running_loop().run_in_executor(
                None, lambda: plan.compose_head(client_system, sources))
            head, parts, warnings = composed
            if not isinstance(head, str):
                raise TypeError("compose_head returned %s instead of the head text" % type(head).__name__)
        except apierr.ApiError:
            raise
        except Exception as exc:  # a failing engine must not fail the request (plan I8): the legacy head serves it
            text = redact(str(exc)).replace("\n", " ")[:160]
            log("prompt.runtime_error", "Слой промптов не собрал head, запрос идёт прежним путём: %s: %s" % (
                type(exc).__name__, text), "error", stage="compose")
            console_log.write("[err] prompt layer failed (compose): %s; legacy head" % type(exc).__name__)
            return None
        return head, parts, warnings

    def _note_plan(self, log, holder, parsed, composed):
        """The prompt.plan event of a turn and, once per request, the ring entry of what the model saw (plan 4.1, 4.2).

        The journal gets lengths, a sha8 and the source of every part, never a text; the ring keeps the texts, in memory."""
        import prompt_policy
        plan, (head, parts, warnings) = parsed.prompt_plan, composed
        changes = list(getattr(parsed, "prompt_changes", None) or ())
        history, tools = split_changes(changes)
        notes = list(warnings or ()) + list(getattr(parsed, "prompt_warnings", ()))
        if not head.strip() and client_tools(parsed) and EMPTY_HEAD_WARNING not in notes:
            # No text of the profile or the client is quoted: the warning is as good for the journal as for the ring.
            notes.append(EMPTY_HEAD_WARNING)
        inp = getattr(parsed, "prompt_input", None)
        try:
            summary = journal_changes(changes)
            summary_history, summary_tools = split_changes(summary)
            log("prompt.plan", "Промпт: профиль «%s», правило «%s», ревизия %s" % (plan.profile, plan.rule, plan.rev),
                "debug", rev=plan.rev, profile=plan.profile, rule=plan.rule, pinned=plan.pinned,
                protected=plan.protected, head_chars=len(head), head_sha8=prompt_policy.sha8(head),
                parts=[[part.source, len(part.text)] for part in parts],
                history_changes=summary_history, tools_changes=summary_tools,
                generation=dict(plan.generation or {}), warnings=notes)
            if self.prompts is None or holder.get("_prompt_recorded"):
                return
            holder["_prompt_recorded"] = True
            entry = {"ts": time.time(), "request_id": getattr(parsed, "request_id", None),
                     "client": getattr(inp, "client", None) or prompt_policy.prompt_client(client_context.current(), {}),
                     "agent": getattr(inp, "agent", None) or client_context.current().get("agent") or "",
                     "model": parsed.model, "dialect": getattr(parsed, "dialect", None),
                     "purpose": getattr(inp, "purpose", None) or _purpose(parsed),
                     "rule": plan.rule, "profile": plan.profile, "rev": plan.rev, "pinned": plan.pinned,
                     "protected": plan.protected, "head": head, "parts": prompt_policy.part_spans(parts),
                     "history_changes": history[:RING_CHANGES], "tools_changes": tools[:RING_CHANGES],
                     "changes_summary": summary, "warnings": notes}
            if len(history) > RING_CHANGES or len(tools) > RING_CHANGES:
                entry["changes_total"] = {"history": len(history), "tools": len(tools)}
            if getattr(parsed, "prompt_request", None):
                entry["request"] = parsed.prompt_request
            self.prompts.record(entry)
        except Exception:  # recording is best effort: the turn goes on
            log("prompt.runtime_error", "Не удалось записать план промпта", "error", stage="record")

    @staticmethod
    def _usage_update(holder, event):
        """Keep the attempt snapshot; journal additive deltas exactly once."""
        snapshot = holder.setdefault('_attempt_usage', {})
        delta = {}
        for key in ('input_tokens', 'output_tokens', 'cache_read', 'cache_write'):
            value = credits.token_counter((event.get('usage') or {}).get(key))
            if value is not None:
                delta[key] = value - snapshot.get(key, 0)
                snapshot[key] = value
        return snapshot, delta

    @staticmethod
    def _tool_prompt(parsed):
        tools = getattr(parsed, "tools", None) or []
        if not tools or getattr(parsed, "tool_choice", "auto") == "none":
            return ""
        choice = getattr(parsed, "tool_choice", "auto")
        return (
            "\n\n[NATIVE CLIENT TOOLS]\n"
            "The client supplied these tools. Do not execute them locally. "
            "These are real tools on the CLIENT machine, invoked by the relay returning your envelope. "
            "Retool's built-in sandbox tools and its filesystem are a different environment: "
            "do not use them to inspect or edit the client's project. "
            "The absence of the client's files inside Retool is expected and does not mean the tools are unavailable. "
            "When a tool is needed, emit one JSON envelope per call and no prose: "
            "<tool_call>{\"id\":\"call_id\",\"name\":\"tool_name\","
            "\"arguments\":{...}}</tool_call>. The name must be one of the supplied tools. "
            "After the client returns a tool result, continue the original task.\n"
            "For a custom tool, place the exact freeform input inside arguments.input as a JSON string. "
            "For file or test work, first call the appropriate supplied client tool; never invent its result.\n"
            "tool_choice=" + json.dumps(choice, ensure_ascii=False) + "\n"
            "tools=" + json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        )

    async def _explain_empty(self, holder):
        """Silent turns are usually a persisted upstream error (e.g. out of credits)."""
        session = holder.get("session")
        if session is None:
            return None
        try:
            backend = getattr(session, "backend", None)
            if not hasattr(backend, "get_messages"):
                backend = self.pool.backend
            data = await backend.get_messages(session.thread_id)
        except Exception as exc:
            print("[explain] fetch failed:", exc, flush=True)
            return None
        # Retool returns messages newest-first.
        for message in data.get("messages", []):
            try:
                created = datetime.fromisoformat(str(message.get("createdAt", "")).replace("Z", "+00:00")).timestamp()
                if created < holder.get("upstream_started_at", 0):
                    continue
            except (ValueError, TypeError):
                if holder.get("upstream_started_at"):
                    continue
            content = message.get("content") or {}
            if message.get("senderRole") == "sandpack" and content.get("type") == "error":
                text = str(content.get("error") or "").strip()
                if "credit" in text.lower():
                    return apierr.ApiError(402, "insufficient_credits", text or "Retool credits exhausted")
                return apierr.upstream_error(text or "upstream returned an error message")
            if message.get("senderRole") == "sandpack" and content.get("type") in ("text", "tool_call"):
                return None
        return None

    async def run(self, parsed, out: queue.Queue, holder: dict):
        """Run a turn; retry on another account after a confirmed low balance or an hourly limit.

        The retry happens only while nothing was sent to the client. A low balance must be
        confirmed by the dispatcher's fresh balance fetch (the serving account dropped below
        min_account_balance); Retool's hourly managed-key limit parks the serving account at
        once, even when the turn is already streaming and cannot be retried. Other failures
        (empty turns, 5xx) are returned as before.
        """
        exclude = []
        rechecked = False
        cancelled = holder.setdefault("cancel_event", threading.Event())
        max_steps = int(self.cfg.get("max_exec_turns", 0) or 0)
        # Exec steps are bounded only by max_exec_turns; each step (plus the summary
        # step) may spend up to MAX_ATTEMPTS POSTs, which is the hard cap per request.
        holder["_attempt_budget"] = AttemptBudget(
            cancelled, total_cap=MAX_ATTEMPTS * (max_steps + 2) if max_steps > 0 else None,
            clock=getattr(getattr(self, "accounts", None), "clock", None))
        while True:
            relay = _FailoverOut(out)
            holder.pop("session", None)
            try:
                await self._run_once(parsed, relay, holder, exclude=tuple(exclude))
            except BaseException:
                if relay.error is not None:
                    out.put({"type": "error", "error": relay.error})
                out.put(None)
                raise
            error = relay.error
            lease = holder.get("session")
            backend = getattr(getattr(lease, "session", lease), "backend", None)
            accounts = getattr(self, "accounts", None)
            retryable = (error is not None and not relay.started and not cancelled.is_set()
                         and accounts is not None and holder["_attempt_budget"].remaining > 0)
            exhausted = False
            rate_limited = False
            try:
                if error is not None and backend is not None and apierr.is_account_quota(error):
                    # Hourly managed-key limit: park the account so later requests skip it, then
                    # retry here while the client has seen nothing.
                    if hasattr(accounts, "mark_rate_limited"):
                        # A limit that a request sent before the account's last return reports is old news.
                        accounts.mark_rate_limited(backend, retry_after=getattr(error, "retry_after", None),
                                                   request_id=getattr(parsed, "request_id", None),
                                                   sent_at=holder["_attempt_budget"].sent_at)
                    rate_limited = retryable and backend not in exclude
                if (retryable and backend is None and error.code == "insufficient_credits"
                        and not rechecked and hasattr(accounts, "recheck_exhausted")):
                    # The pool refused before any upstream call: confirm the excluded
                    # balances once, so a top-up is used without waiting for the timer.
                    rechecked = True
                    if await asyncio.to_thread(accounts.recheck_exhausted):
                        continue
                if (retryable and backend is not None and backend not in exclude
                        and _may_be_balance_related(error)):
                    # With no account left, the next acquire raises a clear 402.
                    exhausted = await asyncio.to_thread(accounts.check_balance_now, backend)
            except BaseException:
                out.put({"type": "error", "error": error})
                out.put(None)
                raise
            if rate_limited:
                exclude.append(backend)
                journal.emit("account.failover", "Часовой лимит Retool — запрос повторён на другом аккаунте",
                             "warn", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                             error=error.code, reason="rate_limited")
                console_log.write("[%s] [account] %s: часовой лимит Retool, повтор на другом аккаунте" % (
                    getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED)))
                holder["reason"] = "stop"
                continue
            if exhausted:
                exclude.append(backend)
                journal.emit("account.failover", "Баланс аккаунта закончился — запрос повторён на другом",
                             "warn", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                             error=error.code)
                console_log.write("[%s] [account] %s: баланс закончился, повтор на другом аккаунте" % (
                    getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED)))
                holder["reason"] = "stop"
                continue
            if error is not None:
                out.put({"type": "error", "error": error})
            out.put(None)
            return

    async def _run_once(self, parsed, out, holder, exclude=()):
        turn_id = str(uuid.uuid4())
        holder["turn_id"] = turn_id
        # Loop detection is per client turn, never shared by concurrent chats.
        recent = []
        cancelled = holder.setdefault("cancel_event", threading.Event())
        exec_steps = 0
        empty_retries = 0
        final_summary = False
        tool_retries = 0
        turn_started = time.time()
        outcome = {"reason": None, "error": None, "first": False}
        trace = {"turn_id": turn_id, "request_id": getattr(parsed, "request_id", None),
                 "conversation": journal.tag(getattr(parsed, "conversation_key", None))}

        def log(kind, message, level="info", **fields):
            journal.emit(kind, message, level, **trace, **fields)

        try:
            native_tools = getattr(parsed, "tools", None) or []
            tool_definitions = {item["function"]["name"]: item["function"] for item in native_tools}
            choice = getattr(parsed, "tool_choice", "auto")
            tool_names = list(tool_definitions) if choice != "none" else []
            if isinstance(choice, dict):
                tool_names = [choice["function"]["name"]]
            direct_mode = self.cfg.get("transport") == "direct"
            # native_tools is what the model is offered; what the client sent decides who owns the job, what the legacy
            # head says and whether anything runs on this machine (plan I9): a profile can hide every tool.
            client_has_tools = client_tools(parsed)
            is_native = client_has_tools
            purpose = _purpose(parsed)
            client_text = purpose in client_context.CLIENT_TEXT_KINDS
            output_spec = getattr(parsed, "structured_output", None)
            client_system = _client_system(parsed)
            client_owns_instructions = _client_owns_instructions(parsed, purpose, native_tools)
            composed = await self._compose_head(parsed, client_system, client_owns_instructions, native_tools,
                                                output_spec, choice, log)
            if composed is not None:
                # A prompt profile composed the whole head, the structured contract and billing header included;
                # nothing is appended to it below (plan 4.3 item 12).
                instructions = composed[0]
                self._note_plan(log, holder, parsed, composed)
            elif client_owns_instructions:
                # The client owns this job's system prompt and execution policy.
                # A title/probe/permission check must not acquire relay project or shell instructions.
                instructions = client_system
                if not client_context.keeps_client_prompt(purpose):
                    instructions = self._client_instructions(instructions)
            else:
                instructions = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._instructions(native_tools=is_native))
            if cancelled.is_set():
                raise asyncio.CancelledError()
            if output_spec:
                # The route lacks constrained decoding. Supply the contract as
                # instructions; the HTTP adapter validates the complete reply.
                if composed is None:
                    instructions = "\n\n".join(part for part in (
                        instructions, structured.instructions(output_spec, tools=bool(native_tools),
                                                              tool_choice=choice)) if part)
                log("turn.structured_output", "Структурированный вывод эмулируется проверкой ответа: %s" % output_spec["kind"],
                    "debug", output_format=output_spec["kind"], source=output_spec["source"])
            reminder = ""
            if native_tools and not direct_mode and getattr(parsed, "tool_choice", "auto") != "none":
                reminder = self._tool_prompt(parsed)
            elif (composed is None and not client_has_tools and not client_owns_instructions
                  and self.cfg["exec_policy"] != "off"):
                reminder = EXEC_REMINDER
            if direct_mode:
                # Direct path keeps the stable instructions (plus tool block) as the
                # cached system head and sends the raw client tail as the user turn.
                if reminder:
                    instructions += reminder
                reminder = ""
                prompt = parsed.prompt
            elif native_tools:
                prompt = (instructions + reminder + _CONV_HEADER + parsed.prompt + _NATIVE_CONTINUE)
            else:
                prompt = (instructions + "\n\n" + parsed.prompt) if instructions else parsed.prompt
                prompt += reminder
            task_block = ""
            previous_steps = []
            last_followup = None
            if not model_catalog.PROVIDER_RESOURCE_KEY.get(parsed.entry["provider"]):
                raise apierr.service_unavailable(f"provider '{parsed.entry['provider']}' is not routed")
            # The resource is per-account (org-scoped UUID or __managed__): it is
            # resolved from the acquired session's backend cfg below. Resolving it
            # from the global cfg here would 503 account-only configurations.
            model_provider = {"model": parsed.model}
            effort = model_catalog.effective_effort(parsed.entry, parsed.effort, self.cfg.get("reasoning_effort"))
            # The effort of the request is the client's, then the profile's when the profile sets one (proxy applied it
            # to parsed.effort); requested_effort is always what the client sent, effort_source says who decided.
            profile_effort = getattr(parsed, "prompt_effort", None)
            asked = getattr(parsed, "requested_effort", None)
            requested_effort = asked if profile_effort else (asked or parsed.effort)
            effort_source = "client"
            if requested_effort is None:
                requested_effort = self.cfg.get("reasoning_effort") if parsed.entry.get("reasoning") else None
                effort_source = "model_default" if requested_effort in (None, "auto") else "configured_default"
            if profile_effort:
                effort_source = "profile"
            task = getattr(parsed, "client_task", "")
            # A client's compaction runs on the effort of its dialog when that is what makes the dialog's signed
            # history resume (README, "Сжатие истории"); the journal still says what the client asked for.
            # That comes last: the profile sets the effort of the request, the compaction may keep the dialog's over it.
            kept, why = None, None
            holder.pop("kept_effort", None)  # a retry on another account decides again
            if (task == client_context.COMPACTION and native_tools and direct_mode
                    and self.cfg.get("compaction_keep_effort", True)):
                try:
                    if hasattr(self.pool, "load_stored"):
                        # A dialog stored before a restart is only on disk: read it for the question below.
                        await self.pool.load_stored(getattr(parsed, "conversation_key", None), model_entry=parsed.entry,
                                                    exclude=exclude, **acquire_plan(self.cfg, parsed).kwargs)
                    kept, why = self._dialog_effort(parsed, instructions, choice, exclude)
                except Exception as exc:  # a lookup that only helps: without it the request runs on the client's effort
                    log("cache.effort_check_failed", "Не удалось сверить effort сжатия с диалогом, запрос идёт как есть: "
                        "%s: %s" % (type(exc).__name__, redact(str(exc)).replace("\n", " ")[:160]), "warn")
            if kept:
                effort, effort_source = kept, "dialog"
            if effort is not None:
                model_provider["reasoningEffort"] = effort
            if getattr(parsed, 'verbosity', None) is not None:
                model_provider['verbosity'] = parsed.verbosity
            effort_note = ""
            if kept:
                effort_note = " (effort диалога, клиент просил %s)" % (requested_effort or "по умолчанию",)
            elif effort_source == "profile":
                effort_note = " (effort профиля «%s», клиент просил %s)" % (
                    getattr(getattr(parsed, "prompt_plan", None), "profile", None), requested_effort or "по умолчанию")
            log("turn.begin", "Ход %s · effort %s · %s%s%s" % (
                    parsed.model, effort or "—", "direct" if direct_mode else "agent",
                    " · сжатие истории" if task == client_context.COMPACTION else "", effort_note),
                model=parsed.model, effort=effort, transport="direct" if direct_mode else "agent",
                requested_effort=requested_effort, effective_effort=effort,
                effort_source=effort_source,
                effort_resolution=("cache_continuity" if kept else
                                   "provider_managed" if effort_source == "profile" and effort is None else
                                   "profile" if effort_source == "profile" else
                                   "model_default" if requested_effort in (None, "auto") else
                                   "provider_managed" if effort is None else
                                   "exact" if requested_effort == effort else "compatibility"),
                dialect=getattr(parsed, "dialect", None), stream=bool(getattr(parsed, "stream", False)),
                tools=len(native_tools), tool_choice=choice if isinstance(choice, str) else "function",
                prompt_chars=len(parsed.prompt or ""), system_chars=len(parsed.system or ""),
                messages=len(getattr(parsed, "message_chunks", None) or []), request_purpose=purpose,
                **({"client_task": task} if task else {}))
            if kept:
                holder["kept_effort"] = kept
                log("cache.effort_kept", "Сжатие истории: effort диалога %s вместо запрошенного %s, подписанная история "
                    "продолжится" % (kept, requested_effort or "по умолчанию"), "info",
                    requested_effort=requested_effort, dialog_effort=kept)
                console_log.write("[%s] [cache] compaction keeps the dialog's effort %s (the client asked for %s): "
                                  "the signed history continues" % (getattr(parsed, "request_id", None) or "req", kept,
                                                                    requested_effort or "the default"))
            elif why:
                # What the compaction cannot continue is what it costs: say which part of the dialog it does not match.
                reason = _RESTART_REASONS[why["reason"]]
                if why.get("differs"):
                    reason = reason % ", ".join(_SCOPE_NAMES.get(name, name) for name in why["differs"])
                log("cache.compaction_restart", "Сжатие не продолжает подписанную историю диалога: %s; история будет "
                    "записана заново" % reason, "info", requested_effort=requested_effort, **why)
                console_log.write("[%s] [cache] compaction does not continue the signed history (%s)" % (
                    getattr(parsed, "request_id", None) or "req", why["reason"] + (
                        ": " + ", ".join(why["differs"]) if why.get("differs") else "")))

            client_chunks = list(getattr(parsed, 'message_chunks', None) or ([parsed.prompt] if parsed.prompt else []))
            if direct_mode:
                self._check_prompt(self._direct_text(instructions, client_chunks))
            base_prompt = self._check_prompt(prompt)
            session = None
            account_context = None
            try:
                key = getattr(parsed, "conversation_key", None)
                parent_key = getattr(parsed, "parent_conversation_key", None)
                plan = acquire_plan(self.cfg, parsed)
                persistent_text, isolated = plan.kind == "text", plan.kind == "isolated"
                failover = {"exclude": exclude} if exclude else {}
                if parent_key and parent_key != key:
                    failover["parent_key"] = parent_key
                session = await self.pool.acquire(key, model_entry=parsed.entry, **plan.kwargs, **failover)
                holder["session"] = session
                acquired = session.session if isinstance(session, SessionLease) else session
                # Every later event of this turn, errors included, names the account serving it.
                trace["account"] = account_tag(getattr(acquired, "backend", None))
                if trace["account"]:
                    # Events that other modules emit for this turn (the transport, native history) get the
                    # tag through the context: they know no more than the session they run on.
                    account_context = client_context.bind({**client_context.current(), "account": trace["account"]})
                # Debit the account that actually served the turn.
                account_credits = getattr(getattr(acquired, "backend", None), "credits", None) or self.credits
                acquired._credits_manager = account_credits
                acquired._attempt_budget = holder["_attempt_budget"]
                # Console lines (heartbeat, [cache]) carry the relay request id so
                # concurrent requests of one conversation can be told apart.
                acquired._request_tag = getattr(parsed, "request_id", None)
                # Per-account AI resources: the resource UUID is org-scoped, so it
                # must come from the backend cfg the turn actually runs on, not the
                # global config (accounts[] can span tenants).
                acc_cfg = getattr(getattr(acquired, "backend", None), "cfg", None) or self.cfg
                resource = model_catalog.provider_resource(acc_cfg, parsed.entry)
                if resource:
                    model_provider["providerResourceName"] = resource
                else:
                    # __managed__: omit the field entirely (never send null).
                    model_provider.pop("providerResourceName", None)
                log("session.acquire", "Сессия получена за %d мс" % round((time.time() - turn_started) * 1000),
                    "debug", thread=journal.tag(getattr(acquired, "thread_id", None)),
                    isolated=bool(isolated), mode=self.cfg.get("session_mode"),
                    context_scope="text" if persistent_text else None,
                    wait_ms=round((time.time() - turn_started) * 1000))
                native_target, native_context = None, None
                if native_tools:
                    prompt, native_target, native_context = await self._native_prompt(
                        session, parsed, instructions, reminder, prompt, cancelled)
                    if direct_mode:
                        # The direct head is cached, so always send the full client history
                        # instead of the agent's incremental suffix.
                        prompt = parsed.prompt
                elif not key:
                    # Preserve legacy anonymous shared-mode invalidation. Explicit
                    # tool-less requests never acquire or mutate the native session.
                    target = session.session if isinstance(session, SessionLease) else session
                    if not direct_mode and getattr(target, "_client_context", None):
                        target._client_context_pending = True
                exec_chunks = []
                while True:
                    self._check_prompt(prompt)
                    if cancelled.is_set():
                        session.cancel()
                        raise asyncio.CancelledError()
                    final_text = ""
                    native_text = _ClientText() if native_tools else None
                    upstream_text = ""
                    visible_text = ""
                    holder["upstream_started_at"] = time.time()
                    streamed_tool_call = False
                    upstream_completed = False
                    stream_prompt = prompt
                    # Token estimates must describe what the model actually received.
                    parsed._full_prompt_text = (instructions + "\n" + prompt) if instructions else prompt
                    if direct_mode:
                        direct_target = session.session if isinstance(session, SessionLease) else session
                        if not hasattr(direct_target, "set_direct"):
                            # Injected/fake session: fall back to the agent prompt shape.
                            direct_mode = False
                            stream_prompt = (instructions + self._tool_prompt(parsed) + _CONV_HEADER + stream_prompt + _NATIVE_CONTINUE
                                             if native_tools else instructions + "\n\n" + stream_prompt)
                    if direct_mode:
                        if last_followup and instructions and stream_prompt.startswith(instructions + '\n\n'):
                            stream_prompt = stream_prompt[len(instructions) + 2:]
                        chunks = list(client_chunks)
                        if exec_chunks:
                            exec_chunks[-1] = stream_prompt
                            chunks.extend(exec_chunks)
                        elif last_followup:
                            chunks.append(stream_prompt.strip())
                        elif chunks and prompt.startswith(parsed.prompt) and prompt != parsed.prompt:
                            chunks.append(prompt[len(parsed.prompt):])
                        self._check_prompt(self._direct_text(instructions, chunks or [stream_prompt]))

                        if native_tools:
                            tools_converted = direct.tools_to_retool(parsed.tools)
                            if hasattr(direct_target, "register_tools"):
                                direct_target.register_tools(tools_converted)
                            final_tools = tools_converted
                            final_choice = getattr(parsed, "tool_choice", "auto")
                            final_head = instructions
                        else:
                            # Keep the stable job instructions BEFORE the changing input,
                            # in system role. The client supplied no tools for this job.
                            final_head = instructions
                            final_tools = None
                            final_choice = "none"

                        direct_target.set_direct(
                            final_head,
                            final_tools,
                            final_choice,
                            getattr(parsed, "max_tokens", None),
                            chunks)
                        direct_target._native_client_messages = (getattr(parsed, "native_messages", None)
                                                                 if native_tools else None)
                        direct_target._native_tool_defaults = (native_history.tool_defaults(parsed.tools)
                                                               if native_tools else None)
                        # Relay-only retry instructions live in chunks; a cold
                        # canonical replay must not discard those later attempts.
                        direct_target._native_allow_cold_replay = not (empty_retries or tool_retries or exec_steps)
                        direct_target._native_conversation_key = key
                    stream = (session.stream(stream_prompt, model_provider, tool_names=tool_names)
                              if native_tools else session.stream(stream_prompt, model_provider))
                    log("upstream.request", "Запрос к upstream (%s)" % ("direct" if direct_mode else "agent"),
                        "debug", transport="direct" if direct_mode else "agent",
                        prompt_chars=len(stream_prompt or ""), attempt=empty_retries + tool_retries + exec_steps + 1)
                    first_token = False
                    text_open = False
                    think_started = None
                    tool_started = {}
                    out.put({"type": "usage_start"})
                    journal_attempt = holder.get('_journal_attempt', 0) + 1
                    holder['_journal_attempt'] = journal_attempt
                    log('upstream.usage_start', 'Начат учёт попытки upstream', 'debug', attempt=journal_attempt)
                    usage_started = False
                    try:
                        # Leaving the loop (an error event raises below) closes the generators of the stream at once:
                        # left to the garbage collector, a cut turn keeps its session "busy" until some later collection.
                        async with contextlib.aclosing(stream):
                            async for event in stream:
                                if not first_token and event["type"] in ("text", "thinking", "tool_call"):
                                    first_token = True
                                    ttft = round((time.time() - holder["upstream_started_at"]) * 1000)
                                    log("upstream.first_token", "Первый токен через %d мс" % ttft,
                                        "success" if ttft < 8000 else "warn", ttft_ms=ttft, first=event["type"])
                                if event["type"] in ("thinking_status", "thinking_start", "thinking", "thinking_signature",
                                                     "tool_call"):
                                    text_open = False
                                if event["type"] == "text":
                                    if not text_open and event["delta"]:
                                        # Every text segment (text, thinking, text) opens its own answer span.
                                        text_open = True
                                        ms = round((time.time() - holder["upstream_started_at"]) * 1000)
                                        log("upstream.text_start", "Модель начала писать текст через %d мс" % ms,
                                            "debug", ms=ms)
                                    final_text += event["delta"]
                                    upstream_text += event["delta"]
                                    # Tool envelopes are parsed only after the complete
                                    # upstream turn, so do not leak their markup to a
                                    # native client. Ordinary text remains streamed.
                                    if direct_mode or not native_tools:
                                        out.put(event)
                                        visible_text += event["delta"]
                                    else:
                                        visible = native_text.feed(event["delta"])
                                        if visible:
                                            visible_text += visible
                                            out.put({"type": "text", "delta": visible})
                                elif event["type"] == "final_text":
                                    if event.get("text"):
                                        final_text = event["text"]
                                elif event["type"] in ("thinking_status", "thinking_start"):
                                    if think_started is None:
                                        think_started = time.time()
                                        ms = round((think_started - holder["upstream_started_at"]) * 1000)
                                        log("upstream.thinking_start", "Upstream сообщил начало thinking через %d мс" % ms,
                                            "info", ms=ms)
                                    out.put(event)
                                elif event["type"] in ("thinking", "thinking_signature"):
                                    if event["type"] == "thinking_signature" and think_started is not None:
                                        span = round((time.time() - think_started) * 1000)
                                        log("upstream.thinking_end", "Мышление заняло %d мс" % span, "info",
                                            thinking_ms=span)
                                        think_started = None
                                    out.put(event)
                                elif event["type"] in ("thinking_heartbeat", "usage_start"):
                                    if event["type"] == "usage_start":
                                        if usage_started:
                                            journal_attempt += 1
                                            holder['_journal_attempt'] = journal_attempt
                                            log('upstream.usage_start', 'Начат учёт попытки upstream', 'debug', attempt=journal_attempt)
                                        usage_started = True
                                        think_started = None
                                        holder['_attempt_usage'] = {}
                                        holder['_attempt_credited'] = False
                                    out.put(event)
                                elif event["type"] == "tool_call" and event.get("name") in tool_names:
                                    # A call whose arguments stop short of valid JSON (the cap, a cut) is not a whole
                                    # call: it is not reported as one (C5), and the turn ends as cut, not as a tool call.
                                    complete = not event.get("partial") and not event.get("invalid")
                                    if complete:
                                        streamed_tool_call = True
                                    definition = tool_definitions[event["name"]]
                                    call_key = event.get("id") or event["name"]
                                    if call_key not in tool_started:
                                        tool_started[call_key] = time.time()
                                        ms = round((tool_started[call_key] - holder["upstream_started_at"]) * 1000)
                                        log("upstream.tool_start", "Модель начала вызов %s через %d мс" % (event["name"], ms),
                                            "debug", name=event["name"], call_id=event.get("id"), ms=ms)
                                    if complete:
                                        args_ms = round((time.time() - tool_started[call_key]) * 1000)
                                        log("tool.call", "Инструмент клиента: %s (аргументы %d мс)" % (event["name"], args_ms),
                                            "info", name=event["name"], call_id=event.get("id"),
                                            arguments=_preview(event.get("arguments")), streamed=True,
                                            args_ms=args_ms, args_chars=len(event.get("arguments") or ""))
                                    elif event.get("invalid"):
                                        log("tool.call_incomplete",
                                            "Вызов %s оборван: аргументы не дописаны до конца JSON" % event["name"], "warn",
                                            name=event["name"], call_id=event.get("id"),
                                            arguments=_preview(event.get("arguments")),
                                            args_chars=len(event.get("arguments") or ""))
                                    out.put({key: value for key, value in event.items() if key not in ("partial", "invalid")}
                                            | {"tool_type": definition.get("x-client-type", "function"),
                                               "done": complete})
                                elif event["type"] == "tool_call":
                                    # A call of a tool the client did not declare (under a forced choice, of another
                                    # tool) has nothing to run it on the client and is not passed on; the journal names
                                    # it instead of losing it without a trace (ultracode audit of the relay, C4).
                                    if not event.get("partial"):
                                        log("tool.call_dropped",
                                            "Вызов инструмента %s не передан клиенту: клиент его не объявлял" % (
                                                event.get("name"),),
                                            "warn", name=event.get("name"), call_id=event.get("id"),
                                            declared=len(tool_names))
                                elif event['type'] == 'usage':
                                    usage, delta = self._usage_update(holder, event)
                                    breakdown = credits.cost_breakdown(usage, parsed.model)
                                    known = holder.get('_attempt_credited') or event.get('credits_reported')
                                    log('upstream.usage', ('Токены обновлены; стоимость уже сообщена' if known else
                                                          'Токены получены; стоимость upstream не сообщена'),
                                        'info' if known else 'warn', credits=None, attempt=journal_attempt,
                                        credits_reported=bool(known), **delta,
                                        **credits.cost_breakdown(delta, parsed.model), usage_snapshot=dict(usage))
                                    if session is not None:
                                        acquired._last_prefix_tokens = breakdown['prompt_tokens']
                                        _remember_prompt_size(acquired, parsed, breakdown['prompt_tokens'])
                                    out.put(event)
                                elif event["type"] == "credits":
                                    # Each upstream attempt (retry, exec follow-up) is billed
                                    # separately: a per-turn dedup key dropped all but the first.
                                    credit_events = holder.get("_credit_events", 0) + 1
                                    holder["_credit_events"] = credit_events
                                    account_credits.record_turn(
                                        turn_id if credit_events == 1 else "%s#%d" % (turn_id, credit_events),
                                        event["value"])
                                    holder['_attempt_credited'] = True
                                    usage, delta = self._usage_update(holder, event)
                                    breakdown = credits.cost_breakdown(usage, parsed.model)
                                    hit = breakdown["cache_hit_ratio"]
                                    log("upstream.usage", "Кредиты %.4f · кэш read %s / write %s · hit %s" % (
                                        event["value"], usage.get("cache_read", "—"), usage.get("cache_write", "—"),
                                        "%d%%" % round(hit * 100) if hit is not None else "—"),
                                        "info", credits=event["value"], attempt=journal_attempt, credits_reported=True, **delta,
                                        **credits.cost_breakdown(delta, parsed.model), usage_snapshot=dict(usage))
                                    if session is not None:
                                        # Write through the lease: SessionLease only proxies
                                        # reads, so keepwarm (which holds the real session)
                                        # saw prefix_tokens=0 forever.
                                        acquired._last_prefix_tokens = breakdown["prompt_tokens"]
                                        _remember_prompt_size(acquired, parsed, breakdown["prompt_tokens"])
                                    out.put(event)
                                elif event["type"] == "hitl":
                                    out.put(event)
                                    final_text += "\n" + event["question"]
                                elif event["type"] == "end":
                                    holder["reason"] = event["reason"]
                                    upstream_completed = event["reason"] in ("stop", "tool_calls")
                                elif event["type"] == "error":
                                    # A truncated upstream stream must reach the client as
                                    # an error, not fall through to a clean "stop".
                                    raise event["error"]
                    except apierr.ApiError:
                        raise
                    holder.setdefault("reason", "stop")
                    if holder.get("reason") == "cancelled" or cancelled.is_set():
                        holder["reason"] = "cancelled"
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "cancelled"})
                        return
                    if holder.get("reason") in ("timeout", "length"):
                        out.put({"type": "end", "reason": holder["reason"]})
                        return

                    native_calls = (executor.extract_tool_calls(final_text, tool_names) if tool_names else []) if not direct_mode else []
                    if not direct_mode and not native_calls and tool_names:
                        command_call = executor.command_tool_call(final_text, tool_names)
                        if command_call is not None:
                            native_calls = [command_call]
                    if native_calls:
                        if not streamed_tool_call:
                            for native_call in native_calls:
                                definition = tool_definitions[native_call["name"]]
                                log("tool.call", "Инструмент клиента: %s" % native_call["name"], "info",
                                    name=native_call["name"], call_id=native_call.get("id"),
                                    arguments=_preview(native_call.get("arguments")), streamed=False)
                                out.put({"type": "tool_call", **native_call,
                                         "tool_type": definition.get("x-client-type", "function")})
                        holder["reason"] = "tool_calls"
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "tool_calls"})
                        return
                    if streamed_tool_call:
                        holder["reason"] = "tool_calls"
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "tool_calls"})
                        return
                    if not direct_mode and native_tools:
                        has_envelope = bool(re.search(r"<tool_call\b[^>]*>.*?</tool_call>", final_text, re.DOTALL | re.IGNORECASE))
                        if not has_envelope and re.search(r"<tool_call\b", final_text, re.IGNORECASE):
                            match = re.search(r"<tool_call\b([^>]*)>(.*)$", final_text, re.DOTALL | re.IGNORECASE)
                            if match:
                                after = match.group(2).strip()
                                if any(tok in after for tok in ("{", "```", '"name"', '"arguments"')):
                                    has_envelope = True
                                elif not match.group(2).startswith("`"):
                                    has_envelope = True
                        if has_envelope:
                            raise apierr.upstream_error("upstream returned a malformed or disallowed client tool call")
                    if native_tools and not direct_mode:
                        # Some upstream transports only provide a final snapshot.
                        # Feed its unstreamed suffix once instead of duplicating it.
                        suffix = (final_text[len(upstream_text):]
                                  if final_text.startswith(upstream_text) else "")
                        visible = native_text.feed(suffix, final=True)
                        if visible:
                            visible_text += visible
                            out.put({"type": "text", "delta": visible})
                    refuses_tools = re.search(
                        r"(?:can(?:not|'t)|could(?:\s+not|n't)|unable|not (?:available|accessible)|"
                        r"no (?:access|tools)|не (?:могу|доступ|имею)|нет доступа)", final_text, re.IGNORECASE)
                    if (not direct_mode and tool_names and refuses_tools and tool_retries < 1
                            and not visible_text.strip()):
                        tool_retries += 1
                        log("turn.retry", "Модель отказалась от инструментов — повтор с коррекцией", "warn",
                            reason="tool_refusal")
                        if not direct_mode:
                            native_context = None  # Retry prompts are not part of the client history in agent mode.
                        prompt = (base_prompt + "\n\n[CLIENT TOOL TRANSPORT CORRECTION]\n"
                                  "Your previous reply treated the client's tools as unavailable. "
                                  "Invoke a supplied tool using <tool_call> JSON instead of Retool sandbox tools. "
                                  "The relay passes it to the real client and returns its result next turn. "
                                  "For this requested task, issue the appropriate client tool call now.")
                        continue
                    if tool_names and (choice == "required" or isinstance(choice, dict)):
                        # Same error shape as an empty turn: diagnostic_id and attempt count.
                        raise holder["_attempt_budget"].error(
                            "upstream_tool_choice_ignored" if final_text.strip() else "upstream_empty_turn",
                            "upstream did not honor the required client tool choice")

                    # Once the summary turn is requested, nothing is executed any more.
                    can_exec = self._can_exec(exec_steps) and not final_summary and not client_owns_instructions
                    command = executor.extract_command(final_text) if can_exec and not client_has_tools else None

                    if not final_summary and exec_steps and not self._can_exec(exec_steps):
                        # Explicit step cap reached (max_exec_turns > 0 only).
                        final_summary = True
                        summary = ("\n\n[ИТОГ] Команды больше не выполняй. "
                                   "Дай итоговый ответ по уже собранной информации, на русском.")
                        prompt = (self._followup(instructions, reminder + summary, *last_followup,
                                                direct_chunks=client_chunks + exec_chunks[:-1] if direct_mode else None)
                                  if last_followup else self._check_prompt(prompt + summary))
                        continue

                    # Loop guard: the same command keeps coming back (any period).
                    if command and not final_summary and self._looks_looping(command, recent):
                        final_summary = True
                        log("exec.loop_guard", "Команда повторяется — исполнение остановлено", "warn",
                            command=command[:300])
                        summary = ("\n\n[ИТОГ] Команда '" + command + "' повторяется. "
                                   "Команды больше не выполняй, дай итоговый ответ.")
                        prompt = (self._followup(instructions, reminder + summary, *last_followup,
                                                direct_chunks=client_chunks + exec_chunks[:-1] if direct_mode else None)
                                  if last_followup else self._check_prompt(prompt + summary))
                        continue

                    if cancelled.is_set() or holder.get("reason") == "cancelled":
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "cancelled"})
                        return

                    if not direct_mode and not command and not final_text.strip():
                        # Silent turns are explained via persisted sandpack messages,
                        # which the direct transport never writes.
                        explained = await self._explain_empty(holder)
                        if explained is not None:
                            raise explained

                    if (not direct_mode and not command and not final_text.strip() and empty_retries < 1 and not client_text
                            and holder["_attempt_budget"].remaining):
                        # The agent transport needs a textual continuation. Direct
                        # retries are handled in the transport with the same body.
                        empty_retries += 1
                        log("turn.retry", "Пустой ход — повтор", "warn", reason="empty_turn")
                        if not direct_mode:
                            native_context = None  # Retry prompts are not part of the client history in agent mode.
                        retry_instruction = ("Use the supplied native client tools for tool calls; "
                                             "otherwise return a short text answer." if native_tools else
                                             "If a local command is needed, output <exec_command>...</exec_command>; "
                                             "otherwise return a short text answer.")
                        prompt = (base_prompt + "\n\n[RETRY] The previous attempt returned an empty answer. "
                                  + retry_instruction)
                        continue

                    if not command and not final_text.strip():
                        raise holder["_attempt_budget"].error()

                    if not can_exec or not command:
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        if direct_mode and native_target is not None and upstream_completed and not cancelled.is_set():
                            native_target._client_context_pending = False
                        out.put({"type": "end", "reason": holder.get("reason", "stop")})
                        return

                    if self.cfg["show_exec_output"]:
                        out.put({"type": "exec_start", "cmd": command})
                    log("exec.start", "Локальная команда: %s" % redact(command)[:160], "info",
                        command=redact(command)[:2000], policy=self.cfg["exec_policy"], step=exec_steps + 1)
                    exec_started = time.time()

                    try:
                        code, output = await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: executor.run_command(
                                command, self.cfg["workspace_root"],
                                timeout=self.cfg["command_timeout"],
                                limit=self.cfg["command_output_limit"],
                                policy=self.cfg["exec_policy"],
                                allowlist=self.cfg["exec_allowlist"],
                                audit_path=self.cfg.get("command_log"),
                                cancel_event=cancelled,
                            ),
                        )
                    except executor.PolicyError as exc:
                        code, output = 126, str(exc)
                    if cancelled.is_set():
                        session.cancel()
                        raise asyncio.CancelledError()
                    output = redact(output, cookie_values=self.cfg.get("cookie_values", ()))
                    exec_steps += 1
                    # A usable answer plus its executed command is progress, not a retry.
                    holder["_attempt_budget"].new_step()
                    log("exec.result", "Команда завершилась с кодом %s" % code,
                        "success" if code == 0 else "warn" if code == 126 else "error",
                        command=redact(command)[:2000], exit=code, output=(output or "")[:1500],
                        output_chars=len(output or ""), duration_ms=round((time.time() - exec_started) * 1000),
                        blocked=code == 126)

                    if self.cfg["show_exec_output"]:
                        out.put({"type": "exec_result", "cmd": command, "output": output, "exit": code})

                    # Keep the task text once; earlier steps go in as a short trail.
                    if not task_block:
                        task_block = "USER TASK:\n" + parsed.prompt
                    trail = "; ".join(
                        (redact(c, cookie_values=self.cfg.get("cookie_values", ()))[:60] + "…" if len(c) > 60
                         else redact(c, cookie_values=self.cfg.get("cookie_values", ()))) + " -> exit " + str(e)
                        for c, e in previous_steps[-4:]
                    ) or "(none yet)"
                    previous_steps.append((command, code))
                    self._remember(command, recent)

                    # The command text is model-generated and may echo secrets; the
                    # output is already redacted above. Mask commands before upstream.
                    header = task_block + "\n\nPrevious steps: " + trail
                    safe_command = redact(command, cookie_values=self.cfg.get("cookie_values", ()))
                    last_followup = (safe_command, code, output, header)
                    followup_text = executor.format_command_followup(safe_command, code, output, header)
                    exec_chunks.append(followup_text)
                    prompt = self._followup(instructions, reminder, *last_followup,
                                            direct_chunks=client_chunks + exec_chunks[:-1] if direct_mode else None)
            finally:
                if account_context is not None:
                    # Releasing may evict other conversations, and the next attempt may use another account.
                    try:
                        client_context.reset(account_context)
                    except ValueError:  # a token of another context: nothing of ours is left to undo
                        pass
                if session is not None:
                    await self.pool.release(session)
        except apierr.ApiError as err:
            outcome["error"] = err
            out.put({"type": "error", "error": err})
        except asyncio.CancelledError:
            outcome["reason"] = "cancelled"
            out.put({"type": "end", "reason": "cancelled"})
            raise
        except Exception as exc:  # defensive: never mask a crash as success
            outcome["error"] = apierr.internal(str(exc))
            out.put({"type": "error", "error": outcome["error"]})
        finally:
            duration = round((time.time() - turn_started) * 1000)
            err = outcome["error"]
            if err is not None:
                log("turn.error", "%s: %s" % (err.code, redact(err.message or "")[:300]), "error",
                    status=err.status, code=err.code)
                reason = "error"
            else:
                reason = outcome["reason"] or holder.get("reason") or "stop"
            log("turn.end", "Ход завершён: %s за %.1f с" % (reason, duration / 1000),
                "error" if reason == "error" else "warn" if reason in ("cancelled", "timeout", "length") else "success",
                reason=reason, duration_ms=duration, exec_steps=exec_steps,
                retries=empty_retries + tool_retries)
            out.put(None)
