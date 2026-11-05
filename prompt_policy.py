"""User control over the system prompt the model sees (prompts.json).

Contract-first skeleton committed by the coordinator; the full design is in
docs/prompt-control-plan.md (section 4 and the contract clarifications in 4.3). Without
a prompts file every request takes the existing path unchanged: PromptStore.plan()
returns a legacy Plan (profile None) and nothing here touches the request.

Signatures and dataclass fields are a contract between work packages: change them
only through the coordinator.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
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
# The revision a conversation is pinned to while no prompts file is loaded (plan 4.3): when a file
# appears, conversations that started without one stay on the legacy path until unpinned.
LEGACY_REV = 0

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
RELAY_TEXT_NAMES = ("role", "native_role", "structured", "reminder", "codex_base")
# Keys of codex.models.<glob> (plan 4.3): what /codex/models may override per model besides codex_base.
CODEX_OVERRIDE_KEYS = ("persistent_instructions", "include_skills_usage_instructions",
                       "include_plugin_usage_instructions", "include_apps_usage_instructions")
# A profile name is also a model id suffix (<model>@<profile>), so it is kept to these characters.
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
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


def _lower_headers(headers) -> dict:
    return {str(key).lower(): value for key, value in (headers or {}).items()}


def prompt_client(identity: dict, headers: dict) -> str:
    """The client family used by rules: one of CLIENTS.

    It never changes identity["client"] or client_context.request_kind (plan I1).
    Coordinator baseline: an explicit x-proxi-client naming a family, then the family
    client_context.describe() recognised, then Codex by its originator / user-agent prefix.
    P1 owns this function: it verifies the Codex signals against the codex-rs sources,
    adds Harness when it is distinguishable and pins every branch with tests.
    """
    headers = _lower_headers(headers)
    explicit = str(headers.get("x-proxi-client") or "").strip().lower()
    if explicit in CLIENTS:
        return explicit
    client = str((identity or {}).get("client") or "").strip().lower()
    if client in CLIENTS:
        return client
    originator = str(headers.get("originator") or "").strip().lower()
    agent = str(headers.get("user-agent") or "").strip().lower()
    if originator.startswith("codex") or agent.startswith("codex"):
        return "codex"
    return "unknown"


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

    @classmethod
    def from_request(cls, parsed, identity: dict, headers: dict, dialect: str | None = None) -> "PlanInput":
        """The one mapping from a parsed request to the matching input, shared by the runtime and preview.

        parsed.requested_model (set by validate, P6) is the client's model name without the
        @profile and #effort suffixes and before alias resolution; it falls back to parsed.model.
        parsed.virtual_profile (P6) is the @profile suffix or None. parsed.prompt_purpose (P2) is
        the prompt-layer purpose, e.g. "client-service", while parsed.request_purpose stays what
        it is today (plan 4.3); it falls back to parsed.request_purpose.
        """
        headers = _lower_headers(headers)
        identity = identity or {}
        model = str(getattr(parsed, "model", "") or "")
        header_profile = str(headers.get("x-proxi-profile") or "").strip()
        return cls(
            dialect=str(dialect or getattr(parsed, "dialect", "") or ""),
            client=prompt_client(identity, headers),
            agent=str(identity.get("agent") or ""),
            purpose=str(getattr(parsed, "prompt_purpose", "") or getattr(parsed, "request_purpose", "") or ""),
            request_class=str(identity.get("request_class") or ""),
            model=model,
            requested_model=str(getattr(parsed, "requested_model", "") or model),
            virtual_profile=getattr(parsed, "virtual_profile", None) or None,
            header_profile=header_profile or None,
            tools=bool(getattr(parsed, "tools", None)),
            structured=bool(getattr(parsed, "structured_output", None)),
            has_client_system=bool((getattr(parsed, "system", "") or "").strip()),
            conversation_key=getattr(parsed, "conversation_key", None) or None,
        )


@dataclass(frozen=True)
class Part:
    """One piece of the composed head with the place it came from (journal and preview).

    compose_head keeps head == "".join(part.text for part in parts): template literals and
    separators are "literal" parts, so offsets follow from the order (see part_spans).
    """
    source: str
    text: str


def sha8(text: str) -> str:
    """Short stable fingerprint used by the journal, preview and recent heads."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def part_spans(parts) -> list[dict]:
    """[{"source", "start", "end", "chars", "sha8"}] for the parts of one head, offsets in characters."""
    spans, offset = [], 0
    for part in parts:
        size = len(part.text)
        spans.append({"source": part.source, "start": offset, "end": offset + size, "chars": size,
                      "sha8": sha8(part.text)})
        offset += size
    return spans


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
        variable set, keeps a Claude Code billing header line first (plan I5: head starts with
        that line and "\\n", which direct.build_body splits into its own block) and appends the
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
        """The normalized profile: KeyError for an unknown name.

        Every section is present with its default (history.mid_system "keep", empty
        user_blocks / tools.edits / client_system.edits, generation {} with only non-null
        values, publish_as_model False); every text is a literal string ({"file": ...} read,
        {{text:<name>}} expanded inside edit "text" / "with" fields, but not inside "head",
        which compose_head renders). The built-in passthrough profile is included.
        """
        raise NotImplementedError

    def profile_names(self) -> list[str]:
        """Built-in and document profiles, in a stable order (built-ins first, then document order)."""
        raise NotImplementedError

    def relay_text(self, name: str) -> str | None:
        """The user's text for relay_texts[name] (one of RELAY_TEXT_NAMES except codex_base), None = built-in."""
        raise NotImplementedError

    def regex(self, pattern: str, flags: str = "") -> re.Pattern:
        """The pattern compiled when the revision was compiled (plan I11); flags is a subset of "ims"."""
        raise NotImplementedError

    def codex_base(self, model: str) -> str | None:
        """The base instructions /codex/models publishes for model, or None for the built-in text."""
        raise NotImplementedError

    def codex_overrides(self, model: str) -> dict:
        """codex.models overrides for model: the first matching glob in document order, {} when none."""
        raise NotImplementedError


def compile_document(doc: dict, base_dir: pathlib.Path, *, rev: int) -> Snapshot:
    """Validate and compile a prompts document; raises PromptConfigError."""
    raise NotImplementedError


def structured_contract(snapshot: Snapshot | None, output_spec: dict | None, *, tools: bool, tool_choice) -> str:
    """The structured-output contract for a head (plan I10), shared by the runtime and preview.

    "" without output_spec. The default text is structured.instructions(output_spec, tools=...,
    tool_choice=...) called, never copied; relay_texts.structured, when set, wraps it with
    {{default}}, {{schema}} and {{schema_name}}. P1 implements it; until then callers fall back
    to structured.instructions on NotImplementedError.
    """
    raise NotImplementedError


class PromptStore:
    """Revisions, conversation pins, live reload and the in-memory record of recent heads.

    The file is explicit path > cfg["prompts_file"] > DEFAULT_FILE; a relative path is resolved
    against the folder of cfg["_config_path"] when that is set, otherwise against the working
    directory. The constructor loads the file when it exists; maybe_reload() also picks up a
    file that appears, changes or disappears later (plan 4.3).
    """

    def __init__(self, cfg: dict, path: str | None = None, clock=time.monotonic):
        self.cfg = cfg
        configured = pathlib.Path(path or cfg.get("prompts_file") or DEFAULT_FILE)
        if not configured.is_absolute() and cfg.get("_config_path"):
            configured = pathlib.Path(cfg["_config_path"]).parent / configured
        self.path = configured
        self.clock = clock
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def plan(self, inp: PlanInput) -> Plan:
        """Select the rule and profile for a request, honouring the conversation pin (plan I3).

        Refusals are apierr.ApiError raised before any upstream call (plan 4.3): 400 with
        param "x-proxi-profile" when $header names a missing profile, 404 not_found_error with
        param "model" when <model>@<profile> names a missing profile (also with no file loaded).
        """
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

    def errors(self) -> list[dict]:
        """Problems of the file on disk that kept the previous revision: [{"message", "param", "ts"}], [] when valid."""
        return []

    def pins(self) -> list[dict]:
        return []

    def unpin(self, conversation_key: str | None = None) -> int:
        return 0

    def record(self, entry: dict) -> None:
        """Keep one non-legacy request in the in-memory ring (RECENT_LIMIT entries, newest first in recent()).

        entry may carry "request": {"body", "dialect", "headers"} for preview without a pasted
        body (plan 4.3); the ring keeps it only for the newest few entries and never journals it.
        """
        if self.snapshot() is None:
            return
        raise NotImplementedError

    def recent(self, limit: int = 50) -> list[dict]:
        return []

    def request(self, request_id: str | None = None) -> dict | None:
        """The kept {"body", "dialect", "headers"} of the newest recorded request (or of request_id), None if not kept."""
        return None

    def preview(self, body: dict, dialect: str, headers: dict, document: dict | None = None) -> dict:
        raise NotImplementedError
