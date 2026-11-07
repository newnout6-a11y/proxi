"""The image URL guard refuses every address that is not on the public internet."""
import socket

import pytest

import apierr
import validate


@pytest.mark.parametrize("address", ["100.64.0.1", "100.100.100.100", "10.0.0.1", "127.0.0.1"])
def test_an_image_url_on_an_address_off_the_public_internet_is_refused(monkeypatch, address):
    # 100.64.0.0/10: carrier-grade NAT and every Tailscale node; neither private nor reserved, so it used to pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, 1, 6, "", (address, 0))])
    with pytest.raises(apierr.ApiError, match="disallowed destination host"):
        validate._assert_public_http_url("http://node.tailnet.example/render.png")


def test_a_public_address_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, 1, 6, "", ("8.8.8.8", 0))])
    validate._assert_public_http_url("http://images.example/cat.png")
