"""Minara accounts: what a Bearer token says about itself, and how new tokens join config.json.

A Minara token is a JWT from Minara's web app. Its payload names the user (`sub`) and the moment the token stops working
(`exp`, 30 days after `iat`). The relay reads the payload without checking the signature, and only for these two facts:
the account dispatcher takes an account whose token has expired out of the pool without asking Minara, and adding
accounts (the admin form, scripts/add_minara_accounts.py) skips a token whose user is already in the pool. A token that
is not a readable JWT changes neither.

`plan` is the one rule both ways of adding accounts follow, so the form and the script never disagree about what is a
duplicate or how a new account is labelled.
"""
import base64
import copy
import json
import math
import os
import re

import apierr
import config
import models

DEFAULT_BASE = "https://api.minara.ai/v1"
LABEL_PREFIX = "Minara"
LABEL_MAX = 40          # config._check_pool_fields
TOKEN_MAX = 16384       # config._check_upstream
MAX_TOKENS = 200        # tokens one request may add; a pool is a few dozen accounts
WARN_SECONDS = 3 * 86400  # a token with less time left is announced, once a day (AccountDispatcher.check_expiry)


def claims(token):
    """The payload of a JWT-shaped token as a dict, read without checking the signature; None when there is none."""
    if not isinstance(token, str) or len(token) > TOKEN_MAX or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError):  # bad base64 (binascii.Error), bad UTF-8 or JSON, a hostile nesting
        return None
    return value if isinstance(value, dict) else None


def expires_at(token):
    """The token's `exp` in epoch seconds; None when it is not a JWT or names no usable expiry."""
    value = (claims(token) or {}).get("exp")
    # An exp too large for a float (a token made to break it) raised OverflowError and stopped every account's checks.
    if type(value) is int:
        return value if 0 < value < 10 ** 11 else None
    return value if type(value) is float and math.isfinite(value) and 0 < value < 1e11 else None


def subject(token):
    """The Minara user the token belongs to (`sub`, as text); None when unknown."""
    value = (claims(token) or {}).get("sub")
    if type(value) is int:
        value = str(value)
    return value if isinstance(value, str) and value else None


def record_token(record):
    """The token a config.json account record gives its Minara account, resolved the way config._check_upstream
    resolves it (the environment variable it names, else bearer_token); None for any other record."""
    if not isinstance(record, dict) or record.get("upstream") != "minara":
        return None
    env = record.get("bearer_token_env")
    token = os.environ.get(env) if isinstance(env, str) and env else record.get("bearer_token")
    return token if isinstance(token, str) and token else None


def last_number(records, prefix=LABEL_PREFIX):
    """The largest N among the labels "<prefix> N" of these records (any case), or 0."""
    pattern = re.compile(re.escape(prefix) + r" (\d{1,9})", re.IGNORECASE)
    numbers = [int(match.group(1)) for record in records if isinstance(record, dict)
               for match in [pattern.fullmatch(str(record.get("label") or "").strip())] if match]
    return max(numbers, default=0)


def _plain(text):
    """Printable text: no control character, no half of a surrogate pair (a label is shown and journaled)."""
    return all(ord(char) >= 32 and ord(char) != 127 and not 0xD800 <= ord(char) <= 0xDFFF for char in text)


def _token_problem(token, template, position):
    """(code, message) of why this list item cannot be an account's token, or None. Neither contains the token.

    The code lets the admin form say it in its own words: "empty", "format" (not printable ASCII without spaces, or too
    long), "cut" (starts like a JWT, unreadable: cut off when copied) or "rejected" (the account record is refused)."""
    if not isinstance(token, str) or not token:
        return "empty", "not a token: expected a non-empty string"
    if len(token) > TOKEN_MAX or any(ord(char) < 33 or ord(char) > 126 for char in token):
        # Never "Bearer <word>" in a message: redact() masks the word after it (admin responses pass through it).
        return "format", "a token is printable ASCII without spaces, at most %d characters" % TOKEN_MAX
    if token.startswith("eyJ") and claims(token) is None:
        # Minara's tokens are JWTs: one that starts like a JWT and cannot be read was cut off when it was copied.
        return "cut", "starts like a JWT but its payload cannot be read: was it cut off when copied?"
    try:
        config.validate_minara_account(dict(copy.deepcopy(template), bearer_token=token), "token %d" % position)
    except apierr.ApiError as error:
        return "rejected", error.message
    return None


_TOKEN = "_token"  # the private key of a renewal result that holds the new token


def apply_renewals(results):
    """Store the new token of every "renewed" result in its account record (the record of config.json that plan()
    was given); the tokens stored, for the caller to mask."""
    stored = []
    for result in results:
        if result.get("status") == "renewed":
            result["of"]["bearer_token"] = result[_TOKEN]
            stored.append(result[_TOKEN])
    return stored


def plan(records, tokens, *, now, label=None, base=None, minara_models=None):
    """What adding these tokens to the account `records` of config.json would do; nothing is changed.

    Returns (results, new_records). There is a result per token, in order, naming the token by its position (1-based),
    never by its value: {"token": 3, "status": ...} with status
      "added"      `record` is the new account (label, expires_at);
      "renewed"    `of` is the account of the same Minara user whose token this one replaces: it expires later and
                   is still valid (Minara gives a new 30-day token at each sign-in; the account keeps its label, id and
                   place). The caller stores it with apply_renewals. An account whose token comes from an environment
                   variable is never rewritten: it stays a duplicate with `newer`;
      "duplicate"  `of` is the account that already has this token (reason "same_token") or another token of the same
                   Minara user (reason "same_user", `newer` when this token expires later), either in `records` or added
                   by an earlier token of the list (`repeat_of` is its position);
      "expired"    the token's `exp` has passed;
      "invalid"    `problem` (empty, format, cut, rejected) and `error` say why it cannot be a token.
    New accounts are labelled "<prefix> N" after the largest N of that prefix in `records`: the prefix is `label`, else
    "Minara"; a single token with a label gets the label as it is. `base` and `minara_models` apply to every new account
    (no list: every published model). A problem of the request itself (no tokens, a bad label, base or model list, a model
    another account describes otherwise) raises a 400 and nothing is planned.
    """
    if not isinstance(tokens, list) or not tokens:
        raise apierr.bad_request("'bearer_tokens' must be a non-empty list of tokens", param="bearer_tokens")
    if len(tokens) > MAX_TOKENS:
        raise apierr.bad_request("At most %d tokens can be added at once" % MAX_TOKENS, param="bearer_tokens")
    if label is not None and (not isinstance(label, str) or not _plain(label) or len(label.strip()) > LABEL_MAX):
        raise apierr.bad_request("'label' must be text of at most %d characters" % LABEL_MAX, param="label")
    label = (label or "").strip()
    template = {"upstream": "minara", "base": DEFAULT_BASE if base is None else base}
    if minara_models not in (None, []):
        template["minara_models"] = copy.deepcopy(minara_models)
    checked = dict(copy.deepcopy(template), bearer_token="placeholder")
    try:
        config.validate_minara_account(checked, "new accounts")
    except apierr.ApiError as error:
        raise apierr.bad_request(error.message)
    template["base"] = checked["base"]
    records = [record for record in records if isinstance(record, dict)]
    numbered = not (label and len(tokens) == 1)
    prefix = label or LABEL_PREFIX
    number = last_number(records, prefix) if numbered else 0
    if numbered and len("%s %d" % (prefix, number + len(tokens))) > LABEL_MAX:
        raise apierr.bad_request("'label' is too long to number the new accounts (at most %d characters with the number)"
                                 % LABEL_MAX, param="label")

    by_token, by_user, position_of = {}, {}, {}
    for record in records:
        token = record_token(record)
        if token is not None:
            by_token.setdefault(token, record)
            by_user.setdefault(subject(token), record)
    by_user.pop(None, None)
    results, added = [], []
    for position, raw in enumerate(tokens, 1):
        token = raw.strip() if isinstance(raw, str) else raw
        result = {"token": position}
        results.append(result)
        problem = _token_problem(token, template, position)
        if problem:
            result.update(status="invalid", problem=problem[0], error=problem[1])
            continue
        expiry, user = expires_at(token), subject(token)
        same, reason = by_token.get(token), "same_token"
        if same is None and user is not None:
            same, reason = by_user.get(user), "same_user"
        if same is not None and reason == "same_user" and id(same) not in position_of and not same.get(
                "bearer_token_env") and expiry is not None and expiry > now:
            known = expires_at(record_token(same))
            if known is None or expiry > known:
                result.update(status="renewed", of=same, expires_at=expiry, was_expires_at=known)
                result[_TOKEN] = token  # never in a report: _public() and the script's describe() leave it out
                position_of[id(same)] = position
                by_token[token] = same
                continue
        if same is not None:
            result.update(status="duplicate", reason=reason, of=same, expires_at=expiry)
            if reason == "same_user":
                known = expires_at(record_token(same))
                result["newer"] = expiry is not None and known is not None and expiry > known
            if id(same) in position_of:
                result["repeat_of"] = position_of[id(same)]
            continue
        if expiry is not None and expiry <= now:
            result.update(status="expired", expires_at=expiry)
            continue
        if numbered:
            number += 1
        record = {"label": "%s %d" % (prefix, number) if numbered else label, **copy.deepcopy(template),
                  "bearer_token": token, "enabled": True, "priority": 0}
        result.update(status="added", record=record, expires_at=expiry)
        added.append(record)
        position_of[id(record)] = position
        by_token[token] = record
        if user is not None:
            by_user[user] = record
    if added:
        try:
            models.build_minara_catalog([*records, *added])
        except apierr.ApiError as error:
            raise apierr.bad_request(error.message)
    return results, added
