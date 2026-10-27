import re

content = open("chunks/AppContainer-DcqGKqHp.js", "r", encoding="utf-8", errors="ignore").read()
matches = re.findall(r'url:"(/api/credits/[^"]+)"', content)
print("Credits endpoints:", set(matches))
