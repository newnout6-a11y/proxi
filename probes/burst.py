
import concurrent.futures as cf, json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = open(r"C:\specialinay_papka\relay_key.txt").read().strip()
H = {"content-type": "application/json", "authorization": "Bearer " + KEY,
     "x-opencode-session": "conv-1"}
def hit(i):
    data = json.dumps({"model": "claude-opus-5-5",
                       "messages": [{"role": "user", "content": "скажи " + str(i)}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:8787/v1/chat/completions", data=data, method="POST", headers=H)
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return i, r.status
    except urllib.error.HTTPError as e:
        return i, e.code
    except Exception as e:
        return i, str(e)[:60]
with cf.ThreadPoolExecutor(6) as ex:
    print(list(ex.map(hit, range(6))))
