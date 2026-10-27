import pathlib, re, json

chunks = list(pathlib.Path("chunks").glob("*.js"))
all_files = [pathlib.Path("bundle.js")] + chunks

print(f"Scanning {len(all_files)} files...")

patterns = [
    r'run_credits_persisted',
    r'creditsUsed',
    r'/api/[a-zA-Z0-9_\-/]*(?:credit|usage|billing|plan|subscription|limit|balance)[a-zA-Z0-9_\-/]*',
    r'reasoning_effort',
    r'thinking',
    r'claude-opus-5-5',
    r'providerResourceName',
    r'user_prompt'
]

for pat in patterns:
    regex = re.compile(pat, re.IGNORECASE)
    matches = []
    for f in all_files:
        if not f.exists(): continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            continue
        for m in regex.finditer(content):
            snippet = content[max(0, m.start()-100):min(len(content), m.end()+100)].replace("\n", " ")
            matches.append((f.name, snippet))
            if len(matches) >= 5:
                break
        if len(matches) >= 5:
            break
    print(f"\nPattern: {pat} (Found {len(matches)} matches)")
    for fname, snip in matches[:3]:
        print(f"  [{fname}] ...{snip}...")
