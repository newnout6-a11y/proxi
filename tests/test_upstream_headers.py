"""The browser headers the relay sends Retool agree with the account's User-Agent."""
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
