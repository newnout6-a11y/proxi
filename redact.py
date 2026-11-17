"""Secret redaction for logs, injected context and command output."""
import re

MASK = "[redacted]"

# Patterns are deliberately narrow so ordinary text survives.
# The two `name = value` patterns start a name only where a run of [\w-] starts: with \b they tried again at every '-' of a
# run and scanned the rest of it each time, quadratic time, and the relay runs them on its event loop: 32k characters of
# 'build-build-...' in a command output took 13 s, 24 KB froze every client's stream for 25 s (audit of 2026-11-08). A name
# that starts mid-run matches from the start of its run as well, so what is masked is the same.
_PATTERNS = [
    (re.compile(r'''(?i)["']?(?<![\w-])([\w-]*(?:api[_-]?key|secret|password|passwd|token))\b["']?\s*[:=]\s*(["'])(.*?)\2'''), None),
    # JWT (accessToken / sandboxToken style)
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    # Cloudflare clearance and generic long cookie values
    (re.compile(r"cf_clearance=[^;\s\"']+"), "cf_clearance=" + MASK),
    (re.compile(r"accessToken=[^;\s\"']+"), "accessToken=" + MASK),
    (re.compile(r"xsrfToken=[^;\s\"']+"), "xsrfToken=" + MASK),
    # Relay key in a query string (?key=...)
    (re.compile(r"([?&]key=)([^&\s\"']+)"), None),
    # Common key shapes
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), MASK),
    (re.compile(r"\bretool_[A-Za-z0-9_-]{16,}\b"), MASK),
    (re.compile(r"\brelay-[A-Za-z0-9_-]{20,}\b"), MASK),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), MASK),
    # Assignment style secrets in .env / yaml / json
    (re.compile(r"(?i)[\"']?(?<![\w-])([\w-]*(?:api[_-]?key|secret|password|passwd|token))\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+=]{12,})[\"']?"), None),
]

_SENSITIVE_NAMES = re.compile(
    r"(?i)(\.env(\..*)?$|credentials.*\.json$|\.pem$|\.pfx$|\.key$|id_rsa.*|\.npmrc$|\.netrc$|config\.json$|cookies?\.txt$|relay_key\.txt$)"
)

# What a text must hold for any of the patterns above to match it. Each of them needs one of the literals of _LITERALS, or a keyword
# of _KEYWORDS together with a ":" or "=" (the two patterns for `name: value` and `name=value`; accessToken= and xsrfToken= hold
# "token" and "="). Both are case-insensitive here, wherever the pattern itself is not, so the test is never narrower than a
# pattern, Unicode case folding included. Nearly every string the journal and the console see (keys, ids, models, messages)
# holds none of them, and it is returned as it is: 14 regular expressions took 3.3 us for such a string, this test 0.4 us.
_LITERALS = re.compile(r"(?i)eyj|key=|sk-|retool_|relay-|ghp_|xox|akia|cf_clearance=")
_KEYWORDS = re.compile(r"(?i)api[_-]?key|secret|password|passwd|token")


def _may_hold_a_secret(text: str) -> bool:
    return bool(_LITERALS.search(text)) or ((":" in text or "=" in text) and bool(_KEYWORDS.search(text)))


def redact(text: str, *, cookie_values=()) -> str:
    """Mask known secret shapes plus any literal cookie value we hold."""
    if not text:
        return text
    out = text
    for value in cookie_values:
        if value and len(value) >= 12:
            out = out.replace(value, MASK)
    if not _may_hold_a_secret(out):
        return out
    for pattern, replacement in _PATTERNS:
        if replacement is None:
            # Keep the key, mask only the value.
            out = pattern.sub(lambda m: f"{m.group(1)}={MASK}", out)
        else:
            out = pattern.sub(replacement, out)
    return out


def is_sensitive_path(name: str) -> bool:
    return bool(_SENSITIVE_NAMES.search(name))
