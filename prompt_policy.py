"""User control over the system prompt the model sees (prompts.json).

Contract-first skeleton committed by the coordinator; the full design is in
docs/prompt-control-plan.md. Without a prompts file every request takes the
existing path unchanged: PromptStore.plan() returns a legacy Plan (profile None)
and nothing here touches the request.

Signatures and dataclass fields are a contract between work packages: change them
only through the coordinator.
"""
from __future__ import annotations

import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_FILE = "prompts.json"
DOCUMENT_VERSION = 1
MAX_TEXT_BYTES = 256 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
SNAPSHOT_HISTORY = 32
RECENT_LIMIT = 100
RELOAD_INTERVAL = 1.0

CLIENTS = ("opencode", "claude-code", "claude-desktop", "codex", "harness", "unknown")
VARIABLES = ("client_system", "client_system_raw", "role", "native_role", "relay_task", "local_exec",
             "project_context", "exec_reminder", "structured", "model", "client", "agent", "purpose")
# Variables that expand only for a request the relay owns (orchestrator client_owns_instructions False).
RELAY_TASK_VARIABLES = ("relay_task", "local_exec", "project_context", "exec_reminder")
SYSTEM_EDIT_OPS = ("remove_section", "keep_sections", "remove_tag", "keep_tag_inner", "remove_between",
                   "remove_regex", "replace_regex", "replace_text", "prepend", "append")
MID_SYSTEM_MODES = ("keep", "wrap", "plain", "drop")
USER_BLOCK_OPS = ("drop_tag", "remove_regex", "replace_text")
TOOL_EDIT_OPS = ("append", "prepend", "replace_text", "replace_regex", "set")
CONDITIONS = ("client", "agent", "purpose", "request_class", "model", "tools", "structured",
              "has_client_system", "dialect", "header_profile", "virtual_profile")
SPECIAL_USES = ("$header", "$virtual", "$legacy")
BUILTIN_PROFILES = {"passthrough": {"description": "the client's system prompt, unchanged",
                                    "head": "{{client_system}}"}}
# Service requests keep their own instructions unless the user disables protection (plan I6).
PROTECTED_RULES = (
    {"name": "protected: service requests", "if": {"purpose": ["session-title", "auto-mode-check",
                                                               "connection-probe", "client-service"]},
     "use": "passthrough"},
    {"name": "protected: Claude Code compaction", "if": {"request_class": ["compaction", "auxiliary"]},
     "use": "passthrough"},
)


class PromptConfigError(ValueError):
    """An invalid prompts document; `path` names the offending field, e.g. profiles.mine.head."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


@dataclass(frozen=True)
class PlanInput:
    """What rule matching may look at; built once per request from pass-1 parsing."""
    dialect: str
    client: str
    agent: str
    purpose: str
    request_class: str
    model: str
    requested_model: str
    virtual_profile: str | None
    header_profile: str | None
    tools: bool
    structured: bool
    has_client_system: bool
    conversation_key: str | None


@dataclass(frozen=True)
class Part:
    """One piece of the composed head with the place it came from (journal and preview)."""
    source: str
    text: str


class HeadSources(Protocol):
    """Relay-owned texts, computed lazily by the runtime (orchestrator) only when a template names them."""
    client_owned: bool

    def relay_role(self) -> str: ...

    def native_role(self) -> str: ...

    def relay_task(self) -> str: ...

    def local_exec(self) -> str: ...

    def project_context(self) -> str: ...

    def exec_reminder(self) -> str: ...

    def structured(self) -> str: ...


@dataclass(frozen=True)
class Plan:
    """The prompt decision for one request.

    profile None means the legacy path: the orchestrator runs its existing head code and
    nothing else in this module applies. body_edits says the profile rewrites the request
    body (history or tools) before parsing, see prompt_body.rewrite.
    """
    rev: int | None = None
    profile: str | None = None
    rule: str | None = None
    pinned: bool = False
    protected: bool = False
    body_edits: bool = False
    generation: dict = field(default_factory=dict)
    snapshot: "Snapshot | None" = None

    @property
    def legacy(self) -> bool:
        return self.profile is None

    def compose_head(self, client_system: str, sources: HeadSources) -> tuple[str, list[Part], list[str]]:
        """Return (head, parts, warnings) for this plan's profile.

        Applies the profile's client_system.edits, renders its head template with the closed
        variable set, keeps a Claude Code billing header line first (plan I5) and appends the
        structured contract when the template does not place {{structured}} (plan I10).
        """
        raise NotImplementedError


class Snapshot:
    """An immutable compiled revision of the prompts document."""

    def __init__(self, rev: int, document: dict, source_path: str | None = None):
        self.rev = rev
        self.document = document
        self.source_path = source_path

    def profile(self, name: str) -> dict:
        raise NotImplementedError

    def codex_base(self, model: str) -> str | None:
        """The base instructions /codex/models publishes for model, or None for the built-in text."""
        raise NotImplementedError


def compile_document(doc: dict, base_dir: pathlib.Path, *, rev: int) -> Snapshot:
    """Validate and compile a prompts document; raises PromptConfigError."""
    raise NotImplementedError


def prompt_client(identity: dict, headers: dict) -> str:
    """The client family used by rules: one of CLIENTS.

    It never changes identity["client"] or client_context.request_kind (plan I1).
    """
    raise NotImplementedError


class PromptStore:
    """Revisions, conversation pins, live reload and the in-memory record of recent heads."""

    def __init__(self, cfg: dict, path: str | None = None, clock=time.monotonic):
        self.cfg = cfg
        configured = path or cfg.get("prompts_file") or DEFAULT_FILE
        self.path = pathlib.Path(configured)
        self.clock = clock
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def plan(self, inp: PlanInput) -> Plan:
        """Select the rule and profile for a request, honouring the conversation pin (plan I3)."""
        if self.snapshot() is None:
            return Plan()
        raise NotImplementedError

    def maybe_reload(self) -> None:
        """Recompile when the prompts file or a referenced text changed; stat at most once per RELOAD_INTERVAL."""
        if self.snapshot() is None:
            return
        raise NotImplementedError

    def save(self, document: dict, base_rev: int | None) -> Snapshot:
        raise NotImplementedError

    def pins(self) -> list[dict]:
        return []

    def unpin(self, conversation_key: str | None = None) -> int:
        return 0

    def record(self, entry: dict) -> None:
        if self.snapshot() is None:
            return
        raise NotImplementedError

    def recent(self, limit: int = 50) -> list[dict]:
        return []

    def preview(self, body: dict, dialect: str, headers: dict, document: dict | None = None) -> dict:
        raise NotImplementedError
