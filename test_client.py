import argparse, json, sys, time, urllib.request, urllib.error
from auth import client_headers

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def main():
    parser = argparse.ArgumentParser(description="Retool AI Proxy Client")
    parser.add_argument("prompt", nargs="?", default="Проверь статус git в проекте", help="Prompt to send")
    parser.add_argument("--model", default="claude-opus-5-5", help="Model name (e.g. claude-opus-5-5, gpt-5.2)")
    parser.add_argument("--effort", default=None, help="Reasoning effort (low, medium, high)")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming")
    parser.add_argument("--balance", action="store_true", help="Check credits balance")
    parser.add_argument("--models", action="store_true", help="List available models")
    parser.add_argument("--url", default="http://127.0.0.1:8787", help="Base URL of proxy")
    parser.add_argument("--key", help="Relay key (otherwise RELAY_API_KEY or relay_key.txt)")
    args = parser.parse_args()
    headers = client_headers(args.key)

    if args.balance:
        with urllib.request.urlopen(urllib.request.Request(f"{args.url}/v1/balance", headers=headers)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if args.models:
        with urllib.request.urlopen(urllib.request.Request(f"{args.url}/v1/models", headers=headers)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print("Models:")
            for m in data.get("data", []):
                print(f" - {m['id']} (provider: {m.get('owned_by')})")
        return

    payload = {
        "model": args.model,
        "stream": not args.no_stream,
        "messages": [{"role": "user", "content": args.prompt}]
    }
    if args.effort:
        payload["reasoning_effort"] = args.effort

    req = urllib.request.Request(
        f"{args.url}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers
    )

    print(f"\n[Sending prompt to {args.model} | stream={not args.no_stream}]")
    print(f"User: {args.prompt}\nAssistant: ", end="", flush=True)

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            if args.no_stream:
                res = json.loads(resp.read().decode("utf-8"))
                text = res["choices"][0]["message"]["content"]
                thinking = res["choices"][0]["message"].get("reasoning_content")
                if thinking:
                    print("\n[Thinking]\n" + thinking + "\n[Answer]")
                print(text)
                usage = res.get("usage", {})
                print(f"\n[Usage]: Credits used={usage.get('credits_used')} | Remaining={usage.get('credits_available')}")
            else:
                channel = None
                for line in resp:
                    s = line.decode("utf-8", "replace").strip()
                    if not s or s == "data: [DONE]":
                        continue
                    if s.startswith("data: "):
                        chunk = json.loads(s[6:])
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        piece = choices[0].get("delta", {})
                        thinking = piece.get("reasoning_content")
                        if thinking:
                            if channel != "thinking":
                                print("\n[Thinking]", flush=True)
                            sys.stdout.write(thinking)
                            sys.stdout.flush()
                            channel = "thinking"
                        delta = piece.get("content", "")
                        if delta:
                            if channel == "thinking":
                                print("\n[Answer]", flush=True)
                            channel = "text"
                            sys.stdout.write(delta)
                            sys.stdout.flush()
                print(f"\n\n[Done in {time.time()-t0:.2f}s]")
    except urllib.error.HTTPError as e:
        print(f"\nHTTP ERROR {e.code}: {e.read().decode('utf-8', 'replace')}")
    except Exception as e:
        print(f"\nERROR: {e}")

if __name__ == "__main__":
    main()
