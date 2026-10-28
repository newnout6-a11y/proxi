
import sys, asyncio, urllib.error, io
sys.path.insert(0, r"C:\specialinay_papka")
import session as S
cfg = {"base":"https://x.invalid","page_uuid":"p","ua":"t","cookie":"c","xsrf":"x","upstream_timeout":0.5}
class Ref:
    last_error = None
    calls = 0
    def refresh(self):
        Ref.calls += 1
        return True
calls = {"n":0}
def post(path, payload, timeout):
    calls["n"] += 1
    raise urllib.error.HTTPError("https://x.invalid", 401, "e", {}, io.BytesIO(b"{}"))
b = S.Backend(cfg, http_post=post, refresher=Ref())
b.cfg = cfg
try:
    asyncio.run(b._call("/x", {}))
except BaseException as e:
    print("RESULT:", type(e).__name__, "|", e, "| attempts:", calls["n"], "refreshes:", Ref.calls)
