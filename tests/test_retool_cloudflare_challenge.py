"""Cloudflare's challenge in front of Retool is no verdict on the session (R1a R9).

Retool's frontend tells a 403 with `cf-mitigated: challenge` from a refusal of the session (isCloudflareChallenge, main
@2269090: CloudflareChallengeError). The relay took it for an expired session: the transport renewed the cookies (a request
that the same challenge refuses), answered upstream_auth_error ("cookies/xsrf expired?"), and the dispatcher counted the
renewal's 403 as a failure of the account's sign-in, which marks the account auth_dead after a few. A challenge concerns
the relay's connection (its address, its client), the same for every account."""
import asyncio
import io
import urllib.error
from types import SimpleNamespace

import pytest

import apierr
import auth
import direct
import journal
from test_refresh import CFG
from test_review_fixes_pr19 import END, response, text
from upstream_attempts import AttemptBudget
from upstream_refresh import Refresher

CHALLENGE = {"cf-mitigated": "challenge", "content-type": "text/html; charset=UTF-8"}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)


def refusal(headers):
    return urllib.error.HTTPError("https://fixture.invalid", 403, "Forbidden", headers,
                                  io.BytesIO(b"<!DOCTYPE html><title>Just a moment...</title>"))


def stream(tmp_path, monkeypatch, headers):
    """(error, renewals) of _stream_body when the first POST meets a 403 with `headers` and the second is answered."""
    posts, renewals = [], []

    def opener(_request, **_kw):
        posts.append(1)
        if len(posts) == 1:
            raise refusal(headers)
        return response(text("ok"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag")}
    refresher = SimpleNamespace(refresh=lambda rejected_cookie=None: renewals.append(1) or True)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {"cookie": "c"}, refresher=refresher),
                              _cancelled=False)

    async def go():
        try:
            async for _event in direct._stream_body(cfg, session, {"model": "claude-opus-5-5"}):
                pass
        except apierr.ApiError as error:
            return error
        return None

    return asyncio.run(go()), renewals


def test_the_challenge_is_recognised_as_the_frontend_does():
    assert auth.cloudflare_challenge(403, CHALLENGE) is True
    assert auth.cloudflare_challenge(403, {"CF-Mitigated": "challenge"}) is True
    assert auth.cloudflare_challenge(403, {}) is False and auth.cloudflare_challenge(401, CHALLENGE) is False


def test_a_challenge_renews_no_session_and_names_itself(tmp_path, monkeypatch):
    seq = journal.JOURNAL.last_seq
    error, renewals = stream(tmp_path, monkeypatch, CHALLENGE)
    assert renewals == [], "the cookies are not the cause, and the renewal would meet the same challenge"
    assert error is not None and error.code == "upstream_error" and "Cloudflare" in error.message
    assert [event["kind"] for event in journal.JOURNAL.query(since=seq, limit=1000)
            if event["kind"] == "upstream.cloudflare_challenge"] == ["upstream.cloudflare_challenge"]


def test_a_plain_403_still_renews_the_session_and_asks_again(tmp_path, monkeypatch):
    error, renewals = stream(tmp_path, monkeypatch, {"content-type": "application/json"})
    assert renewals == [1] and error is None


def test_a_challenged_renewal_is_no_failure_of_the_account_s_sign_in():
    def challenged():
        raise refusal(CHALLENGE)

    refresher = Refresher(dict(CFG), http_refresh=challenged)
    ok, kind, error = refresher.refresh_detailed(force=True)
    assert ok is False and kind == "network" and "Cloudflare" in error


def test_a_plain_403_of_the_renewal_still_is():
    def refused():
        raise refusal({"content-type": "application/json"})

    refresher = Refresher(dict(CFG), http_refresh=refused)
    assert refresher.refresh_detailed(force=True)[1] == "auth"
