"""Regression tests for the context-mechanics hardening fixes."""
import subprocess

import context
import direct


def test_cache_key_distinguishes_model_and_tools():
    """The upstream prompt prefix includes the model and the toolset, so the cache
    key must change when either changes (previously it hashed only the head)."""
    head = "stable instructions"
    base = direct.cache_key_for(head, "claude-opus-5-5", None)
    assert direct.cache_key_for(head, "gpt-5.5", None) != base
    tools = [{"name": "exec_command", "description": "d", "parameters": {}}]
    assert direct.cache_key_for(head, "claude-opus-5-5", tools) != base
    # Same model/tools/head -> same key.
    assert direct.cache_key_for(head, "claude-opus-5-5", tools) == direct.cache_key_for(head, "claude-opus-5-5", tools)


def test_image_only_chunk_never_gets_a_cache_flag():
    """A history chunk that renders to an image-only content block has no cacheable
    text; the cache flag must be skipped instead of landing on the image block."""
    cfg = {"direct_cache": True}
    model = {"model": "claude-opus-5-5"}
    head = "You are a helpful assistant."
    image_chunk = [{"type": "image", "imageData": "aGVsbG8=", "mimeType": "image/png"}]
    body = direct.build_body(cfg, model, head, "", None, "auto", 100,
                             chunks=["User: hi", image_chunk, "User: continue"],
                             session=None)
    for message in body["messages"]:
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("cache"):
                assert block.get("type") == "text", "cache flag on non-text block: %r" % block


def test_project_context_hides_sensitive_git_paths_and_redacts_commit(tmp_path):
    """git status must not advertise sensitive file names, and the commit message
    goes through redact before entering the cached head."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "config.json").write_text('{"token": "x"}\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    (tmp_path / "relay_key.txt").write_text("relay-secret\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "relay_key.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "add app.py token relay-abcdefghijklmnopqrstuvwx"], check=True)

    text = context.build_project_context(str(tmp_path), limit=8000)
    status_part = text.split("Git status: ", 1)[1].split("\n", 1)[0]
    assert "config.json" not in status_part, "sensitive untracked file leaked into head"
    assert "relay_key.txt" not in status_part, "sensitive untracked file leaked into head"
    assert "relay-abcdefghijklmnopqrstuvwx" not in text, "commit message secret leaked into head"


def test_project_context_stays_within_byte_limit(tmp_path):
    """The assembled context is byte-bounded and UTF-8-safe even with Cyrillic docs
    (the excerpt budget is spent in bytes, not characters)."""
    (tmp_path / "README.md").write_text("ПРИВЕТ " * 2000, encoding="utf-8")  # ~14 KB
    text = context.build_project_context(str(tmp_path), limit=4000)
    assert len(text.encode("utf-8")) <= 4000 + len("\n[END OF PROJECT CONTEXT]") + 2
