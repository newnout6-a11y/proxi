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


def alternation(values):
    """What the pattern was before redact.literal_pattern: every value, longest first."""
    import re
    return re.compile("|".join(re.escape(value) for value in sorted(set(values), key=len, reverse=True)))


def test_the_prefix_tree_replaces_exactly_what_the_alternation_did():
    # Values that share prefixes, values that are prefixes or parts of others, regex metacharacters: random texts built
    # of their pieces must come out the same.
    import random
    rng = random.Random(20261113)
    alphabet = "ab.*+?()[]{}|^$" + chr(92) + "-_="  # chr(92): a backslash
    for _ in range(300):
        values = list({"".join(rng.choice(alphabet[:rng.randint(2, len(alphabet))]) for _ in range(rng.randint(1, 9)))
                       for _ in range(rng.randint(1, 30))})
        values += [value + "x" for value in values[:3]] + [value[:1] for value in values[:2] if value]
        tree, plain = redact.literal_pattern(values), alternation(values)
        for _ in range(20):
            text = "".join(rng.choice(values + list(alphabet) + [" "]) for _ in range(rng.randint(0, 25)))
            assert tree.sub("#", text) == plain.sub("#", text), (values, text)


def test_a_thousand_tokens_of_a_kilobyte_make_one_pattern_and_the_longest_value_wins():
    tokens = ["eyJ" + secrets.token_urlsafe(750) for _ in range(1000)]
    pattern = redact.literal_pattern(tokens + [tokens[5][:40]])
    assert pattern.sub("#", "x %s y %s z" % (tokens[999], tokens[5])) == "x # y # z"
    assert pattern.sub("#", "only " + tokens[5][:40] + "!") == "only #!"
    assert redact.literal_pattern([]) is None and redact.literal_pattern(["", None]) is None


def test_a_pathological_chain_of_prefixes_falls_back_to_the_alternation():
    chain = ["a" * length for length in range(1, 3000)]  # each a prefix of the next: the tree would nest 3,000 deep
    pattern = redact.literal_pattern(chain)
    assert pattern.sub("#", "b" + "a" * 2999 + "b") == "b#b"


def test_a_pattern_built_once_masks_like_the_values_it_was_built_for():
    values = ["eyJ" + secrets.token_urlsafe(300) for _ in range(5)] + ["short"]
    built = redact.cookie_pattern(values)
    text = "a %s b short" % values[2]
    assert redact.redact(text, cookie_pattern=built) == redact.redact(text, cookie_values=values) == "a %s b short" % (
        redact.MASK)
    assert redact.cookie_pattern(()) is None and redact.redact(text, cookie_pattern=None) == text
