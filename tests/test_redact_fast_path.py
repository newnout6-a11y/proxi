"""redact() and the journal's cleaning of a text are what they were: only the work for a text with nothing to hide is gone.

Both ran every pattern and every secret over every string of every event (14 regular expressions and about 150 str.replace for
each of the 30 strings of an event): 7.4 us for a short string. A text that holds no trigger is now returned as it is. These tests
hold the original loops in this file and compare the two on random texts made of the very fragments the patterns look for (and the
characters whose case folding the patterns' (?i) reaches), so that a trigger that is narrower than a pattern shows up as a
difference.
"""
import random

import pytest

import journal
import redact

FRAGMENTS = [
    "eyJ", "key=", "?key=", "&key=", "sk-", "retool_", "relay-", "ghp_", "xox", "xoxb-", "AKIA", "cf_clearance=", "accessToken=",
    "xsrfToken=", "api_key", "API-KEY", "apikey", "ApiKey", "secret", "SECRET", "Password", "passwd", "token", "Token", "TOKEN",
    ":", "=", " : ", " = ", "'", '"', " ", "\n", "abcdefghijklmnopqrstuvwx", "ABCDEFGHIJKLMNOP", "0123456789ab", "A1b2C3d4E5f6G7h8",
    ".", "-", "_", "/", "+", "x", "y", "é", "Я", "ſ", "\u212a", "İ", "ı", "ß", "\U0001f600",
]


def reference(text, cookie_values=()):
    """redact() as it was: the cookie values, then every pattern in turn."""
    if not text:
        return text
    out = text
    for value in cookie_values:
        if value and len(value) >= 12:
            out = out.replace(value, redact.MASK)
    for pattern, replacement in redact._PATTERNS:
        if replacement is None:
            out = pattern.sub(lambda m: f"{m.group(1)}={redact.MASK}", out)
        else:
            out = pattern.sub(replacement, out)
    return out


def random_text(rng):
    return "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, 10)))


def test_redact_is_the_same_on_random_texts_made_of_what_the_patterns_look_for():
    rng = random.Random(20261107)
    cookies = ["abcdefghijklmnopqrstuvwx", "0123456789ab", "short"]
    changed = 0
    for _ in range(60000):
        text = random_text(rng)
        values = [value for value in cookies if rng.random() < 0.1]
        expected = reference(text, values)
        assert redact.redact(text, cookie_values=values) == expected, repr(text)
        changed += expected != text
    assert changed > 5000  # the corpus does reach the patterns


@pytest.mark.parametrize("text, masked", [
    ("Authorization: token abcdefghijklmnop", False), ("api_key=sk-" + "x" * 24, True), ("see https://example.test/?key=abc123", True),
    ("password: 'hunter2hunter2'", True), ("ſecret: abcdefghijklmnop", None), ("toKen=abcdefghijklmnop", None),
    ("{\"apiKey\": \"abcdefghijklmnopqr\"}", True), ("xsrfToken=abc; accessToken=def", True),
    ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", True), ("AKIAABCDEFGHIJKLMNOP", True), ("ghp_" + "a" * 30, True),
    ("relay-" + "b" * 24, True),
])
def test_the_shapes_that_must_be_masked_still_are(text, masked):
    assert redact.redact(text) == reference(text)
    if masked is not None:
        assert (redact.redact(text) != text) is masked


def test_a_text_with_nothing_to_hide_is_returned_as_it_is():
    for text in ["", "request_id", "Ход claude-opus-5-5 · effort high · direct", "POST /v1/chat/completions → 200", "x" * 5000]:
        assert redact.redact(text) is text or redact.redact(text) == text


# -- the journal ---------------------------------------------------------------------------------------------------------------

def make_journal(accounts):
    log = journal.Journal()
    log.configure({"relay_api_key": "relay-key-0123456789", "cookie": "accessToken=root-cookie-0123456789; xsrfToken=rootxsrf",
                   "xsrf": "rootxsrf-value", "thread_id": "thread-root-0123456789", "page_uuid": "page-root-0123456789",
                   "cookie_values": ["root-cookie-0123456789"], "accounts": accounts}, None, load=False)
    return log


def account(index):
    return {"cookie": "accessToken=tok-%02d-abcdef012345; xsrfToken=x%02d" % (index, index), "xsrf": "xsrf-%02d-zzzz" % index,
            "thread_id": "thread-%02d-0123456789abcdef" % index, "page_uuid": "page-%02d-fedcba9876543210" % index,
            "cookie_values": ["tok-%02d-abcdef012345" % index]}


def old_clean(log, text):
    """_clean of a string as it was: redact, then every secret in turn."""
    text = redact.redact(text, cookie_values=(log._cfg or {}).get("cookie_values", ()))
    for secret in log._secrets():
        text = text.replace(secret, "[redacted]")
    return text if len(text) <= 4000 else text[:4000] + "…[+%d]" % (len(text) - 4000)


def test_the_journal_masks_the_secrets_exactly_as_the_loop_did():
    log = make_journal([account(index) for index in range(24)])
    secrets = list(log._secrets())
    assert len(secrets) > 100
    rng = random.Random(7)
    pieces = secrets + [secret[:7] for secret in secrets[:30]] + [secret[3:] for secret in secrets[:30]] + FRAGMENTS
    for _ in range(20000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 5)))
        assert log._clean(text, log._secrets()) == old_clean(log, text), repr(text)


def test_a_secret_that_appears_later_is_masked_from_the_next_event_on():
    accounts = [account(0)]
    log = make_journal(accounts)
    late = account(5)
    assert log._clean(late["thread_id"], log._secrets()) == late["thread_id"]
    accounts.append(late)  # what the admin does when it adds an account
    assert log._clean("see " + late["thread_id"] + " here", log._secrets()) == "see [redacted] here"
    late["thread_id"] = "thread-rotated-0123456789abcdef"  # and when a cookie or an id is rotated
    assert log._clean(late["thread_id"], log._secrets()) == "[redacted]"
    accounts.clear()
    assert log._clean(late["thread_id"], log._secrets()) == late["thread_id"]


def test_secrets_are_matched_as_text_and_in_their_own_case():
    log = make_journal([account(1)])
    secret = "thread-01-0123456789abcdef"
    assert log._clean(secret.upper(), log._secrets()) == secret.upper()
    assert log._clean("(" + secret + ")", log._secrets()) == "([redacted])"
    assert log._clean("a.b" + secret.replace("-", ".") , log._secrets()) == "a.b" + secret.replace("-", ".")


def test_an_event_is_cleaned_in_every_field_and_key():
    log = make_journal([account(2)])
    secret = account(2)["xsrf"]
    event = log.emit("x.y", "message " + secret, "info", **{secret: "k", "value": secret, "nested": {"list": [secret, 1]}})
    assert secret not in str(event)
    assert event["data"]["nested"]["list"] == ["[redacted]", 1]
    assert "[redacted]" in event["data"]
