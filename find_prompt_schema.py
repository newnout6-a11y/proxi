import re

bundle = open("bundle.js", "r", encoding="utf-8", errors="ignore").read()

idx = bundle.find("ANTHROPIC_THINKING_EFFORT_LEVELS")
if idx != -1:
    print(bundle[max(0, idx-200):idx+500])

idx2 = bundle.find("user_prompt")
if idx2 != -1:
    print("--- user_prompt ---")
    print(bundle[max(0, idx2-200):idx2+600])

idx3 = bundle.find("modelProvider")
while idx3 != -1:
    print("--- modelProvider ---")
    print(bundle[max(0, idx3-100):idx3+300])
    idx3 = bundle.find("modelProvider", idx3 + 100)
    if idx3 > idx + 20000:
        break
