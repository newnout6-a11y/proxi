import re
from collections import Counter

efforts = Counter()
models_by_effort = {}

with open('relay.log', 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        m = re.search(r'"reasoningEffort":\s*"([^"]+)"', line)
        if m:
            eff = m.group(1)
            efforts[eff] += 1
            model_match = re.search(r'"model":\s*"([^"]+)"', line)
            mod = model_match.group(1) if model_match else "unknown"
            models_by_effort.setdefault(eff, Counter())[mod] += 1

print("Total reasoningEffort matches:", sum(efforts.values()))
for eff, count in efforts.items():
    print(f"Effort: {eff} (count: {count})")
    for mod, mcount in models_by_effort[eff].items():
        print(f"    model: {mod}: {mcount}")
