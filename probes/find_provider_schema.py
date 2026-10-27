import re

bundle = open("bundle.js", "r", encoding="utf-8", errors="ignore").read()

for m in re.finditer(r'providerResourceName', bundle):
    start = max(0, m.start() - 200)
    end = min(len(bundle), m.end() + 200)
    print("--- providerResourceName ---")
    print(bundle[start:end].replace("\n", " "))
