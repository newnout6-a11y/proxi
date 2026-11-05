"""Deterministic rewrites of a client request body selected by a prompts profile.

Contract-first skeleton; see docs/prompt-control-plan.md (sections 2.3 and 4). The
rewrite runs on the raw JSON before validate.parse_* so native-history fingerprints,
chunks, cache markers and rit all derive from the rewritten request (plan I4).
"""
from __future__ import annotations


def rewrite(data: dict, dialect: str, profile: dict, snapshot) -> tuple[dict, list[dict]]:
    """Return (new body, changes) for a profile's history and tools edits.

    The input body is never mutated: the result is a deep copy. The same input and the same
    profile always produce the same bytes (plan I2), and rewrite(rewrite(x)) == rewrite(x).
    Each change is a dict such as {"index": 12, "op": "drop_tag", "removed_chars": 431}.

    profile is Snapshot.profile(name): normalized, texts already literal (plan 4.3). Patterns
    come from snapshot.regex(pattern, flags) and the mid_system "wrap" template from
    snapshot.relay_text("reminder") (None = the built-in "<system-reminder>\\n{{text}}\\n</system-reminder>").
    """
    raise NotImplementedError
