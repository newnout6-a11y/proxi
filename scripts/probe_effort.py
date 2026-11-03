"""Explicit, bounded live Retool effort probe. Never refreshes or writes credentials."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import upstream_headers
import config
import direct
import models
from redact import redact


def probe(cfg, model, effort, *, timeout=90, head=""):
    entry, _ = models.resolve(model)
    provider = {"model": entry["id"],
                "providerResourceName": models.provider_resource(cfg, entry)}
    body = direct.build_body(cfg, provider, head, "Reply with exactly OK.", [], None, 1024)
    # Bypass local compatibility mapping: measure what this upstream route accepts.
    body["reasoningEffort"] = effort
    headers = upstream_headers(cfg)
    headers["content-type"] = "application/json"
    request = urllib.request.Request(cfg["base"].rstrip("/") + direct.ROUTE,
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
    result = {"model": entry["id"], "sent_effort": effort, "http": None,
              "end_of_stream": False, "text_chars": 0, "usage": {}}
    started = time.monotonic()
    secrets = [cfg.get("xsrf", "")]
    secrets += [p.strip().split("=", 1)[1] for p in cfg["cookie"].split(";") if "=" in p]
    secrets += [str(v) for k, v in cfg.items() if "resource_name" in k and v]
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["http"] = response.status
            for index, raw in enumerate(response):
                if index >= 1000 or time.monotonic() - started > timeout:
                    result["error"] = "probe limit exceeded"
                    break
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "error":
                    result["error"] = redact(str(event.get("message") or event.get("error") or "upstream error"),
                                             cookie_values=secrets)[:500]
                if event.get("type") == "text":
                    result["text_chars"] += len(event.get("partialText") or "")
                usage = event.get("usage") or {}
                if isinstance(usage, dict):
                    result["usage"].update({key: value for key, value in usage.items()
                                            if type(value) in (int, float)})
                if event.get("reasoningEffort"):
                    result["reported_effort"] = event["reasoningEffort"]
                if event.get("event") == "end_of_stream" or event.get("type") == "end_of_stream":
                    result["end_of_stream"] = True
                    break
    except urllib.error.HTTPError as exc:
        result["http"] = exc.code
        result["error"] = redact(exc.read(4000).decode("utf-8", "replace"), cookie_values=secrets)[:500]
        exc.close()
    except (OSError, TimeoutError) as exc:
        result["error"] = type(exc).__name__
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def cache_probe(cfg, model, effort, *, timeout=90):
    head = "Synthetic cache test. Reply only OK.\n" + "\n".join(
        f"Fixture {index:04d}: alpha beta gamma delta epsilon zeta eta theta iota kappa."
        for index in range(400))
    seed = probe(cfg, model, effort, timeout=timeout, head=head)
    yield {"phase": "cache_seed", **seed}
    if not seed["end_of_stream"] or seed.get("error"):
        return
    entry, _ = models.resolve(model)
    provider = {"model": entry["id"], "reasoningEffort": effort,
                "providerResourceName": models.provider_resource(cfg, entry)}
    body = direct.build_body(cfg, provider, head, "Reply with exactly OK.", [], None, 1024)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: upstream_headers(cfg)))
    started = time.monotonic()
    try:
        paid = direct._ping(session, body)
        yield {"phase": "cache_ping", "sent_effort": effort, "end_of_stream": True,
               "usage": {key: value for key, value in session._last_warm_usage.items()
                         if type(value) in (int, float)}, "credits": paid,
               "seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:
        yield {"phase": "cache_ping", "error": type(exc).__name__}
        return
    yield {"phase": "cache_followup", **probe(cfg, model, effort, timeout=timeout, head=head)}


def gateway_probe(cfg, model, efforts, dialects, *, compatibility=False, timeout=90):
    """Exercise this checkout through an ephemeral authenticated HTTP server."""
    import proxy
    import journal

    previous_journal = (journal.JOURNAL._cfg, journal.JOURNAL._path)
    scratch = Path(".agents")
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="effort-probe-", dir=scratch) as folder:
        isolated = dict(cfg, relay_api_key=secrets.token_urlsafe(24), _persist_auth=False,
                        project_context=False, exec_policy="off", direct_debug=False,
                        cache_keepwarm_seconds=0, session_refresh_interval=0,
                        journal_path=str(Path(folder) / "journal.jsonl"),
                        allow_effort_downgrade=compatibility)
        isolated.pop("accounts", None)
        runtime = proxy.Runtime(isolated)

        def no_renewal():
            raise RuntimeError("live probes do not rotate credentials")

        for backend in runtime.pool.backends:
            backend.refresher._http_refresh = no_renewal
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        upstream_efforts = []
        original = urllib.request.urlopen

        def track(request, *args, **kwargs):
            if direct.ROUTE in str(getattr(request, "full_url", request)):
                upstream_efforts.append(json.loads(request.data).get("reasoningEffort"))
            return original(request, *args, **kwargs)

        urllib.request.urlopen = track
        try:
            for dialect in dialects:
                path = {"chat": "/v1/chat/completions", "responses": "/v1/responses",
                        "anthropic": "/v1/messages"}[dialect]
                for effort in efforts:
                    data = {"model": model, "reasoning_effort": effort, "stream": False}
                    if dialect == "responses":
                        data.update(input="Reply with exactly OK.", max_output_tokens=1024)
                    else:
                        data.update(messages=[{"role": "user", "content": "Reply with exactly OK."}],
                                    max_tokens=1024)
                    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                        data=json.dumps(data).encode(), headers={"content-type": "application/json",
                        "authorization": "Bearer " + isolated["relay_api_key"]}, method="POST")
                    start, count = time.monotonic(), len(upstream_efforts)
                    result = {"phase": "gateway", "dialect": dialect, "requested_effort": effort,
                              "compatibility": compatibility}
                    try:
                        with urllib.request.urlopen(request, timeout=timeout) as response:
                            result["http"] = response.status
                            reply = json.loads(response.read())
                            result["usage"] = reply.get("usage")
                    except urllib.error.HTTPError as exc:
                        result["http"] = exc.code
                        exc.close()
                    result.update(sent_upstream_efforts=upstream_efforts[count:],
                                  seconds=round(time.monotonic() - start, 3))
                    yield result
        finally:
            urllib.request.urlopen = original
            server.shutdown()
            server.server_close()
            thread.join(2)

            async def close_sessions():
                for session in list(runtime.pool._conversations.values()):
                    await session.aclose()
                await runtime.loop.shutdown_default_executor()

            asyncio.run_coroutine_threadsafe(close_sessions(), runtime.loop).result(10)
            runtime.loop.call_soon_threadsafe(runtime.loop.stop)
            for _ in range(100):
                if not runtime.loop.is_running():
                    runtime.loop.close()
                    break
                time.sleep(0.01)
            runtime.journal.configure(*previous_journal, load=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--account", type=int, default=0)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--efforts", nargs="+", default=["high", "xhigh", "max", "extra"])
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--cache-check", action="store_true")
    parser.add_argument("--gateway-check", action="store_true")
    parser.add_argument("--compatibility", action="store_true")
    parser.add_argument("--dialects", nargs="+", choices=["chat", "responses", "anthropic"],
                        default=["chat", "responses", "anthropic"])
    args = parser.parse_args()
    saved = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cfg = dict(config.DEFAULTS, **saved)
    if saved.get("accounts"):
        cfg.update(saved["accounts"][args.account])
    if args.gateway_check:
        results = gateway_probe(cfg, args.model, args.efforts, args.dialects,
                                compatibility=args.compatibility, timeout=args.timeout)
    elif args.cache_check:
        if len(args.efforts) != 1:
            parser.error("--cache-check requires exactly one --efforts value")
        results = cache_probe(cfg, args.model, args.efforts[0], timeout=args.timeout)
    else:
        results = (probe(cfg, args.model, effort, timeout=args.timeout) for effort in args.efforts)
    for result in results:
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
