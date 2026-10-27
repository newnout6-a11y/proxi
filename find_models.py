import re, json

bundle = open("bundle.js", "r", encoding="utf-8", errors="ignore").read()

for m in re.finditer(r'([A-Z0-9_]+_MODELS)\s*=\s*(\[[^\]]+\])', bundle):
    print(m.group(1), "=", m.group(2))

for m in re.finditer(r'(COPILOT_MODEL[A-Z0-9_]*)\s*=\s*(\{.+?\})', bundle):
    print(m.group(1), "=", m.group(2)[:300])

for m in re.finditer(r'([A-Z0-9_]+_REASONING_EFFORT[A-Z0-9_]*)\s*=\s*([^,;]+)', bundle):
    print(m.group(1), "=", m.group(2))

for m in re.finditer(r'([A-Z0-9_]+THINKING[A-Z0-9_]*)\s*=\s*([^,;]+)', bundle):
    print(m.group(1), "=", m.group(2))
