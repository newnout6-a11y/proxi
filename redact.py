"""Secret redaction for logs, injected context and command output."""
import re

MASK = "[redacted]"

# Patterns are deliberately narrow so ordinary text survives.
_PATTERNS = [
    (re.compile(r'''(?i)["']?\b([\w-]*(?:api[_-]?key|secret|password|passwd|token))\b["']?\s*[:=]\s*(["'])(.*?)\2'''), None),
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
    (re.compile(r"(?i)[\"']?\b([\w-]*(?:api[_-]?key|secret|password|passwd|token))\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+=]{12,})[\"']?"), None),
]

_SENSITIVE_NAMES = re.compile(
    r"(?i)(\.env(\..*)?$|credentials.*\.json$|\.pem$|\.pfx$|\.key$|id_rsa.*|\.npmrc$|\.netrc$|config\.json$|cookies?\.txt$|relay_key\.txt$)"
)


def redact(text: str, *, cookie_values=()) -> str:
    """Mask known secret shapes plus any literal cookie value we hold."""
    if not text:
        return text
    out = text
    for value in cookie_values:
        if value and len(value) >= 12:
            out = out.replace(value, MASK)
    for pattern, replacement in _PATTERNS:
        if replacement is None:
            # Keep the key, mask only the value.
            out = pattern.sub(lambda m: f"{m.group(1)}={MASK}", out)
        else:
            out = pattern.sub(replacement, out)
    return out


def is_sensitive_path(name: str) -> bool:
    return bool(_SENSITIVE_NAMES.search(name))
