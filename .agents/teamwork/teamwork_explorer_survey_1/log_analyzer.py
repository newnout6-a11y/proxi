import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for sid in ['e6b8a126-3ec5-4d4a-938d-87f022582e9b', 'bd9930ab-ed46-405b-a465-23f263f322f8']:
    path = f'C:/Users/Redmi/.claude/projects/C--specialinay-papka/{sid}.jsonl'
    print(f"=== Session {sid} ===")
    prompts = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get('type') == 'last-prompt':
                p = d.get('lastPrompt')
                if p and p not in prompts:
                    prompts.append(p)
    print("Prompts count:", len(prompts))
    for p in prompts:
        print("  ->", repr(p))
