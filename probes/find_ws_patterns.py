import re

bundle = open("bundle.js", "r", encoding="utf-8", errors="ignore").read()

# Let's search for sandbox websocket message schemas
patterns = [
    r'type:\s*"user_prompt"',
    r'"user_prompt"',
    r'agentMode',
    r'testingEnabled',
    r'requestOrigin',
    r'r2OnboardingTemplateId',
    r'taggedResourceNames',
    r'reasoningEffort',
    r'thinkingBudget',
    r'run_credits_persisted'
]

for p in patterns:
    matches = list(re.finditer(p, bundle))
    print(f"Pattern {p}: {len(matches)} matches")
    for m in matches[:2]:
        start = max(0, m.start() - 150)
        end = min(len(bundle), m.end() + 250)
        print("  ...", bundle[start:end].replace("\n", " "), "...")
