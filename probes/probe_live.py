
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = "live-test-key"
def hit(path, key=KEY, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("http://127.0.0.1:8787" + path, data=data,
                                 headers={"content-type": "application/json", "authorization": "Bearer " + key},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read()[:220].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:220].decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)[:200]

print("models+key :", hit("/v1/models"))
print("models-nokey:", hit("/v1/models", key="wrong"))
print("models-nokey2:", hit("/v1/models", key=""))
print("health     :", hit("/health"))
print("chat       :", hit("/v1/chat/completions", payload={"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}))
