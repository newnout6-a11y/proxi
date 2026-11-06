"""User control over the system prompt the model sees (prompts.json).

A prompts document names texts, profiles and rules. A rule picks a profile for a request; a profile says how the head (the
system message sent to Retool) is composed from the client's own system prompt, relay texts and the user's texts, and which
edits apply to the client's system prompt, to the history and to the tool descriptions. The design and the contract are in
docs/prompt-control-plan.md (section 4 and the clarifications in 4.3). Without a prompts file, or when no rule matches and
default_profile is null, PromptStore.plan() returns a legacy Plan (profile None) and nothing here touches the request.

Snapshot.profile(name) is the normalized profile every package reads: all sections are present with their defaults, every text
is a literal string, {{text:<name>}} is expanded inside the "text" and "with" fields of the edits (not inside "head", which
compose_head renders) and a "with" of replace_regex is a ready re.sub template. Edits come in these shapes:

    client_system.edits  remove_section {heading} | keep_sections {headings} | remove_tag {tag} | keep_tag_inner {tag} |
                         remove_between {start, end, inclusive=False} | remove_regex {pattern, flags=""} |
                         replace_regex {pattern, flags="", with} | replace_text {find, with, all=True} |
                         prepend {text} | append {text}
    history.user_blocks  drop_tag {tag, contains=None, pattern=None, flags=""} | remove_regex {pattern, flags=""} |
                         replace_text {find, with, all=True}
    tools.edits          {tool, hide=False, op, ...} with op append | prepend | set {text} | replace_text {find, with, all} |
                         replace_regex {pattern, flags="", with}; "tool" is a glob or a list of globs; a hidden tool is
                         just {tool, hide: True}

A tag block (remove_tag, keep_tag_inner) is one whose opening tag begins a line, as <env> and <available_skills> do in a
prompt: a tag mentioned inside a sentence is not a block. Every pattern of every section is checked and compiled with the
revision, and every "with" of replace_regex is checked against its pattern.

Limits (review R1): text files are read only from prompts/ next to the prompts file (.md and .txt). A pattern has the syntax of
`re` but is a pattern of the `regex` module, which runs it with the GIL released and under limits (REGEX_WORK characters scanned
per call; as an emergency stop REGEX_TIMEOUT per operation and REGEX_BUDGET seconds per call): an edit that goes over them is
not applied (a warning of the head, `skipped: "timeout"` in
the report of the body). The head is at most MAX_HEAD_BYTES (a document that cannot stay under it is refused, a head that
goes over it is a 413); an edit may add at most MAX_EDIT_GROWTH characters to its text and the history edits (and, apart, the
tool edits) of one request at most MAX_BODY_GROWTH in all (`skipped: "too_large"`).

Pins (review R3-02): a conversation is held on the revision it started on (plan I3), and that holds across a restart of the
relay when the stored conversations are on. The pins are written to <conversation_state_dir>/prompt-pins/ (pins.json: one record
per pin under the sha256 of its (conversation key, purpose), never the key; revisions/<sha256>.json: the literal document of
every revision a pin names, see Snapshot.literal_document and Snapshot.fingerprint). A restart reads them as dormant pins, and
the first request of a conversation that has one is given a live pin on the revision it started on, made again from the stored
document when the file has changed since. PromptStore.close() writes what is still waiting.

Signatures and dataclass fields are a contract between work packages: change them only through the coordinator.
"""
from __future__ import annotations

import collections
import fnmatch
import functools
import hashlib
import itertools
import json
import math
import os
import pathlib
import re
import re._constants as _sre_const
import re._parser as _sre_parse
import shutil
import stat
import tempfile
import threading
import time
import warnings as _warnings
from dataclasses import dataclass, field
from typing import Protocol

import apierr
import journal
import models
import structured

try:
    import regex as _regex
except ImportError:     # the relay still starts without it: a document that has a regular expression is refused (see _compile_pattern)
    _regex = None

DEFAULT_FILE = "prompts.json"
DOCUMENT_VERSION = 1
MAX_TEXT_BYTES = 256 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# The literal document of a revision (every {"file": ...} text replaced by what was read) is what a pin keeps on disk. A file
# that several texts name is inlined once per text, so it can be longer than the document and its files together; a literal
# document over this size is not kept (the revision has no fingerprint, and the conversations on it are not pinned past a
# restart).
MAX_REVISION_BYTES = 4 * MAX_DOCUMENT_BYTES
# A {"file": ...} text is read from this folder next to the prompts file, and only with one of these extensions.
TEXT_DIR = "prompts"
TEXT_SUFFIXES = (".md", ".txt")
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

MAX_PATTERN_CHARS = 512
# What a compiled pattern may cost: the `regex` module writes a fixed repeat count (a{1000}) out copy by copy, so nested counts
# multiply (a{1000}{1000} is a million of them, a{99999999} does not fit in memory). The estimate counts those copies.
MAX_PATTERN_WEIGHT = 50_000
# Regular expressions run in the `regex` module with a time limit (plan I11, review R1-01): Python's own `re` holds the GIL
# while it matches, so one catastrophic pattern on untrusted text (a tool result, an AGENTS.md) would freeze every thread of
# the relay. `regex` lets the other threads run while it matches (concurrent=True) and stops at the limit.
# What bounds the ordinary work of a call is not time but work: every operation is charged the length of the text it runs on, and
# a call may scan REGEX_WORK characters in all. The same body then costs the same on every machine and under any load: the
# wall-clock budget of 1 s alone skipped edits whenever the relay was busy, so the same request had another head or other tool
# descriptions from one turn to the next, and with them another scope of the signed history (a5, G4.1). The time limits are
# the emergency stop for a pattern that backtracks without end, and are loose enough for a loaded machine not to reach them.
REGEX_WORK = 250_000_000  # characters all the regular expressions of one call (compose_head, rewrite) may scan together
REGEX_TIMEOUT = 1.0       # seconds one search or replacement may take
REGEX_BUDGET = 8.0        # seconds all the regular expressions of one call may take together
# How large the texts that edits make may be (review R1-03). The head, in UTF-8 bytes, is what every request sends to Retool;
# an edit may make the text it edits at most MAX_EDIT_GROWTH characters longer, and the edits of the history (and, apart from
# them, those of the tools) of one request together at most MAX_BODY_GROWTH characters: replacing " " with a long text, or a
# mid-conversation {{text}} wrapper, would otherwise multiply a small request by thousands.
MAX_HEAD_BYTES = 1024 * 1024
MAX_EDIT_GROWTH = 512 * 1024
MAX_BODY_GROWTH = 4 * 1024 * 1024
# A remove_regex of the client's system prompt keeps a list of the places it matched; it may match in this many.
MAX_MATCHES = 200_000
MAX_GLOB_CHARS = 200
MAX_NAME_CHARS = 200
MAX_EDITS = 200
# What the edits may expand to in total: {{text:<name>}} copied into many fields must not multiply a small document.
MAX_EXPANDED_BYTES = 16 * 1024 * 1024
REGEX_FLAGS = "ims"
VERBOSITY_LEVELS = ("low", "medium", "high")
DIALECTS = ("chat", "responses", "anthropic")
# Only the newest records keep the request body that preview(None) can replay.
RECENT_REQUESTS_KEPT = 10
# A pin protects the cache and the signed history of a live conversation. The pool closes an idle conversation after
# 15 minutes (conversation_idle_timeout), so a pin that was not used for 6 hours protects nothing; the count bound is
# far above max_conversations.
MAX_PINS = 4096
PIN_IDLE_SECONDS = 6 * 3600
# Pins that survive a restart (review R3-02) are kept next to the stored conversations (cfg["conversation_state_dir"]), in this
# folder: PINS_FILE and, in REVISIONS_DIR, one literal document for each revision a pin names. The pins file is written at most
# once per PINS_WRITE_INTERVAL seconds, and a pin that was used again is written down as touched only when the stored touch is
# PIN_TOUCH_INTERVAL old: a pin lives for hours, so the touch is a rough time. A new pin, a pin that moves to another revision
# and an unpin are written at the next opportunity (an unpin at once).
PINS_DIR = "prompt-pins"
PINS_FILE = "pins.json"
REVISIONS_DIR = "revisions"
PINS_FORMAT = 1
PINS_WRITE_INTERVAL = 1.0
PIN_TOUCH_INTERVAL = 60.0
MAX_PINS_BYTES = 8 * 1024 * 1024
STALE_TEMP_SECONDS = 600.0
# Saving replaces the file; a sharing violation is retried this many times, REPLACE_PAUSE seconds apart.
REPLACE_ATTEMPTS = 4
REPLACE_PAUSE = 0.05
BILLING_PREFIX = "x-anthropic-billing-header:"
DEFAULT_REMINDER = "<system-reminder>\n{{text}}\n</system-reminder>"
DEFAULT_RULE_NAME = "default profile"
VIRTUAL_RULE_NAME = "virtual model"

TEXT_NAME = re.compile(r"\w[\w.-]{0,63}")
TAG_NAME = re.compile(r"[A-Za-z_][\w.:-]{0,63}")
_HEADING = re.compile(r"#{1,6}[ \t]+\S.*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX8 = re.compile(r"[0-9a-f]{8}")
_BOOL_CONDITIONS = ("tools", "structured", "has_client_system")


class PromptConfigError(ValueError):
    """An invalid prompts document; `path` names the offending field, e.g. profiles.mine.head.

    `message` is the full text for the author of the document (the admin shows it); `brief` says the same without quoting
    anything the document says (a placeholder, a pattern, a name), and is what the journal gets (plan I7: no prompt text in
    journal.jsonl). It is the message itself unless the message quotes the document.
    """

    def __init__(self, path: str, message: str, brief: str | None = None):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message
        self.brief = message if brief is None else brief


def _err(path, message, brief=None):
    raise PromptConfigError(path, message, brief)


def _path(parent, key):
    return "%s.%s" % (parent, key) if parent else str(key)


def _clip(value, limit=80):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _lower_headers(headers) -> dict:
    return {str(key).lower(): value for key, value in (headers or {}).items()}


# Spellings of a client family that people put into x-proxi-client. Anything else names no family.
_FAMILY_ALIASES = {"claude-cli": "claude-code", "deepseek-harness": "harness", "dsh": "harness"}


def _family(value) -> str:
    name = re.sub(r"[\s_]+", "-", str(value or "").strip().lower())
    if name in CLIENTS:
        return name
    if name.startswith("codex"):
        return "codex"
    return _FAMILY_ALIASES.get(name, "")


def prompt_client(identity: dict, headers: dict) -> str:
    """The client family used by rules: one of CLIENTS.

    It never changes identity["client"] or client_context.request_kind (plan I1). The first signal that names a family wins:
    1. an explicit x-proxi-client header (a family name, or a spelling of one: "Claude Code", "codex_cli_rs", "dsh");
    2. the client client_context.describe() recognised (OpenCode, Claude Code, Claude Desktop);
    3. Codex, by what codex-rs sends (rust-v0.164.0): the `originator` header (codex_cli_rs, codex-tui, codex_exec,
       codex_vscode, codex_desktop, codex_sdk_ts, "Codex Desktop", ...: every first-party value starts with "codex"), a
       User-Agent that begins with that originator (`codex_cli_rs/0.164.0 (Windows 10.0; x86_64) ...`) or any x-codex-*
       header (x-codex-window-id goes with every Responses request, even when a wrapper overrides the originator);
    4. DeepSeek Harness, by the User-Agent every provider request of the harness carries, `deepseek-harness/<version>
       (+https://github.com/deepseek-ai/deepseek-harness)` (packages/llm/llm/src/attribution.ts);
    5. "unknown".
    """
    headers = _lower_headers(headers)
    explicit = _family(headers.get("x-proxi-client"))
    if explicit:
        return explicit
    client = _family((identity or {}).get("client"))
    if client:
        return client
    originator = str(headers.get("originator") or "").strip().lower()
    agent = str(headers.get("user-agent") or "").strip().lower()
    if originator.startswith("codex") or agent.startswith("codex") or any(
            name.startswith("x-codex-") for name in headers):
        return "codex"
    if agent.startswith("deepseek-harness/"):
        return "harness"
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


# -- templates ------------------------------------------------------------------------------------------------------

_TEMPLATE_TOKEN = re.compile(r"\\\{\{|\{\{([^{}\n]*)\}\}")


class _Template:
    """A parsed {{...}} template: tokens are ("lit", text), ("var", name) or ("text", name)."""
    __slots__ = ("tokens", "variables")

    def __init__(self, tokens):
        merged = []
        for kind, value in tokens:
            if kind == "lit":
                if not value:
                    continue
                if merged and merged[-1][0] == "lit":
                    merged[-1] = ("lit", merged[-1][1] + value)
                    continue
            merged.append((kind, value))
        self.tokens = tuple(merged)
        self.variables = frozenset(value for kind, value in merged if kind == "var")


def _parse_template(source, path, *, variables, texts=None):
    """Tokens of `source`: `\\{{` is a literal {{, {{name}} one of `variables`, {{text:name}} one of `texts`."""
    tokens, last = [], 0

    def literal(chunk, offset):
        if "{{" in chunk:
            _err(path, "'{{' at character %d does not start a variable (write \\{{ for a literal)"
                 % (offset + chunk.index("{{")))
        tokens.append(("lit", chunk))

    for match in _TEMPLATE_TOKEN.finditer(source):
        literal(source[last:match.start()], last)
        last = match.end()
        if match.group(1) is None:
            tokens.append(("lit", "{{"))
            continue
        name = match.group(1).strip()
        where = "at character %d" % match.start()
        if not name:
            _err(path, "an empty {{}} %s" % where)
        if name.startswith("text:"):
            reference = name[5:].strip()
            if texts is None:
                _err(path, "{{text:...}} is not available here (%s)" % where)
            if reference not in texts:
                _err(path, "unknown text %r in {{%s}} %s (define it under texts)" % (_clip(reference), _clip(name), where),
                     "unknown text in a {{text:...}} placeholder %s" % where)
            tokens.append(("text", reference))
        elif name in variables:
            tokens.append(("var", name))
        else:
            known = ", ".join(variables) if variables else "none"
            _err(path, "unknown variable {{%s}} %s (known here: %s)" % (_clip(name), where, known),
                 "unknown variable in a placeholder %s" % where)
    literal(source[last:], last)
    return _Template(tokens)


def _fill(template, values):
    return "".join(value if kind == "lit" else values.get(value, "") for kind, value in template.tokens)


def _collapse_segment(segment, at_start, at_end):
    """The literal text between two non-empty values, with the blank lines an empty substitution left behind tidied.

    Only a run of newlines that touches an empty substitution is changed: three or more become two, and a run at the very
    start or end of the head disappears. Newlines elsewhere in the template are the user's own text.
    """
    text, gaps = "", []
    for kind, _source, value in segment:
        if kind == "gap":
            gaps.append(len(text))
        else:
            text += value
    if not gaps:
        return text
    runs, seen = [], set()
    for gap in gaps:
        begin = gap
        while begin > 0 and text[begin - 1] == "\n":
            begin -= 1
        end = gap
        while end < len(text) and text[end] == "\n":
            end += 1
        if end > begin and (begin, end) not in seen:
            seen.add((begin, end))
            runs.append((begin, end))
    out, position = [], 0
    for begin, end in runs:
        out.append(text[position:begin])
        size = end - begin
        if (begin == 0 and at_start) or (end == len(text) and at_end):
            size = 0
        elif size >= 3:
            size = 2
        out.append("\n" * size)
        position = end
    out.append(text[position:])
    return "".join(out)


def _render(template, expand):
    """[(source, text)] for a template; expand(kind, name) -> (source, text) resolves a variable or a text."""
    items = []
    for kind, value in template.tokens:
        if kind == "lit":
            items.append(("lit", "literal", value))
        else:
            source, text = expand(kind, value)
            items.append(("val" if text else "gap", source, text))
    pieces, index, count = [], 0, len(items)
    while index < count:
        if items[index][0] == "val":
            pieces.append((items[index][1], items[index][2]))
            index += 1
            continue
        end = index
        while end < count and items[end][0] != "val":
            end += 1
        pieces.append(("literal", _collapse_segment(items[index:end], index == 0, end == count)))
        index = end
    return pieces


# -- regular expressions: a bounded language, checked when a revision is compiled (plan I11) -----------------------------

_ASCII = tuple(range(128))
# Stand-ins for the parts of Unicode a pattern can tell apart: Latin and Cyrillic letters, an Arabic digit, no-break and em
# spaces, line separators, CJK, an emoji, a combining mark, a BOM.
_EXTRA = (0x85, 0xA0, 0xA9, 0xB5, 0xE9, 0x301, 0x416, 0x663, 0x2003, 0x2028, 0x3000, 0x4E2D, 0xFEFF, 0x1F600)
# A repeat bound this big is as good as unbounded on a text of ordinary size; a loop that runs more often than _LOOP_REPEAT
# times can multiply an ambiguity into a blow-up.
_LARGE_REPEAT = 1000
_LOOP_REPEAT = 8
_REPEATS = (_sre_const.MAX_REPEAT, _sre_const.MIN_REPEAT)
_WARN_LOCK = threading.Lock()


class _RegexHazard(Exception):
    pass


def _items(node):
    return node.data if hasattr(node, "data") else node


@functools.lru_cache(maxsize=4096)
def _case_variants(point):
    variants = set()
    for variant in (chr(point).lower(), chr(point).upper(), chr(point).swapcase()):
        if len(variant) == 1:
            variants.add(ord(variant))
    return frozenset(variants)


def _collect_points(node, points):
    for op, av in _items(node):
        if op in (_sre_const.LITERAL, _sre_const.NOT_LITERAL):
            points.add(av)
        elif op == _sre_const.RANGE:
            points.update(av)
        elif op == _sre_const.IN:
            _collect_points(av, points)
        elif op == _sre_const.SUBPATTERN:
            _collect_points(av[3], points)
        elif op in (_sre_const.MAX_REPEAT, _sre_const.MIN_REPEAT, _sre_const.POSSESSIVE_REPEAT):
            _collect_points(av[2], points)
        elif op == _sre_const.BRANCH:
            for alternative in av[1]:
                _collect_points(alternative, points)
        elif op == _sre_const.ATOMIC_GROUP:
            _collect_points(av, points)
        elif op in (_sre_const.ASSERT, _sre_const.ASSERT_NOT):
            _collect_points(av[1], points)
        elif op == _sre_const.GROUPREF_EXISTS:
            _collect_points(av[1], points)
            if av[2]:
                _collect_points(av[2], points)


class _Shape:
    """First-character sets of a parsed pattern over a sample alphabet, and the checks that need them.

    The checks look for what makes a backtracking engine blow up: an unbounded (or long) repeat inside another repeat whose
    iterations can start with the character the inner repeat can end on, alternatives that overlap inside a repeat, a repeat
    of something that can match nothing, and neighbouring unbounded repeats over the same characters. Possessive repeats and
    atomic groups never backtrack into themselves, so they are opaque to an enclosing loop.
    """

    def __init__(self, tree):
        points = set()
        _collect_points(tree, points)
        sample = set(_ASCII) | set(_EXTRA)
        for point in points:
            for near in (point - 1, point, point + 1):
                if 0 <= near <= 0x10FFFF:
                    sample.add(near)
        for point in list(sample):
            sample |= _case_variants(point)
        self.alphabet = frozenset(sample)
        self.empty = frozenset()

    def fold(self, chars):
        return frozenset(chars | {variant for point in chars for variant in _case_variants(point)})

    def category(self, kind, point):
        char = chr(point)
        if kind in (_sre_const.CATEGORY_DIGIT, _sre_const.CATEGORY_UNI_DIGIT):
            return char.isdecimal()
        if kind in (_sre_const.CATEGORY_NOT_DIGIT, _sre_const.CATEGORY_UNI_NOT_DIGIT):
            return not char.isdecimal()
        if kind in (_sre_const.CATEGORY_SPACE, _sre_const.CATEGORY_UNI_SPACE):
            return char.isspace()
        if kind in (_sre_const.CATEGORY_NOT_SPACE, _sre_const.CATEGORY_UNI_NOT_SPACE):
            return not char.isspace()
        if kind in (_sre_const.CATEGORY_WORD, _sre_const.CATEGORY_UNI_WORD):
            return char.isalnum() or char == "_"
        if kind in (_sre_const.CATEGORY_NOT_WORD, _sre_const.CATEGORY_UNI_NOT_WORD):
            return not (char.isalnum() or char == "_")
        if kind in (_sre_const.CATEGORY_LINEBREAK, _sre_const.CATEGORY_UNI_LINEBREAK):
            return point == 10
        if kind in (_sre_const.CATEGORY_NOT_LINEBREAK, _sre_const.CATEGORY_UNI_NOT_LINEBREAK):
            return point != 10
        return True

    def charset(self, items, flags):
        negate, chars = False, set()
        for op, av in items:
            if op == _sre_const.NEGATE:
                negate = True
            elif op == _sre_const.LITERAL:
                chars.add(av)
            elif op == _sre_const.RANGE:
                low, high = av
                chars.update(point for point in self.alphabet if low <= point <= high)
            elif op == _sre_const.CATEGORY:
                chars.update(point for point in self.alphabet if self.category(av, point))
            else:
                return self.alphabet
        found = self.fold(chars) if flags & _sre_const.SRE_FLAG_IGNORECASE else frozenset(chars)
        return self.alphabet - found if negate else found

    def first(self, node, flags, reverse=False):
        """(characters a match of node can start with, whether it can match the empty string).

        With reverse the characters a match can end with.
        """
        chars = set()
        items = _items(node)
        for op, av in (reversed(items) if reverse else items):
            found, empty = self.item_first(op, av, flags, reverse)
            chars |= found
            if not empty:
                return frozenset(chars), False
        return frozenset(chars), True

    def item_first(self, op, av, flags, reverse=False):
        ignore = flags & _sre_const.SRE_FLAG_IGNORECASE
        if op == _sre_const.LITERAL:
            return (self.fold({av}) if ignore else frozenset({av})), False
        if op == _sre_const.NOT_LITERAL:
            return self.alphabet - (self.fold({av}) if ignore else frozenset({av})), False
        if op == _sre_const.ANY:
            return (self.alphabet if flags & _sre_const.SRE_FLAG_DOTALL else self.alphabet - {10}), False
        if op == _sre_const.IN:
            return self.charset(av, flags), False
        if op in (_sre_const.AT, _sre_const.ASSERT, _sre_const.ASSERT_NOT):
            return self.empty, True
        if op == _sre_const.GROUPREF:
            return self.alphabet, True
        if op == _sre_const.SUBPATTERN:
            _group, added, removed, body = av
            return self.first(body, (flags | added) & ~removed, reverse)
        if op == _sre_const.BRANCH:
            chars, nullable = set(), False
            for alternative in av[1]:
                found, empty = self.first(alternative, flags, reverse)
                chars |= found
                nullable = nullable or empty
            return frozenset(chars), nullable
        if op in (_sre_const.MAX_REPEAT, _sre_const.MIN_REPEAT, _sre_const.POSSESSIVE_REPEAT):
            low, _high, body = av
            found, empty = self.first(body, flags, reverse)
            return found, low == 0 or empty
        if op == _sre_const.ATOMIC_GROUP:
            return self.first(av, flags, reverse)
        if op == _sre_const.GROUPREF_EXISTS:
            _group, yes, no = av
            chars, nullable = self.first(yes, flags, reverse)
            if no:
                other, empty = self.first(no, flags, reverse)
                return chars | other, nullable or empty
            return chars, True
        return self.alphabet, True

    @staticmethod
    def unbounded(op, av):
        return op in _REPEATS and av[1] >= _LARGE_REPEAT

    def wide(self, op, av):
        """True when one match of the item can be arbitrarily long: an unbounded repeat inside it, not an atomic one."""
        if op in _REPEATS:
            return av[1] >= _LARGE_REPEAT or self.wide_in(av[2])
        if op == _sre_const.SUBPATTERN:
            return self.wide_in(av[3])
        if op == _sre_const.BRANCH:
            return any(self.wide_in(alternative) for alternative in av[1])
        return False

    def wide_in(self, node):
        return any(self.wide(op, av) for op, av in _items(node))

    def scan(self, node, follow, flags, in_loop, big_loop):
        """Raise _RegexHazard for the first dangerous construct in node.

        `follow` is what can come right after node inside the enclosing loop (its next iteration and whatever follows it).
        """
        items = list(_items(node))
        count = len(items)
        firsts = [self.item_first(op, av, flags) for op, av in items]
        suffix = [(self.empty, True)] * (count + 1)
        for index in range(count - 1, -1, -1):
            chars, nullable = firsts[index]
            tail, tail_nullable = suffix[index + 1]
            suffix[index] = (chars | tail, tail_nullable) if nullable else (chars, False)
        self.adjacent(items, firsts, flags)
        for index, (op, av) in enumerate(items):
            tail, tail_nullable = suffix[index + 1]
            after = (tail | follow) if tail_nullable else tail
            if op in _REPEATS:
                low, high, body = av
                found, empty = self.first(body, flags)
                loops = high > 1
                if loops and empty and high > _LOOP_REPEAT:
                    raise _RegexHazard("a repeated group can match the empty string")
                if in_loop and low != high and (found & after) and (big_loop or high >= _LARGE_REPEAT):
                    raise _RegexHazard("a repeat inside a repeated group can end on a character the next iteration "
                                       "starts with (nested quantifiers)")
                self.scan(body, (found | after) if loops else after, flags, in_loop or loops,
                          big_loop or (loops and high > _LOOP_REPEAT))
            elif op == _sre_const.BRANCH:
                alternatives = av[1]
                if in_loop and big_loop:
                    self.alternatives(alternatives, after, flags)
                for alternative in alternatives:
                    self.scan(alternative, after, flags, in_loop, big_loop)
            elif op == _sre_const.SUBPATTERN:
                _group, added, removed, body = av
                self.scan(body, after, (flags | added) & ~removed, in_loop, big_loop)
            elif op == _sre_const.GROUPREF_EXISTS:
                for branch in (av[1], av[2]):
                    if branch:
                        self.scan(branch, after, flags, in_loop, big_loop)
            elif op in (_sre_const.ASSERT, _sre_const.ASSERT_NOT):
                self.scan(av[1], self.empty, flags, False, False)
            elif op == _sre_const.POSSESSIVE_REPEAT:
                self.scan(av[2], self.empty, flags, False, False)
            elif op == _sre_const.ATOMIC_GROUP:
                self.scan(av, self.empty, flags, False, False)

    def leading(self, node, flags, continues=False):
        """Raise _RegexHazard when the pattern starts with an unbounded repeat of almost any character that something
        follows: the engine retries it at every position of the text, so a text without a match costs time squared.

        Returns True when the pattern is anchored at the very start (nothing to check), False when a consuming item came
        first, None when only zero-width items were seen so far. `continues` says something follows node.
        """
        items = list(_items(node))
        for index, (op, av) in enumerate(items):
            after = continues or index < len(items) - 1
            if op == _sre_const.AT:
                if av == _sre_const.AT_BEGINNING_STRING or (
                        av == _sre_const.AT_BEGINNING and not flags & _sre_const.SRE_FLAG_MULTILINE):
                    return True
                continue
            if op in (_sre_const.ASSERT, _sre_const.ASSERT_NOT):
                continue
            if op in _REPEATS and self.unbounded(op, av):
                chars = self.first(av[2], flags)[0]
                if after and 10 in chars and len(chars) >= 0.9 * len(self.alphabet):
                    raise _RegexHazard("the pattern starts with an unbounded repeat of almost any character, which is "
                                       "retried at every position of the text (anchor it with ^ or \\A, or start it "
                                       "with a literal)")
                return False
            if op == _sre_const.SUBPATTERN:
                found = self.leading(av[3], (flags | av[1]) & ~av[2], after)
                if found is not None:
                    return found
                continue
            if op == _sre_const.BRANCH:
                results = [self.leading(alternative, flags, after) for alternative in av[1]]
                if all(result is True for result in results):
                    return True
                if any(result is not None for result in results):
                    return False
                continue
            return False
        return None

    def alternatives(self, alternatives, after, flags):
        seen, empties = self.empty, 0
        for alternative in alternatives:
            chars, empty = self.first(alternative, flags)
            if seen & chars:
                raise _RegexHazard("alternatives inside a repeated group can match the same text")
            seen = seen | chars
            empties += empty
        if empties > 1 or (empties and seen & after):
            raise _RegexHazard("an optional alternative inside a repeated group can match what follows it")

    def adjacent(self, items, firsts, flags):
        """Two long items side by side are ambiguous when they can match one and the same non-empty text.

        Such a text starts with a character both can start with and ends with a character both can end with.
        """
        for index, (op, av) in enumerate(items):
            if not self.wide(op, av):
                continue
            starts = self.item_first(op, av, flags)[0]
            ends = self.item_first(op, av, flags, True)[0]
            for other in range(index + 1, len(items)):
                other_op, other_av = items[other]
                if (self.wide(other_op, other_av) and starts & self.item_first(other_op, other_av, flags)[0]
                        and ends & self.item_first(other_op, other_av, flags, True)[0]):
                    raise _RegexHazard("neighbouring unbounded repeats can match the same text")
                if not firsts[other][1]:
                    break


def _regex_hazard(tree):
    """Why the parsed pattern can backtrack catastrophically, or None."""
    shape = _Shape(tree)
    try:
        shape.scan(tree, shape.empty, tree.state.flags, False, False)
        shape.leading(tree, tree.state.flags)
    except _RegexHazard as exc:
        return str(exc)
    except RecursionError:
        return "the pattern is nested too deeply to be checked"
    return None


def _normalize_flags(flags, path):
    if flags is None:
        return ""
    if not isinstance(flags, str) or any(char not in REGEX_FLAGS for char in flags):
        _err(path, "flags must use only i, m and s")
    return "".join(char for char in REGEX_FLAGS if char in flags)


def _brief_regex_error(exc, what="invalid regular expression"):
    """`what`, with the position the engine reports: the message of the engine can quote a group name or an escape."""
    position = getattr(exc, "pos", None)
    return "%s at position %d" % (what, position) if isinstance(position, int) else what


def _unrolled(node):
    """How many pieces the `regex` module writes out for a parsed pattern: a fixed repeat count is spelled out copy by copy."""
    total = 0
    for op, av in _items(node):
        if op in _REPEATS or op == _sre_const.POSSESSIVE_REPEAT:
            low, _high, body = av
            total += (max(low, 1) + 1) * _unrolled(body)
        elif op == _sre_const.SUBPATTERN:
            total += _unrolled(av[3])
        elif op == _sre_const.BRANCH:
            total += 1 + sum(_unrolled(alternative) for alternative in av[1])
        elif op == _sre_const.ATOMIC_GROUP:
            total += _unrolled(av)
        elif op in (_sre_const.ASSERT, _sre_const.ASSERT_NOT):
            total += _unrolled(av[1])
        elif op == _sre_const.GROUPREF_EXISTS:
            total += 1 + _unrolled(av[1]) + (_unrolled(av[2]) if av[2] else 0)
        else:
            total += 1
    return total


_REPEAT_COUNT = re.compile(r"\{(?:\d+(?:,\d*)?|,\d*)\}")


def _fuzzy_lookalike(pattern):
    """True when a `{` that `re` takes for a literal brace starts a fuzzy-match constraint in the `regex` module.

    a{e<=1} matches only the text "a{e<=1}" in `re`, and "a" with one error in `regex`. A pattern written for `re` keeps
    its meaning only if such a brace is escaped, so the pattern is refused. Braces inside a character set are literal in both.
    """
    index, size, in_set = 0, len(pattern), False
    while index < size:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_set:
            in_set = char != "]"
        elif char == "[":
            in_set = True
            index += 1 + (index + 1 < size and pattern[index + 1] == "^")
            if index < size and pattern[index] == "]":      # a ] first in a set is a literal one
                index += 1
            continue
        elif char == "{":
            counted = _REPEAT_COUNT.match(pattern, index)
            if counted:
                index = counted.end()
                continue
            if index + 1 < size and (pattern[index + 1] in "deis" or pattern[index + 1].isdecimal()):
                return True
        index += 1
    return False


def _re_bits(flags):
    bits = 0
    for char, bit in (("i", re.IGNORECASE), ("m", re.MULTILINE), ("s", re.DOTALL)):
        if char in flags:
            bits |= bit
    return bits


def _regex_bits(flags):
    bits = _regex.VERSION0
    for char, bit in (("i", _regex.IGNORECASE), ("m", _regex.MULTILINE), ("s", _regex.DOTALL)):
        if char in flags:
            bits |= bit
    return bits


def _compile_pattern(pattern, flags, path):
    """The compiled pattern, or PromptConfigError: bounded length, valid, and free of catastrophic backtracking.

    The syntax is the one of `re`: `re` parses the pattern first (what it refuses is refused, and so is what it warns about,
    as a set that is a future syntax), the analysis below looks for what makes a backtracking engine blow up, and then the
    `regex` module compiles it: every pattern runs there, under a time limit (see Deadline). The analysis is the first
    line, a pattern it cannot judge (or judges wrongly) is stopped by the limit.
    """
    if _regex is None:
        _err(path, "regular expressions need the 'regex' package, which is not installed (pip install regex)")
    if not isinstance(pattern, str) or not pattern:
        _err(path, "must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        _err(path, "the pattern is longer than %d characters" % MAX_PATTERN_CHARS)
    flags = _normalize_flags(flags, path)
    try:
        with _WARN_LOCK, _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            re.compile(pattern, _re_bits(flags))
            tree = _sre_parse.parse(pattern, _re_bits(flags))
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        _err(path, "invalid regular expression: %s" % exc, _brief_regex_error(exc))
    # Only the warnings of the parser count: another thread's warning must not condemn the pattern.
    suspicious = [item for item in caught if issubclass(item.category, FutureWarning)
                  and str(item.message).startswith("Possible ")]
    if suspicious:
        _err(path, "suspicious regular expression: %s" % suspicious[0].message)
    problem = _regex_hazard(tree)
    if problem:
        _err(path, "the pattern can backtrack catastrophically (%s); use a possessive quantifier, an atomic group or "
                   "a simpler pattern" % problem)
    try:
        weight = _unrolled(tree)
    except RecursionError:
        weight = MAX_PATTERN_WEIGHT + 1
    if weight > MAX_PATTERN_WEIGHT:
        _err(path, "the pattern repeats too much: nested repeat counts make it about %d pieces long (up to %d are "
                   "allowed)" % (min(weight, 10 ** 12), MAX_PATTERN_WEIGHT))
    if _fuzzy_lookalike(pattern):
        _err(path, "a '{' that is not a repeat count (a{2,3}) starts a fuzzy-match constraint in the regex engine: "
                   "write \\{ for a literal brace")
    try:
        return _regex.compile(pattern, _regex_bits(flags))
    except (_regex.error, RecursionError, OverflowError, ValueError, MemoryError) as exc:
        _err(path, "invalid regular expression: %s" % exc, _brief_regex_error(exc))


def compile_pattern(pattern, flags=""):
    """A pattern checked and compiled by the rules of a prompts document: PromptConfigError when it breaks one of them."""
    return _compile_pattern(pattern, _normalize_flags(flags, "flags"), "pattern")


def _check_template(pattern, flags, template, path):
    """A replacement template that `re.sub` takes for this pattern, or PromptConfigError.

    The `regex` module looks at a template only when something matches, so the stdlib parser judges it here; a template it
    takes, the `regex` module expands the same way.
    """
    try:
        re.compile(pattern, _re_bits(flags)).sub(template, "")
    except (re.error, IndexError) as exc:
        _err(path, "invalid replacement: %s" % exc, _brief_regex_error(exc, "invalid replacement"))


# -- running a compiled pattern: a time limit and a limit on the size of the result ---------------------------------------


class EditRefused(Exception):
    """An edit that was not applied on this request; `reason` is what the journal and the preview call it."""
    reason = "refused"


class RegexTimeout(EditRefused):
    """A pattern did not finish in its share: REGEX_TIMEOUT for one operation, REGEX_BUDGET seconds or REGEX_WORK characters for
    a call."""
    reason = "timeout"


class EditTooLarge(EditRefused):
    """An edit would make its text longer than the limits allow."""
    reason = "too_large"


# What a refusal is called in the warnings of a head (the journal and the reports carry the reason itself).
_REFUSALS = {"timeout": "it ran out of time (a pattern that takes too long on this text)",
             "too_large": "its result would be too large"}


_MIN_SLICE = 0.001

# What compiling or expanding a pattern raises: `re` and `regex` have their own error classes (neither is a ValueError).
REGEX_ERRORS = (re.error,) + ((_regex.error,) if _regex is not None else ())


class Deadline:
    """What the regular expressions of one call (compose_head, prompt_body.rewrite) may use: REGEX_WORK characters of text to
    scan in all, and as an emergency stop REGEX_BUDGET seconds.

    An operation is charged the length of its text before it runs, so what is refused depends on the request and on nothing
    else: the same request gets the same edits on a quiet machine and a busy one. One operation may take REGEX_TIMEOUT at most
    (less when the time is nearly gone). Without a budget, 200 edits that each time out would cost 200 timeouts; with it, the
    first few do and the rest are refused at once.
    """
    __slots__ = ("per_operation", "left", "work")

    def __init__(self, budget=None, per_operation=None, work=None):
        self.left = REGEX_BUDGET if budget is None else budget
        self.per_operation = REGEX_TIMEOUT if per_operation is None else per_operation
        self.work = REGEX_WORK if work is None else work

    def run(self, call, size=0):
        """call(timeout) with the time that is left, after charging `size` characters of work; RegexTimeout when no work or
        time is left, or when the call used all of it."""
        if size > self.work:
            raise RegexTimeout("the work for regular expressions is spent")
        if self.left < _MIN_SLICE:
            raise RegexTimeout("the time for regular expressions is spent")
        self.work -= size
        started = time.perf_counter()
        try:
            return call(min(self.per_operation, self.left))
        except TimeoutError:
            raise RegexTimeout("a regular expression ran out of time") from None
        finally:
            self.left -= time.perf_counter() - started


def _limits(compiled, timeout):
    """The keywords that make a `regex` pattern release the GIL while it matches and stop at the time limit."""
    if _regex is not None and isinstance(compiled, _regex.Pattern):
        return {"concurrent": True, "timeout": timeout}
    return {}


def regex_search(compiled, text, deadline=None):
    """The first match of the compiled pattern in text, or None; RegexTimeout when the time is up."""
    return (deadline or Deadline()).run(lambda timeout: compiled.search(text, **_limits(compiled, timeout)), len(text))


def regex_spans(compiled, text, deadline=None):
    """[(start, end)] of the matches of the compiled pattern that are not empty; RegexTimeout when the time is up.

    At most MAX_MATCHES of them: a pattern that matches in hundreds of thousands of places would otherwise build a list as
    big as the text before anything is cut (EditTooLarge).
    """
    def spans(timeout):
        found = []
        for match in compiled.finditer(text, **_limits(compiled, timeout)):
            if match.end() > match.start():
                found.append((match.start(), match.end()))
                if len(found) > MAX_MATCHES:
                    raise EditTooLarge("the pattern matches in more than %d places" % MAX_MATCHES)
        return found
    return (deadline or Deadline()).run(spans, len(text))


def regex_remove(compiled, text, deadline=None):
    """(text without the matches, how many there were); RegexTimeout when the time is up."""
    return (deadline or Deadline()).run(lambda timeout: compiled.subn("", text, **_limits(compiled, timeout)), len(text))


def regex_replace(compiled, template, text, deadline=None, room=None):
    """(new text, matches replaced, characters taken out, characters put in): every match is replaced by the expansion of
    template, a re.sub template.

    The result may be at most `room` characters longer than text (MAX_EDIT_GROWTH by default), or EditTooLarge is raised.
    The replacement is made by a function that keeps the count, so a pattern that matches everywhere cannot build a result
    of any size before the check; the time is limited as in regex_search.
    """
    room = MAX_EDIT_GROWTH if room is None else room
    removed = added = 0

    def swap(match):
        nonlocal removed, added
        value = match.expand(template)
        removed += match.end() - match.start()
        added += len(value)
        if added - removed > room:
            raise EditTooLarge("the replacement would make the text more than %d characters longer" % room)
        return value

    new, count = (deadline or Deadline()).run(lambda timeout: compiled.subn(swap, text, **_limits(compiled, timeout)),
                                              len(text))
    return new, count, removed, added


# -- edits of the client's system prompt ----------------------------------------------------------------------------------


def _delete_spans(text, spans):
    """text without the (start, end) spans; the blank lines around each deletion are tidied.

    A span that covers whole lines takes the newline that ends its last line with it. Where the text before and after a
    deletion meet, three or more newlines become two and newlines at the very start or end of the text go: a deleted
    block leaves neither a hole nor a dangling blank line.
    """
    if not spans:
        return text
    widened = []
    for start, end in spans:
        if (start == 0 or text[start - 1] == "\n") and text[end:end + 1] == "\n":
            end += 1
        widened.append((start, end))
    spans = _merge_spans(widened)
    pieces, position = [], 0
    for start, end in spans:
        pieces.append(text[position:start])
        position = end
    pieces.append(text[position:])
    out = [pieces[0]]
    last = len(pieces) - 1
    for number, piece in enumerate(pieces[1:], 1):
        trailing = 0
        while out:
            tail = out[-1]
            stripped = tail.rstrip("\n")
            trailing += len(tail) - len(stripped)
            if stripped:
                out[-1] = stripped
                break
            out.pop()
        leading = len(piece) - len(piece.lstrip("\n"))
        core = piece[leading:]
        total = trailing + leading
        if total == 0 or not out or (not core and number == last):
            join = ""
        elif total >= 3:
            join = "\n\n"
        else:
            join = "\n" * total
        out.append(join)
        out.append(core)
    return "".join(out)


def _merge_spans(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _join_blank(left, right):
    """left and right separated by one blank line (a boundary that already has newlines keeps them)."""
    if not left:
        return right
    if not right:
        return left
    existing = len(left) - len(left.rstrip("\n")) + len(right) - len(right.lstrip("\n"))
    return left + "\n" * max(0, 2 - existing) + right


_FENCE_OR_HEADING = re.compile(r"^ {0,3}(?:(?P<fence>`{3,}|~{3,})[^\n]*|(?P<hashes>#{1,6})(?:[ \t][^\n]*)?)$", re.M)


def _markdown_headings(text):
    """[(level, start of the line, the stripped line)] of the ATX headings outside fenced code blocks."""
    found, fence = [], None
    for match in _FENCE_OR_HEADING.finditer(text):
        mark = match.group("fence")
        if mark:
            line = match.group(0).strip()
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and set(line) == {mark[0]}:
                fence = None
        elif fence is None:
            found.append((len(match.group("hashes")), match.start(), match.group(0).strip()))
    return found


def _section_ends(headings, size):
    """[offset where the section of each heading ends]: at the next heading of the same or a higher level, else at `size`.

    One pass over the headings with a stack of the sections still open, so that a document with many headings costs time
    linear in them (a search forward from every heading, over a copy of the rest of the list, cost time squared).
    """
    ends = [size] * len(headings)
    open_sections = []
    for index, (level, start, _line) in enumerate(headings):
        while open_sections and headings[open_sections[-1]][0] >= level:
            ends[open_sections.pop()] = start
        open_sections.append(index)
    return ends


def _op_remove_section(text, edit, run):
    heading = edit["heading"].strip()
    headings = _markdown_headings(text)
    ends = _section_ends(headings, len(text))
    spans = []
    for position, (_level, start, line) in enumerate(headings):
        if line != heading or (spans and start < spans[-1][1]):
            continue
        spans.append((start, ends[position]))
    return (_delete_spans(text, spans), True) if spans else (text, False)


def _op_keep_sections(text, edit, run):
    wanted = {heading.strip() for heading in edit["headings"]}
    headings = _markdown_headings(text)
    ends = _section_ends(headings, len(text))
    kept = [(start, ends[position]) for position, (_level, start, line) in enumerate(headings) if line in wanted]
    if not kept:
        return text, False
    kept.append((0, headings[0][1]))
    spans, position = [], 0
    for start, end in _merge_spans(span for span in kept if span[0] < span[1]):
        if start > position:
            spans.append((position, start))
        position = end
    if position < len(text):
        spans.append((position, len(text)))
    return _delete_spans(text, spans), True


# The attributes of a tag: no `<` or `>` among them and at most this many characters. Without a bound, every opening tag with
# no `>` after it makes the engine read to the end of the text.
_TAG_ATTRIBUTES = 512


@functools.lru_cache(maxsize=512)
def _tag_pattern(tag):
    return re.compile(r"<(/?)%s(?=[\s/>])[^<>]{0,%d}>" % (re.escape(tag), _TAG_ATTRIBUTES))


def _tag_elements(text, tag):
    """[(open start, open anchor, open end, close start, close anchor, close end)] of the complete <tag>…</tag> blocks, nested
    ones included, sorted as they are found.

    A block is a block only when its opening tag begins a line (after indentation), the way <env> and
    <available_skills> stand in a prompt; the closing tag may stand anywhere. A tag that is merely mentioned in a sentence
    or in the middle of a line is left alone, and so is what lies between its tags. Opening tags that are not at the
    start of a line still count for the nesting of blocks of the same name.

    An anchor is the start of the line of a tag when only indentation stands before the tag on it (the indentation goes with
    the tag when it is cut), else None. The scan is linear: the start of the line is tracked from one tag to the next, and the
    indentation is read backwards only as far as blanks go.
    """
    elements, stack = [], []
    line, seen = 0, 0
    for match in _tag_pattern(tag).finditer(text):
        begin = match.start()
        newline = text.rfind("\n", seen, begin)
        if newline >= 0:
            line = newline + 1
        seen = begin
        indent = begin
        while indent > line and text[indent - 1] in " \t":
            indent -= 1
        anchor = line if indent == line else None
        if match.group(1):
            if stack:
                opened, opened_anchor = stack.pop()
                if opened_anchor is not None:
                    elements.append((opened.start(), opened_anchor, opened.end(), begin, anchor, match.end()))
        elif match.group(0).endswith("/>"):
            if anchor is not None:
                elements.append((begin, anchor, match.end(), match.end(), None, match.end()))
        else:
            stack.append((match, anchor))
    return elements


def _op_remove_tag(text, edit, run):
    elements = _tag_elements(text, edit["tag"])
    if not elements:
        return text, False
    spans = []
    for start, anchor, _open_end, _close_start, _close_anchor, end in sorted(elements):
        if not spans or start >= spans[-1][1]:
            spans.append((anchor, end))
    return _delete_spans(text, spans), True


def _op_keep_tag_inner(text, edit, run):
    elements = _tag_elements(text, edit["tag"])
    if not elements:
        return text, False
    spans = []
    for _start, anchor, open_end, close_start, close_anchor, end in elements:
        spans.append((anchor, open_end))
        if close_start < end:
            spans.append((close_start if close_anchor is None else close_anchor, end))
    return _delete_spans(text, _merge_spans(spans)), True


def _op_remove_between(text, edit, run):
    start_marker, end_marker, inclusive = edit["start"], edit["end"], edit["inclusive"]
    spans, position, matched = [], 0, False
    while True:
        begin = text.find(start_marker, position)
        if begin < 0:
            break
        end = text.find(end_marker, begin + len(start_marker))
        if end < 0:
            break
        matched = True
        span = (begin, end + len(end_marker)) if inclusive else (begin + len(start_marker), end)
        if span[0] < span[1]:
            spans.append(span)
        position = end + len(end_marker)
    return (_delete_spans(text, spans), True) if matched else (text, False)


class _Run:
    """What the edits of the client's system prompt run with: the snapshot (its compiled patterns), the regex time left and
    how long the text may get."""
    __slots__ = ("snapshot", "deadline", "ceiling")

    def __init__(self, snapshot, deadline, original=""):
        self.snapshot, self.deadline = snapshot, deadline
        # No edit makes the text longer than a head may be, or than it was to begin with.
        self.ceiling = max(len(original), MAX_HEAD_BYTES)

    def room(self, text):
        """How many characters one edit may add to `text`."""
        return max(0, min(MAX_EDIT_GROWTH, self.ceiling - len(text)))


def _op_remove_regex(text, edit, run):
    compiled = run.snapshot.regex(edit["pattern"], edit["flags"])
    spans = regex_spans(compiled, text, run.deadline)
    if spans:
        return _delete_spans(text, spans), True
    return text, regex_search(compiled, text, run.deadline) is not None


def _op_replace_regex(text, edit, run):
    if edit["with"] == "":
        return _op_remove_regex(text, edit, run)
    result, count, _removed, _added = regex_replace(run.snapshot.regex(edit["pattern"], edit["flags"]), edit["with"], text,
                                                    run.deadline, run.room(text))
    return result, count > 0


def _op_replace_text(text, edit, run):
    find, replacement = edit["find"], edit["with"]
    if find not in text:
        return text, False
    if replacement:
        count = text.count(find) if edit["all"] else 1
        if count * (len(replacement) - len(find)) > run.room(text):
            raise EditTooLarge("the replacement would make the text more than %d characters longer" % run.room(text))
        return text.replace(find, replacement, -1 if edit["all"] else 1), True
    spans, position = [], 0
    while True:
        begin = text.find(find, position)
        if begin < 0:
            break
        spans.append((begin, begin + len(find)))
        position = begin + len(find)
        if not edit["all"]:
            break
    return _delete_spans(text, spans), True


def _op_prepend(text, edit, run):
    if len(edit["text"]) + 2 > run.room(text):
        raise EditTooLarge("the text would make the system prompt more than %d characters longer" % run.room(text))
    return _join_blank(edit["text"], text), True


def _op_append(text, edit, run):
    if len(edit["text"]) + 2 > run.room(text):
        raise EditTooLarge("the text would make the system prompt more than %d characters longer" % run.room(text))
    return _join_blank(text, edit["text"]), True


_SYSTEM_EDITORS = {
    "remove_section": _op_remove_section, "keep_sections": _op_keep_sections, "remove_tag": _op_remove_tag,
    "keep_tag_inner": _op_keep_tag_inner, "remove_between": _op_remove_between, "remove_regex": _op_remove_regex,
    "replace_regex": _op_replace_regex, "replace_text": _op_replace_text, "prepend": _op_prepend,
    "append": _op_append}


def _apply_system_edits(text, edits, snapshot, deadline=None):
    """(new text, [{"index", "op", "matched", "chars_before", "chars_after"[, "skipped"]}]): the edits run in order.

    An edit that cannot run on this text (a pattern out of time, see Deadline) leaves the text as it was, is not matched and
    says why in "skipped".
    """
    report = []
    run = _Run(snapshot, deadline or Deadline(), text)
    for index, edit in enumerate(edits):
        before = len(text)
        skipped = None
        try:
            text, matched = _SYSTEM_EDITORS[edit["op"]](text, edit, run)
        except EditRefused as refused:
            matched, skipped = False, refused.reason
        entry = {"index": index, "op": edit["op"], "matched": matched, "chars_before": before, "chars_after": len(text)}
        if skipped:
            entry["skipped"] = skipped
        report.append(entry)
    return text, report


# What the edits of a system prompt gave. OpenCode sends the same system prompt on every turn of a conversation, so the second
# turn costs a hash instead of the edits. One answer per (snapshot, profile, sha256 of the text), in a least-recently-used list
# bounded by count and by characters; a snapshot is immutable, so an answer never goes stale.
_MEMO = collections.OrderedDict()
_MEMO_LOCK = threading.Lock()
_MEMO_ENTRIES = 32
_MEMO_CHARS = 4 * 1024 * 1024
_MEMO_HELD = [0]
_TOKENS = itertools.count(1)


def _edited_system(snapshot, name, system, edits):
    """(edited text, report) of the edits of profile `name` over `system`; the answer for a text seen before is remembered.

    A refused edit (a pattern out of time) is remembered like any other result: the next turn gets the same head and does not
    wait for the same pattern again.
    """
    key = (getattr(snapshot, "_token", id(snapshot)), name, hashlib.sha256(system.encode("utf-8", "surrogatepass")).digest())
    with _MEMO_LOCK:
        found = _MEMO.get(key)
        if found is not None:
            _MEMO.move_to_end(key)
    if found is not None:
        return found[0], [dict(entry) for entry in found[1]]
    edited, report = _apply_system_edits(system, edits, snapshot)
    if len(edited) <= _MEMO_CHARS:
        with _MEMO_LOCK:
            if key not in _MEMO:
                _MEMO[key] = (edited, tuple(dict(entry) for entry in report))
                _MEMO_HELD[0] += len(edited)
            while _MEMO and (len(_MEMO) > _MEMO_ENTRIES or _MEMO_HELD[0] > _MEMO_CHARS):
                _key, (dropped, _report) = _MEMO.popitem(last=False)
                _MEMO_HELD[0] -= len(dropped)
    return edited, report


def _edit_target(edit):
    """What an edit looks for, short enough for the preview report (edits_unmatched); the journal never gets it."""
    for key in ("heading", "tag", "start", "find", "pattern", "text"):
        if key in edit:
            return _clip(edit[key], 60)
    if "headings" in edit:
        return _clip(", ".join(edit["headings"]), 60)
    return ""


# -- compiling a document -------------------------------------------------------------------------------------------------

# (key, kind, required, default) of every edit op; the kinds are converted by _Compiler._param.
_SYSTEM_SPECS = {
    "remove_section": (("heading", "heading", True, None),),
    "keep_sections": (("headings", "headings", True, None),),
    "remove_tag": (("tag", "tag", True, None),),
    "keep_tag_inner": (("tag", "tag", True, None),),
    "remove_between": (("start", "str", True, None), ("end", "str", True, None), ("inclusive", "bool", False, False)),
    "remove_regex": (("pattern", "pattern", True, None), ("flags", "flags", False, "")),
    "replace_regex": (("pattern", "pattern", True, None), ("flags", "flags", False, ""),
                      ("with", "regex_with", True, None)),
    "replace_text": (("find", "str", True, None), ("with", "text0", True, None), ("all", "bool", False, True)),
    "prepend": (("text", "text", True, None),),
    "append": (("text", "text", True, None),),
}
_USER_SPECS = {
    "drop_tag": (("tag", "tag", True, None), ("contains", "str", False, None), ("pattern", "pattern", False, None),
                 ("flags", "flags", False, "")),
    "remove_regex": _SYSTEM_SPECS["remove_regex"],
    "replace_text": _SYSTEM_SPECS["replace_text"],
}
_TOOL_SPECS = {
    "append": (("text", "text", True, None),),
    "prepend": (("text", "text", True, None),),
    "set": (("text", "text0", True, None),),
    "replace_text": _SYSTEM_SPECS["replace_text"],
    "replace_regex": _SYSTEM_SPECS["replace_regex"],
}
_TOP_KEYS = ("version", "default_profile", "protected", "texts", "relay_texts", "profiles", "rules", "codex")


def _describe_ops(specs, extra_required=()):
    return {op: {"required": [*extra_required, *(row[0] for row in rows if row[2])],
                 "optional": {row[0]: row[3] for row in rows if not row[2]}} for op, rows in specs.items()}


# What each edit op takes, for the admin forms: {section: {op: {"required": [names], "optional": {name: default}}}}.
# A hidden tool is {"tool", "hide": true} and takes no op.
EDIT_OP_PARAMS = {
    "client_system": _describe_ops(_SYSTEM_SPECS),
    "user_blocks": _describe_ops(_USER_SPECS),
    "tools": {**_describe_ops(_TOOL_SPECS, ("tool",)), "hide": {"required": ["tool"], "optional": {}}}}
_PROFILE_KEYS = ("description", "head", "client_system", "history", "tools", "generation", "publish_as_model")
_STRUCTURED_VARIABLES = ("default", "schema", "schema_name")
# Values a rule condition can usefully name; a literal outside them cannot match, which deserves a warning.
_KNOWN_VALUES = {
    "client": CLIENTS, "dialect": DIALECTS,
    "purpose": ("client-tools", "client-text", "generation", "session-title", "auto-mode-check", "connection-probe",
                "client-service")}
_PLACEHOLDER_WARNING = ("the blocks of a relay task are placeholders in the preview: the relay builds them from its "
                        "configuration when it owns the request")


def _glob_regex(pattern):
    return re.compile(fnmatch.translate(pattern.casefold()))


def _has_regex_with(spec):
    return any(row[1] == "regex_with" for row in spec)


def _conditions_match(conditions, inp):
    for name, is_bool, expected in conditions:
        if is_bool:
            if bool(getattr(inp, name)) is not expected:
                return False
            continue
        values = (inp.model, inp.requested_model) if name == "model" else (getattr(inp, name),)
        if not any(value and any(rx.match(str(value).casefold()) for rx in expected) for value in values):
            return False
    return True


class _Rule:
    __slots__ = ("name", "conditions", "use", "override_protected")

    def __init__(self, name, conditions, use, override_protected=False):
        self.name = name
        self.conditions = conditions
        self.use = use
        self.override_protected = override_protected

    def matches(self, inp):
        return _conditions_match(self.conditions, inp)


class _Profile:
    __slots__ = ("name", "description", "head", "template", "system_edits", "mid_system", "user_blocks", "tool_edits",
                 "generation", "publish", "body_edits", "normalized")

    def __init__(self, name, description, head, template, system_edits, mid_system, user_blocks, tool_edits,
                 generation, publish):
        self.name, self.description, self.head, self.template = name, description, head, template
        self.system_edits, self.mid_system, self.user_blocks = system_edits, mid_system, user_blocks
        self.tool_edits, self.generation, self.publish = tool_edits, generation, publish
        self.body_edits = mid_system != "keep" or bool(user_blocks) or bool(tool_edits)
        self.normalized = {
            "name": name, "description": description, "head": head,
            "client_system": {"edits": system_edits},
            "history": {"mid_system": mid_system, "user_blocks": user_blocks},
            "tools": {"edits": tool_edits},
            "generation": generation, "publish_as_model": publish}


class _Data:
    """What compiling a document produced: the tables behind a Snapshot.

    `literal` is the document with every {"file": ...} text replaced by the text that was read (None when the document names
    no file: it is its own literal document)."""
    literal = None


def _copy(value):
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


def _inlined(value, found):
    """A copy of the document in which each dict that `found` names (by id) is replaced by the text that was read for it.

    The text objects are shared, not copied: a file named many times costs one string in memory (its size shows up only when
    the literal document is written out, see Snapshot._canonical)."""
    if isinstance(value, dict):
        if id(value) in found:
            return found[id(value)]
        return {key: _inlined(item, found) for key, item in value.items()}
    if isinstance(value, list):
        return [_inlined(item, found) for item in value]
    return value


_UNSET = object()


def _weight(value) -> int:
    """A cheap lower bound of the length of the JSON of `value`, in characters; it stops counting past MAX_REVISION_BYTES."""
    total, todo = 0, [value]
    while todo and total <= MAX_REVISION_BYTES:
        item = todo.pop()
        if isinstance(item, str):
            total += len(item) + 2
        elif isinstance(item, dict):
            for key, child in item.items():
                total += len(key) + 4
                todo.append(child)
        elif isinstance(item, list):
            total += len(item) + 2
            todo.extend(item)
        else:
            total += 1
    return total


def _nbytes(text):
    return len(text.encode("utf-8", "surrogatepass"))


def _object(value, path, what="an object"):
    if not isinstance(value, dict):
        _err(path, "must be %s" % what)
    for key in value:
        if not isinstance(key, str):
            _err(path, "keys must be strings")
    return value


def _no_half_pairs(document):
    """Refuse a string that holds half of a surrogate pair ("\\ud83d"): JSON can carry it, no UTF-8 text can.

    Such a text was accepted once and then failed every request that used it, while the head was encoded (a 500), and
    /codex/models too. The walk is iterative, so a hostile nesting cannot exhaust the stack; the error names the field,
    and for a key its parent, so the offending text is never echoed.
    """
    todo = [(document, "")]
    while todo:
        value, path = todo.pop()
        if isinstance(value, str):
            _utf8(value, path)
        elif isinstance(value, dict):
            children = []
            for key, item in value.items():
                if isinstance(key, str):
                    _utf8(key, path)
                    children.append((item, _path(path, key)))
            todo.extend(reversed(children))
        elif isinstance(value, list):
            todo.extend(reversed([(item, "%s[%d]" % (path, index)) for index, item in enumerate(value)]))


def _utf8(text, path):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        _err(path, "contains an unpaired surrogate (half of a Unicode pair, such as \\ud83d), which cannot be sent as "
                   "UTF-8 text")


def _only_keys(value, path, allowed):
    for key in value:
        if key not in allowed:
            _err(_path(path, key), "unknown key (allowed: %s)" % ", ".join(allowed))


def _check_glob(value, path):
    if not isinstance(value, str) or not value:
        _err(path, "must be a non-empty glob string")
    if len(value) > MAX_GLOB_CHARS:
        _err(path, "a glob is limited to %d characters" % MAX_GLOB_CHARS)
    return value


class _Compiler:
    """Validates one document and builds its tables; `files` lists every text file it looked at (also on a failure).

    A `literal` compile is the one of a stored revision (see Snapshot.literal_document): its texts are all strings, a
    {"file": ...} is refused (the file on disk is not what the revision was made of), and the size limit is the larger one of
    a literal document."""

    def __init__(self, document, base_dir, *, literal=False):
        self.document = document
        self.base = pathlib.Path(base_dir)
        self.files = []
        self.total = 0
        self.expanded = 0
        self.texts = {}
        self.patterns = {}
        self.warnings = []
        self.literal = literal
        self.limit = MAX_REVISION_BYTES if literal else MAX_DOCUMENT_BYTES
        self._files = {}
        self._inlined = {}
        self.data = _Data()

    # -- sizes and files
    def _grow(self, count, path):
        self.total += count
        if self.total > self.limit:
            _err(path, "the document and its text files together are larger than %d MiB"
                 % (self.limit // (1024 * 1024)))

    def _size(self, text, path):
        if _nbytes(text) > MAX_TEXT_BYTES:
            _err(path, "a text is limited to %d KiB" % (MAX_TEXT_BYTES // 1024))

    def _locate(self, name, path):
        """The resolved path of a text file: a regular .md or .txt file inside prompts/ next to the prompts file.

        The folder is not the one of the prompts file itself, which also holds config.json (the cookies and the relay key)
        and the journal: a document must not be able to read them into a head. Links are followed before the check, so a
        link inside prompts/ that leads out (a symlink, a junction) is refused too.
        """
        if not isinstance(name, str) or not name.strip():
            _err(path, "must be a non-empty relative path")
        windows = pathlib.PureWindowsPath(name)
        if ("\x00" in name or os.path.isabs(name) or windows.is_absolute() or windows.drive
                or name.startswith(("/", "\\"))):
            _err(path, "must be a relative path inside the %s/ folder next to the prompts file" % TEXT_DIR)
        if ".." in re.split(r"[\\/]+", name):
            _err(path, "must not contain '..'")
        if ":" in name:
            _err(path, "must not contain ':' (a drive, a stream or a device)")
        try:
            base = self.base.resolve()
            allowed = base / TEXT_DIR
            target = (base / name).resolve()
            target.relative_to(allowed)
        except ValueError:
            _err(path, "must be a file inside the %s/ folder next to the prompts file (links that lead out of it are "
                       "refused too)" % TEXT_DIR)
        except OSError as exc:
            _err(path, "cannot resolve the path: %s" % (exc.strerror or exc))
        if target.suffix.lower() not in TEXT_SUFFIXES or pathlib.PurePath(name).suffix.lower() not in TEXT_SUFFIXES:
            _err(path, "must name a %s file" % " or ".join(TEXT_SUFFIXES))
        return target

    def _file(self, name, path):
        target = self._locate(name, path)
        key = str(target)
        if key in self._files:
            return self._files[key]
        try:
            info = target.stat()
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                self.files.append((key, None, None))
                _err(path, "file not found: %s" % _clip(name), "file not found")
            self._unreadable(key, path, exc)
        self.files.append((key, info.st_mtime_ns, info.st_size))
        if not stat.S_ISREG(info.st_mode):
            _err(path, "must name a regular file")
        if info.st_size > MAX_TEXT_BYTES:
            _err(path, "a text is limited to %d KiB" % (MAX_TEXT_BYTES // 1024))
        self._grow(info.st_size, path)
        try:
            with open(target, "rb") as handle:
                data = handle.read(MAX_TEXT_BYTES + 1)
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            _err(path, "the file is not valid UTF-8")
        except OSError as exc:
            self._unreadable(key, path, exc, replace=True)
        if len(data) > MAX_TEXT_BYTES:
            _err(path, "a text is limited to %d KiB" % (MAX_TEXT_BYTES // 1024))
        if text.startswith("\ufeff"):
            text = text[1:]
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._files[key] = text
        return text

    def _unreadable(self, key, path, exc, replace=False):
        """A file that exists but cannot be read now: not judged, so the stamp kept for it never matches a real one and
        the next look tries again; the error is marked transient (a repeat of it is journaled once)."""
        stamp = (key, -1, -1)
        if replace and self.files and self.files[-1][0] == key:
            self.files[-1] = stamp
        else:
            self.files.append(stamp)
        error = PromptConfigError(path, "cannot read the file: %s" % (exc.strerror or exc))
        error.transient = True
        raise error

    def _text(self, value, path):
        """A text given as a string or {"file": "prompts/<name>.md"}: the literal text."""
        if isinstance(value, str):
            self._size(value, path)
            return value
        if isinstance(value, dict) and set(value) == {"file"}:
            if self.literal:
                _err(path, "a stored revision names no file: its texts are in the document itself")
            text = self._file(value["file"], _path(path, "file"))
            self._inlined[id(value)] = text
            return text
        _err(path, 'must be a string or {"file": "prompts/<name>.md"}')

    def _expand(self, source, path, *, regex_replacement=False):
        template = _parse_template(source, path, variables=(), texts=self.texts)
        out = []
        for kind, value in template.tokens:
            if kind == "lit":
                out.append(value)
            else:
                text = self.texts[value]
                out.append(text.replace("\\", "\\\\") if regex_replacement else text)
        result = "".join(out)
        size = _nbytes(result)
        if size > MAX_TEXT_BYTES:
            _err(path, "the text is larger than %d KiB once {{text:...}} is expanded" % (MAX_TEXT_BYTES // 1024))
        self.expanded += size
        if self.expanded > MAX_EXPANDED_BYTES:
            _err(path, "the edits expand to more than %d MiB of text" % (MAX_EXPANDED_BYTES // (1024 * 1024)))
        return result

    def _pattern(self, pattern, flags, path):
        key = (pattern, flags)
        compiled = self.patterns.get(key)
        if compiled is None:
            compiled = self.patterns[key] = _compile_pattern(pattern, flags, path)
        return compiled

    # -- the document
    def compile(self):
        document = _object(self.document, "", "a JSON object")
        _no_half_pairs(document)
        try:
            raw = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            _err("", "the document must be plain JSON: %s" % exc)
        self._grow(_nbytes(raw), "")
        version = document.get("version")
        if type(version) is not int or version != DOCUMENT_VERSION:
            _err("version", "must be %d" % DOCUMENT_VERSION)
        _only_keys(document, "", _TOP_KEYS)
        data = self.data
        data.texts = self.texts
        self._compile_texts()
        self._compile_relay_texts()
        self._compile_profiles()
        self._compile_default_profile()
        self._compile_protected()
        self._compile_rules()
        self._compile_codex()
        data.patterns = self.patterns
        data.files = tuple(self.files)
        data.warnings = tuple(dict.fromkeys(self.warnings))
        data.size = self.total
        if self._inlined:
            data.literal = _inlined(self.document, self._inlined)
        return data

    def _compile_texts(self):
        raw = self.document.get("texts")
        if raw is None:
            return
        _object(raw, "texts")
        for name, value in raw.items():
            path = _path("texts", name)
            if not TEXT_NAME.fullmatch(name):
                _err(path, "a text name uses letters, digits, '_', '.' and '-' (up to 64 characters)")
            self.texts[name] = self._text(value, path)

    def _compile_relay_texts(self):
        data = self.data
        data.relay, data.relay_templates = {name: None for name in RELAY_TEXT_NAMES}, {}
        data.codex_base = None
        raw = self.document.get("relay_texts")
        if raw is None:
            return
        _object(raw, "relay_texts")
        _only_keys(raw, "relay_texts", RELAY_TEXT_NAMES)
        for name in RELAY_TEXT_NAMES:
            value = raw.get(name)
            if value is None:
                continue
            path = _path("relay_texts", name)
            if name == "codex_base":
                data.codex_base = self._codex_base(value, path)
                continue
            text = self._text(value, path)
            if name == "reminder":
                template = _parse_template(text, path, variables=("text",))
                if "text" not in template.variables:
                    _err(path, "must contain {{text}}: it stands for the message being wrapped")
            elif name == "structured":
                if not text.strip():
                    _err(path, "must not be empty: the structured-output contract is always sent")
                template = _parse_template(text, path, variables=_STRUCTURED_VARIABLES)
                if not template.variables & {"default", "schema"}:
                    _err(path, "must contain {{default}} or {{schema}}: the model has to be told the contract, and "
                               "the reply is judged against the schema")
            else:
                template = None
            data.relay[name] = text
            if template is not None:
                data.relay_templates[name] = template

    def _codex_base(self, value, path):
        if isinstance(value, str) or (isinstance(value, dict) and set(value) == {"file"}):
            return ("all", self._text(value, path))
        if isinstance(value, dict):
            _object(value, path)
            if "file" in value:
                _err(_path(path, "file"), 'cannot be mixed with model globs: use {"file": ...} alone or one entry '
                                          'per model glob')
            entries = []
            for glob, item in value.items():
                item_path = _path(path, glob)
                _check_glob(glob, item_path)
                entries.append((glob, _glob_regex(glob), self._text(item, item_path)))
            return ("map", tuple(entries))
        _err(path, 'must be a string, {"file": ...} or an object {"<model glob>": "..."}')

    def _compile_profiles(self):
        data = self.data
        raw = self.document.get("profiles")
        profiles = {}
        passthrough = BUILTIN_PROFILES["passthrough"]
        profiles["passthrough"] = _Profile(
            "passthrough", passthrough["description"], passthrough["head"],
            _parse_template(passthrough["head"], "", variables=VARIABLES), [], "keep", [], [], {}, False)
        data.profile_order = ["passthrough"]
        if raw is not None:
            _object(raw, "profiles")
            for name, value in raw.items():
                path = _path("profiles", name)
                if name in BUILTIN_PROFILES:
                    self._builtin_entry(name, value, path)
                    continue
                if not PROFILE_NAME.fullmatch(name):
                    _err(path, "a profile name uses letters, digits, '.', '_' and '-', starts with a letter or digit "
                               "and has up to 64 characters (it is also the model suffix after @)")
                profiles[name] = self._profile(name, value, path)
                data.profile_order.append(name)
        data.profiles = profiles

    @staticmethod
    def _builtin_entry(name, value, path):
        """An entry for a built-in profile may only repeat what the built-in says (the plan's example lists passthrough)."""
        builtin = BUILTIN_PROFILES[name]
        repeats = (isinstance(value, dict) and set(value) <= {"description", "head"}
                   and value.get("head", builtin["head"]) == builtin["head"]
                   and isinstance(value.get("description", ""), str))
        if not repeats:
            _err(path, "the built-in profile %r cannot be redefined (an entry may only repeat it)" % name)

    def _check_head_size(self, template, path):
        """Refuse a head that cannot stay under MAX_HEAD_BYTES even without the client's system prompt.

        What the document itself fixes is counted: the literals of the template, every {{text:...}} (a text used a thousand
        times is a thousand copies) and the user's texts behind {{role}}, {{relay_task}}, {{native_role}} and {{structured}}.
        What the client or the relay brings at run time is not known here; compose_head measures the whole head.
        """
        relay = {"role": "role", "relay_task": "role", "native_role": "native_role", "structured": "structured"}
        sizes, total = {}, 0
        for kind, value in template.tokens:
            if kind == "lit":
                total += _nbytes(value)
            elif kind == "text":
                if (kind, value) not in sizes:
                    sizes[kind, value] = _nbytes(self.texts[value])
                total += sizes[kind, value]
            elif value in relay:
                text = self.data.relay.get(relay[value])
                if text:
                    if (kind, value) not in sizes:
                        sizes[kind, value] = _nbytes(text)
                    total += sizes[kind, value]
            if total > MAX_HEAD_BYTES:
                _err(path, "the head can be longer than %d KiB without the client's system prompt: its literals and texts "
                           "alone are over that (each use of a text counts again)" % (MAX_HEAD_BYTES // 1024))

    def _profile(self, name, raw, path):
        _object(raw, path)
        _only_keys(raw, path, _PROFILE_KEYS)
        description = raw.get("description", "")
        if not isinstance(description, str):
            _err(_path(path, "description"), "must be a string")
        head_path = _path(path, "head")
        head = self._text(raw["head"], head_path) if raw.get("head") is not None else "{{client_system}}"
        template = _parse_template(head, head_path, variables=VARIABLES, texts=self.texts)
        self._check_head_size(template, head_path)
        system_edits = self._edit_list(raw.get("client_system"), _path(path, "client_system"), "edits",
                                       SYSTEM_EDIT_OPS, _SYSTEM_SPECS)
        history = raw.get("history")
        mid_system, user_blocks = "keep", []
        if history is not None:
            history_path = _path(path, "history")
            _object(history, history_path)
            _only_keys(history, history_path, ("mid_system", "user_blocks"))
            mid_system = history.get("mid_system", "keep")
            if mid_system not in MID_SYSTEM_MODES:
                _err(_path(history_path, "mid_system"), "must be one of: %s" % ", ".join(MID_SYSTEM_MODES))
            user_blocks = self._edits(history.get("user_blocks"), _path(history_path, "user_blocks"),
                                      USER_BLOCK_OPS, _USER_SPECS, self._edit)
        tools = raw.get("tools")
        tool_edits = []
        if tools is not None:
            tools_path = _path(path, "tools")
            _object(tools, tools_path)
            _only_keys(tools, tools_path, ("edits",))
            tool_edits = self._edits(tools.get("edits"), _path(tools_path, "edits"), TOOL_EDIT_OPS, _TOOL_SPECS,
                                     self._tool_edit)
            if any(edit["hide"] for edit in tool_edits):
                self.warnings.append("profiles.%s hides a tool: removing a tool from a signed dialog resets its "
                                     "history, so keep such conversations pinned" % name)
        generation = self._generation(raw.get("generation"), _path(path, "generation"))
        publish = self._publish(raw.get("publish_as_model", False), _path(path, "publish_as_model"))
        return _Profile(name, description, head, template, system_edits, mid_system, user_blocks, tool_edits,
                        generation, publish)

    def _generation(self, raw, path):
        if raw is None:
            return {}
        _object(raw, path)
        _only_keys(raw, path, ("effort", "verbosity"))
        out = {}
        effort = raw.get("effort")
        if effort is not None:
            if effort not in models.EFFORT_ORDER:
                _err(_path(path, "effort"), "must be null or one of: %s" % ", ".join(models.EFFORT_ORDER))
            out["effort"] = effort
        verbosity = raw.get("verbosity")
        if verbosity is not None:
            if verbosity not in VERBOSITY_LEVELS:
                _err(_path(path, "verbosity"), "must be null or one of: %s" % ", ".join(VERBOSITY_LEVELS))
            out["verbosity"] = verbosity
        return out

    def _publish(self, raw, path):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                _check_glob(item, "%s[%d]" % (path, index))
            return list(raw)
        _err(path, "must be false, true or a list of model globs")

    def _edit_list(self, raw, path, key, ops, specs):
        if raw is None:
            return []
        _object(raw, path)
        _only_keys(raw, path, (key,))
        return self._edits(raw.get(key), _path(path, key), ops, specs, self._edit)

    def _edits(self, raw, path, ops, specs, build):
        if raw is None:
            return []
        if not isinstance(raw, list):
            _err(path, "must be a list")
        if len(raw) > MAX_EDITS:
            _err(path, "a list holds up to %d edits" % MAX_EDITS)
        return [build(item, "%s[%d]" % (path, index), ops, specs) for index, item in enumerate(raw)]

    def _edit(self, raw, path, ops, specs, extra=()):
        _object(raw, path)
        op = raw.get("op")
        if not isinstance(op, str) or op not in ops:
            _err(_path(path, "op"), "must be one of: %s" % ", ".join(ops))
        spec = specs[op]
        allowed = ("op", *(row[0] for row in spec), *extra)
        for key in raw:
            if key not in allowed:
                _err(_path(path, key), "unknown key for op %r (allowed: %s)" % (op, ", ".join(allowed)))
        out = {"op": op}
        for key, kind, required, default in spec:
            key_path = _path(path, key)
            if raw.get(key) is None:
                if required:
                    _err(key_path, "is required for op %r" % op)
                out[key] = default
            else:
                out[key] = self._param(raw[key], kind, key_path, out)
        if "pattern" in out and out["pattern"] is not None:
            self._pattern(out["pattern"], out["flags"], _path(path, "pattern"))
            if _has_regex_with(spec):
                _check_template(out["pattern"], out["flags"], out["with"], _path(path, "with"))
        if op == "drop_tag":
            if out["contains"] is not None and out["pattern"] is not None:
                _err(path, "use either contains or pattern, not both")
            if out["pattern"] is None and "flags" in raw:
                _err(_path(path, "flags"), "flags need a pattern")
        return out

    def _param(self, value, kind, path, out):
        if kind in ("str", "pattern", "heading", "tag", "text", "text0", "regex_with"):
            if not isinstance(value, str):
                _err(path, "must be a string")
            if kind in ("str", "pattern", "heading", "tag", "text") and not value:
                _err(path, "must not be empty")
        if kind == "bool":
            if not isinstance(value, bool):
                _err(path, "must be true or false")
            return value
        if kind == "str":
            self._size(value, path)
            return value
        if kind == "pattern":
            if len(value) > MAX_PATTERN_CHARS:
                _err(path, "the pattern is longer than %d characters" % MAX_PATTERN_CHARS)
            return value
        if kind == "flags":
            return _normalize_flags(value, path)
        if kind == "heading":
            self._heading(value, path)
            return value
        if kind == "headings":
            if not isinstance(value, list) or not value:
                _err(path, "must be a non-empty list of heading lines")
            for index, item in enumerate(value):
                self._heading(item, "%s[%d]" % (path, index))
            return list(value)
        if kind == "tag":
            if not TAG_NAME.fullmatch(value):
                _err(path, "must be a tag name such as system-reminder")
            _tag_pattern(value)
            return value
        return self._expand(value, path, regex_replacement=(kind == "regex_with"))

    def _heading(self, value, path):
        if not isinstance(value, str) or not _HEADING.fullmatch(value.strip()):
            _err(path, "must be a markdown heading line such as '## Title'")

    def _tool_edit(self, raw, path, ops, specs):
        _object(raw, path)
        tool = raw.get("tool")
        tool_path = _path(path, "tool")
        if tool is None:
            _err(tool_path, "is required: a glob over tool names, or a list of them")
        if isinstance(tool, list):
            if not tool:
                _err(tool_path, "must not be an empty list")
            for index, item in enumerate(tool):
                _check_glob(item, "%s[%d]" % (tool_path, index))
            tool = list(tool)
        else:
            _check_glob(tool, tool_path)
        hide = raw.get("hide", False)
        if not isinstance(hide, bool):
            _err(_path(path, "hide"), "must be true or false")
        if hide:
            for key in raw:
                if key not in ("tool", "hide"):
                    _err(_path(path, key), "a hidden tool takes no other key")
            return {"tool": tool, "hide": True}
        if "op" not in raw:
            _err(_path(path, "op"), "is required unless the tool is hidden (\"hide\": true)")
        edit = self._edit({key: value for key, value in raw.items() if key not in ("tool", "hide")}, path, ops, specs)
        return {"tool": tool, "hide": False, **edit}

    def _compile_default_profile(self):
        value = self.document.get("default_profile")
        self.data.default_profile = None
        if value is None:
            return
        if not isinstance(value, str) or value not in self.data.profiles:
            _err("default_profile", "must be null or the name of a profile")
        self.data.default_profile = value

    def _compile_protected(self):
        raw = self.document.get("protected")
        self.data.protected = True
        if raw is None:
            return
        _object(raw, "protected")
        _only_keys(raw, "protected", ("enabled",))
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            _err("protected.enabled", "must be true or false")
        self.data.protected = enabled
        if not enabled:
            self.warnings.append("protected.enabled is false: title, compaction and other service requests are no "
                                 "longer kept out of the profiles")

    def _compile_rules(self):
        data = self.data
        raw = self.document.get("rules")
        data.override_rules, data.user_rules, names = [], [], set()
        if raw is None:
            return
        if not isinstance(raw, list):
            _err("rules", "must be a list")
        for index, item in enumerate(raw):
            path = "rules[%d]" % index
            _object(item, path)
            _only_keys(item, path, ("name", "enabled", "if", "use", "override_protected"))
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                _err(_path(path, "name"), "is required: a short unique name")
            if len(name) > MAX_NAME_CHARS:
                _err(_path(path, "name"), "is limited to %d characters" % MAX_NAME_CHARS)
            if name in names:
                _err(_path(path, "name"), "duplicate rule name %r" % _clip(name), "duplicate rule name")
            names.add(name)
            enabled = item.get("enabled", True)
            override = item.get("override_protected", False)
            for key, value in (("enabled", enabled), ("override_protected", override)):
                if not isinstance(value, bool):
                    _err(_path(path, key), "must be true or false")
            use = item.get("use")
            if not isinstance(use, str) or not (use in SPECIAL_USES or use in data.profiles):
                _err(_path(path, "use"), "must be the name of a profile or one of: %s" % ", ".join(SPECIAL_USES))
            conditions = self._conditions(item.get("if"), _path(path, "if"))
            if override and enabled:
                self.warnings.append("rule %r overrides the protection of service requests" % _clip(name))
            if not enabled:
                continue
            rule = _Rule(name, conditions, use, override)
            (data.override_rules if override else data.user_rules).append(rule)

    def _conditions(self, raw, path):
        if raw is None:
            return ()
        _object(raw, path)
        found = []
        for key, value in raw.items():
            key_path = _path(path, key)
            if key not in CONDITIONS:
                _err(key_path, "unknown condition (allowed: %s)" % ", ".join(CONDITIONS))
            if key in _BOOL_CONDITIONS:
                if not isinstance(value, bool):
                    _err(key_path, "must be true or false")
                found.append((key, True, value))
                continue
            patterns = [value] if isinstance(value, str) else value
            if not isinstance(patterns, list) or not patterns:
                _err(key_path, "must be a glob string or a non-empty list of glob strings")
            for number, pattern in enumerate(patterns):
                pattern_path = key_path if isinstance(value, str) else "%s[%d]" % (key_path, number)
                _check_glob(pattern, pattern_path)
                known = _KNOWN_VALUES.get(key)
                if known and not any(char in pattern for char in "*?[") and pattern.casefold() not in known:
                    self.warnings.append("%s: %r matches no request (known values: %s)"
                                         % (pattern_path, _clip(pattern), ", ".join(known)))
            found.append((key, False, tuple(_glob_regex(pattern) for pattern in patterns)))
        return tuple(found)

    def _compile_codex(self):
        data = self.data
        raw = self.document.get("codex")
        data.codex_models = ()
        if raw is None:
            return
        _object(raw, "codex")
        _only_keys(raw, "codex", ("models",))
        models_raw = raw.get("models")
        if models_raw is None:
            return
        _object(models_raw, "codex.models")
        entries = []
        for glob, overrides in models_raw.items():
            path = _path("codex.models", glob)
            _check_glob(glob, path)
            _object(overrides, path)
            _only_keys(overrides, path, CODEX_OVERRIDE_KEYS)
            checked = {}
            for key, value in overrides.items():
                key_path = _path(path, key)
                if key == "persistent_instructions":
                    checked[key] = self._text(value, key_path)
                elif isinstance(value, bool):
                    checked[key] = value
                else:
                    _err(key_path, "must be true or false")
            entries.append((glob, _glob_regex(glob), checked))
        data.codex_models = tuple(entries)


@dataclass(frozen=True)
class Plan:
    """The prompt decision for one request.

    profile None means the legacy path: the orchestrator runs its existing head code and
    nothing else in this module applies. body_edits says the profile rewrites the request
    body (history or tools) before parsing, see prompt_body.rewrite.

    `pinned` says the plan came from a pin an earlier request of the conversation made (plan I3); the first request of a
    conversation makes the pin and is not "pinned". `input` is the PlanInput the plan was selected for (None for a Plan
    built by hand): {{model}}, {{client}}, {{agent}} and {{purpose}} read it.
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

    @property
    def input(self) -> "PlanInput | None":
        return self.__dict__.get("_input")

    def compose_head(self, client_system: str, sources: HeadSources) -> tuple[str, list[Part], list[str]]:
        """Return (head, parts, warnings) for this plan's profile.

        Applies the profile's client_system.edits, renders its head template with the closed
        variable set, keeps a Claude Code billing header line first (plan I5: head starts with
        that line and "\\n", which direct.build_body splits into its own block) and appends the
        structured contract when the template does not place {{structured}} (plan I10).

        An edit that cannot run on this text (a pattern out of time, a result too long) leaves the text as it was and is named
        in the warnings, without quoting its target. The edits of one system text are remembered per (revision, profile, text).
        A head longer than MAX_HEAD_BYTES is apierr.ApiError 413 (prompt_too_large), which the runtime passes to the client.
        """
        head, parts, warnings, _report = _compose(self, client_system, sources)
        return head, parts, warnings


def _compose(plan, client_system, sources):
    """compose_head with the report of the client_system edits (preview shows which of them matched)."""
    snapshot = plan.snapshot
    if plan.profile is None or snapshot is None:
        raise RuntimeError("a legacy plan has no head to compose")
    profile = snapshot._data.profiles.get(plan.profile)
    if profile is None:
        raise RuntimeError("profile %r is not in revision %s" % (plan.profile, snapshot.rev))
    warnings, report = [], []
    template = profile.template
    system = str(client_system or "")
    billing, newline = "", ""
    first, separator, rest = system.partition("\n")
    if first.startswith(BILLING_PREFIX):
        billing, newline, system = first, separator, rest
    edited, changed = system, False
    if profile.system_edits:
        if "client_system" in template.variables:
            edited, report = _edited_system(snapshot, plan.profile, system, profile.system_edits)
            changed = edited != system
            for entry in report:
                op = profile.system_edits[entry["index"]]["op"]
                if entry.get("skipped"):
                    warnings.append("client_system.edits[%d] (%s) was not applied: %s"
                                    % (entry["index"], op, _REFUSALS.get(entry["skipped"], entry["skipped"])))
                elif not entry["matched"]:
                    warnings.append("client_system.edits[%d] (%s) matched nothing" % (entry["index"], op))
        else:
            warnings.append("client_system.edits are not applied: the head template does not use {{client_system}}")
    if (system.strip() and "client_system" not in template.variables
            and "client_system_raw" not in template.variables):
        warnings.append("the client's system prompt (%d characters) is not part of the head: the template does not use "
                        "{{client_system}}" % len(system))
    client_owned = bool(getattr(sources, "client_owned", True))
    inp = plan.input
    cache = {}

    def variable(name):
        if name in cache:
            return cache[name]
        if name == "client_system":
            value = ("client_edited" if changed else "client", edited)
        elif name == "client_system_raw":
            value = ("client", system)
        elif name == "role":
            text = snapshot.relay_text("role")
            value = ("relay:role", text if text is not None else sources.relay_role())
        elif name == "native_role":
            text = snapshot.relay_text("native_role")
            value = ("relay:native_role", text if text is not None else sources.native_role())
        elif name in RELAY_TASK_VARIABLES:
            source = "relay:task" if name == "relay_task" else "relay:" + name
            if client_owned:
                warnings.append("{{%s}} is empty: the client owns this request (the relay adds no task blocks)" % name)
                value = (source, "")
            else:
                value = (source, getattr(sources, name)())
        elif name == "structured":
            value = ("relay:structured", sources.structured())
        else:
            value = ("literal", getattr(inp, name, "") if inp is not None else "")
        cache[name] = (value[0], str(value[1] or ""))
        return cache[name]

    def expand(kind, name):
        if kind == "text":
            return "text:" + name, snapshot._data.texts[name]
        return variable(name)

    pieces = [(source, text) for source, text in _render(template, expand) if text]
    parts = []
    if billing:
        parts.append(Part("billing", billing + ("\n" if pieces or newline else "")))
    for source, text in pieces:
        if source == "literal" and parts and parts[-1].source == "literal":
            parts[-1] = Part("literal", parts[-1].text + text)
        else:
            parts.append(Part(source, text))
    if "structured" not in template.variables:
        contract = str(sources.structured() or "")
        if contract:
            if parts:
                parts.append(Part("literal", "\n\n"))
            parts.append(Part("relay:structured", contract))
    _check_head_size(plan.profile, parts)
    head = "".join(part.text for part in parts)
    return head, parts, list(dict.fromkeys(warnings)), report


def _check_head_size(profile, parts):
    """ApiError 413 (prompt_too_large) when the head would be longer than MAX_HEAD_BYTES in UTF-8; nothing is joined yet.

    The sizes of the pieces are added up first: a template that names a text a thousand times must not build 100 MiB of
    string to find that out. The client reads the message (the runtime passes an ApiError on), so it names the profile and
    the limit and quotes none of the texts.
    """
    characters = sum(len(part.text) for part in parts)
    if characters * 4 <= MAX_HEAD_BYTES:
        return
    if characters > MAX_HEAD_BYTES:
        size = "at least %d" % characters
    else:
        exact = sum(len(part.text.encode("utf-8", "surrogatepass")) for part in parts)
        if exact <= MAX_HEAD_BYTES:
            return
        size = str(exact)
    raise apierr.ApiError(
        413, "prompt_too_large",
        "the system prompt that profile '%s' builds is %s bytes, more than the %d bytes allowed: shorten the texts the "
        "profile uses, or cut more of the client's system prompt with client_system.edits"
        % (_clip(profile, 80), size, MAX_HEAD_BYTES))


class Snapshot:
    """An immutable compiled revision of the prompts document.

    Built by compile_document (or directly from a document: it is then validated the same way, with text files read
    relative to the folder of source_path or the working directory). `rev` is None for a draft compiled for a preview.

    `literal_document` is the document the revision was compiled from with every {"file": ...} text replaced by the text that
    was read for it: compiling it gives the same revision in everything a request can see (heads, edits, generation, rules,
    Codex texts), whatever has become of the files. `fingerprint` is the sha256 of its canonical JSON, the name by which a
    conversation pin recognises a revision across restarts. `restored` is True for a revision that was read back from the
    pins folder (see PromptStore): it serves the conversations that were started on it and is never the current one.
    """

    def __init__(self, rev: int, document: dict, source_path: str | None = None, *, base_dir=None, _compiled=None,
                 _restored=False, _fingerprint=None):
        self._rev = rev
        self._source_path = source_path
        self._document = _copy(document)
        if _compiled is None:
            if base_dir is None:
                base_dir = pathlib.Path(source_path).parent if source_path else pathlib.Path.cwd()
            _compiled = _Compiler(document, base_dir).compile()
        self._data = _compiled
        self._literal = _compiled.literal
        self._restored = bool(_restored)
        self._fingerprint = _UNSET if _fingerprint is None else _fingerprint
        self._extra_patterns = {}
        self._token = next(_TOKENS)        # what the memory of edited system prompts tells this snapshot by

    @property
    def rev(self):
        return self._rev

    @property
    def source_path(self):
        return self._source_path

    @property
    def document(self) -> dict:
        return _copy(self._document)

    @property
    def restored(self) -> bool:
        return self._restored

    @property
    def literal_document(self) -> dict:
        """The document with the texts of its {"file": ...} entries put in; a copy."""
        return _copy(self._literal if self._literal is not None else self._document)

    @property
    def fingerprint(self) -> str | None:
        """sha256 (hex) of the canonical JSON of the literal document, or None when that is longer than MAX_REVISION_BYTES.

        The canonical JSON keeps the order of the keys (the order of codex.models and of the profiles means something), has
        no spaces and does not escape non-ASCII text. The same literal content gives the same fingerprint however it was
        written (a text in the document or in a file, with CRLF or LF line ends)."""
        if self._fingerprint is _UNSET:
            blob = self._canonical()
            self._fingerprint = hashlib.sha256(blob).hexdigest() if blob is not None else None
        return self._fingerprint

    def _canonical(self) -> bytes | None:
        """The bytes the fingerprint is taken of (and a stored revision is made of); None when they would be too many.

        A text file named by many texts is one string in memory but many in this JSON, so the size is estimated first, over
        the strings of the document: a document that would write out gigabytes is turned down before anything is built."""
        document = self._literal if self._literal is not None else self._document
        if _weight(document) > MAX_REVISION_BYTES:
            return None
        try:
            blob = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return None
        return blob if len(blob) <= MAX_REVISION_BYTES else None

    @property
    def warnings(self) -> list[str]:
        """What the admin should know about this revision: disabled protection, overridden protection, hidden tools."""
        return list(self._data.warnings)

    @property
    def files(self) -> tuple:
        """((path, mtime_ns, size), ...) of the text files the revision read."""
        return self._data.files

    def __repr__(self):
        return "<Snapshot rev=%s profiles=%s>" % (self._rev, ",".join(self._data.profile_order))

    def profile(self, name: str) -> dict:
        """The normalized profile: KeyError for an unknown name.

        Every section is present with its default (history.mid_system "keep", empty
        user_blocks / tools.edits / client_system.edits, generation {} with only non-null
        values, publish_as_model False); every text is a literal string ({"file": ...} read,
        {{text:<name>}} expanded inside edit "text" / "with" fields, but not inside "head",
        which compose_head renders). The built-in passthrough profile is included.
        """
        return _copy(self._data.profiles[name].normalized)

    def profile_names(self) -> list[str]:
        """Built-in and document profiles, in a stable order (built-ins first, then document order)."""
        return list(self._data.profile_order)

    def relay_text(self, name: str) -> str | None:
        """The user's text for relay_texts[name] (one of RELAY_TEXT_NAMES except codex_base), None = built-in."""
        if name not in RELAY_TEXT_NAMES:
            raise KeyError(name)
        return self._data.relay.get(name)

    def regex(self, pattern: str, flags: str = ""):
        """The pattern compiled when the revision was compiled (plan I11), a pattern of the `regex` module; flags is a subset
        of "ims".

        Run it with regex_search, regex_spans, regex_remove or regex_replace (or the keywords of Deadline.run): they let
        the other threads go on while it matches and stop it at the time limit. A pattern the revision does not contain is
        checked and compiled by the same rules, so an unsafe pattern never gets through this door; it raises
        PromptConfigError.
        """
        flags = _normalize_flags(flags, "flags")
        compiled = self._data.patterns.get((pattern, flags)) or self._extra_patterns.get((pattern, flags))
        if compiled is None:
            compiled = _compile_pattern(pattern, flags, "pattern")
            if len(self._extra_patterns) < 256:
                self._extra_patterns[(pattern, flags)] = compiled
        return compiled

    def codex_base(self, model: str) -> str | None:
        """The base instructions /codex/models publishes for model, or None for the built-in text."""
        base = self._data.codex_base
        if base is None:
            return None
        if base[0] == "all":
            return base[1]
        wanted = str(model or "").casefold()
        for _glob, matcher, text in base[1]:
            if matcher.match(wanted):
                return text
        return None

    def codex_overrides(self, model: str) -> dict:
        """codex.models overrides for model: the first matching glob in document order, {} when none."""
        wanted = str(model or "").casefold()
        for _glob, matcher, overrides in self._data.codex_models:
            if matcher.match(wanted):
                return dict(overrides)
        return {}


def compile_document(doc: dict, base_dir: pathlib.Path, *, rev: int) -> Snapshot:
    """Validate and compile a prompts document; raises PromptConfigError."""
    compiler = _Compiler(doc, base_dir)
    data = compiler.compile()
    return Snapshot(rev, doc, None, base_dir=base_dir, _compiled=data)


def structured_contract(snapshot: Snapshot | None, output_spec: dict | None, *, tools: bool, tool_choice) -> str:
    """The structured-output contract for a head (plan I10), shared by the runtime and preview.

    "" without output_spec. The default text is structured.instructions(output_spec, tools=...,
    tool_choice=...) called, never copied; relay_texts.structured, when set, wraps it with
    {{default}}, {{schema}} and {{schema_name}}.
    """
    if not output_spec:
        return ""
    default = structured.instructions(output_spec, tools=bool(tools), tool_choice=tool_choice)
    template = snapshot._data.relay_templates.get("structured") if snapshot is not None else None
    if template is None:
        return default
    if output_spec.get("kind") == "json_schema" and "schema" in output_spec:
        schema = json.dumps(output_spec["schema"], ensure_ascii=False, separators=(",", ":"))
    else:
        schema = '{"type":"object"}'
    return _fill(template, {"default": default, "schema": schema, "schema_name": str(output_spec.get("name") or "")})


# -- the store ------------------------------------------------------------------------------------------------------------

_PROTECTED_CACHE = [None, ()]


def _protected_rules():
    """The built-in rules compiled; recompiled if PROTECTED_RULES is replaced."""
    if _PROTECTED_CACHE[0] is not PROTECTED_RULES:
        compiled = []
        for rule in PROTECTED_RULES:
            conditions = []
            for key, value in rule["if"].items():
                patterns = [value] if isinstance(value, str) else value
                if key in _BOOL_CONDITIONS:
                    conditions.append((key, True, value))
                else:
                    conditions.append((key, False, tuple(_glob_regex(pattern) for pattern in patterns)))
            compiled.append(_Rule(rule["name"], tuple(conditions), rule["use"]))
        _PROTECTED_CACHE[:] = [PROTECTED_RULES, tuple(compiled)]
    return _PROTECTED_CACHE[1]


class _Pin:
    """A conversation's hold on a revision.

    `print` is the fingerprint of the revision (None for the legacy pin, and while nothing is persisted), `digest` the
    (key digest, tag) it is stored under, `disk` what the pins file last said about it, (print, profile), and `disk_touched`
    when that was, in clock units; `restored` says the pin came back from the pins file after a restart."""
    __slots__ = ("rev", "created", "touched", "profile", "print", "digest", "disk", "disk_touched", "restored")

    def __init__(self, rev, now, print_=None):
        self.rev, self.created, self.touched, self.profile = rev, now, now, None
        self.print, self.digest, self.disk, self.disk_touched, self.restored = print_, None, None, None, False


class _Dormant:
    """A pin read from the pins file that no request has claimed yet: its conversation has not come back.

    `print` is the fingerprint of its revision, None for the legacy pin; `created` and `touched` are wall-clock times."""
    __slots__ = ("tag", "purpose", "print", "profile", "created", "touched")

    def __init__(self, tag, purpose, print_, profile, created, touched):
        self.tag, self.purpose, self.print, self.profile = tag, purpose, print_, profile
        self.created, self.touched = created, touched


def _pin_digest(key):
    """What a pin is stored under: the sha256 of the canonical JSON of (conversation key, purpose). The key is never written."""
    try:
        text = json.dumps([key[0], key[1]], ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _stored_pin(digest, row):
    """The dormant pin a record of the pins file stands for, or None when the record is not one.

    "rev" is the fingerprint of the revision the conversation was started on, or LEGACY_REV (0) for a conversation that
    started while no prompts file was loaded."""
    if not (isinstance(digest, str) and _HEX64.fullmatch(digest) and isinstance(row, dict)):
        return None
    tag, purpose, profile = row.get("tag"), row.get("purpose"), row.get("profile")
    created, touched = row.get("created"), row.get("touched")
    if not (isinstance(tag, str) and _HEX8.fullmatch(tag) and isinstance(purpose, str) and len(purpose) <= 128
            and (profile is None or isinstance(profile, str) and len(profile) <= 128)
            and _number(created) and _number(touched)):
        return None
    revision = row.get("rev")
    if type(revision) is int and revision == LEGACY_REV:
        print_ = None
    elif isinstance(revision, str) and _HEX64.fullmatch(revision):
        print_ = revision
    else:
        return None
    return _Dormant(tag, purpose, print_, profile, float(created), float(touched))


def _stored_row(tag, purpose, print_, profile, created, touched):
    return {"tag": tag, "purpose": purpose, "rev": LEGACY_REV if print_ is None else print_, "profile": profile,
            "created": round(created, 3), "touched": round(touched, 3)}


class _TooLarge(Exception):
    """A stored file is larger than it may be."""


def _read_limited(path, limit):
    """The bytes of a file of at most `limit` bytes (_TooLarge past that); a file a scanner holds for a moment is tried again."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            with open(path, "rb") as handle:
                blob = handle.read(limit + 1)
            break
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_PAUSE)
    if len(blob) > limit:
        raise _TooLarge(str(path))
    return blob


def _put(path, blob):
    """Write `blob` to `path` atomically: a temporary file in the same folder, flushed to disk, then os.replace (tried again
    while Windows says a reader holds the target)."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent), prefix=".pins-", suffix=".tmp",
                                         delete=False) as output:
            temporary = output.name
            output.write(blob)
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                temporary = None
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_PAUSE)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _tidy_temporaries(folder):
    """Remove the temporary files an interrupted write of ours left in a pins folder (a recent one may be a write that is
    going on). Anything else in the folder is not ours and stays."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        try:
            if (name.startswith(".pins-") and name.endswith(".tmp") and entry.is_file(follow_symlinks=False)
                    and time.time() - entry.stat().st_mtime > STALE_TEMP_SECONDS):
                os.unlink(entry.path)
        except OSError:
            continue


def _tidy_revisions(folder, keep):
    """Remove from the folder of stored revisions the ones no pin names (`keep` is the set of fingerprints that are named),
    and the temporary files of interrupted writes. A file that is not named <64 hex digits>.json is not ours and stays."""
    _tidy_temporaries(folder)
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        stem, _dot, suffix = entry.name.rpartition(".")
        try:
            if suffix == "json" and _HEX64.fullmatch(stem) and stem not in keep and entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)
        except OSError:
            continue


class _DuplicateKey(ValueError):
    """A JSON object that repeats a key; the key is in the message for the author, not in the brief of the journal."""


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey("duplicate key %s" % json.dumps(key, ensure_ascii=False))
        out[key] = value
    return out


def _signature(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


_PIXEL = "data:image/png;base64,AAAA"


def _remote(url):
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _offline(value):
    """value with the remote images swapped for a data URL: preview parses a body and must not download anything."""
    if isinstance(value, list):
        items = [_offline(item) for item in value]
        return items if any(new is not old for new, old in zip(items, value)) else value
    if not isinstance(value, dict):
        return value
    out = None
    if value.get("type") in ("image_url", "input_image"):
        image = value.get("image_url")
        if _remote(image):
            out = {**value, "image_url": _PIXEL}
        elif isinstance(image, dict) and _remote(image.get("url")):
            out = {**value, "image_url": {**image, "url": _PIXEL}}
        elif _remote(value.get("url")):
            out = {**value, "url": _PIXEL}
        if out is not None:
            return out
    for key, item in value.items():
        new = _offline(item)
        if new is not item:
            if out is None:
                out = dict(value)
            out[key] = new
    return out if out is not None else value


class _PreviewSources:
    """HeadSources for preview: relay texts are the user's or the built-in ones, a relay task shows placeholders."""

    _PLACEHOLDERS = {
        "relay_task": "[RELAY TASK: role, local execution and project context are filled in by the relay when it owns "
                      "the request]",
        "local_exec": "[LOCAL EXECUTION: filled in by the relay when it owns the request]",
        "project_context": "[PROJECT CONTEXT: filled in by the relay when it owns the request]",
        "exec_reminder": "[LOCAL EXEC REMINDER: filled in by the relay when it owns the request]"}

    def __init__(self, snapshot, client_owned, contract, relay_task=None):
        self.client_owned = client_owned
        self.placeholders = []
        self._snapshot = snapshot
        self._contract = contract
        # A request is a relay task only when the client owns nothing and sent no tools (orchestrator.RelaySources):
        # a client with tools and no system gets empty relay blocks there, so the preview shows none either.
        self._relay_task = (not client_owned) if relay_task is None else relay_task

    def relay_role(self):
        import context
        text = self._snapshot.relay_text("role")
        return text if text is not None else context.DEFAULT_RELAY_SYSTEM_PROMPT

    def native_role(self):
        import orchestrator
        text = self._snapshot.relay_text("native_role")
        return text if text is not None else orchestrator.DEFAULT_NATIVE_TOOLS_ROLE

    def _placeholder(self, name):
        if not self._relay_task:
            return ""
        self.placeholders.append(name)
        return self._PLACEHOLDERS[name]

    def relay_task(self):
        return self._placeholder("relay_task")

    def local_exec(self):
        return self._placeholder("local_exec")

    def project_context(self):
        return self._placeholder("project_context")

    def exec_reminder(self):
        return self._placeholder("exec_reminder")

    def structured(self):
        return self._contract


def _prompt_purpose(parsed, identity):
    """The prompt-layer purpose of a request: client_context.prompt_purpose when P2 has added it, else request_purpose."""
    import client_context
    function = getattr(client_context, "prompt_purpose", None)
    if callable(function):
        try:
            return function(parsed, identity)
        except Exception:  # noqa: BLE001 - the preview shows what it can
            pass
    return getattr(parsed, "request_purpose", "")


class PromptStore:
    """Revisions, conversation pins, live reload and the in-memory record of recent heads.

    The file is explicit path > cfg["prompts_file"] > DEFAULT_FILE; a relative path is resolved
    against the folder of cfg["_config_path"] when that is set, otherwise against the working
    directory. The constructor loads the file when it exists; maybe_reload() also picks up a
    file that appears, changes or disappears later (plan 4.3).

    Pins survive a restart when the stored conversations are on (cfg["conversation_state"], cfg["conversation_state_dir"]; the
    pins live in <conversation_state_dir>/prompt-pins/, see PINS_DIR). The constructor reads them as dormant pins; the first
    plan() of a conversation that has one gives it a live pin on the revision it was started on: the current one when the
    fingerprints agree, else a revision made again from the stored literal document (a restored revision: it gets the next
    number, is kept with the history and never becomes the current one), else, when that is gone, the current revision. The
    file is written from maybe_reload() (at most once per PINS_WRITE_INTERVAL), at once from unpin() and before save() changes
    the file, and by close(), which the runtime calls when it stops. `wall` is the wall clock the stored times are in
    (time.time); `clock` stays the clock of everything in memory.
    """

    def __init__(self, cfg: dict, path: str | None = None, clock=time.monotonic, *, wall=None):
        self.cfg = cfg
        configured = pathlib.Path(str(path or cfg.get("prompts_file") or DEFAULT_FILE))
        if not configured.is_absolute() and cfg.get("_config_path"):
            configured = pathlib.Path(cfg["_config_path"]).parent / configured
        self.path = configured
        self.clock = clock
        self._wall = wall or time.time
        self._lock = threading.RLock()
        self._reload_lock = threading.RLock()
        self._persist_lock = threading.RLock()
        self._ring_lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._history = collections.OrderedDict()
        self._next_rev = 1
        self._pins = collections.OrderedDict()
        self._pins_path = self._pin_folder(cfg)
        self._dormant: dict[str, _Dormant] = {}
        self._broken: dict[str, str] = {}
        self._stored_prints: set[str] = set()
        self._dirty = False
        self._flushed = None
        self._failing = False
        self._closed = False
        self._errors: list[dict] = []
        self._ring = collections.deque(maxlen=RECENT_LIMIT)
        self._watched = [str(self.path)]
        self._signature = ((str(self.path), None),)
        self._missing_since = None
        self._checked = clock()
        with self._reload_lock:
            self._refresh("startup")
        if self._pins_path is not None:
            self._load_pins()

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    # -- loading ----------------------------------------------------------------------------------------------------
    @staticmethod
    def _emit(kind, message, level="info", **fields):
        journal.emit(kind, message, level, **fields)

    def _disk_signature(self):
        return tuple((path, _signature(path)) for path in self._watched)

    def _read_document(self):
        info = self.path.stat()
        signature = (info.st_mtime_ns, info.st_size)
        if not stat.S_ISREG(info.st_mode):
            raise self._unreadable("the prompts file is not a regular file", signature)
        if info.st_size > MAX_DOCUMENT_BYTES:
            raise self._unreadable("the prompts file is larger than %d MiB" % (MAX_DOCUMENT_BYTES // (1024 * 1024)),
                                   signature)
        data = self.path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise self._unreadable("the prompts file is not valid UTF-8", signature) from None
        if text.startswith("\ufeff"):
            text = text[1:]
        try:
            document = json.loads(text, object_pairs_hook=_no_duplicates)
        except (ValueError, RecursionError) as exc:
            brief = "invalid JSON: duplicate key" if isinstance(exc, _DuplicateKey) else None
            raise self._unreadable("invalid JSON: %s" % exc, signature, brief) from None
        return document, signature

    @staticmethod
    def _unreadable(message, signature, brief=None):
        """A file-level error that remembers the stamp the file had before it was read: a rewrite during the read shows up."""
        error = PromptConfigError("", message, brief)
        error.signature = signature
        return error

    def _compile(self, document, rev):
        compiler = _Compiler(document, self.path.parent)
        try:
            data = compiler.compile()
        except PromptConfigError as exc:
            return None, exc, compiler.files
        return Snapshot(rev, document, str(self.path), _compiled=data), None, compiler.files

    def _watch(self, main_signature, files):
        self._missing_since = None
        self._watched = [str(self.path)] + [path for path, _mtime, _size in files]
        self._signature = ((str(self.path), main_signature),) + tuple(
            (path, None if mtime is None else (mtime, size)) for path, mtime, size in files)

    def _refresh(self, source):
        """Read the file again; install a new revision, remember the error or forget the snapshot (called under _reload_lock)."""
        try:
            document, main_signature = self._read_document()
        except FileNotFoundError:
            self._vanished()
            return
        except PromptConfigError as exc:
            self._watch(getattr(exc, "signature", None), ())
            self._reject(exc)
            return
        except OSError as exc:
            # A file that cannot be opened at this instant (an editor or a scanner holds it) is not judged: the next
            # look tries again, so the error cannot outlive the lock.
            self._watch(None, ())
            error = PromptConfigError("", "cannot read the prompts file: %s" % (exc.strerror or exc))
            error.transient = True
            self._reject(error)
            return
        snapshot, error, files = self._compile(document, self._next_rev)
        self._watch(main_signature, files)
        if error is not None:
            self._reject(error)
            return
        self._install(snapshot, source, document)

    def _vanished(self):
        """The prompts file is not there: it counts as removed only when two looks, RELOAD_INTERVAL apart, both miss it.

        An editor that saves by deleting the file and creating it again, or a sync client, leaves it missing for a moment.
        Believing the first look would send every conversation that starts in that moment down the legacy path, and a
        conversation keeps the path it started on until it is unpinned. While the removal is not confirmed the current
        revision keeps serving; the signature that was remembered stays, so the next look sees the difference again, and
        _watch (any look that finds the file, or a save) forgets the pending removal.
        Without a revision to lose (nothing loaded, or only an error remembered) the file is simply forgotten at once.
        """
        now = self.clock()
        with self._lock:
            if self._snapshot is not None:
                if self._missing_since is None:
                    self._missing_since = now
                    return
                if now - self._missing_since < RELOAD_INTERVAL:
                    return
        self._watch(None, ())
        with self._lock:
            had = self._snapshot is not None or bool(self._errors)
            self._snapshot, self._errors = None, []
        if had:
            self._emit("prompt.reload", "Файл промптов удалён: новые диалоги идут прежним путём", rev=None,
                       source="removed")

    def _reject(self, error):
        """Remember a problem of the file on disk and journal it; a transient one that repeats is journaled once."""
        record = {"message": error.message, "param": error.path or None, "ts": round(time.time(), 3)}
        with self._lock:
            known = [(item["message"], item["param"]) for item in self._errors]
            if getattr(error, "transient", False) and known == [(record["message"], record["param"])]:
                return
            self._errors = [record]
        self._emit("prompt.config_error",
                   "Файл промптов не принят: %s" % _clip(("%s: %s" % (error.path, error.brief)) if error.path
                                                         else error.brief, 300),
                   "error", param=_clip(error.path, 200) or None, error=_clip(error.brief, 300))

    def _install(self, snapshot, source, document):
        # The fingerprint is worked out here, not under a lock and not when the first pin is written: the pins that name
        # this revision come as soon as it is the current one.
        nameable = self._pins_path is None or snapshot.fingerprint is not None
        with self._lock:
            self._snapshot = snapshot
            self._history[snapshot.rev] = snapshot
            self._trim_history()
            self._next_rev = max(self._next_rev, snapshot.rev + 1)
            self._errors = []
        canonical = json.dumps(document, sort_keys=True, ensure_ascii=False)
        self._emit("prompt.reload", "Промпты загружены: ревизия %d" % snapshot.rev, rev=snapshot.rev, source=source,
                   profiles=len(snapshot._data.profile_order) - 1,
                   rules=len(snapshot._data.override_rules) + len(snapshot._data.user_rules),
                   bytes=snapshot._data.size, sha8=sha8(canonical), warnings=len(snapshot._data.warnings))
        if not nameable:
            self._emit("prompt.pins_error", "Ревизия %d слишком велика, чтобы закрепления пережили перезапуск" % snapshot.rev,
                       "warn", reason="revision_too_large", rev=snapshot.rev, limit=MAX_REVISION_BYTES)

    def _trim_history(self):
        """Forget the oldest revisions past SNAPSHOT_HISTORY, never the current one (called under _lock)."""
        keep = self._snapshot.rev if self._snapshot is not None else None
        while len(self._history) > SNAPSHOT_HISTORY:
            for rev in self._history:
                if rev != keep:
                    del self._history[rev]
                    break
            else:
                return

    def maybe_reload(self) -> None:
        """Recompile when the prompts file or a referenced text changed; stat at most once per RELOAD_INTERVAL.

        It is also where the pins file is written when something changed (_flush_pins): the runtime calls it on every request."""
        now = self.clock()
        due = False
        with self._lock:
            if now - self._checked >= RELOAD_INTERVAL:
                self._checked = now
                due = True
        if due:
            with self._reload_lock:
                self._reload_if_changed()
        self._flush_pins()

    def _reload_if_changed(self):
        if self._disk_signature() != self._signature:
            self._refresh("file")
        else:
            self._missing_since = None

    def errors(self) -> list[dict]:
        """Problems of the file on disk that kept the previous revision: [{"message", "param", "ts"}], [] when valid."""
        with self._lock:
            return [dict(item) for item in self._errors]

    # -- saving -----------------------------------------------------------------------------------------------------
    def save(self, document: dict, base_rev: int | None) -> Snapshot:
        """Validate `document`, write it atomically and make it the current revision.

        base_rev is the revision the editor started from: any other current revision is a conflict (409), and None is
        accepted only while no revision is loaded. PromptConfigError says what is wrong with the document.
        """
        with self._reload_lock:
            self._reload_if_changed()
            compiler = _Compiler(document, self.path.parent)
            data = compiler.compile()
            current = self.snapshot()
            expected = current.rev if current is not None else None
            if base_rev != expected:
                raise apierr.conflict(
                    "the prompts changed since revision %s was read (now %s): load them again before saving"
                    % ("none" if base_rev is None else base_rev, "none" if expected is None else expected))
            snapshot = Snapshot(self._next_rev, document, str(self.path), _compiled=data)
            # The pins of the revision that is about to stop being the current one are on disk before the file changes.
            self._flush_pins(force=True)
            main_signature = self._write(document)
            self._watch(main_signature, compiler.files)
            self._install(snapshot, "admin", document)
            return snapshot

    def _write(self, document):
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=str(self.path.parent),
                                             prefix=".prompts-", suffix=".tmp", delete=False) as output:
                temporary = output.name
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if self.path.exists():
                os.chmod(temporary, stat.S_IMODE(os.stat(self.path).st_mode))
                try:
                    shutil.copyfile(self.path, str(self.path) + ".bak")
                except OSError:
                    pass
            info = os.stat(temporary)
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    # Windows refuses to replace a file that a reader or a virus scanner has open at this instant.
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(REPLACE_PAUSE)
            return self._written(info, payload)
        except (OSError, ValueError):
            raise apierr.service_unavailable("Cannot save the prompts file") from None
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.chmod(temporary, stat.S_IWRITE | stat.S_IREAD)
                    os.unlink(temporary)
                except OSError:
                    pass

    def _written(self, before, payload):
        """The (mtime_ns, size) of the file that save just put in place, to be remembered as already known.

        A rename keeps the stamp, so it is normally the one `before` (the temporary file) had. If the file system stamped it
        otherwise, or something wrote over it at once, the bytes decide: only our own bytes are remembered, anything else
        stays unknown and the next maybe_reload() picks it up as the outside edit it is.
        """
        after = _signature(self.path)
        if after == (before.st_mtime_ns, before.st_size):
            return after
        try:
            ours = self.path.read_bytes() == payload.encode("utf-8")
        except OSError:
            ours = False
        return after if ours else None

    # -- planning ---------------------------------------------------------------------------------------------------
    def plan(self, inp: PlanInput) -> Plan:
        """Select the rule and profile for a request, honouring the conversation pin (plan I3).

        Order: rules with override_protected, the built-in protected rules (unless protected.enabled is false), the
        implicit rule of a virtual profile, the user's rules (the first enabled one that matches), default_profile and,
        when none applies, the legacy path. Refusals are apierr.ApiError raised before any upstream call (plan 4.3): 400
        with param "x-proxi-profile" when $header names a missing profile, 404 not_found_error with param "model" when
        <model>@<profile> names a missing profile (also with no file loaded).

        A conversation keeps the revision it first met, also when that was "no file" (LEGACY_REV). A profile the request
        names itself (@profile, x-proxi-profile) that the pinned revision lacks, while the current one has it, moves the
        pin to the current revision rather than refusing a model the catalog offers.

        After a restart the first request of a conversation whose pin was stored takes the pin back (see PromptStore); the
        plan is `pinned` as it is for any conversation that has met a request before. This is the only place plan() reads
        the disk, and only once for each revision it has to make again.
        """
        now = self.clock()
        key = (inp.conversation_key, inp.purpose) if inp.conversation_key else None
        if key is not None and self._dormant and key not in self._pins:
            self._wake(key, now)
        with self._lock:
            current = self._snapshot
            chosen, pinned, pin = current, False, None
            if key is not None:
                pin = self._pins.get(key)
                if pin is not None and now - pin.touched > PIN_IDLE_SECONDS:
                    del self._pins[key]
                    self._dirty = True
                    pin = None
                if pin is not None:
                    if pin.rev == LEGACY_REV:
                        chosen, pinned = None, True
                    elif current is not None and pin.rev == current.rev:
                        chosen, pinned = current, True
                    elif pin.rev in self._history:
                        chosen, pinned = self._history[pin.rev], True
                    else:
                        pin = None
                if pin is None:
                    pin = _Pin(current.rev if current is not None else LEGACY_REV, now, self._print_of(current))
                    self._pins[key] = pin
                    self._trim_pins(now)
                pin.touched = now
                self._pins.move_to_end(key)
        if chosen is None and current is not None and inp.virtual_profile:
            chosen, pinned = current, False
        try:
            plan = self._select(chosen, inp, pinned)
        except apierr.ApiError:
            if chosen is current or current is None:
                raise
            plan = self._select(current, inp, False)
            chosen, pinned = current, False
        if plan.snapshot is not None and chosen is not None and key is not None and pin.rev != chosen.rev:
            with self._lock:
                pin.rev = chosen.rev
                pin.print = self._print_of(chosen)
        if key is not None:
            pin.profile = plan.profile
            if self._pins_path is not None:
                self._note(pin, now)
        return plan

    def _print_of(self, snapshot):
        """The fingerprint a pin on `snapshot` is stored by (None for the legacy pin and while nothing is persisted)."""
        return snapshot.fingerprint if snapshot is not None and self._pins_path is not None else None

    def _note(self, pin, now):
        """Say that the pins file is out of date when what it holds about this pin is not what the pin is now.

        The touch is rough on purpose: the file hears of it when the stored one is PIN_TOUCH_INTERVAL old."""
        disk = pin.disk
        if (disk is None or disk[0] != pin.print or disk[1] != pin.profile
                or now - pin.disk_touched >= PIN_TOUCH_INTERVAL):
            self._dirty = True

    def _trim_pins(self, now):
        while self._pins:
            key, pin = next(iter(self._pins.items()))
            if len(self._pins) > MAX_PINS or now - pin.touched > PIN_IDLE_SECONDS:
                del self._pins[key]
                self._dirty = True
            else:
                break

    def _select(self, snapshot, inp, pinned):
        """The plan of `inp` under one revision (no pins); None snapshot means the legacy path."""
        if snapshot is None:
            if inp.virtual_profile:
                raise self._unknown_virtual(inp, None)
            return Plan(pinned=pinned)
        data = snapshot._data
        for rule in data.override_rules:
            if rule.matches(inp):
                plan = self._use(snapshot, rule.use, rule.name, inp, pinned, False)
                if plan is not None:
                    return plan
        if data.protected:
            for rule in _protected_rules():
                if rule.matches(inp):
                    return self._plan(snapshot, rule.use, rule.name, inp, pinned, True)
        if inp.virtual_profile:
            return self._use(snapshot, "$virtual", VIRTUAL_RULE_NAME, inp, pinned, False)
        for rule in data.user_rules:
            if rule.matches(inp):
                plan = self._use(snapshot, rule.use, rule.name, inp, pinned, False)
                if plan is not None:
                    return plan
        if data.default_profile is not None:
            return self._plan(snapshot, data.default_profile, DEFAULT_RULE_NAME, inp, pinned, False)
        return Plan(rev=snapshot.rev, pinned=pinned, snapshot=snapshot)

    @staticmethod
    def _unknown_virtual(inp, snapshot):
        where = ("the prompts file has no such profile" if snapshot is not None
                 else "no prompts file is loaded")
        return apierr.ApiError(
            404, "not_found_error",
            "model '%s@%s' names the profile '%s', which does not exist (%s)"
            % (_clip(inp.requested_model or inp.model, 80), _clip(inp.virtual_profile, 80),
               _clip(inp.virtual_profile, 80), where), param="model")

    def _use(self, snapshot, use, rule_name, inp, pinned, protected):
        if use == "$legacy":
            return Plan(rev=snapshot.rev, rule=rule_name, pinned=pinned, snapshot=snapshot)
        if use == "$header":
            name = inp.header_profile
            if not name:
                return None
            if name not in snapshot._data.profiles:
                raise apierr.bad_request(
                    "x-proxi-profile names the profile '%s', which does not exist in the prompts file (revision %s)"
                    % (_clip(name, 80), snapshot.rev), param="x-proxi-profile")
            return self._plan(snapshot, name, rule_name, inp, pinned, protected)
        if use == "$virtual":
            name = inp.virtual_profile
            if not name:
                return None
            if name not in snapshot._data.profiles:
                raise self._unknown_virtual(inp, snapshot)
            return self._plan(snapshot, name, rule_name, inp, pinned, protected)
        return self._plan(snapshot, use, rule_name, inp, pinned, protected)

    @staticmethod
    def _plan(snapshot, name, rule_name, inp, pinned, protected):
        profile = snapshot._data.profiles[name]
        plan = Plan(rev=snapshot.rev, profile=name, rule=rule_name, pinned=pinned, protected=protected,
                    body_edits=profile.body_edits, generation=dict(profile.generation), snapshot=snapshot)
        object.__setattr__(plan, "_input", inp)
        return plan

    # -- pins -------------------------------------------------------------------------------------------------------
    def pins(self) -> list[dict]:
        """[{"conversation": tag, "purpose", "rev", "profile", "age_s"}], the most recently used first.

        The pins that came back from the pins file after a restart have "restored": true; the ones whose conversation has not
        come back yet (dormant) are listed last, with the number of the revision they stand for when it is in memory and
        None when it is still only a stored document (a legacy pin has rev 0, LEGACY_REV)."""
        now = self.clock()
        with self._lock:
            items = [(key, pin.rev, pin.profile, pin.created, pin.restored) for key, pin in reversed(self._pins.items())
                     if now - pin.touched <= PIN_IDLE_SECONDS]
            dormant = sorted(self._dormant.values(), key=lambda entry: entry.touched, reverse=True)
            known = {}
            if dormant:
                for snapshot in [self._snapshot, *self._history.values()]:
                    if snapshot is not None:
                        known.setdefault(snapshot.fingerprint, snapshot.rev)
        rows = []
        for key, rev, profile, created, restored in items:
            row = {"conversation": journal.tag(key[0]), "purpose": key[1], "rev": rev, "profile": profile,
                   "age_s": round(now - created, 1)}
            if restored:
                row["restored"] = True
            rows.append(row)
        wall = self._wall()
        for entry in dormant:
            if wall - entry.touched <= PIN_IDLE_SECONDS:
                rows.append({"conversation": entry.tag, "purpose": entry.purpose,
                             "rev": LEGACY_REV if entry.print is None else known.get(entry.print),
                             "profile": entry.profile, "age_s": round(max(0.0, wall - entry.created), 1),
                             "restored": True})
        return rows

    def unpin(self, conversation_key: str | None = None) -> int:
        """Forget the pins of one conversation (its raw key or the tag pins() shows); None forgets all of them.

        The pins that came back from the pins file and have not been used yet are forgotten too, and the file is written at
        once, so that an unpin is not undone by a restart."""
        with self._lock:
            if conversation_key is None:
                count = len(self._pins) + len(self._dormant)
                self._pins.clear()
                self._dormant.clear()
            else:
                doomed = [key for key in self._pins
                          if key[0] == conversation_key or journal.tag(key[0]) == conversation_key]
                for key in doomed:
                    del self._pins[key]
                tag = journal.tag(conversation_key)
                asleep = [digest for digest, entry in self._dormant.items()
                          if entry.tag == conversation_key or (tag is not None and entry.tag == tag)]
                for digest in asleep:
                    del self._dormant[digest]
                count = len(doomed) + len(asleep)
            if count:
                self._dirty = True
        if count:
            self._flush_pins(force=True)
        return count

    # -- pins on disk -----------------------------------------------------------------------------------------------
    @staticmethod
    def _pin_folder(cfg):
        """<conversation_state_dir>/prompt-pins; None when the stored conversations are off or have no folder."""
        if not cfg.get("conversation_state", True):
            return None
        directory = cfg.get("conversation_state_dir")
        if not directory or not isinstance(directory, (str, os.PathLike)):
            return None
        try:
            return pathlib.Path(os.path.abspath(os.fspath(directory))) / PINS_DIR
        except (TypeError, ValueError, OSError):
            return None

    def _pins_problem(self, reason, exc=None):
        self._emit("prompt.pins_error", "Закрепления промптов не прочитаны: %s" % reason, "warn", reason=reason,
                   error=None if exc is None else type(exc).__name__)

    def _load_pins(self):
        """Read the pins file into dormant pins; a request of the conversation wakes its pin (plan()).

        Nothing is written here: a store that only looks (a script that lists the models) leaves the files as they are. A
        file that cannot be read or is not a pins file is ignored whole, a record that is not a pin one by one."""
        wall = self._wall()
        try:
            blob = _read_limited(self._pins_path / PINS_FILE, MAX_PINS_BYTES)
        except FileNotFoundError:
            return
        except (OSError, _TooLarge) as exc:
            self._pins_problem("too_large" if isinstance(exc, _TooLarge) else "unreadable", exc)
            return
        try:
            payload = json.loads(blob.decode("utf-8"))
            version = payload["format"]
            stored = payload["pins"] if type(version) is int and version == PINS_FORMAT else None
            if not isinstance(stored, dict):
                raise ValueError("not a pins file")
        except (ValueError, KeyError, TypeError, RecursionError):
            self._pins_problem("corrupt")
            return
        entries, invalid, expired = [], 0, 0
        for digest, row in stored.items():
            entry = _stored_pin(digest, row)
            if entry is None:
                invalid += 1
            elif wall - entry.touched > PIN_IDLE_SECONDS:
                expired += 1
            else:
                entries.append((digest, entry))
        entries.sort(key=lambda item: item[1].touched)
        over = max(0, len(entries) - MAX_PINS)
        entries = entries[over:]
        with self._lock:
            self._dormant = dict(entries)
            if invalid or expired or over:
                self._dirty = True
        revisions = {entry.print for _digest, entry in entries if entry.print is not None}
        self._emit("prompt.pins_loaded", "Закрепления промптов прочитаны с диска: %d" % len(entries),
                   "info" if entries else "debug", pins=len(entries), revisions=len(revisions),
                   legacy=sum(1 for _digest, entry in entries if entry.print is None), expired=expired, invalid=invalid,
                   over_limit=over)

    def _wake(self, key, now):
        """Give the conversation the live pin its stored one stands for, when it has a stored one and no live pin."""
        digest = _pin_digest(key)
        if digest is None:
            return
        with self._lock:
            if digest not in self._dormant or key in self._pins:
                return
        try:
            # A restored revision takes a number and joins the history: not while a reload or a save does the same.
            with self._reload_lock:
                self._wake_pin(key, digest, now)
        except Exception as exc:  # noqa: BLE001 - a stored pin must never take a request down
            with self._lock:
                self._dormant.pop(digest, None)
                self._dirty = True
            self._emit("prompt.pins_error", "Закрепление промпта не восстановлено: %s" % type(exc).__name__, "error",
                       reason="restore", error=type(exc).__name__)

    def _wake_pin(self, key, digest, now):
        with self._lock:
            entry = self._dormant.get(digest)
            if entry is None or key in self._pins:
                return
            current = self._snapshot
        wall = self._wall()
        snapshot, source, reason = None, None, None
        if wall - entry.touched > PIN_IDLE_SECONDS:
            reason = "expired"
        elif entry.print is None:
            source = "legacy"
        elif current is not None and current.fingerprint == entry.print:
            snapshot, source = current, "current"
        else:
            snapshot, reason = self._revision(entry.print, entry.tag)
            source = "revision"
        with self._lock:
            if self._dormant.get(digest) is not entry or key in self._pins:
                return                  # unpinned, or pinned afresh, while the revision was being made
            del self._dormant[digest]
            if reason is None:
                # The file already says what this pin says: it is written again only when something about it changes (its
                # touch, once it is a minute old), not because it woke.
                pin = _Pin(LEGACY_REV if snapshot is None else snapshot.rev, now, entry.print)
                pin.created = now - max(0.0, wall - entry.created)
                pin.touched = now - max(0.0, wall - entry.touched)
                pin.profile, pin.restored = entry.profile, True
                pin.disk, pin.disk_touched = (entry.print, entry.profile), pin.touched
                self._pins[key] = pin
                self._trim_pins(now)
            else:
                self._dirty = True      # the file names a pin that cannot be taken back: it is not named again
        if reason is None:
            self._emit("prompt.pin_restored", "Закрепление диалога восстановлено после перезапуска: ревизия %s" % (
                       "без файла" if snapshot is None else snapshot.rev), "debug", conversation=entry.tag,
                       purpose=entry.purpose, source=source, rev=LEGACY_REV if snapshot is None else snapshot.rev)
        else:
            self._emit("prompt.pin_dropped", "Закрепление диалога не восстановлено (%s): он начнёт с текущей ревизии" % reason,
                       "debug" if reason == "expired" else "info", conversation=entry.tag, purpose=entry.purpose,
                       reason=reason, revision=None if entry.print is None else entry.print[:8])

    def _find_revision(self, print_):
        """The revision with this fingerprint among the ones in memory, or None (called under _lock)."""
        for snapshot in [self._snapshot, *self._history.values()]:
            if snapshot is not None and snapshot.fingerprint == print_:
                return snapshot
        return None

    def _revision(self, print_, tag):
        """(snapshot, None) for the revision with this fingerprint, made again from its stored document when it is not in
        memory; (None, reason) when it cannot be. A revision is made once: it is looked for in memory first, and one that
        could not be made is not tried again (a disk that was busy for a moment is)."""
        with self._lock:
            found = self._find_revision(print_)
            broken = self._broken.get(print_)
        if found is not None:
            return found, None
        if broken is not None:
            return None, broken
        snapshot, reason = self._read_revision(print_)
        if snapshot is None:
            if reason != "revision_unreadable":
                with self._lock:
                    self._broken[print_] = reason
            self._emit("prompt.pins_error", "Ревизия закрепления не восстановлена: %s" % reason, "warn", reason=reason,
                       revision=print_[:8], conversation=tag)
        return snapshot, reason

    def _read_revision(self, print_):
        """(snapshot, None) made from revisions/<fingerprint>.json, or (None, reason). Called under _reload_lock."""
        path = self._pins_path / REVISIONS_DIR / (print_ + ".json")
        try:
            blob = _read_limited(path, MAX_REVISION_BYTES)
        except FileNotFoundError:
            return None, "revision_missing"
        except _TooLarge:
            return None, "revision_corrupt"
        except OSError:
            return None, "revision_unreadable"
        if hashlib.sha256(blob).hexdigest() != print_:
            return None, "revision_corrupt"
        try:
            document = json.loads(blob.decode("utf-8"), object_pairs_hook=_no_duplicates)
            data = _Compiler(document, self.path.parent, literal=True).compile()
        except Exception:  # noqa: BLE001 - not JSON, not a document, or one this build refuses: it is not a revision
            return None, "revision_invalid"
        with self._lock:
            rev = self._next_rev
            self._next_rev = rev + 1
        snapshot = Snapshot(rev, document, None, _compiled=data, _restored=True, _fingerprint=print_)
        with self._lock:
            self._history[rev] = snapshot
            self._trim_history()
        self._emit("prompt.revision_restored", "Ревизия %d восстановлена из закреплений" % rev, "info", rev=rev,
                   revision=print_[:8], profiles=len(data.profile_order) - 1, bytes=len(blob))
        return snapshot, None

    def _flush_pins(self, force=False):
        """Write the pins file when something changed since the last write: at most once per PINS_WRITE_INTERVAL, or at once
        with `force`. Returns whether it wrote.

        It never raises: a disk that fails is journaled (once until it works again) and tried again at the next
        opportunity, and the request that came by goes on. A request that comes by while another thread is writing does not
        wait for it (what changed since that write started is still marked, and goes out at the next opportunity)."""
        if self._pins_path is None or not self._dirty or self._closed:
            return False
        if not self._persist_lock.acquire(blocking=force):
            return False
        try:
            now = self.clock()
            if not self._dirty or self._closed:
                return False
            if not force and self._flushed is not None and now - self._flushed < PINS_WRITE_INTERVAL:
                return False
            self._flushed = now
            self._write_pins(now)
            if self._failing:
                self._failing = False
                self._emit("prompt.pins_saved", "Закрепления промптов снова записываются", "info")
            return True
        except Exception as exc:  # noqa: BLE001 - keeping pins is best effort
            with self._lock:
                self._dirty = True
            if not self._failing:
                self._failing = True
                self._emit("prompt.pins_error", "Закрепления промптов не записаны: %s" % type(exc).__name__, "warn",
                           reason="write", error=type(exc).__name__, errno=getattr(exc, "errno", None))
            return False
        finally:
            self._persist_lock.release()

    def _write_pins(self, now):
        """Write the pins file, the revisions it names and nothing else; remove the revisions it no longer names."""
        started = time.perf_counter()
        wall = self._wall()
        with self._lock:
            self._dirty = False
            live = [(key, pin, pin.rev, pin.print, pin.profile, pin.created, pin.touched)
                    for key, pin in self._pins.items() if now - pin.touched <= PIN_IDLE_SECONDS]
            asleep = list(self._dormant.items())
            known = {}
            for snapshot in [self._snapshot, *self._history.values()]:
                if snapshot is not None and snapshot.fingerprint is not None:
                    known.setdefault(snapshot.fingerprint, snapshot)
        folder = self._pins_path / REVISIONS_DIR
        self._pins_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        ready = {}

        def stored(print_):
            """Whether a document for this revision is on disk. One that this process knows is put there the first time it
            comes up (a file of an earlier run may be damaged: it is replaced by what the revision really is), and again when
            it goes missing; one that only a stored document stands for is trusted as it is until a conversation wakes it."""
            if print_ not in ready:
                target = folder / (print_ + ".json")
                have = print_ in self._stored_prints and target.exists()
                if not have and print_ in known:
                    blob = known[print_]._canonical()
                    if blob is not None and hashlib.sha256(blob).hexdigest() == print_:
                        _put(target, blob)
                        self._stored_prints.add(print_)
                        have = True
                elif not have:
                    have = target.exists()
                ready[print_] = have
            return ready[print_]

        rows, marks = [], []
        for key, pin, rev, print_, profile, created, touched in live:
            if print_ is None and rev != LEGACY_REV:
                continue                # a revision with no fingerprint cannot be named after a restart
            if print_ is not None and not stored(print_):
                continue
            if pin.digest is None:
                digest = _pin_digest(key)
                if digest is None:
                    continue
                pin.digest = (digest, journal.tag(key[0]))
            digest, tag = pin.digest
            when = wall - max(0.0, now - touched)
            rows.append((when, digest, _stored_row(tag, key[1], print_, profile, wall - max(0.0, now - created), when)))
            marks.append((pin, print_, profile, touched))
        for digest, entry in asleep:
            if wall - entry.touched > PIN_IDLE_SECONDS or (entry.print is not None and not stored(entry.print)):
                continue
            rows.append((entry.touched, digest, _stored_row(entry.tag, entry.purpose, entry.print, entry.profile,
                                                            entry.created, entry.touched)))
        rows.sort(key=lambda item: item[0])
        rows = rows[max(0, len(rows) - MAX_PINS):]
        named = {row["rev"] for _when, _digest, row in rows if row["rev"] != LEGACY_REV}
        payload = {"format": PINS_FORMAT, "saved_at": round(wall, 3),
                   "pins": {digest: row for _when, digest, row in rows}}
        blob = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        _put(self._pins_path / PINS_FILE, blob)
        _tidy_revisions(folder, named)
        _tidy_temporaries(self._pins_path)
        written = {digest for _when, digest, _row in rows}
        with self._lock:
            for pin, print_, profile, touched in marks:
                pin.disk, pin.disk_touched = (print_, profile), touched
            for digest, entry in asleep:
                if digest not in written and self._dormant.get(digest) is entry:
                    del self._dormant[digest]
        self._emit("prompt.pins_saved", "Закрепления промптов записаны: %d" % len(rows), "debug", pins=len(rows),
                   revisions=len(named), bytes=len(blob), duration_ms=round((time.perf_counter() - started) * 1000, 1))

    def close(self) -> None:
        """Write what has not been written yet. The store goes on serving; nothing more is written after this.

        The runtime calls it when it stops: a pin made in the last moments, and a touch, are then not lost."""
        if self._pins_path is not None:
            self._flush_pins(force=True)
            self._closed = True

    # -- what the model saw -------------------------------------------------------------------------------------------
    def record(self, entry: dict) -> None:
        """Keep one non-legacy request in the in-memory ring (RECENT_LIMIT entries, newest first in recent()).

        entry may carry "request": {"body", "dialect", "headers"} for preview without a pasted
        body (plan 4.3); the ring keeps it only for the newest few entries and never journals it. The body is kept
        as given, not copied (it can be a megabyte): the caller must not change it afterwards.
        """
        if not isinstance(entry, dict):
            return
        item = {key: value if key == "request" else _copy(value) for key, value in entry.items()}
        item.setdefault("ts", round(time.time(), 3))
        if "request" in item and not isinstance(item["request"], dict):
            del item["request"]
        with self._ring_lock:
            self._ring.append(item)
            if len(self._ring) > RECENT_REQUESTS_KEPT:
                self._ring[-1 - RECENT_REQUESTS_KEPT].pop("request", None)

    def recent(self, limit: int = 50) -> list[dict]:
        """The newest records, newest first; the request body is replaced by "has_request"."""
        try:
            count = max(0, min(int(limit), RECENT_LIMIT))
        except (TypeError, ValueError):
            count = 50
        with self._ring_lock:
            items = list(self._ring)[::-1][:count]
            rows = [{**{key: value for key, value in item.items() if key != "request"},
                     "has_request": "request" in item} for item in items]
        return [_copy(row) for row in rows]

    def request(self, request_id: str | None = None) -> dict | None:
        """The kept {"body", "dialect", "headers"} of the newest recorded request (or of request_id), None if not kept."""
        with self._ring_lock:
            for item in reversed(self._ring):
                if "request" not in item:
                    continue
                if request_id is None or item.get("request_id") == request_id:
                    return _copy(item["request"])
        return None

    # -- preview ----------------------------------------------------------------------------------------------------
    def preview(self, body: dict, dialect: str, headers: dict, document: dict | None = None) -> dict:
        """What the model would see for `body` (plan 4.1), without writing pins and without any upstream call.

        `document` is a draft compiled for this call only; without it the current revision is used (the pins of live
        conversations are not consulted: preview shows what a new conversation would get). The result has the fields of
        plan 4.1 and, besides them, "edits": what each client_system edit did ({"index", "op", "matched", "chars_before",
        "chars_after"}); "edits_unmatched" lists the ones that matched nothing. "tokens_estimate" is a rough guess of one
        token per four UTF-8 bytes of the head. A request that takes the legacy path (profile None) shows the head the
        existing code builds, with the blocks of a relay task left out. ApiError and PromptConfigError are raised as at
        runtime (bad body, a profile that does not exist, an invalid draft).
        """
        import client_context
        import config
        import validate
        if dialect not in DIALECTS:
            raise apierr.bad_request("dialect must be one of: %s" % ", ".join(DIALECTS), param="dialect")
        if not isinstance(body, dict):
            raise apierr.bad_request("the request must be a JSON object", param="request")
        self.maybe_reload()
        draft = compile_document(document, self.path.parent, rev=None) if document is not None else None
        snapshot = draft if draft is not None else self.snapshot()
        headers = {str(key): str(value) for key, value in (headers or {}).items()}
        cfg = {**config.DEFAULTS, **self.cfg}
        identity_cfg = {**cfg, "client_metadata_home": os.path.join(tempfile.gettempdir(), "proxi-preview-no-clients")}
        parse = {"chat": validate.parse_chat, "responses": validate.parse_responses,
                 "anthropic": validate.parse_anthropic}[dialect]
        data = _offline(body)
        parsed = parse(data, cfg)
        identity = client_context.describe(data, headers, identity_cfg)
        parsed.dialect = dialect
        parsed.conversation_key, parsed.parent_conversation_key = validate.conversation_affinity(data, headers, identity)
        parsed.request_purpose = client_context.request_kind(parsed, identity)
        parsed.prompt_purpose = _prompt_purpose(parsed, identity)
        inp = PlanInput.from_request(parsed, identity, headers, dialect)
        plan = self._select(snapshot, inp, False)
        if plan.legacy:
            return self._preview_legacy(plan, inp, parsed, identity, cfg, snapshot)
        warnings = []
        history_changes, tools_changes, after = [], [], parsed
        body_unmatched = []
        if plan.body_edits:
            try:
                import prompt_body
                profile = snapshot.profile(plan.profile)
                rewritten, changes = prompt_body.rewrite(data, dialect, profile, snapshot)
            except NotImplementedError:
                warnings.append("history and tool edits are not available in this build: the preview shows none "
                                "of them")
            else:
                for change in changes:
                    scope = change.get("scope")
                    tools = scope == "tools" if scope else ("tool" in change or change.get("section") == "tools")
                    (tools_changes if tools else history_changes).append(change)
                body_unmatched = list(prompt_body.unmatched_edits(profile, changes))
                after = parse(rewritten, cfg)
                for name in ("dialect", "conversation_key", "parent_conversation_key", "request_purpose",
                             "prompt_purpose"):
                    setattr(after, name, getattr(parsed, name))
        import orchestrator
        if after is not parsed:
            # Who owns the request and whether it is a relay task follow the body the client sent (plan I9), as at run time.
            after.prompt_origin = orchestrator.client_origin(parsed)
        native_tools = after.tools or []
        owned = orchestrator._client_owns_instructions(after, orchestrator._purpose(after), native_tools)
        contract = structured_contract(snapshot, after.structured_output, tools=bool(native_tools),
                                       tool_choice=after.tool_choice)
        sources = _PreviewSources(snapshot, owned, contract,
                                  relay_task=not owned and not orchestrator.client_tools(after))
        head, parts, notes, report = _compose(plan, orchestrator._client_system(after), sources)
        warnings = notes + warnings
        if sources.placeholders:
            warnings.append(_PLACEHOLDER_WARNING)
        return self._preview_result(plan, inp, head, parts, history_changes, tools_changes, report, warnings,
                                    body_unmatched)

    @staticmethod
    def _preview_result(plan, inp, head, parts, history_changes, tools_changes, report, warnings, body_unmatched=()):
        head_bytes = len(head.encode("utf-8", "surrogatepass"))
        unmatched = []
        if plan.profile is not None and plan.snapshot is not None:
            edits = plan.snapshot._data.profiles[plan.profile].system_edits
            for entry in report:
                if not entry["matched"] and not entry.get("skipped"):
                    unmatched.append({"scope": "client_system", "index": entry["index"], "op": entry["op"],
                                      "target": _edit_target(edits[entry["index"]])})
        unmatched.extend(body_unmatched)
        return {"rule": plan.rule, "profile": plan.profile, "rev": plan.rev, "protected": plan.protected,
                "purpose": inp.purpose, "client": inp.client, "model": inp.model, "head": head,
                "parts": part_spans(parts), "history_changes": history_changes, "tools_changes": tools_changes,
                "edits_unmatched": unmatched, "edits": report, "warnings": warnings, "head_bytes": head_bytes,
                "tokens_estimate": (head_bytes + 3) // 4}

    def _preview_legacy(self, plan, inp, parsed, identity, cfg, snapshot):
        """Preview of a request that takes the existing path: the head the orchestrator's own functions give."""
        import client_context
        import orchestrator
        from types import SimpleNamespace
        runner = SimpleNamespace(cfg=cfg)
        native_tools = parsed.tools or []
        purpose = orchestrator._purpose(parsed)
        system = orchestrator._client_system(parsed)
        warnings = ["the request takes the legacy path: no profile applies" if snapshot is None
                    else "the request takes the legacy path: %s" % (
                        "rule %r says $legacy" % plan.rule if plan.rule else "no rule matched and default_profile is null")]
        if orchestrator._client_owns_instructions(parsed, purpose, native_tools):
            head = system
            if not client_context.keeps_client_prompt(purpose, identity):
                head = orchestrator.TurnRunner._client_instructions(runner, head)
            source = "client"
        else:
            relay_cfg = {**cfg, "workspace_root": "", "project_context": False, "exec_policy": "off"}
            head = orchestrator.TurnRunner._instructions(SimpleNamespace(cfg=relay_cfg), native_tools=bool(native_tools))
            source = "relay:native_role" if native_tools else "relay:role"
            if not native_tools:
                warnings.append("the local execution and project context blocks of a relay task are not shown")
        # The legacy head appends the built-in contract (orchestrator._run_once): relay_texts.structured wraps only the
        # head of a profile, so the preview of the legacy path shows the contract exactly as the request gets it.
        contract = structured_contract(None, parsed.structured_output, tools=bool(native_tools),
                                       tool_choice=parsed.tool_choice)
        parts = [Part(source, head)] if head else []
        if contract:
            if head:
                parts.append(Part("literal", "\n\n"))
            parts.append(Part("relay:structured", contract))
        text = "".join(part.text for part in parts)
        return self._preview_result(plan, inp, text, parts, [], [], [], warnings)
