
import json, pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).parent
blob = (HERE / "bundle.js").read_text(encoding="utf-8", errors="ignore")

def array(name):
    m = re.search(name + r"=\[([^\]]*)\]", blob)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []

providers = {
    "openai": array("OPENAI_COPILOT_MODELS"),
    "anthropic": array("ANTHROPIC_COPILOT_MODELS"),
    "google": array("GOOGLE_COPILOT_MODELS"),
    "retool-fast": array("RETOOL_FAST_COPILOT_MODELS"),
}
i = blob.find("COPILOT_MODEL_INPUT_CONTEXT_SIZE_MAP={")
ctx = json.loads(blob[i + len("COPILOT_MODEL_INPUT_CONTEXT_SIZE_MAP="):blob.find("}", i) + 1])
labels = dict(re.findall(r'"([^"]+)":\{label:"([^"]*)"\}', blob))

rows = []
for provider, ids in providers.items():
    for mid in ids:
        rows.append({"id": mid, "provider": provider, "label": labels.get(mid, mid), "context": ctx.get(mid, 128000)})

print("parsed:", {k: len(v) for k, v in providers.items()})
pathlib.Path(HERE / "models_parsed.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
print("rows:", len(rows), "| sample:", rows[0], rows[-1])
