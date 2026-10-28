import json, pathlib
p = pathlib.Path("config.json")
c = json.loads(p.read_text(encoding="utf-8"))
c["cookie"] = "experimentsId=-1201135262; in_retool_canary_group=never; nav-badge-assignment=control.51e35182-5ac4-42ff-8004-4459302262c2; nav-contact-sales-assignment=variant.51e35182-5ac4-42ff-8004-4459302262c2; stable-id=331eb91c-1770-432c-89ba-747a4f655a27; nav-app-gallery-assignment=off.51e35182-5ac4-42ff-8004-4459302262c2; ajs_anonymous_id=1babb352-d234-4f40-acdc-292eb91ee9bf; subdomain=vovasahren.retool.com; _cfuvid=_sqD_LsqlujgCjvE2lzXLrOUXnouJ4K5.Xi93zrEIY4-1793143422.6213365-1.0.1.1-v40UAXc.ha62oEXuNH_BYTQXnlGN.twtRHQLyG_P00o; x-geo=NO-07; accessToken=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ4c3JmVG9rZW4iOiI4MDE3ODU0MS01MDA2LTRiYzEtOTAyZC0yNmZkZTlkMjk2NjAiLCJ2ZXJzaW9uIjoiMS4yIiwiaWF0IjoxNzkzMTg0OTgxfQ.FFixYfoiRHIL3Dxzys0VKAJvrWcXEdHKESiWw5_-CpI; xsrfToken=80178541-5006-4bc1-902d-26fde9d29660; xsrfTokenSameSite=80178541-5006-4bc1-902d-26fde9d29660; cf_clearance=Ha5cRsRMg.ILXwu.0n3hoWHkXMIeMrMV3GZxvRA8mEU-1793184982-1.2.1.1-6ArRcMx6VgJJyGMqBFC0.uOVhsRvY7gLry6ytZt9Mn5i0Gg3aUPNM80b2RylvTPOBeocbLoifwU1kts2UiqKYBURDkpgHgrmmOvVkOVlb8yEDmW6d6Eln6BlJpHsdA7ZbCcBvJflu5dayuXeLfKw4SYWJpuG27psoJgLrzp7gdY7DcXOdOkUUFenwlYOaEYtJdIS36.Cn5fnq_ZSvILhadGi3DYJvVmgfLH1FaWrwrSRAcbkRjAuER1RAB_HsMLyI90FGqE6VAv0kQfW5FHhFDGjdKHr.gzFHa1kJ3c7R5EysLbHaOUGI0SMerYOhAAt4f4MiJOYYUXNs8OgNmkowDzDRoEpx0UI7hZsZ28AeNs"
c["xsrf"] = "80178541-5006-4bc1-902d-26fde9d29660"
c["page_uuid"] = "55684e84-d2b3-11f1-85d9-e3cf7f0008ad"
c["thread_id"] = "92983ebe-2f7f-4e6e-bdca-ecccb8792c42"
c["session_mode"] = "per_request"
p.write_text(json.dumps(c, indent=2), encoding="utf-8")
print("updated; cookie len", len(c["cookie"]))