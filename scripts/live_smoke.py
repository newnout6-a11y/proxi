"""Live smoke checks of a running relay on 127.0.0.1:8787: models, balance, a plain and a streamed answer, a local command.

Spends credits of the real accounts. Not part of the offline suite (tests/):

    python scripts/live_smoke.py
"""
import json, sys, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import client_headers  # noqa: E402  (the relay modules live in the repository root)

BASE = "http://127.0.0.1:8787"


def _urlopen(request, **kwargs):
    if isinstance(request, str):
        request = urllib.request.Request(request)
    for name, value in client_headers().items():
        request.add_header(name, value)
    return urllib.request.urlopen(request, **kwargs)

def test_models():
    print("\n--- 1. Testing /v1/models ---")
    r = json.loads(_urlopen(f"{BASE}/v1/models").read().decode("utf-8"))
    models = [m["id"] for m in r.get("data", [])]
    print(f"Available models ({len(models)}): {', '.join(models)}")
    assert "claude-opus-5-5" in models
    assert "gpt-5.2" in models
    print("PASS: models endpoint")

def test_balance():
    print("\n--- 2. Testing /v1/balance ---")
    r = json.loads(_urlopen(f"{BASE}/v1/balance").read().decode("utf-8"))
    print(f"Balance info: Available={r.get('available')} / Total={r.get('monthly_allocation')} | Reset={r.get('reset_date')}")
    assert r.get("available") is not None
    print("PASS: balance endpoint")

def test_non_streaming():
    print("\n--- 3. Testing Non-Streaming (Claude Opus 5.5) ---")
    payload = {
        "model": "claude-opus-5-5",
        "stream": False,
        "messages": [{"role": "user", "content": "Ответь одним словом: 2 + 2 = ?"}]
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}
    )
    res = json.loads(_urlopen(req, timeout=60).read().decode("utf-8"))
    content = res["choices"][0]["message"]["content"]
    usage = res.get("usage", {})
    print(f"Response: {content.strip()}")
    print(f"Usage: credits_used={usage.get('credits_used')} | credits_available={usage.get('credits_available')}")
    print("PASS: non-streaming")

def test_streaming_openai():
    print("\n--- 4. Testing Streaming (GPT-5.2) ---")
    payload = {
        "model": "gpt-5.2",
        "stream": True,
        "messages": [{"role": "user", "content": "Say: GPT Online"}]
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}
    )
    collected = []
    with _urlopen(req, timeout=60) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if not s or s == "data: [DONE]":
                continue
            if s.startswith("data: "):
                chunk = json.loads(s[6:])
                choices = chunk.get("choices") or []  # the last chunk carries only usage
                delta = choices[0].get("delta", {}).get("content", "") if choices else ""
                if delta:
                    collected.append(delta)
    full_text = "".join(collected)
    print(f"Streamed Text: {full_text.strip()}")
    assert len(full_text) > 0
    print("PASS: streaming OpenAI")

def test_local_exec_and_context():
    print("\n--- 5. Testing Local Command Execution & Context ---")
    payload = {
        "model": "claude-opus-5-5",
        "stream": True,
        "messages": [{"role": "user", "content": "Используй команду на моем ПК чтобы прочитать первые 2 строки файла README.md"}]
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}
    )
    collected = []
    with _urlopen(req, timeout=120) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if not s or s == "data: [DONE]":
                continue
            if s.startswith("data: "):
                chunk = json.loads(s[6:])
                choices = chunk.get("choices") or []  # the last chunk carries only usage
                delta = choices[0].get("delta", {}).get("content", "") if choices else ""
                if delta:
                    collected.append(delta)
                    sys.stdout.write(delta)
                    sys.stdout.flush()
    full_text = "".join(collected)
    print("\n")
    print(f"Total response length: {len(full_text)}")
    assert "readme" in full_text.lower() or "relay" in full_text.lower()
    print("PASS: local command execution & context")

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_models()
    test_balance()
    test_non_streaming()
    test_streaming_openai()
    test_local_exec_and_context()
    print("\n==============================")
    print(" ALL INTEGRATION TESTS PASSED ")
    print("==============================")
