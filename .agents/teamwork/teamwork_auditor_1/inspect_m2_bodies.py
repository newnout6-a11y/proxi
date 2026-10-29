import re

with open('relay.log', 'r', encoding='utf-8', errors='ignore') as f:
    for idx, line in enumerate(f, 1):
        if idx > 7400 and line.startswith('[body] '):
            m_eff = re.search(r'"reasoningEffort":\s*"([^"]+)"', line)
            eff = m_eff.group(1) if m_eff else 'none'
            m_mod = re.search(r'"model":\s*"([^"]+)"', line)
            mod = m_mod.group(1) if m_mod else 'none'
            m_tokens = re.search(r'"maxCompletionTokens":\s*(\d+)', line)
            toks = m_tokens.group(1) if m_tokens else 'none'
            print(f"Line {idx}: model={mod:25s} eff={eff:8s} maxTokens={toks}")
