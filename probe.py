
import json, re, urllib.request, urllib.error, pathlib, sys, gzip, io
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COOKIE = ("experimentsId=-1201135262; in_retool_canary_group=never; "
"nav-badge-assignment=control.51e35182-5ac4-42ff-8004-4459302262c2; "
"nav-contact-sales-assignment=variant.51e35182-5ac4-42ff-8004-4459302262c2; "
"stable-id=331eb91c-1770-432c-89ba-747a4f655a27; "
"nav-app-gallery-assignment=off.51e35182-5ac4-42ff-8004-4459302262c2; x-geo=NL-NH; "
"ajs_anonymous_id=1babb352-d234-4f40-acdc-292eb91ee9bf; "
"_cfuvid=CtVqJxNCmN5a1IKd91ESzbbD50chaUQ_TBATwQAtkwg-1793133055.3454611-1.0.1.1-qKO8Ws1i5xCQgTLJ6Hf3d4QKVI1MNE3ULNeEDl8VEBM; "
"accessToken=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ4c3JmVG9rZW4iOiI3NGZlYmFiOS05OWUxLTQ5NmYtYWM3YS04YjZhODNiMjI0ZTYiLCJ2ZXJzaW9uIjoiMS4yIiwiaWF0IjoxNzkzMTMzMDk5fQ._qQbzpJPF1eZYpMvdP61Yu5jJSCuMUrgwFvF36IxkoI; "
"xsrfToken=74febab9-99e1-496f-ac7a-8b6a83b224e6; xsrfTokenSameSite=74febab9-99e1-496f-ac7a-8b6a83b224e6; "
"subdomain=vovasahren.retool.com; "
"cf_clearance=D7.XPjveTCmq9O96hyzFdWIVbhU5GNm97dz0nkFKnck-1793137633-1.2.1.1-wJ0FgQEr0fhYL4tpdeHekWlSa8AtIx8jiYOq1Rq3H5lTHu3_5c0mk79_zStezM3eIX1CY6TwTCIoibOFHAIqwapb1uGJN6HW64sqk8ZqC0tH_Gb5uoBpU0XO9b4CHnX1Rel0V.5B_IaSSpl2gsnB7XHyf4okca_xxvrZ8qVXBOVH6AkKfiB30iz9udDbArEshUEAuypgEnTTAdZjWYC5F_q2eT3krgoUPHB2n3TzYtZkcBz.LBKkzRC3uoftFv3WCeu3pjQK1liE8tqGZ6GfHLer_UEFePBJOiRw9tvLkwa4Jxi_roodQQPzLD3qOdehAbgpBqjzOHxktUeFgGfpz.BoG.fTsIHCWuSLSq0fpo4")

BASE = "https://vovasahren.retool.com"
PAGE = "fc1a6956-d24f-11f1-a342-e70a1f6ca7fe"
THREAD = "f3b05dc9-0e17-4cc8-8fc7-b52ca1d208fb"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"

H = {"accept": "application/json", "accept-encoding": "gzip", "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
     "cookie": COOKIE, "origin": BASE, "referer": BASE + "/", "user-agent": UA,
     "x-xsrf-token": "74febab9-99e1-496f-ac7a-8b6a83b224e6",
     "x-retool-client-version": "4.84.0-8ec403c (Build 409410)"}

pathlib.Path("cookies.txt").write_text(COOKIE, encoding="utf-8")

def req(url, headers, method="GET", data=None, timeout=45):
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()

s, b = req(f"{BASE}/api/pages/{PAGE}/sandpack/threads/{THREAD}/messages", H)
print("MESSAGES:", s, b[:600].decode("utf-8", "replace"))

s2, b2 = req(BASE + "/", H)
html = b2.decode("utf-8", "replace")
print("PAGE:", s2, "len", len(html))
js = sorted(set(re.findall(r'["/]([\w./-]*main-[\w]+\.js)', html)))
print("BUNDLES:", js[:5])
