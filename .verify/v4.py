
import sys, asyncio, urllib.error, io
sys.path.insert(0, r"C:\specialinay_papka")
import session as S, apierr
cfg = {"base":"https://x.invalid","page_uuid":"p","ua":"t","cookie":"c","xsrf":"x","upstream_timeout":0.5}
class Ref:
    last_error = "nope"
    def __init__(self, ok): self.ok = ok
    def refresh(self): return self.ok
def mk(exc):
    def post(path, payload, timeout): raise exc
    return post
loop = asyncio.new_event_loop()

def run(exc, refresh_ok=False):
    b = S.Backend(cfg, http_post=mk(exc), refresher=Ref(refresh_ok))
    try:
        loop.run_until_complete(b._call("/x", {}))
        return "200/ok"
    except apierr.ApiError as e:
        return "%s %s" % (e.status, e.code)
    except Exception as e:
        return "RAW %s %s" % (type(e).__name__, e)

def http_error(code, hdrs=None):
    return urllib.error.HTTPError("https://x.invalid", code, "e", hdrs or {}, io.BytesIO(b'{"error":"z"}'))

import time
t=time.time(); print("401 (no refresh):", run(http_error(401), False), "%.1fs" % (time.time()-t))
t=time.time(); print("429:", run(http_error(429, {"retry-after":"1"})), "%.1fs" % (time.time()-t))
t=time.time(); print("500:", run(http_error(500)), "%.1fs" % (time.time()-t))
t=time.time(); print("418:", run(http_error(418)), "%.1fs" % (time.time()-t))
t=time.time(); print("timeout:", run(TimeoutError()), "%.1fs" % (time.time()-t))
t=time.time(); print("urlerror:", run(urllib.error.URLError("down")), "%.1fs" % (time.time()-t))
print("401 with refresh success:", run(http_error(401), True))
