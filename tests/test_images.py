"""Tests for image attachments support across dialects and direct upstream mapping."""
import pytest
import config
import direct
import validate
from apierr import ApiError


def test_openai_data_url_image():
    cfg = config.DEFAULTS
    req = {
        "model": "claude-opus-5-5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                        }
                    }
                ]
            }
        ]
    }
    parsed = validate.parse_chat(req, cfg)
    assert len(parsed.message_chunks) == 1
    blocks = parsed.message_chunks[0]
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "User: What color is this?"
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/png"
    assert blocks[1]["imageData"].startswith("iVBORw0")


def test_anthropic_base64_image():
    cfg = config.DEFAULTS
    req = {
        "model": "claude-opus-5-5",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "123456"
                        }
                    }
                ]
            }
        ]
    }
    parsed = validate.parse_anthropic(req, cfg)
    assert len(parsed.message_chunks) == 1
    blocks = parsed.message_chunks[0]
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == "User: Describe"
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/jpeg"
    assert blocks[1]["imageData"] == "123456"


def test_direct_build_body_with_images():
    cfg = dict(config.DEFAULTS, transport="direct")
    req = {
        "model": "claude-opus-5-5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this pixel"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                        }
                    }
                ]
            }
        ]
    }
    parsed = validate.parse_chat(req, cfg)
    body = direct.build_body(cfg, parsed.entry, parsed.system, parsed.prompt, None, None, 1000, chunks=parsed.message_chunks)
    messages = body["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[0].get("cache") is True
    assert content[1]["type"] == "image"
    assert content[1]["mimeType"] == "image/png"
    assert "rit" in body


def test_invalid_image_source_raises_unprocessable():
    cfg = config.DEFAULTS
    req = {
        "model": "claude-opus-5-5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "ftp://invalid-scheme"}}
                ]
            }
        ]
    }
    with pytest.raises(ApiError) as exc:
        validate.parse_chat(req, cfg)
    assert exc.value.status == 422
    assert "attachment" in exc.value.message.lower()
