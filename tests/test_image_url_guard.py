"""The image URL guard refuses every address that is not on the public internet."""
import io
import socket
from types import SimpleNamespace

import pytest

import apierr
import validate

CFG = {"strict_models": True, "model": "claude-opus-5-5"}


@pytest.mark.parametrize("address", ["100.64.0.1", "100.100.100.100", "10.0.0.1", "127.0.0.1"])
def test_an_image_url_on_an_address_off_the_public_internet_is_refused(monkeypatch, address):
    # 100.64.0.0/10: carrier-grade NAT and every Tailscale node; neither private nor reserved, so it used to pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, 1, 6, "", (address, 0))])
    with pytest.raises(apierr.ApiError, match="disallowed destination host"):
        validate._assert_public_http_url("http://node.tailnet.example/render.png")


def test_a_public_address_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, 1, 6, "", ("8.8.8.8", 0))])
    validate._assert_public_http_url("http://images.example/cat.png")


# A Messages image named by URL (adapters-3) is downloaded through the same path as an image_url, with every check of it.


def messages_with(url):
    return {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image", "source": {"type": "url", "url": url}}]}]}


class Answer(io.BytesIO):
    """A download whose socket is connected to `peer`."""
    headers = {"content-type": "image/png"}

    def __init__(self, data, peer="93.184.216.34"):
        super().__init__(data)
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(getpeername=lambda: (peer, 80))))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("address", ["100.64.0.1", "127.0.0.1"])
def test_a_messages_image_named_by_url_off_the_public_internet_is_refused_before_any_download(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, 1, 6, "", (address, 0))])
    monkeypatch.setattr(validate._IMAGE_OPENER, "open", lambda *a, **k: pytest.fail("downloaded"))
    with pytest.raises(apierr.ApiError, match="disallowed destination host"):
        validate.parse_anthropic(messages_with("http://node.tailnet.example/render.png"), CFG)


@pytest.mark.parametrize("url", ["file:///etc/passwd", " https://images.example/cat.png"])
def test_a_messages_image_named_by_a_link_that_is_not_http_is_refused_without_a_lookup(monkeypatch, url):
    # The preview swaps only links that start with http(s) for a pixel; urlparse reads the second one as https, so the
    # guard alone would have resolved its host, from a preview too.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("resolved"))
    monkeypatch.setattr(validate._IMAGE_OPENER, "open", lambda *a, **k: pytest.fail("opened"))
    with pytest.raises(apierr.ApiError, match=r"image source url must be an http\(s\) URL"):
        validate.parse_anthropic(messages_with(url), CFG)


def test_a_messages_image_whose_connection_lands_off_the_public_internet_is_refused(monkeypatch):
    # the name resolved to a public address and the socket landed on an internal one (DNS rebinding)
    monkeypatch.setattr(validate, "_assert_public_http_url", lambda url, where="image": None)
    monkeypatch.setattr(validate._IMAGE_OPENER, "open", lambda request, timeout=None: Answer(b"png", peer="10.0.0.1"))
    with pytest.raises(apierr.ApiError, match="disallowed destination host"):
        validate.parse_anthropic(messages_with("http://images.example/cat.png"), CFG)


def test_a_messages_image_named_by_url_past_10_mb_is_refused(monkeypatch):
    monkeypatch.setattr(validate, "_assert_public_http_url", lambda url, where="image": None)
    monkeypatch.setattr(validate._IMAGE_OPENER, "open",
                        lambda request, timeout=None: Answer(bytes(10 * 1024 * 1024 + 1)))
    with pytest.raises(apierr.ApiError, match="exceeds maximum allowed size"):
        validate.parse_anthropic(messages_with("http://images.example/cat.png"), CFG)
