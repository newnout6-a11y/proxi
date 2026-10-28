
import json, sys, urllib.error, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
key = sys.argv[1]
for label, k in (("good", key), ("bad", "wrong")):
    try:
        r = urllib.request.Request("http://127.0.0.1:8787/v1/models",
                                   headers={"authorization": "Bearer " + k})
        d = json.loads(urllib.request.urlopen(r).read().decode())
        print(label, "->", len(d["data"]), "моделей, 200")
    except urllib.error.HTTPError as e:
        print(label, "->", e.code)
