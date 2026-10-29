import re

counts = {}
with open('relay.log', 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if line.startswith('[body] '):
            m_mod = re.search(r'"model":\s*"([^"]+)"', line)
            m_eff = re.search(r'"reasoningEffort":\s*"([^"]+)"', line)
            mod = m_mod.group(1) if m_mod else 'none'
            eff = m_eff.group(1) if m_eff else 'none'
            key = (mod, eff)
            counts[key] = counts.get(key, 0) + 1

for (mod, eff), count in sorted(counts.items()):
    print(f"model: {mod:30s} | reasoningEffort: {eff:10s} | count: {count}")
