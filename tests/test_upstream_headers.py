"""The browser headers the relay sends Retool agree with the account's User-Agent."""
import http.client

import pytest

import auth

BASE = {"base": "https://tenant.invalid", "cookie": "accessToken=a", "xsrf": "x"}
CHROME_141 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36")


def test_the_client_hints_name_the_chrome_version_of_the_user_agent():
    headers = auth.upstream_headers({**BASE, "ua": CHROME_141})
    assert headers["user-agent"] == CHROME_141
    assert headers["sec-ch-ua"] == '"Google Chrome";v="141", "Chromium";v="141", "Not_A Brand";v="24"'
    assert headers["sec-ch-ua-platform"] == '"Windows"' and headers["sec-ch-ua-mobile"] == "?0"


def test_an_account_brand_list_is_kept_as_it_is():
    brands = '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"'
    headers = auth.upstream_headers({**BASE, "ua": CHROME_141.replace("141", "131"), "sec_ch_ua": brands})
    assert headers["sec-ch-ua"] == brands


def test_without_a_user_agent_the_default_one_and_its_hints_agree():
    headers = auth.upstream_headers(dict(BASE))
    assert headers["user-agent"] == auth.DEFAULT_UA and "Chrome/131." in auth.DEFAULT_UA
    assert 'v="131"' in headers["sec-ch-ua"]


def test_a_browser_that_is_not_chromium_sends_no_client_hints():
    firefox = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
    headers = auth.upstream_headers({**BASE, "ua": firefox})
    assert not any(name.startswith("sec-ch-ua") for name in headers)
    assert headers["sec-fetch-mode"] == "cors"  # fetch metadata is sent by every current browser


def test_the_platform_and_mobile_flag_follow_the_user_agent():
    mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    android = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 "
               "Mobile Safari/537.36")
    assert auth.client_hints(mac)["sec-ch-ua-platform"] == '"macOS"'
    hints = auth.client_hints(android)
    assert hints["sec-ch-ua-platform"] == '"Android"' and hints["sec-ch-ua-mobile"] == "?1"


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl"


def test_a_cookie_read_with_its_line_break_is_sent_without_it():
    """RETOOL_COOKIE from Get-Content -Raw ends in a line break: http.client refused the header with a ValueError that quoted
    it whole, the session JWT included, and the banner, /v1/balance and the completion errors showed that text."""
    headers = auth.upstream_headers({**BASE, "cookie": "accessToken=%s; xsrfToken=t\r\n" % JWT, "xsrf": " t\n"})
    assert headers["cookie"] == "accessToken=%s; xsrfToken=t" % JWT and headers["x-xsrf-token"] == "t"
    connection = http.client.HTTPConnection("tenant.invalid")
    connection.putrequest("GET", "/")
    for name, value in headers.items():
        connection.putheader(name, value)  # the check http.client makes before anything is sent


@pytest.mark.parametrize("field, value", [("cookie", "accessToken=%s\nxsrfToken=t" % JWT), ("cookie", "a=%s\x00" % JWT),
                                          ("xsrf", JWT + "…")])
def test_a_secret_with_a_control_character_inside_is_refused_without_showing_it(field, value):
    with pytest.raises(ValueError) as refused:
        auth.upstream_headers({**BASE, field: value})
    assert JWT not in str(refused.value) and "eyJ" not in str(refused.value) and field in str(refused.value)


@pytest.mark.parametrize("key", ["mysecretkey1", "short", "a-key-of-20-letters!", "x" * 31])
def test_the_banner_shows_no_part_of_a_short_relay_key(key):
    """The banner goes to relay.log: a key of up to 12 characters was printed whole, a longer one by 12 of its characters."""
    shown = auth.mask(key)
    assert shown == "(set, %d characters)" % len(key)


def test_the_banner_shows_the_ends_and_the_length_of_a_long_relay_key():
    key = "Q2v7Hk9mPq4WzR8tLx3nBc6YdF1gJs5eUa0iOo2pVh"
    assert auth.mask(key) == "Q2v7…2pVh (42 characters)" and auth.mask("") == auth.mask(None) == "(none)"
