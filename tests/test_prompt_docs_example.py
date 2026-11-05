"""The prompt control guide, the README snippet and the example stay valid.

Every json / jsonc block of docs/prompt-control.md parses (jsonc after dropping comments); the prompts documents
among them, the one in the README and prompts.example.json follow the closed lists of prompt_policy and compile
through prompt_policy.compile_document (skipped while the engine is only the contract skeleton).
"""
import json
import re
from pathlib import Path

import pytest

import prompt_policy

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "prompt-control.md"
README = ROOT / "README.md"
EXAMPLE = ROOT / "prompts.example.json"
FENCE = re.compile(r"^```(json|jsonc)[ \t]*\r?\n(.*?)^```[ \t]*\r?$", re.S | re.M)
RECIPES = "\n## Рецепты\n"
RULE_KEYS = {"name", "if", "use", "enabled", "override_protected"}
VARIABLE = re.compile(r"(?<!\\)\{\{([^{}]*)\}\}")
PERSONAL = (re.compile(r"[A-Za-z]:\\Users\\"), re.compile(r"/(?:home|Users)/[^\s/]+"),
            re.compile(r"(?i)(?:api[_-]?key|cookie|token|secret)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{16,}"))


def strip_jsonc(text):
    """Drop // and /* */ comments that are outside strings: an annotated example needs it before json.loads."""
    result, i, size, in_string = [], 0, len(text), False
    while i < size:
        char = text[i]
        if in_string:
            result.append(char)
            if char == "\\" and i + 1 < size:
                result.append(text[i + 1])
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            result.append(char)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated /* comment")
            i = end + 2
            continue
        else:
            result.append(char)
        i += 1
    return "".join(result)


def guide_blocks():
    """[(line, language, body, in_recipes)] for every json / jsonc fence of the guide."""
    text = GUIDE.read_text(encoding="utf-8")
    recipes_at = text.index(RECIPES)
    return [(text.count("\n", 0, match.start()) + 1, match.group(1), match.group(2), match.start() > recipes_at)
            for match in FENCE.finditer(text)]


def parse(language, body):
    return json.loads(strip_jsonc(body) if language == "jsonc" else body)


def guide_documents():
    """[(line, document, in_recipes)] for the blocks that are whole prompts documents (they carry "version")."""
    documents = []
    for line, language, body, recipe in guide_blocks():
        value = parse(language, body)
        if isinstance(value, dict) and "version" in value:
            documents.append((line, value, recipe))
    return documents


def named_texts(document):
    return set(document.get("texts") or {})


def readme_documents():
    """[(line, document)] for the prompts documents the README shows."""
    text = README.read_text(encoding="utf-8")
    documents = []
    for match in FENCE.finditer(text):
        value = parse(match.group(1), match.group(2))
        if isinstance(value, dict) and "version" in value:
            documents.append((text.count("\n", 0, match.start()) + 1, value))
    return documents


def contract_problems(document):
    """What differs from the closed lists of prompt_policy in a document: typos and unknown names."""
    problems, texts = [], named_texts(document)
    profiles = document.get("profiles") or {}

    def template(path, value):
        for name in VARIABLE.findall(value):
            base, _sep, text = name.partition(":")
            if base == "text":
                if text not in texts:
                    problems.append(f"{path}: unknown text {text}")
            elif name not in prompt_policy.VARIABLES:
                problems.append(f"{path}: unknown variable {name}")

    for name, profile in profiles.items():
        if name == "passthrough" or not prompt_policy.PROFILE_NAME.fullmatch(name):
            problems.append(f"profiles.{name}: not a usable profile name")
        template(f"profiles.{name}.head", profile.get("head", ""))
        for edit in (profile.get("client_system") or {}).get("edits", []):
            if edit.get("op") not in prompt_policy.SYSTEM_EDIT_OPS:
                problems.append(f"profiles.{name}.client_system: unknown op {edit.get('op')}")
            for field in ("text", "with"):
                template(f"profiles.{name}.client_system.{field}", edit.get(field, ""))
        history = profile.get("history") or {}
        if history.get("mid_system", "keep") not in prompt_policy.MID_SYSTEM_MODES:
            problems.append(f"profiles.{name}.history: unknown mid_system {history.get('mid_system')}")
        for edit in history.get("user_blocks", []):
            if edit.get("op") not in prompt_policy.USER_BLOCK_OPS:
                problems.append(f"profiles.{name}.history.user_blocks: unknown op {edit.get('op')}")
        for edit in (profile.get("tools") or {}).get("edits", []):
            if not edit.get("hide") and edit.get("op") not in prompt_policy.TOOL_EDIT_OPS:
                problems.append(f"profiles.{name}.tools: unknown op {edit.get('op')}")
    for index, rule in enumerate(document.get("rules") or []):
        path = f"rules[{index}]"
        if set(rule) - RULE_KEYS or not rule.get("name"):
            problems.append(f"{path}: unexpected keys {sorted(set(rule) - RULE_KEYS)} or no name")
        problems += [f"{path}.if: unknown condition {key}" for key in rule.get("if", {})
                     if key not in prompt_policy.CONDITIONS]
        use = rule.get("use")
        if use not in profiles and use != "passthrough" and use not in prompt_policy.SPECIAL_USES:
            problems.append(f"{path}: unknown profile {use}")
    problems += [f"relay_texts.{key}: unknown" for key in (document.get("relay_texts") or {})
                 if key not in prompt_policy.RELAY_TEXT_NAMES]
    for glob, override in ((document.get("codex") or {}).get("models") or {}).items():
        problems += [f"codex.models.{glob}: unknown key {key}" for key in override
                     if key not in prompt_policy.CODEX_OVERRIDE_KEYS]
    return problems


def materialize_files(node, base):
    """Create the {"file": ...} texts a document names, so compile_document can read them."""
    if isinstance(node, dict):
        if set(node) == {"file"} and isinstance(node["file"], str):
            target = base / node["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("A placeholder text.\n", encoding="utf-8")
            return
        for value in node.values():
            materialize_files(value, base)
    elif isinstance(node, list):
        for value in node:
            materialize_files(value, base)


def compile_or_skip(document, base):
    base.mkdir(parents=True, exist_ok=True)
    materialize_files(document, base)
    try:
        return prompt_policy.compile_document(document, base, rev=1)
    except NotImplementedError:
        pytest.skip("prompt_policy.compile_document is not implemented yet")


def test_every_json_block_of_the_guide_parses():
    blocks = guide_blocks()
    assert len(blocks) >= 12, "the guide lost its examples"
    failures = []
    for line, language, body, _recipe in blocks:
        try:
            parse(language, body)
        except ValueError as error:
            failures.append(f"docs/prompt-control.md:{line} ({language}): {error}")
    assert failures == []


def test_plain_json_blocks_have_no_comments_to_strip():
    # A json block is what a reader copies into prompts.json: comments belong in a jsonc block only.
    commented = [line for line, language, body, _recipe in guide_blocks()
                 if language == "json" and strip_jsonc(body) != body]
    assert commented == []


def test_the_documents_of_the_guide_follow_the_contract():
    documents = guide_documents()
    assert len(documents) >= 10
    problems = [f"docs/prompt-control.md:{line}: {problem}" for line, document, _recipe in documents
                for problem in contract_problems(document)]
    assert problems == []


def test_the_example_is_plain_json_and_follows_the_contract():
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert document["version"] == prompt_policy.DOCUMENT_VERSION
    assert document["default_profile"] is None
    assert contract_problems(document) == []
    assert (document.get("protected") or {}).get("enabled", True) is True
    # Safe to copy as it is: only an explicit X-Proxi-Profile header selects a profile, and no Codex catalog text is set.
    assert [rule["use"] for rule in document["rules"]] == ["$header"]
    assert "codex" not in document and not (document.get("relay_texts") or {}).get("codex_base")
    assert not any(profile.get("publish_as_model") for profile in document["profiles"].values())


def test_the_example_holds_every_profile_and_text_of_the_recipes():
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    recipes = [(line, document) for line, document, recipe in guide_documents() if recipe]
    assert len(recipes) >= 8
    missing = []
    for line, document in recipes:
        missing += [f"line {line}: profile {name}" for name in document.get("profiles", {})
                    if name not in example["profiles"]]
        missing += [f"line {line}: text {name}" for name in document.get("texts", {}) if name not in example["texts"]]
    assert missing == []


def test_the_guide_and_the_example_carry_no_personal_data():
    found = []
    for path in (GUIDE, EXAMPLE):
        text = path.read_text(encoding="utf-8")
        found += [f"{path.name}: {match.group(0)}" for pattern in PERSONAL for match in pattern.finditer(text)]
    assert found == []


def test_the_readme_example_follows_the_contract():
    documents = readme_documents()
    assert documents, "the README lost its prompts.json example"
    problems = [f"README.md:{line}: {problem}" for line, document in documents for problem in contract_problems(document)]
    assert problems == []


def test_the_example_compiles(tmp_path):
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    snapshot = compile_or_skip(document, tmp_path)
    assert snapshot.rev == 1
    names = snapshot.profile_names()
    assert set(document["profiles"]) | {"passthrough"} <= set(names)
    for name in names:
        assert isinstance(snapshot.profile(name), dict)


def test_every_document_of_the_guide_compiles(tmp_path):
    failures = []
    for line, document, _recipe in guide_documents():
        try:
            compile_or_skip(document, tmp_path / f"line{line}")
        except prompt_policy.PromptConfigError as error:
            failures.append(f"docs/prompt-control.md:{line}: {error}")
    assert failures == []


def test_the_readme_example_compiles(tmp_path):
    for line, document in readme_documents():
        compile_or_skip(document, tmp_path / f"line{line}")
