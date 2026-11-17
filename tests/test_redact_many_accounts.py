"""Redaction with many accounts: one pattern for every cookie value, built again only when the values change."""
import secrets

import redact


def test_every_cookie_value_is_masked_with_one_pattern_and_short_values_are_left():
    values = ["eyJ" + secrets.token_urlsafe(700) for _ in range(51)] + ["accessToken" + secrets.token_hex(8), "short"]
    text = "a %s b %s c short" % (values[7], values[51])
    out = redact.redact(text, cookie_values=values)
    assert values[7] not in out and values[51] not in out and out.count(redact.MASK) == 2 and out.endswith("short")
    # a value that contains another is masked whole (longest first)
    inner = values[3][:40]
    out = redact.redact("x " + values[3] + " y " + inner, cookie_values=[inner, values[3]])
    assert out == "x %s y %s" % (redact.MASK, redact.MASK)
    # the values change (a cookie rotated): the next call uses the new ones
    rotated = values[:]
    rotated[7] = "eyJ" + secrets.token_urlsafe(700)
    assert rotated[7] not in redact.redact("z " + rotated[7], cookie_values=rotated)
    assert values[7] in redact.redact("z " + values[7], cookie_values=rotated)
