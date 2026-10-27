import json

cfg_path = "config.json"
cfg = json.load(open(cfg_path, encoding="utf-8"))
cfg.setdefault("openai_provider_resource_name", "1b7f4c97-241f-4117-a710-18c7cbfa74b1")
cfg.setdefault("reasoning_effort", "high")
cfg.setdefault("local_exec", True)
cfg.setdefault("project_context", True)
cfg.setdefault("workspace_root", "C:\\specialinay_papka")

with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)

print("config.json updated successfully")
