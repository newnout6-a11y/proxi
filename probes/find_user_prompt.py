import pathlib, re

for f in list(pathlib.Path("chunks").glob("*.js")) + [pathlib.Path("bundle.js")]:
    if not f.exists(): continue
    txt = f.read_text(encoding="utf-8", errors="ignore")
    for m in re.finditer(r'\"user_prompt\"', txt):
        print(f"=== {f.name} ===")
        print(txt[max(0, m.start()-300):m.start()+600])
