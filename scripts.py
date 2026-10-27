
import re, urllib.request, pathlib, sys, gzip
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
COOKIE = pathlib.Path("cookies.txt").read_text(encoding="utf-8")
BASE = "https://vovasahren.retool.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
H = {"accept": "text/html,application/xhtml+xml", "accept-encoding": "gzip", "cookie": COOKIE, "user-agent": UA,
     "x-xsrf-token": "74febab9-99e1-496f-ac7a-8b6a83b224e6", "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}
with urllib.request.urlopen(urllib.request.Request(BASE + "/", headers=H), timeout=60) as resp:
    raw = resp.read()
    if resp.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
html = raw.decode("utf-8", "replace")
for t in re.findall(r"<script[^>]*>", html):
    print("TAG:", t[:300])
for m in re.findall(r'(?:src|href)="([^"]*main[^"]*\.js[^"]*)"', html):
    print("MAIN:", m)
