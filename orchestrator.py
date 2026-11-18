"""Turn orchestration: context injection, execution loop, event fan-out."""
import asyncio
import collections
import contextlib
import contextvars
import hashlib
import math

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
import cost_guard
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
import web_search


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
    "lower": "effort ветки ниже того, на котором пошёл бы запрос: relay не понижает effort ради кэша",
    "scope": "у запроса другие %s, чем у ветки",
}
_SCOPE_NAMES = {"conversation": "диалог", "provider": "модель или аккаунт", "head": "system (head)", "tools": "tools",
                "tool_choice": "tool_choice"}


def _below(effort, than):
    """Whether `effort` is a lower reasoning level than `than` (False when either is not a level)."""
    levels = model_catalog.EFFORT_ORDER
    return effort in levels and than in levels and levels.index(effort) < levels.index(than)


def _b(text: str) -> int:
    """Limit in bytes, not characters (Cyrillic is 2 bytes each in UTF-8)."""
    return len((text or "").encode("utf-8"))


def _preview(value, limit=600):
    """Compact, bounded rendering of tool arguments for the journal."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _loads_object(raw):
    """The JSON object in tool arguments, {} for anything else."""
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


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

    # Events that carry nothing the reply is made of: accounting, and the display statuses that say the model reasons
    # (or that time passes) without a word of it. A refusal after them was not answered yet: it moves like one before
    # them. Counted as output, a thinking_status made every unbilled refusal after it final (A02-01).
    NO_OUTPUT = frozenset({"credits", "usage", "usage_start", "thinking_status", "thinking_heartbeat"})

    def __init__(self, out):
        self.out = out
        self.error = None
        self.started = False
        self.sent = False  # a request left (Minara's request_sent): from then on only an unbilled refusal moves it

    def put(self, event):
        if event is None:
            return
        if event.get("type") == "error":
            self.error = event["error"]
            return
        # request_sent says that a request left (the Minara transport sends it before each POST, for the ledger): no
        # output yet. Counted as output, it made every failed Minara turn final, a 402 included (audit, 2026-11-10).
        if event.get("type") == "request_sent":
            self.sent = True
        elif event.get("type") not in self.NO_OUTPUT:
            self.started = True
        self.out.put(event)


async def _off_loop(fn, *args):
    """Run a blocking call (a web search, a balance check) on the relay's daemon IO workers, with the caller's context.

    The default executor (run_in_executor(None), asyncio.to_thread) is joined at interpreter exit: a search or a balance
    call under way kept a stopped relay alive for its 10-20 s of network timeouts after Ctrl+C. Without the relay's pool
    (a bare loop in tests) it is the default executor, as before."""
    loop = asyncio.get_running_loop()
    io_executor = getattr(loop, "_relay_io_executor", None)
    if io_executor is None:
        return await asyncio.to_thread(fn, *args)
    return await loop.run_in_executor(io_executor, contextvars.copy_context().run, fn, *args)


def _minara_refusal(error):
    """A Minara answer that takes the account out of the pool by itself: 402 (no credits) or 401/403 (the token)."""
    return getattr(error, "status", 0) == 402 or getattr(error, "code", None) == "upstream_auth_error"


def _model_id(parsed, lease=None):
    """The catalog id of the model a request runs on: what the pool and an account's plan refusals are keyed by. A route
    alias runs as the target its lease was bound to (SessionPool._bound_target), and a refusal is that target's: one kept
    under the alias's id was seen by no account choice (audit A08-01)."""
    entry = getattr(parsed, "entry", None) or {}
    bound = getattr(lease, "bound_entry", None) if lease is not None else None
    if isinstance(bound, dict) and bound.get("alias_id") and bound.get("alias_id") == entry.get("id"):
        return bound.get("id")
    return entry.get("id") or getattr(parsed, "model", None)


def _failed_pair(parsed, lease, backend):
    """What a failure of the model takes out of a request's failover: the account, or for a route alias the pair (account,
    target the turn ran as), so that the alias's other targets of that account are still tried (audit A17-01)."""
    model = _model_id(parsed, lease)
    entry = getattr(parsed, "entry", None) or {}
    return (backend, model) if entry.get("route_targets") and model != entry.get("id") else backend


def _may_be_balance_related(error):
    """Rate limits, timeouts and client errors never justify a balance fetch."""
    status = getattr(error, "status", 0) or 0
    code = getattr(error, "code", None)
    if status == 402 or (status == 429 and code == "insufficient_credits"):
        return True
    return status >= 500 and code != "upstream_timeout"


# A conversation moved to another account writes its prefix there again; below this many prompt tokens that costs next
# to nothing, and waiting for the limited account (account_rate_limit_wait) is only a delay.
LIMIT_WAIT_MIN_PREFIX = 8000
# How long a universal provider that answered 429 without a Retry-After is left out of the pool: a provider's limit is per
# minute (OpenRouter, OpenAI, Anthropic), not Retool's sliding hour, so account_rate_limit_cooldown (900 s) never fits it.
PROVIDER_RATE_LIMIT_PAUSE = 60


def _provider_label(backend):
    """The name of the universal provider behind this account (OpenRouter, Anthropic, ...), None for Retool and Minara."""
    cfg = getattr(backend, "cfg", None) or {}
    if cfg.get("upstream", "retool") in ("retool", "minara"):
        return None
    return str(cfg.get("provider_name") or cfg.get("provider_id") or account_tag(backend) or cfg.get("upstream"))


async def _pause(cancelled, seconds):
    """Wait `seconds` without holding a thread; True when the client went away first."""
    end = time.monotonic() + seconds
    while not cancelled.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return False
        await asyncio.sleep(min(0.25, left))
    return True


def _settle_turn(holder, went_on=None):
    """Write the turn.error and turn.end a failed attempt left pending (TurnRunner._run_once), now that its fate is known.

    `went_on` says what the request did next ("moved" to another account, "retried"): the attempt's error reached no client
    and is logged at warn with that outcome. None: the error is the request's answer, logged at error as before."""
    close = holder.pop("_close_turn", None)
    if close is not None:
        close(went_on)


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
    if purpose == "prompt-suggestion":
        return AcquirePlan("isolated", {"isolated": True})
    if getattr(parsed, "web_search", None) and len(getattr(parsed, "tools", None) or ()) == 1:
        # Claude Code runs WebSearch as a request of its own, carrying the id of the chat it serves: that is a job on its own
        # (a standalone head, one tool), and on the chat's session it would replace the head and the tools the chat's
        # signed history and cache are keyed by.
        return AcquirePlan("isolated", {"isolated": True})
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
        # (profile, client) pairs whose dropped system prompt the journal has already named (_warn_dropped_client_system).
        self._dropped_noted = set()
        # What Minara spent, by the hour, by client key and by dialog: the cost guard's ceilings are kept against it.
        self.cost_guard = cost_guard.Guard(cost_guard.state_path(cfg))

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

    def _minara_dialog_effort(self, parsed, session):
        """The effort a Minara dialog's turns went with, for its compaction, or None to keep the client's own.

        Minara keeps no signed branch: the effort is what the dialog's last turn sent (recorded on its session). It is kept
        when the compaction asks for no level or a lower one (the relay never lowers an effort to save a cache): a request
        on another level may miss the dialog's cache (Opus 4.7 at high did not read what low had written, 2026-11-10)."""
        if session is None or not getattr(session, "_minara_effort_set", False):
            return None
        if getattr(session, "_minara_effort_model", None) != parsed.model:
            return None  # another model's level is no level of this one (its map may send it as another)
        dialog = getattr(session, "_minara_effort", None)
        wanted = model_catalog.effective_effort(parsed.entry, parsed.effort, self.cfg.get("reasoning_effort"),
                                                self.cfg.get("claude_xhigh_effort", "high"))
        order = model_catalog.EFFORT_ORDER
        if dialog is None or dialog == wanted or dialog not in order:
            return None
        if wanted is None or (wanted in order and order.index(dialog) >= order.index(wanted)):
            return dialog
        return None

    def _dialog_effort(self, parsed, head, choice, exclude):
        """(the effort of the dialog, why the history restarts anyway) for a compaction.

        The first is the effort of the dialog when asking for it makes the signed history of the dialog resume and it is
        not lower than the effort the request would run on (the relay never lowers an effort to save a cache), else None
        to leave the client's own effort alone. The second is None, or, when the request does not continue the dialog's
        signed history on any effort, what to tell the journal: {"reason": "no_branch" | "history" | "effort" | "lower"} or
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
        if parsed.entry.get("upstream", "retool") != "retool":
            return self._minara_dialog_effort(parsed, session), None
        history = getattr(session, "_native_history", None)
        if history is None or not history.branches:
            return None, {"reason": "no_branch"}
        try:
            resource = model_catalog.provider_resource(
                getattr(getattr(session, "backend", None), "cfg", None) or self.cfg, parsed.entry)
        except apierr.ApiError:
            return None, None  # acquire says why
        wanted = model_catalog.effective_effort(parsed.entry, parsed.effort, self.cfg.get("reasoning_effort"),
                                                self.cfg.get("claude_xhigh_effort", "high"))
        tools = direct.tools_to_retool(parsed.tools)

        def scope_of(effort):
            provider = {"model": parsed.model}
            if effort is not None:
                provider["reasoningEffort"] = effort
            if resource:
                provider["providerResourceName"] = resource
            return direct.native_scope(key, provider, head or "", tools, choice)

        refused = lowered = False
        defaults = native_history.tool_defaults(parsed.tools)

        def continues(scope):
            # The request continues the branch as a turn of it, or forks from it (History.resume_fork: Desktop's compaction).
            return (history.resume(scope, client, defaults)[0] is not None
                    or history.resume_fork(scope, client)[0] is not None)

        for effort in dict.fromkeys(direct.scope_effort(branch.scope) for branch in reversed(list(history.branches))):
            if effort is None:
                continue
            if not continues(scope_of(effort)):
                continue
            if effort == wanted:
                return None, None  # the request resumes as it is
            if _below(effort, wanted):
                # The relay never lowers an effort to keep a cache: the dialog's is taken only when it is not below the
                # one the request would run on. The history restarts and is written again, at the effort that was asked.
                lowered = True
                continue
            try:
                _, resolved = model_catalog.resolve_tolerant(
                    parsed.model, effort, strict=self.cfg.get("strict_models", True), default_model=self.cfg.get("model"),
                    allow_downgrade=self.cfg.get("allow_effort_downgrade", False), cfg=self.cfg)
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
        if not continues(newest.scope):
            return None, {"reason": "history"}
        return None, ({"reason": "lower"} if lowered else {"reason": "effort"} if refused else None)

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

    def _warn_dropped_client_system(self, log, parsed, plan, inp):
        """Once per profile and client: the head leaves out the system prompt of a client that sent tools.

        That prompt holds the client's rules for its tools, edits and permissions. The prompt.plan warning of every request
        is a debug line nobody reads, and a user's profile cut Harness's own instructions out for a day (2026-11-06) with
        nothing in the journal at a level anyone watches."""
        import prompt_policy
        chars = prompt_policy.dropped_client_system(plan, _client_system(parsed)) if client_tools(parsed) else 0
        if not chars:
            return
        client = getattr(inp, "client", None) or prompt_policy.prompt_client(client_context.current(), {})
        key = (plan.profile, client)
        if key in self._dropped_noted or len(self._dropped_noted) >= 256:
            return
        self._dropped_noted.add(key)
        log("prompt.client_system_dropped",
            "Профиль «%s» не передаёт модели системный промпт клиента %s (%d симв.): его правила по инструментам, правкам и "
            "разрешениям не доходят до модели; добавьте {{client_system}} в head профиля или сузьте правило" % (
                plan.profile, client, chars), "warn", profile=plan.profile, rule=plan.rule, client=client, chars=chars)
        console_log.write("[prompt] profile %s drops the system prompt of %s (%d characters): its rules for tools never "
                          "reach the model; add {{client_system}} to the head" % (plan.profile, client, chars))

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
        self._warn_dropped_client_system(log, parsed, plan, inp)
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
    def _unpriced(parsed, usage):
        """What the journal says of an attempt's usage that came without a price, and why: (message, extra fields).

        An attempt cut before any usage (a stream that ended without its end, a 503 after the request left) has no
        tokens at all: "tokens received" said of it, 104 times in a day and a half of the live journal, read as if
        Retool had kept a price back. A Minara turn is priced by the catalog: when it cannot be, the reason is named."""
        counted = any(credits.token_counter(usage.get(key)) is not None for key in (
            'input_tokens', 'output_tokens', 'cache_read', 'cache_write'))
        if not counted:
            return 'Попытка оборвалась до отчёта о расходе: стоимость неизвестна', {}
        if str(getattr(parsed, 'model', '')).startswith('minara/'):
            try:
                import minara
                reason = minara.price_problem((getattr(parsed, 'entry', None) or {}).get('pricing'))
            except Exception:  # noqa: BLE001 - what the journal says must never fail the turn it describes
                reason = None
            reason = reason or 'Minara не сообщила цену'
            return 'Токены получены; цену по прайсу Minara не посчитать: %s' % reason, {'price_problem': reason}
        return 'Токены получены; стоимость upstream не сообщена', {}

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
        cost = (event.get('usage') or {}).get('provider_cost')
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            snapshot['provider_cost'] = cost
            currency = (event.get('usage') or {}).get('provider_cost_currency')
            snapshot['provider_cost_currency'] = currency if isinstance(currency, str) and currency else None
            # A cost the relay worked out from the catalog price, not one upstream reported (minara.normalize_usage).
            if (event.get('usage') or {}).get('provider_cost_estimated'):
                snapshot['provider_cost_estimated'] = True
            else:
                snapshot.pop('provider_cost_estimated', None)
        for key in ('cache_write_reported', 'upstream_cost', 'channel', 'channel_path', 'provider'):
            if key in (event.get('usage') or {}):
                snapshot[key] = (event.get('usage') or {})[key]
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
            console_log.write("[explain] fetch failed: %s" % exc)
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

    def _guard_cost(self, parsed, holder, session, entry, model_provider, log):
        """Price a Minara request before anything is sent and raise the cost guard's refusal when it is above a ceiling.

        The estimate (cost_guard.estimate) prices the request with the formula of its debit; the dialog's cache counts as
        warm when the relay has seen a priced turn of it (the guard's memory, or the prefix the session keeps on disk), so a
        running session is never priced as a first contact. The client gets a final 400 cost_limit_exceeded (or 429 for
        the hourly ceiling) that names the estimate, the journal cost.blocked, the console a [cost] line."""
        if self.cfg.get("cost_guard", "block") == "off":
            return
        request_tag = getattr(parsed, "request_id", None) or "req"
        try:
            chars = getattr(parsed, "client_chars", None) or adapters.request_chars(parsed)
            anchor = getattr(session, "_count_anchor", None)
            anchor = anchor[:2] if isinstance(anchor, tuple) and len(anchor) == 3 and anchor[2] == parsed.model else None
            tokens, basis = adapters.count_input_tokens(chars, anchor)
            account = account_tag(getattr(session, "backend", None))
            key = getattr(parsed, "conversation_key", None)
            prefix = self.cost_guard.warm_prefix(
                key if isinstance(key, (str, tuple)) else None, model=parsed.model, account=account,
                shared=cost_guard.shared_cache(entry), session_prefix=getattr(session, "_last_prefix_tokens", 0))
            estimate = cost_guard.estimate(entry, prompt_tokens=tokens, basis=basis, warm_prefix=prefix,
                                           max_tokens=getattr(parsed, "max_tokens", None),
                                           effort=model_provider.get("reasoningEffort"))
            if estimate is None:
                return
            error, warn = self.cost_guard.check(self.cfg, estimate, entry=entry, account=account,
                                                key=getattr(parsed, "client_key", None),
                                                confirm=getattr(parsed, "cost_confirm", None))
        except Exception as exc:  # noqa: BLE001 - a guard that fails must not fail the request it guards
            log("cost.estimate_failed", "Оценка цены запроса не удалась, запрос идёт без проверки: %s" % (
                type(exc).__name__), "warn")
            return
        holder["_cost_estimate"] = estimate
        holder.pop("_cost_actual", None)
        fields = dict(estimate._asdict(), model=entry.get("id"))
        how = cost_guard.explain(estimate, entry, account)
        if error is not None:
            log("cost.blocked", "Запрос остановлен защитой по цене: оценка %s кредитов Minara (%s)" % (
                estimate.credits, error.code), "warn", code=error.code, status=error.status, **fields)
            console_log.write("[%s] [cost] blocked: estimated %s Minara credits (~$%.2f): %s" % (
                request_tag, estimate.credits, estimate.usd, how))
            raise error
        log("cost.estimate", "Оценка цены запроса: %s кредитов Minara (кэш %s)" % (
            estimate.credits, "тёплый" if estimate.warm else "холодный"), "warn" if warn else "debug", **fields)
        if warn:
            console_log.write("[%s] [cost] estimated %s Minara credits (~$%.2f): %s" % (
                request_tag, estimate.credits, estimate.usd, how))

    def _limit_pause(self, error, session):
        """Seconds to wait on an account at its hourly limit before its conversation moves, or None to move it now.

        Only a conversation with a prefix worth keeping waits (LIMIT_WAIT_MIN_PREFIX): the other account writes the
        whole prefix again and cannot take the signed history along (field: 79k tokens in 38 s, and 490k tokens, after one
        429 each). Retool sends no Retry-After for this limit, so a wait is the setting's whole length; when it does
        send one, the wait is the hint if that fits in the setting, and a longer hint moves the conversation at once."""
        try:
            allowed = float(self.cfg.get("account_rate_limit_wait") or 0)
        except (TypeError, ValueError):
            return None
        if allowed <= 0 or int(getattr(session, "_last_prefix_tokens", 0) or 0) < LIMIT_WAIT_MIN_PREFIX:
            return None
        hint = getattr(error, "retry_after", None)
        if hint is None:
            return allowed
        return float(hint) if hint <= allowed else None

    async def _run_searches(self, searcher, spec, calls, used, cancelled, log):
        """Run the search calls of one step: [(call id, content, text)], content as the client gets it, text as the model reads it.

        A call counts against max_uses whether it succeeds or not (Anthropic's rule), and the ones past it get the
        max_uses_exceeded error as their result, so the model goes on with what it has. A provider that fails, or answers
        with a refusal, is an error result too: it never fails the turn."""
        outcomes = []
        for index, call in enumerate(calls):
            started, error, detail, results, query = time.time(), None, "", [], None
            try:
                arguments = json.loads(call["arguments"] or "{}")
                query = arguments.get("query") if isinstance(arguments, dict) else None
            except ValueError:
                pass
            if not isinstance(query, str) or not query.strip():
                error = "invalid_tool_input"
            elif len(query) > web_search.QUERY_LIMIT:
                error = "query_too_long"
            elif used + index >= spec["max_uses"]:
                error = "max_uses_exceeded"
            else:
                try:
                    # Off the event loop: the provider is an HTTP call that may take seconds.
                    found = await _off_loop(searcher.search, query.strip(), spec)
                    results = web_search.within_domains(found, spec)
                except web_search.SearchError as failure:
                    error, detail = failure.code, failure.detail
                except Exception as failure:  # noqa: BLE001 - a provider must never fail the turn
                    error, detail = "unavailable", type(failure).__name__
                if cancelled.is_set():
                    raise asyncio.CancelledError()
            content = ({"type": "web_search_tool_result_error", "error_code": error} if error
                       else web_search.result_blocks(results))
            outcomes.append((call["id"], content, web_search.render_block_content(content)))
            log("web_search.run", "Поиск%s: %s" % (" не удался (%s)" % error if error else "",
                                                   _preview(query, 120) if query else "—"),
                "warn" if error in ("unavailable", "too_many_requests") else "info", call_id=call["id"],
                query=_preview(query, 300) if query else None, results=len(results), error=error, detail=detail or None,
                ms=round((time.time() - started) * 1000), provider=self.cfg.get("web_search_provider") or None,
                allowed_domains=len(spec["allowed_domains"]), blocked_domains=len(spec["blocked_domains"]))
        return outcomes

    async def run(self, parsed, out: queue.Queue, holder: dict):
        """Measure the client request across account retries; account turns keep their own timelines."""
        started = holder.get("_request_started_monotonic")
        if type(started) not in (int, float) or not math.isfinite(started):
            started = time.monotonic()
        holder["_speed_attempts"] = {}
        holder["_speed_reason"] = "error"
        try:
            await self._run_request(parsed, out, holder)
        except BaseException:
            holder["_speed_reason"] = "error"
            raise
        finally:
            duration = max(0, round((time.monotonic() - started) * 1000))
            counts = holder["_speed_attempts"]
            output = sum(counts.values()) if counts and all(value is not None for value in counts.values()) else None
            cancelled = holder.get("cancel_event")
            reason = "cancelled" if cancelled is not None and cancelled.is_set() else holder["_speed_reason"]
            journal.emit("request.speed", "Скорость завершённого запроса", "info",
                         turn_id=holder.get("turn_id"), request_id=getattr(parsed, "request_id", None),
                         conversation=journal.tag(getattr(parsed, "conversation_key", None)),
                         reason=reason, duration_ms=duration, output_tokens=output,
                         tokens_per_second=journal.request_speed(output, duration))

    async def _run_request(self, parsed, out: queue.Queue, holder: dict):
        """Run a turn; retry on another account after a confirmed low balance or an hourly limit.

        The retry happens only while nothing was sent to the client. A low balance must be
        confirmed by the dispatcher's fresh balance fetch (the serving account dropped below
        min_account_balance); Retool's hourly managed-key limit parks the serving account at
        once, even when the turn is already streaming and cannot be retried. Other failures
        (empty turns, 5xx) are returned as before. With account_rate_limit_wait set, a conversation
        with a warm prefix first waits once on the limited account and asks it again (_limit_pause).
        A Retool plan that does not include the model (402) holds the account back from that model
        alone, also when the turn cannot move, and the turn moves like after an hourly limit; once
        no account can take the model, the client gets the pool's settled 404 (_pool_refusal).
        """
        exclude = []
        rechecked = False
        waited, awaiting = False, None
        cancelled = holder.setdefault("cancel_event", threading.Event())
        max_steps = int(self.cfg.get("max_exec_turns", 0) or 0)
        # Exec steps are bounded only by max_exec_turns; each step (plus the summary
        # step) may spend up to MAX_ATTEMPTS POSTs, which is the hard cap per request.
        holder["_attempt_budget"] = AttemptBudget(
            cancelled, total_cap=MAX_ATTEMPTS * (max_steps + 2) if max_steps > 0 else None,
            clock=getattr(getattr(self, "accounts", None), "clock", None))
        while True:
            # A path below that asks again without saying how leaves the attempt's close pending: it was retried.
            _settle_turn(holder, "retried")
            relay = _FailoverOut(out)
            holder.pop("session", None)
            try:
                await self._run_once(parsed, relay, holder, exclude=tuple(exclude))
            except BaseException:
                _settle_turn(holder)
                if relay.error is not None:
                    out.put({"type": "error", "error": relay.error})
                out.put(None)
                raise
            error = relay.error
            lease = holder.get("session")
            backend = getattr(getattr(lease, "session", lease), "backend", None)
            accounts = getattr(self, "accounts", None)
            if awaiting is not None:
                if error is None or not apierr.is_account_quota(error):
                    journal.emit("account.rate_limit_lifted",
                                 "Часовой лимит Retool снят за %g с ожидания — диалог остался на своём аккаунте" % awaiting[1],
                                 "info", account=account_tag(awaiting[0]), request_id=getattr(parsed, "request_id", None),
                                 seconds=awaiting[1])
                awaiting = None
            # A request that left may have been billed: it moves to another account only when upstream refused it before
            # generating anything (minara._http_error marks such answers unbilled: 402, 401/403, 429, no channel). A cut
            # stream or an error inside a 200 went to every account in turn, each attempt billed.
            retryable = (error is not None and not relay.started and not cancelled.is_set()
                         and accounts is not None and holder["_attempt_budget"].remaining > 0
                         and (not relay.sent or getattr(error, "unbilled", False)))
            exhausted = False
            rate_limited = plan_refused = False
            # A universal provider's 429 is a rate limit of its own (drivers/sse_utils marks it an account quota so that the
            # turn moves), not Retool's hourly managed-key limit: no wait on it, no hourly cooldown or ladder (A02-08).
            provider = _provider_label(backend)
            try:
                if error is not None and backend is not None and apierr.is_account_quota(error):
                    pause = (self._limit_pause(error, getattr(lease, "session", lease))
                             if retryable and backend not in exclude and not waited and provider is None
                             and holder["_attempt_budget"].remaining > 1 else None)
                    if pause is not None:
                        # The account is not parked and not excluded: the retry goes to it with the conversation's
                        # history, and only a second 429 takes the usual way below.
                        waited, awaiting = True, (backend, pause)
                        journal.emit("account.rate_limit_wait",
                                     "Часовой лимит Retool — запрос ждёт %g с тот же аккаунт, чтобы диалог не переезжал" % pause,
                                     "info", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                                     seconds=pause, retry_after=getattr(error, "retry_after", None))
                        console_log.write("[%s] [account] %s: часовой лимит Retool, ждём %g с и повторяем на нём" % (
                            getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED), pause))
                        if not await _pause(cancelled, pause):
                            holder["reason"] = "stop"
                            _settle_turn(holder, "retried")
                            continue
                        retryable = False  # the client went away while the request waited: only the account is told
                    # Hourly managed-key limit: park the account so later requests skip it, then
                    # retry here while the client has seen nothing.
                    if hasattr(accounts, "mark_rate_limited"):
                        # A limit that a request sent before the account's last return reports is old news. A provider is
                        # parked for its Retry-After or a short pause: a hint never climbs the hourly ladder.
                        hint = getattr(error, "retry_after", None)
                        if provider is not None and not hint:
                            hint = PROVIDER_RATE_LIMIT_PAUSE
                        accounts.mark_rate_limited(backend, retry_after=hint,
                                                   request_id=getattr(parsed, "request_id", None),
                                                   sent_at=holder["_attempt_budget"].sent_at)
                    rate_limited = retryable and backend not in exclude
                if error is not None and backend is not None and apierr.is_plan_refusal(error):
                    # Retool's 402 "This model is not available for your plan.": nothing was generated or billed. The
                    # account is held back from this model alone (never from the others), whether or not this turn can
                    # still move, and the turn moves while the client has seen nothing. No balance is read for it.
                    if hasattr(accounts, "mark_plan_refusal"):
                        accounts.mark_plan_refusal(backend, _model_id(parsed, lease),
                                                   request_id=getattr(parsed, "request_id", None))
                    plan_refused = retryable and backend not in exclude
                if (retryable and backend is None and error.code == "insufficient_credits"
                        and not rechecked and hasattr(accounts, "recheck_exhausted")):
                    # The pool refused before any upstream call: confirm the excluded
                    # balances once, so a top-up is used without waiting for the timer.
                    rechecked = True
                    if await _off_loop(accounts.recheck_exhausted, getattr(parsed, "entry", None)):
                        _settle_turn(holder, "retried")
                        continue
                minara_refusal = (retryable and backend is not None and backend not in exclude
                                  and (getattr(backend, "cfg", None) or {}).get("upstream") == "minara"
                                  and _minara_refusal(error) and hasattr(accounts, "mark_minara_refusal"))
                if minara_refusal and error.code == "upstream_auth_error" and hasattr(accounts, "confirm_minara_token"):
                    # A 401/403 may be about the request (a WAF rule, a moderation flag), the same on every account: the
                    # account is out only when /auth/me refuses its token too, or one request emptied the pool.
                    exhausted = await _off_loop(accounts.confirm_minara_token, backend)
                elif minara_refusal:
                    # 402: Minara said it in so many words; the one check_balance_now would make may be held back by
                    # FORCED_CHECK_GAP.
                    exhausted = accounts.mark_minara_refusal(backend, error)
                elif (retryable and backend is not None and backend not in exclude
                        and not apierr.is_plan_refusal(error) and _may_be_balance_related(error)):
                    # With no account left, the next acquire raises a clear 402.
                    exhausted = await _off_loop(accounts.check_balance_now, backend)
            except BaseException:
                _settle_turn(holder)
                out.put({"type": "error", "error": error})
                out.put(None)
                raise
            if rate_limited:
                exclude.append(backend)
                _settle_turn(holder, "moved")
                # Refused before anything was generated or billed: the move to the next account costs no attempt.
                holder["_attempt_budget"].refund()
                limit = ("часовой лимит Retool" if provider is None
                         else "лимит частоты провайдера %s" % provider)
                journal.emit("account.failover", "%s — запрос повторён на другом аккаунте" % (limit[0].upper() + limit[1:]),
                             "warn", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                             error=error.code, reason="rate_limited")
                console_log.write("[%s] [account] %s: %s, повтор на другом аккаунте" % (
                    getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED), limit))
                holder["reason"] = "stop"
                continue
            if plan_refused:
                exclude.append(_failed_pair(parsed, lease, backend))
                _settle_turn(holder, "moved")
                holder["_attempt_budget"].refund()
                journal.emit("account.failover", "Модель не входит в тариф аккаунта Retool — запрос повторён на другом аккаунте",
                             "warn", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                             error=error.code, reason="plan_refused", model=_model_id(parsed, lease))
                console_log.write("[%s] [account] %s: %s не входит в тариф Retool, повтор на другом аккаунте" % (
                    getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED), _model_id(parsed, lease)))
                holder["reason"] = "stop"
                continue
            if exhausted:
                exclude.append(backend)
                _settle_turn(holder, "moved")
                if minara_refusal:
                    # Minara said 402/401/403 before generating anything (unbilled). An exhaustion confirmed by a balance
                    # read after some other failure (a 5xx) keeps that failure's attempt charged.
                    holder["_attempt_budget"].refund()
                rejected = error.code == "upstream_auth_error"
                journal.emit("account.failover", "Токен аккаунта отклонён — запрос повторён на другом" if rejected else
                             "Баланс аккаунта закончился — запрос повторён на другом",
                             "warn", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                             error=error.code)
                console_log.write("[%s] [account] %s: %s, повтор на другом аккаунте" % (
                    getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED),
                    "токен отклонён" if rejected else "баланс закончился"))
                holder["reason"] = "stop"
                continue
            if (retryable and error is not None and backend is not None and backend not in exclude
                    and (getattr(backend, "cfg", None) or {}).get("upstream", "retool") not in ("retool", "minara")
                    and error.code in ("upstream_error", "service_unavailable", "upstream_timeout",
                                       "upstream_auth_error", "insufficient_credits", "rate_limit_error")):
                check_next = getattr(self.pool, "unavailable_error", None)
                # A failure of the model takes out the pair (account, target) of a route alias: the next target of the same
                # provider is still tried on this key (A17-01). A failure of the key itself (401, quota, 429) takes the key.
                failed = (_failed_pair(parsed, lease, backend)
                          if error.code in ("upstream_error", "service_unavailable", "upstream_timeout") else backend)
                if check_next is not None and check_next(
                    parsed.entry, exclude=(*exclude, failed), **acquire_plan(self.cfg, parsed).kwargs
                ) is None:
                    exclude.append(failed)
                    _settle_turn(holder, "moved")
                    journal.emit("account.failover", "Сбой апстрима провайдера — запрос переключён на резервный маршрут",
                                 "warn", account=account_tag(backend), request_id=getattr(parsed, "request_id", None),
                                 error=error.code, reason="provider_failover")
                    console_log.write("[%s] [account] %s: %s, failover на резервный маршрут" % (
                        getattr(parsed, "request_id", None) or "req", account_tag(backend, UNNAMED), error.code))
                    holder["reason"] = "stop"
                    continue
            if error is not None and apierr.is_plan_refusal(error):
                # The turn cannot move (no attempt left, the client went away): when no account can take the model now,
                # the client gets the settled 404 that names the cause rather than a 502 it would only send again.
                error = self._pool_refusal(parsed, exclude) or error
            if error is None and relay.started and backend is not None and hasattr(accounts, "mark_plan_served"):
                # The account's Retool plan includes the model: the pool asks such accounts first (retool_plans).
                accounts.mark_plan_served(backend, _model_id(parsed, lease))
            _settle_turn(holder)  # the error, if any, is what the client gets: red
            if error is not None:
                out.put({"type": "error", "error": error})
            out.put(None)
            return

    def _pool_refusal(self, parsed, exclude):
        """The settled 404 the pool gives this request now, when no account can serve its model, else None (reads state only)."""
        check = getattr(self.pool, "unavailable_error", None)
        if check is None:
            return None
        try:
            error = check(parsed.entry, exclude=tuple(exclude), **acquire_plan(self.cfg, parsed).kwargs)
        except Exception:  # noqa: BLE001 - a better error is only a nicety: the refusal itself is still the answer
            return None
        return error if isinstance(error, apierr.ApiError) and error.status == 404 else None

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
        # The web_search server tool of an Anthropic request: the model calls it as a function, the relay answers.
        search_spec = getattr(parsed, "web_search", None)
        searcher = web_search.provider(self.cfg) if search_spec else None
        search_uses = search_steps = 0
        turn_started = time.monotonic()
        output_attempts = {}
        request_attempts = holder.setdefault("_speed_attempts", {})
        outcome = {"reason": None, "error": None, "first": False}
        trace = {"turn_id": turn_id, "request_id": getattr(parsed, "request_id", None),
                 "conversation": journal.tag(getattr(parsed, "conversation_key", None))}

        def log(kind, message, level="info", **fields):
            # Usage and credits can repeat one attempt's cumulative token snapshot. Keep that snapshot, not their sum.
            if kind == "upstream.usage_start":
                output_attempts.setdefault(fields["attempt"], None)
                request_attempts.setdefault((turn_id, fields["attempt"]), None)
            elif kind == "upstream.usage":
                count = credits.token_counter((fields.get("usage_snapshot") or {}).get("output_tokens"))
                if count is not None:
                    output_attempts[fields["attempt"]] = count
                    request_attempts[(turn_id, fields["attempt"])] = count
            journal.emit(kind, message, level, **trace, **fields)

        try:
            native_tools = getattr(parsed, "tools", None) or []
            tool_definitions = {item["function"]["name"]: item["function"] for item in native_tools}
            # The tools the model is given beyond the ones this request declares: the client's earlier ones (tool_union).
            carried_tools = getattr(parsed, "carried_tools", None) or frozenset()
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
            if (parsed.entry.get("upstream", "retool") == "retool"
                    and not parsed.entry.get("route_targets")
                    and not model_catalog.PROVIDER_RESOURCE_KEY.get(parsed.entry["provider"])):
                raise apierr.service_unavailable(f"provider '{parsed.entry['provider']}' is not routed")
            # The resource is per-account (org-scoped UUID or __managed__): it is
            # resolved from the acquired session's backend cfg below. Resolving it
            # from the global cfg here would 503 account-only configurations.
            model_provider = {"model": parsed.entry.get("upstream_model", parsed.model)}
            if parsed.entry.get("upstream") == "minara":
                model_provider["route_model"] = parsed.model
                if parsed.entry.get("pricing"):
                    model_provider["pricing"] = parsed.entry["pricing"]  # prices the replies Minara reports no cost for
                if parsed.entry.get("forced_tool_choice") is False:
                    model_provider["refuses_forced_tool_choice"] = True
                if parsed.entry.get("messages_levels"):
                    # The levels the chat path sends to a channel without cache: they go through /v1/messages (minara).
                    model_provider["messages_levels"] = list(parsed.entry["messages_levels"])
            effort = model_catalog.effective_effort(parsed.entry, parsed.effort, self.cfg.get("reasoning_effort"),
                                                    self.cfg.get("claude_xhigh_effort", "high"))
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
                # A route alias runs as the target the pool chose the account for (lease.bound_entry, A08-02).
                bound_entry = (getattr(session, "bound_entry", None)
                               or model_catalog.bind_entry_for_backend(parsed.entry, acc_cfg))
                acquired._bound_entry = bound_entry
                acquired._provider_options = dict(getattr(parsed, "provider_options", None) or {})
                acquired._structured_output = getattr(parsed, "structured_output", None)
                model_provider["model"] = bound_entry.get("upstream_model") or bound_entry["id"]
                if isinstance(bound_entry, dict) and bound_entry.get("alias_id"):
                    # The bound target's price, Minara fields and effort, not those of the alias's first target (A12-03).
                    model_catalog.bind_provider(model_provider, bound_entry, getattr(parsed, "effort", None), self.cfg,
                                                kept=holder.get("kept_effort"))
                resource = model_catalog.provider_resource(acc_cfg, bound_entry)
                if resource:
                    model_provider["providerResourceName"] = resource
                else:
                    # __managed__: omit the field entirely (never send null).
                    model_provider.pop("providerResourceName", None)
                actual_upstream = acc_cfg.get("upstream", "retool")
                console_log.write(console_log.route_line(
                    getattr(parsed, "request_id", None) or "req", upstream=actual_upstream,
                    account=account_tag(getattr(acquired, "backend", None)), model=parsed.model,
                    upstream_model=model_provider.get("model"), effort=model_provider.get("reasoningEffort"),
                    child=bool(parent_key)))
                log("route.bound", "Выбран фактический маршрут %s" % actual_upstream,
                    upstream=actual_upstream, model=parsed.model, upstream_model=model_provider.get("model"),
                    effective_effort=model_provider.get("reasoningEffort"), child=bool(parent_key))
                if actual_upstream != "retool" and getattr(parsed, "client_task", "") not in client_context.SIDE_TASKS:
                    # What the dialog's turns go with: its compaction keeps it (_minara_dialog_effort).
                    target = acquired.session if isinstance(acquired, SessionLease) else acquired
                    target._minara_effort = model_provider.get("reasoningEffort")
                    target._minara_effort_model = parsed.model
                    target._minara_effort_set = True
                if actual_upstream == "minara":
                    # Priced before anything leaves: a refusal costs nothing (cost_guard).
                    self._guard_cost(parsed, holder, acquired, bound_entry, model_provider, log)
                # How many turns the pool has in flight, on this account and in all, this one included: the load this turn met.
                counted = getattr(self.pool, "inflight", None)
                load = ({} if counted is None else dict(zip(("inflight_account", "inflight_total"),
                                                           counted(getattr(acquired, "backend", None)))))
                log("session.acquire", "Сессия получена за %d мс" % round((time.monotonic() - turn_started) * 1000),
                    "debug", thread=journal.tag(getattr(acquired, "thread_id", None)),
                    isolated=bool(isolated), mode=self.cfg.get("session_mode"),
                    context_scope="text" if persistent_text else None,
                    wait_ms=round((time.monotonic() - turn_started) * 1000), **load)
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
                    dropped_calls = []
                    upstream_completed = False
                    search_calls = {}  # this step's calls of the search tool that upstream has ended, by id, in order
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
                        sent = self._check_prompt(self._direct_text(instructions, chunks or [stream_prompt]))
                        if exec_chunks:
                            # The token estimate counts what the model reads: a follow-up holds only its own step, after
                            # the client's messages and the steps before it.
                            parsed._full_prompt_text = sent

                        if native_tools:
                            import drivers as _drivers
                            uses_retool_tools = _drivers.get_driver(acc_cfg).capabilities.uses_retool_tool_schema
                            tools_converted = (direct.tools_to_retool(parsed.tools) if uses_retool_tools
                                               else parsed.tools)
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
                        # An empty list is no history: a transport that builds from it (Minara, the universal drivers) sent
                        # the head alone, without the chunks of the conversation (audit A15-03).
                        direct_target._native_client_messages = (
                            getattr(parsed, "native_messages", None) or None
                            if native_tools or actual_upstream != "retool"
                            else None
                        )
                        # Which calls the client can receive: a call of another tool is dropped below, and is no part
                        # of what the turn delivered or of the branch kept for it (direct.iter_events).
                        direct_target._native_callable_tools = list(tool_names) if native_tools else None
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
                    provider_recorder = getattr(account_credits, "record_provider_usage", None)
                    provider_attempt = "%s:%d" % (turn_id, journal_attempt)
                    # The ledger row of the request being sent: a transport that asks again inside one attempt (Minara's
                    # forced choice asked in words) starts a new usage_start, and each request keeps its own row. With one
                    # id for both, the second request's cost replaced the first's (4.4 credits debited for 92.4 spent).
                    attempt_cell = {"id": provider_attempt}
                    sent_attempt = threading.Event()
                    accounting_finished = threading.Event()
                    accounting_lock = threading.Lock()
                    provider_target = session.session if isinstance(session, SessionLease) else session
                    if provider_recorder:
                        managers_seen = holder.setdefault('_provider_turn_managers', set())
                        if id(account_credits) not in managers_seen:
                            account_credits.start_provider_turn()
                            managers_seen.add(id(account_credits))
                        def request_sent(recorder=provider_recorder, cell=attempt_cell, turn=turn_id,
                                         sent=sent_attempt, lock=accounting_lock, finished=accounting_finished):
                            with lock:
                                recorder(cell["id"], turn, {}, final=finished.is_set())
                                sent.set()
                        provider_target._provider_request_sent = request_sent
                    try:
                        # Leaving the loop (an error event raises below) closes the generators of the stream at once:
                        # left to the garbage collector, a cut turn keeps its session "busy" until some later collection.
                        async with contextlib.aclosing(stream):
                            async for event in stream:
                                if event["type"] == "request_sent":
                                    out.put(event)
                                    continue
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
                                    if purpose == "prompt-suggestion":
                                        pass
                                    elif direct_mode or not native_tools:
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
                                    # Only a sign the upstream gave starts the thinking span of the journal: a status of the
                                    # relay's own (source "relay", the Minara timer of 54b1023) said nothing about the model.
                                    if think_started is None and event.get("source") != "relay":
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
                                            if provider_recorder:
                                                with accounting_lock:
                                                    if sent_attempt.is_set():
                                                        account_credits.finish_provider_attempt(attempt_cell["id"], turn_id)
                                                    attempt_cell["id"] = "%s:%d" % (turn_id, journal_attempt)
                                        usage_started = True
                                        think_started = None
                                        holder['_attempt_usage'] = {}
                                        holder['_attempt_credited'] = False
                                        holder['_attempt_minara'] = None
                                        holder['_attempt_usd'] = None
                                    out.put(event)
                                elif (event["type"] == "tool_call" and searcher is not None and direct_mode
                                      and event.get("name") == search_spec["name"]):
                                    # The relay's own tool: the client sees a server_tool_use block, not a call to run.
                                    out.put({key: value for key, value in event.items() if key != "partial"}
                                            | {"tool_type": "function", "server": "web_search",
                                               "done": not event.get("partial")})
                                    if not event.get("partial"):
                                        search_calls[event.get("id") or event["name"]] = {
                                            "id": event.get("id") or event["name"], "arguments": event.get("arguments")}
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
                                            args_ms=args_ms, args_chars=len(event.get("arguments") or ""),
                                            **({"carried": True} if event["name"] in carried_tools else {}))
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
                                    ptc_def = tool_definitions.get("run_code") if len(tool_definitions) == 1 else None
                                    ptc_desc = str((ptc_def or {}).get("description") or "")
                                    is_ptc = ptc_def is not None and "Execute a " in ptc_desc and "await tools." in ptc_desc
                                    if is_ptc and event.get("partial"):
                                        pass
                                    elif is_ptc and not event.get("invalid"):
                                        sub_name = str(event.get("name") or "").strip()
                                        try:
                                            args_obj = json.loads(event.get("arguments") or "{}")
                                        except (ValueError, TypeError):
                                            args_obj = None
                                        if sub_name and isinstance(args_obj, dict):
                                            args_json = json.dumps(args_obj, ensure_ascii=False)
                                            if "Execute a Python program" in ptc_desc:
                                                code = (
                                                    "import json\n"
                                                    "_res = await getattr(tools, %s)(json.loads(%s))\n"
                                                    "print(_res if isinstance(_res, str) else json.dumps(_res, ensure_ascii=False, indent=2))"
                                                    % (json.dumps(sub_name), json.dumps(args_json))
                                                )
                                            elif "the BODY of an async function" in ptc_desc:
                                                code = "return await tools[%s](%s);" % (json.dumps(sub_name), args_json)
                                            else:
                                                code = (
                                                    "const _res = await tools[%s](%s);\n"
                                                    "console.log(typeof _res === \"string\" ? _res : JSON.stringify(_res, null, 2));"
                                                    % (json.dumps(sub_name), args_json)
                                                )
                                            desc_label = str(args_obj.get("description") or ("Invoke %s tool" % sub_name)).strip()[:120]
                                            wrapped_args = json.dumps(
                                                {"code": code, "description": desc_label or ("Invoke %s tool" % sub_name)},
                                                ensure_ascii=False,
                                            )
                                            streamed_tool_call = True
                                            if direct_mode:
                                                direct_target._direct_reply = None
                                            log("tool.ptc_wrapped",
                                                "Вызов %s обёрнут в run_code для PTC-режима клиента" % sub_name,
                                                "info", original=sub_name, call_id=event.get("id"))
                                            out.put({
                                                "type": "tool_call",
                                                "id": event.get("id") or ("call_" + uuid.uuid4().hex[:16]),
                                                "name": "run_code",
                                                "arguments": wrapped_args,
                                                "tool_type": ptc_def.get("x-client-type", "function"),
                                                "done": True,
                                            })
                                        else:
                                            dropped_calls.append(event.get("name"))
                                            log("tool.call_dropped",
                                                "Вызов инструмента %s не передан клиенту: клиент его не объявлял" % (
                                                    event.get("name"),),
                                                "warn", name=event.get("name"), call_id=event.get("id"),
                                                declared=len(tool_names))
                                    elif not event.get("partial"):
                                        # A call of a tool the client did not declare (under a forced choice, of another
                                        # tool) has nothing to run it on the client and is not passed on; the journal names
                                        # it instead of losing it without a trace (ultracode audit of the relay, C4).
                                        dropped_calls.append(event.get("name"))
                                        log("tool.call_dropped",
                                            "Вызов инструмента %s не передан клиенту: клиент его не объявлял" % (
                                                event.get("name"),),
                                            "warn", name=event.get("name"), call_id=event.get("id"),
                                            declared=len(tool_names))
                                elif event['type'] == 'usage':
                                    usage, delta = self._usage_update(holder, event)
                                    if provider_recorder and sent_attempt.is_set():
                                        provider_recorder(attempt_cell["id"], turn_id, usage)
                                    breakdown = credits.cost_breakdown(usage, parsed.model)
                                    known = holder.get('_attempt_credited') or event.get('credits_reported')
                                    provider_cost = usage.get('provider_cost')
                                    usd_spent = None
                                    if (isinstance(provider_cost, (int, float)) and not isinstance(provider_cost, bool)
                                            and math.isfinite(provider_cost)
                                            and (usage.get('provider_cost_currency') or 'USD').upper() == 'USD'):
                                        before_usd = holder.get('_attempt_usd') or 0.0
                                        usd_spent = round(max(0.0, float(provider_cost) - before_usd), 10)
                                        holder['_attempt_usd'] = float(provider_cost)
                                    minara_credits = credits.minara_credits(usage)
                                    # The attempt's Minara credits so far, against what earlier usage of the same attempt
                                    # already said: the journal and the console's spent= add each event's share once.
                                    minara_spent = None
                                    if minara_credits is not None:
                                        before = holder.get('_attempt_minara') or 0.0
                                        minara_spent = round(max(0.0, minara_credits - before), 4)
                                        holder['_attempt_minara'] = minara_credits
                                        event = dict(event, minara_credits=minara_spent)
                                        holder['_cost_actual'] = minara_credits  # the attempt's, against its estimate
                                        # What the cost guard keeps its hour, a key's day and the dialog's cache against.
                                        conversation = getattr(parsed, 'conversation_key', None)
                                        self.cost_guard.spent(
                                            minara_spent, key_id=holder.get('_client_key_id'),
                                            conversation=conversation if isinstance(conversation, (str, tuple)) else None,
                                            account=trace.get('account'), model=parsed.model,
                                            prompt_tokens=breakdown['prompt_tokens'] or None)
                                    if usd_spent is not None:
                                        event = dict(event, cost_usd=usd_spent)
                                    ck_id = holder.get('_client_key_id')
                                    ck_rec = holder.get('_client_key_recorder')
                                    if ck_id and callable(ck_rec):
                                        ck_rec(
                                            ck_id,
                                            input_tokens=(delta.get('input_tokens') or 0) + (delta.get('cache_read') or 0) + (delta.get('cache_write') or 0),
                                            output_tokens=delta.get('output_tokens') or 0,
                                            cost_usd=usd_spent,
                                        )
                                    if not known and provider_cost is not None:
                                        # Minara or a universal provider reports a cost in USD (or the relay estimated one
                                        # from the catalog): the turn is priced, only not in Retool credits.
                                        log('upstream.usage', 'Токены получены; стоимость %s %.6f%s%s' % (
                                                usage.get('provider_cost_currency') or 'USD', provider_cost,
                                                ' (оценка по прайсу)' if usage.get('provider_cost_estimated') else '',
                                                ' = %s кредитов Minara' % minara_credits
                                                if minara_credits is not None else ''),
                                            'info', credits=None, attempt=journal_attempt, credits_reported=False,
                                            provider_cost=provider_cost, cost_usd=usd_spent, minara_credits=minara_spent,
                                            provider_cost_estimated=bool(usage.get('provider_cost_estimated')), **delta,
                                            **credits.cost_breakdown(delta, parsed.model), usage_snapshot=dict(usage))
                                    else:
                                        text, unpriced = (('Токены обновлены; стоимость уже сообщена', {}) if known
                                                          else self._unpriced(parsed, usage))
                                        log('upstream.usage', text,
                                            'info' if known else 'warn', credits=None, attempt=journal_attempt,
                                            credits_reported=bool(known), minara_credits=minara_spent, **delta,
                                            **credits.cost_breakdown(delta, parsed.model), usage_snapshot=dict(usage),
                                            **unpriced)
                                    # The same `[cache]` line Retool's path prints, in the same shape — in= read= write=
                                    # credits= out= — for a Minara turn, which had none at all and whose cost was
                                    # therefore invisible. Only for minara/*: direct.py's _drain prints it for Retool,
                                    # and printing it here too would show every Retool turn twice. `credits` is
                                    # Minara's own currency (220 per dollar), never Retool's.
                                    minara_turn = str(parsed.model).startswith('minara/')
                                    write_cost = credits.write_cost(
                                        delta, parsed.model, (getattr(parsed, 'entry', None) or {}).get('pricing'))
                                    monitor = getattr(session, '_console_cache_monitor', None) if session is not None else None
                                    if minara_turn:
                                        if session is not None and monitor is None:
                                            # The Retool path makes this monitor in _drain; a Minara turn never goes
                                            # there, and without it the `[cache]` line was missing for every turn.
                                            monitor = session._console_cache_monitor = console_log.CacheMonitor()
                                        if monitor is not None:
                                            # The attempt's counters, as the relay keeps them (input_tokens, cache_read,
                                            # ...), and who answered it: Minara's channel, the path and the provider
                                            # (minara.provider_of). A Minara line always names both ("?" when unknown).
                                            console_log.write(monitor.line(
                                                getattr(parsed, 'request_id', None) or 'req', usage,
                                                scope=(parsed.model, getattr(parsed, 'conversation_key', None)),
                                                credits=minara_credits, write_cost=write_cost,
                                                channel=usage.get('channel') or '?', path=usage.get('channel_path'),
                                                provider=usage.get('provider') or '?')
                                                + (" " + client_context.label() if client_context.label() else ""))
                                    if write_cost is not None and write_cost >= credits.WRITE_WARN:
                                        # A warning only: by the time the usage arrives the turn is paid for. The
                                        # console shows it as a [WRITE COSTLY …] marker inside the [cache] line.
                                        log('cache.write_expensive', 'Дорогая запись кэша: %s токенов = %s %s' % (
                                                console_log.amount(delta.get('cache_write'), tokens=True),
                                                console_log.amount(write_cost),
                                                'кредитов Minara' if minara_turn else 'кредитов'), 'warn',
                                            model=parsed.model, cache_write=delta.get('cache_write'),
                                            write_cost=write_cost, attempt=journal_attempt)
                                    if (usage.get('cache_write_reported') is False and not delta.get('cache_read')
                                            and (getattr(parsed, 'entry', None) or {}).get('cache') == 'explicit'
                                            and breakdown['prompt_tokens'] >= 2048):
                                        # The marks were sent, and the answer came from a channel that has no cache at all
                                        # (Minara's Bedrock or Anthropic path: no cache write field): the whole prompt is
                                        # billed each turn. The level moves to /v1/messages, where those channels cache.
                                        log('cache.uncached_channel', 'Minara ответила с канала без кэша: промпт %d '
                                            'токенов оплачен полностью' % breakdown['prompt_tokens'], 'warn',
                                            model=parsed.model, effort=getattr(parsed, 'effort', None),
                                            sent_effort=model_provider.get('reasoningEffort'),
                                            channel=usage.get('channel'), path=usage.get('channel_path'),
                                            prompt_tokens=breakdown['prompt_tokens'], attempt=journal_attempt)
                                        if parsed.entry.get('upstream') == 'minara' and usage.get('channel_path') == 'chat':
                                            import minara
                                            minara.switch_to_messages(
                                                parsed.entry.get('upstream_model'), model_provider.get('reasoningEffort'),
                                                reason='chat channel %s' % (usage.get('channel') or 'unknown'))
                                    elif (usage.get('channel_path') == 'messages' and not delta.get('cache_read')
                                            and not delta.get('cache_write')
                                            and (getattr(parsed, 'entry', None) or {}).get('cache') == 'explicit'
                                            and breakdown['prompt_tokens'] >= 8192):
                                        # /v1/messages answered a marked prompt above every model's smallest cacheable
                                        # size without reading or writing it: a channel there that drops the marks too.
                                        log('cache.uncached_channel', 'Minara /v1/messages ответила без кэша: промпт %d '
                                            'токенов оплачен полностью' % breakdown['prompt_tokens'], 'warn',
                                            model=parsed.model, sent_effort=model_provider.get('reasoningEffort'),
                                            channel=usage.get('channel'), path='messages',
                                            prompt_tokens=breakdown['prompt_tokens'], attempt=journal_attempt)
                                    if (minara_turn and (getattr(parsed, 'entry', None) or {}).get('cache') == 'explicit'
                                            and self.cfg.get('direct_cache', True) and usage.get('cache_write_reported')
                                            and not usage.get('cache_write') and usage.get('cache_read')
                                            and (delta.get('input_tokens') or 0) >= credits.TAIL_UNWRITTEN_INPUT):
                                        # The reply carried marks and read the cache, but wrote nothing: Minara then bills
                                        # the whole prompt at the input price, the part it read included (a turn that ended
                                        # in tool results did so until minara.TOOL_RESULTS_END; a retry whose prompt is all
                                        # cached still can). A warning only: the turn is paid for.
                                        log('cache.tail_unwritten', console_log.tail_unwritten_text(
                                                delta.get('input_tokens'), minara_credits), 'warn',
                                            model=parsed.model, path=usage.get('channel_path'), channel=usage.get('channel'),
                                            input_tokens=delta.get('input_tokens'), cache_read=usage.get('cache_read'),
                                            minara_credits=minara_credits, attempt=journal_attempt)
                                        console_log.write(console_log.tail_unwritten_line(
                                            getattr(parsed, 'request_id', None) or 'req', delta.get('input_tokens'),
                                            minara_credits) + (" " + client_context.label() if client_context.label() else ""))
                                    if session is not None and breakdown['prompt_tokens'] > 0:
                                        # Only counts tell the size of the prefix. The empty usage of an attempt whose
                                        # price is unknown (a timeout or a cut stream on a 200) has none: written as 0,
                                        # it switched account_rate_limit_wait off for the conversation (_limit_pause)
                                        # and made the pool take it for the cheapest to evict (_rebuild_cost).
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
                                    if session is not None and breakdown["prompt_tokens"] > 0:
                                        # Write through the lease: SessionLease only proxies
                                        # reads, so keepwarm (which holds the real session)
                                        # saw prefix_tokens=0 forever. A credits event without
                                        # token counts says nothing of the prefix (see usage).
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
                    finally:
                        if provider_recorder:
                            with accounting_lock:
                                accounting_finished.set()
                                if sent_attempt.is_set():
                                    account_credits.finish_provider_attempt(attempt_cell["id"], turn_id)
                        if provider_recorder:
                            provider_target._provider_request_sent = None
                    holder.setdefault("reason", "stop")
                    if holder.get("reason") == "cancelled" or cancelled.is_set():
                        holder["reason"] = "cancelled"
                        self._commit_native_context(native_target, native_context, upstream_completed, cancelled)
                        out.put({"type": "end", "reason": "cancelled"})
                        return
                    # A provider refusal is terminal even when it carries no visible text.
                    # Never execute a filtered partial command or server-tool call.
                    # A prompt suggestion cut by its small cap still has its words: they go to the client below.
                    if (holder.get("reason") in ("timeout", "length", "content_filter")
                            and not (purpose == "prompt-suggestion" and holder.get("reason") == "length")):
                        out.put({"type": "end", "reason": holder["reason"]})
                        return

                    if search_calls:
                        outcomes = await self._run_searches(searcher, search_spec, list(search_calls.values()),
                                                            search_uses, cancelled, log)
                        search_uses += len(outcomes)
                        for call_id, content, _ in outcomes:
                            out.put({"type": "server_tool_result", "tool_use_id": call_id, "content": content})
                        if not streamed_tool_call and search_steps < search_spec["max_uses"] + 2:
                            # Only searches this step: the relay is the client of its own tool. The reply goes on from
                            # the step just ended exactly as a tool loop of a client would continue it: its echo and the
                            # results join the client's messages, so the signed thinking and the cached prefix are kept
                            # (and a later request that echoes this reply finds the same history).
                            search_steps += 1
                            reply = getattr(direct_target, "_direct_reply", None)
                            echo = reply[1] if reply else native_history.canonical_assistant(
                                ([{"type": "text", "text": final_text}] if final_text else [])
                                + [{"type": "tool_call", "toolCallId": call["id"], "toolName": search_spec["name"],
                                    "toolInput": _loads_object(call["arguments"])} for call in search_calls.values()])
                            if getattr(parsed, "native_messages", None) is not None:
                                parsed.native_messages = [*parsed.native_messages, echo, *(
                                    {"role": "tool", "content": {"toolCallId": call_id, "toolResult": text}}
                                    for call_id, _, text in outcomes)]
                            for position, call in enumerate(search_calls.values()):
                                client_chunks.append("Assistant: %s\n[tool_call %s id=%s: %s]" % (
                                    final_text if position == 0 else "", search_spec["name"], call["id"],
                                    call["arguments"] or "{}"))
                            client_chunks.extend("[tool result %s id=%s]: %s" % (search_spec["name"], call_id, text)
                                                 for call_id, _, text in outcomes)
                            # A forced choice applied to the step that asked for the search, not to the answer after it.
                            parsed.tool_choice = choice = "auto"
                            holder["_attempt_budget"].new_step()
                            continue
                        if not streamed_tool_call:
                            log("web_search.loop_stopped", "Цикл поиска остановлен: модель продолжает вызывать поиск "
                                "после лимита max_uses=%d" % search_spec["max_uses"], "warn", uses=search_uses)

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
                                    arguments=_preview(native_call.get("arguments")), streamed=False,
                                    **({"carried": True} if native_call["name"] in carried_tools else {}))
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
                        # Same error shape as an empty turn: diagnostic_id and attempt count. A reply whose only calls
                        # were dropped is no empty turn: the model answered, with a call the client cannot receive.
                        raise holder["_attempt_budget"].error(
                            "upstream_tool_choice_ignored" if final_text.strip() or dropped_calls else "upstream_empty_turn",
                            "upstream did not honor the required client tool choice" + (
                                " (it called only %s, which this request does not offer)" % ", ".join(
                                    sorted({str(name)[:60] for name in dropped_calls})[:5]) if dropped_calls else ""))

                    # Once the summary turn is requested, nothing is executed any more.
                    can_exec = self._can_exec(exec_steps) and not final_summary and not client_owns_instructions
                    command = executor.extract_command(final_text) if can_exec and not client_has_tools else None

                    if (not final_summary and exec_steps and not self._can_exec(exec_steps)
                            and (executor.extract_command(final_text) or not final_text.strip())):
                        # Explicit step cap reached (max_exec_turns > 0 only) by a reply that asks for one more command, or by
                        # an empty one. A reply that answers without a command is the final answer: the client has it already,
                        # and asking again paid one more request and streamed a second answer after it.
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

                    if purpose == "prompt-suggestion":
                        cleaned = final_text.strip().strip("\"'«»").strip()
                        meta_silent = (
                            not cleaned
                            or "\n" in cleaned
                            or len(cleaned.split()) > 16
                            or bool(re.search(
                                r"(?:^\s*[\(\[].*[\)\]]\s*$|"
                                r"^\s*(?:nothing|none|silent)\.?\s*$|"
                                r"\b(?:no\s+suggestion|nothing\s+to\s+suggest|stay(?:ing)?\s+silent|"
                                r"empty\s+response|no\s+natural\s+next)\b)",
                                cleaned, re.IGNORECASE))
                        )
                        if not meta_silent:
                            out.put({"type": "text", "delta": cleaned})
                        out.put({"type": "end", "reason": holder.get("reason", "stop")})
                        return

                    if not command and not final_text.strip():
                        if dropped_calls:
                            names = ", ".join(sorted({str(name)[:60] for name in dropped_calls})[:5])
                            raise holder["_attempt_budget"].error(
                                "upstream_invalid_output",
                                "upstream returned only undeclared tool call(s) (%s) not in the client's tool list" % names,
                            )
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
                                command, self.cfg["workspace_root"],  # noqa: B023 (awaited before the loop moves on)
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
                    if direct_mode:
                        # The task and the trail are for the agent transport, whose follow-up replaces its whole prompt.
                        # The direct body keeps the client's messages and every earlier step after them, so a step adds
                        # only the reply that asked for its command and the result: with the task in each follow-up, k
                        # commands sent the conversation k + 1 times, and each step wrote one more copy of it to the cache.
                        header = ""
                        exec_chunks.append(("Assistant: " + redact(
                            final_text, cookie_values=self.cfg.get("cookie_values", ()))).strip())
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
            duration = round((time.monotonic() - turn_started) * 1000)
            err = outcome["error"]
            reason = "error" if err is not None else outcome["reason"] or holder.get("reason") or "stop"
            holder["_speed_reason"] = reason
            # Explicit account-quota and plan refusals are free; retain their latency, but count no generated tokens.
            if err is not None and (apierr.is_account_quota(err) or apierr.is_plan_refusal(err)):
                number = next(reversed(output_attempts), None)
                if number is not None and output_attempts[number] is None:
                    request_attempts[(turn_id, number)] = 0
            output_tokens = (sum(output_attempts.values()) if output_attempts
                             and all(value is not None for value in output_attempts.values()) else None)
            estimate, actual = holder.pop("_cost_estimate", None), holder.pop("_cost_actual", None)
            if estimate is not None and actual is not None:
                # How far the cost guard's estimate was from the debit: what its rules are calibrated by.
                log("cost.estimate_vs_actual", "Оценка цены %s, списано %s кредитов Minara" % (estimate.credits, actual),
                    "debug", estimate=estimate.credits, actual=actual, warm=estimate.warm,
                    ratio=round(actual / estimate.credits, 3) if estimate.credits else None)

            def close(went_on=None):
                # went_on: what the request did after this failed attempt ("moved" to another account, "retried" here),
                # or None when the error is what the client gets. Only that one is red (_settle_turn).
                moved = {"outcome": went_on} if err is not None and went_on else {}
                if err is not None:
                    # Every POST of the request so far, and how many of them refusals gave back (AttemptBudget.refund).
                    posts = {name: getattr(err, attr) for name, attr in (("upstream_attempts", "upstream_attempts"),
                                                                         ("refunded", "upstream_refunded"))
                             if getattr(err, attr, None)}
                    log("turn.error", "%s: %s" % (err.code, redact(err.message or "")[:300]),
                        "warn" if moved else "error", status=err.status, code=err.code, **moved, **posts)
                log("turn.end", "Ход завершён: %s за %.1f с%s" % (
                        reason, duration / 1000, " · запрос продолжен" if moved else ""),
                    "warn" if moved else "error" if reason == "error"
                    else "warn" if reason in ("cancelled", "timeout", "length") else "success",
                    reason=reason, duration_ms=duration, exec_steps=exec_steps,
                    output_tokens=output_tokens, tokens_per_second=None,
                    retries=empty_retries + tool_retries,
                    **({"web_searches": search_uses} if search_uses else {}), **moved)

            if err is None:
                close()
            else:
                # Whether the client gets this error is decided by _run_request (failover, a wait, a recheck): it writes
                # turn.error and turn.end once it knows (audit A02-06: 304 red events in requests that ended with 200).
                holder["_close_turn"] = close
            out.put(None)
