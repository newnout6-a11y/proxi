import re

bundle = open("bundle.js", "r", encoding="utf-8", errors="ignore").read()

for m in re.finditer(r'reasoningEffort', bundle):
    start = max(0, m.start() - 150)
    end = min(len(bundle), m.end() + 150)
    print("MATCH:", bundle[start:end].replace("\n", " "))
