import re

with open('relay.log', 'r', encoding='utf-8', errors='ignore') as f:
    for idx, line in enumerate(f, 1):
        if line.startswith('[body] ') and '"claude-opus-5-5"' in line:
            if '"reasoningEffort": "medium"' in line or '"reasoningEffort": "low"' in line:
                m_eff = re.search(r'"reasoningEffort":\s*"([^"]+)"', line)
                eff = m_eff.group(1) if m_eff else '?'
                m_tokens = re.search(r'"maxCompletionTokens":\s*(\d+)', line)
                toks = m_tokens.group(1) if m_tokens else '?'
                print(f"Line {idx}: eff={eff}, maxTokens={toks}")
