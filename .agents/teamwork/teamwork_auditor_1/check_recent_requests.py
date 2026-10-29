import re

with open('relay.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

print(f"Total lines in relay.log: {len(lines)}")
# Find requests in the last 2000 lines
last_chunk = lines[-2500:]
recent_requests = []
for i, l in enumerate(last_chunk):
    if l.startswith("relay ") and "POST /v1/messages" in l:
        # scan backwards for [body]
        body_line = None
        for b in range(max(0, i-20), i):
            if last_chunk[b].startswith("[body] "):
                body_line = last_chunk[b]
                break
        eff = "none"
        mod = "unknown"
        if body_line:
            m_eff = re.search(r'"reasoningEffort":\s*"([^"]+)"', body_line)
            if m_eff:
                eff = m_eff.group(1)
            m_mod = re.search(r'"model":\s*"([^"]+)"', body_line)
            if m_mod:
                mod = m_mod.group(1)
        recent_requests.append((l.strip(), mod, eff))

print(f"Recent requests count: {len(recent_requests)}")
for req, mod, eff in recent_requests[-35:]:
    print(f"{req} | model={mod} | effort={eff}")
