"""The prompt control guide, the README snippet and the example stay valid.

Every json / jsonc block of docs/prompt-control.md parses (jsonc after dropping comments); the prompts documents
among them, the one in the README and prompts.example.json follow the closed lists of prompt_policy and compile
through prompt_policy.compile_document (skipped while the engine is only the contract skeleton).

The second half pins what the guide says about the final engine against the code: the constants of the limits it names,
the journal events it lists, the examples it quotes (a regular expression, a text file, a tag, a hidden tool) and the order
in which the store judges the rules.
"""
import copy
import json
import re
from pathlib import Path

import pytest

import prompt_body
import prompt_policy

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "prompt-control.md"
README = ROOT / "README.md"
EXAMPLE = ROOT / "prompts.example.json"
PLAN = ROOT / "docs" / "prompt-control-plan.md"
FENCE = re.compile(r"^```(json|jsonc)[ \t]*\r?\n(.*?)^```[ \t]*\r?$", re.S | re.M)
RECIPES = "\n## Рецепты\n"
RULE_KEYS = {"name", "if", "use", "enabled", "override_protected"}
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
VARIABLE = re.compile(r"(?<!\\)\{\{([^{}]*)\}\}")
PERSONAL = (re.compile(r"[A-Za-z]:\\Users\\"), re.compile(r"/(?:home|Users)/[^\s/]+"),
            re.compile(r"(?i)(?:api[_-]?key|cookie|token|secret)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{16,}"))
# Where the events of the prompt layer are emitted: the engine, the HTTP layer, the runner and the admin API.
EVENT_SOURCES = ("prompt_policy.py", "proxy.py", "orchestrator.py", "admin.py")
EVENT_CALL = re.compile(r"""(?:emit|_emit|log)\(\s*["']((?:prompt|admin\.prompt)[a-z_.]*)["']""")
MIB, KIB = 1024 * 1024, 1024


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


def heading_slugs(path):
    """The anchors GitHub gives the headings of a markdown file (fenced code is skipped)."""
    slugs, fenced = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6} ", line):
            title = line.lstrip("#").strip().replace("`", "").lower()
            slugs.add(re.sub(r"[^\w\- ]", "", title).replace(" ", "-"))
    return slugs


def broken_anchors(source, *, only_into=None):
    """Links of `source` that name a heading missing from the file they point at."""
    broken = []
    for target in LINK.findall(source.read_text(encoding="utf-8")):
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or "#" not in target:
            continue
        name, _hash, anchor = target.partition("#")
        page = (source.parent / name).resolve() if name else source
        if (only_into is None or page == only_into) and anchor not in heading_slugs(page):
            broken.append(f"{source.relative_to(ROOT).as_posix()}: {target}")
    return broken


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


def test_the_guide_names_everything_the_engine_enumerates():
    # The closed lists belong to prompt_policy: a new operation, variable or limit there fails this until it is documented.
    text = GUIDE.read_text(encoding="utf-8")
    expected = [f"{{{{{name}}}}}" for name in prompt_policy.VARIABLES]
    expected += [f"`{name}`" for name in (
        *prompt_policy.CLIENTS, *prompt_policy.SYSTEM_EDIT_OPS, *prompt_policy.MID_SYSTEM_MODES,
        *prompt_policy.USER_BLOCK_OPS, *prompt_policy.TOOL_EDIT_OPS, *prompt_policy.CONDITIONS,
        *prompt_policy.SPECIAL_USES, *prompt_policy.RELAY_TEXT_NAMES, *prompt_policy.CODEX_OVERRIDE_KEYS,
        *prompt_policy.BUILTIN_PROFILES)]
    for rule in prompt_policy.PROTECTED_RULES:
        expected.append(rule["name"])
        expected += [value for values in rule["if"].values() for value in values]
    expected += [prompt_policy.PROFILE_NAME.pattern, prompt_policy.DEFAULT_FILE,
                 f"не больше {prompt_policy.MAX_TEXT_BYTES // 1024} КиБ",
                 f"не больше {prompt_policy.MAX_DOCUMENT_BYTES // (1024 * 1024)} МиБ",
                 f"последние {prompt_policy.SNAPSHOT_HISTORY} ревизии", f"по умолчанию {prompt_policy.RECENT_LIMIT}"]
    assert [item for item in expected if item not in text] == []


def test_anchors_in_the_guide_and_towards_it_resolve():
    # test_docs_consistency checks that a linked file exists; a renamed heading breaks the #anchor silently.
    broken = broken_anchors(GUIDE)
    for source in (README, ROOT / "docs" / "relay-quality-and-prompts.md", ROOT / "docs" / "client-setup.md", PLAN):
        broken += broken_anchors(source, only_into=GUIDE)
    assert broken == []


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


# -- what the guide says about the final engine, against the code ----------------------------------------------------------


def guide_text():
    return GUIDE.read_text(encoding="utf-8")


def guide_section(start, stop):
    text = guide_text()
    begin = text.index(start)
    return text[begin:text.index(stop, begin + len(start))]


def journal_section():
    return guide_section("\n## Журнал\n", "\n## Рецепты\n")


def limits_section():
    return guide_section("\n## Пределы движка\n", "\n## Защитные правила\n")


def test_every_event_the_prompt_layer_emits_is_a_row_of_the_journal_table():
    emitted = {kind for name in EVENT_SOURCES for kind in EVENT_CALL.findall((ROOT / name).read_text(encoding="utf-8"))}
    # The scan itself must keep finding what it was written for: a renamed call must not make this pass by finding nothing.
    assert {"prompt.plan", "prompt.reload", "prompt.config_error", "prompt.edit_skipped", "prompt.runtime_error",
            "admin.prompts", "admin.prompt_pins", "prompt.pins_loaded", "prompt.pins_saved", "prompt.pins_error",
            "prompt.pin_restored", "prompt.pin_dropped", "prompt.revision_restored"} <= emitted
    table = journal_section()
    assert [kind for kind in sorted(emitted) if "| `%s` |" % kind not in table] == []


def test_the_values_the_journal_table_names_are_the_ones_the_code_writes():
    code = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in EVENT_SOURCES)
    table = journal_section()
    # the stages of prompt.runtime_error, the sources of prompt.reload and the reasons of the events about the pins
    for value in ("reload", "plan", "rewrite", "compose", "record", "too_large", "unreadable", "corrupt",
                  "revision_too_large", "revision_missing", "revision_corrupt", "revision_unreadable", "revision_invalid",
                  "expired", "restore", "write", "removed", "startup"):
        assert '"%s"' % value in code, "the code no longer writes %s" % value
        assert "`%s`" % value in table, "the journal table does not name %s" % value


def test_the_limits_table_names_every_limit_constant_with_its_value():
    pp = prompt_policy

    def grouped(number):
        return "{:,}".format(number).replace(",", " ")           # 50 000

    def seconds(number):
        return ("%g" % number).replace(".", ",") + " с"          # 0,25 с

    expected = {
        "MAX_TEXT_BYTES": "%d КиБ" % (pp.MAX_TEXT_BYTES // KIB),
        "MAX_DOCUMENT_BYTES": "%d МиБ" % (pp.MAX_DOCUMENT_BYTES // MIB),
        "MAX_HEAD_BYTES": "%d МиБ" % (pp.MAX_HEAD_BYTES // MIB),
        "MAX_EDIT_GROWTH": "%d КиБ" % (pp.MAX_EDIT_GROWTH // KIB),
        "MAX_BODY_GROWTH": "%d МиБ" % (pp.MAX_BODY_GROWTH // MIB),
        "MAX_EDITS": str(pp.MAX_EDITS),
        "MAX_PATTERN_CHARS": str(pp.MAX_PATTERN_CHARS),
        "MAX_PATTERN_WEIGHT": grouped(pp.MAX_PATTERN_WEIGHT),
        "REGEX_TIMEOUT": seconds(pp.REGEX_TIMEOUT),
        "REGEX_BUDGET": seconds(pp.REGEX_BUDGET),
        "MAX_MATCHES": grouped(pp.MAX_MATCHES),
        "MAX_PINS": str(pp.MAX_PINS),
        "PIN_IDLE_SECONDS": "%d ч" % (pp.PIN_IDLE_SECONDS // 3600),
    }
    rows = [line for line in limits_section().splitlines() if line.startswith("| ")]
    wrong = []
    for name, value in expected.items():
        row = next((line for line in rows if "`%s`" % name in line), None)
        if row is None:
            wrong.append("%s is in no row" % name)
        elif value not in row:
            wrong.append("%s: the row does not say %s" % (name, value))
    assert wrong == []


def test_the_other_numbers_the_guide_quotes_are_the_ones_of_the_code():
    import admin
    text = guide_text()
    assert prompt_policy.RELOAD_INTERVAL == 1.0 and "`RELOAD_INTERVAL`" in text
    assert prompt_policy.SNAPSHOT_HISTORY == 32 and "последние 32 ревизии" in text
    # a document of a pin and the pins file may be this long (the guide says 8 MiB), and so may the body of a save
    assert prompt_policy.MAX_REVISION_BYTES == prompt_policy.MAX_PINS_BYTES == admin.PROMPTS_BODY_LIMIT == 8 * MIB
    assert "8 МиБ" in text
    assert admin.PROMPTS_MAX_DEPTH == 24 and "глубже 24 уровней" in text
    assert (prompt_policy.PINS_DIR, prompt_policy.PINS_FILE, prompt_policy.REVISIONS_DIR) == (
        "prompt-pins", "pins.json", "revisions")
    for name in ("prompt-pins", "pins.json", "revisions/<sha256>.json"):
        assert name in text, name


def test_the_regex_rules_the_guide_states_hold(tmp_path):
    def accepted(pattern):
        document = {"version": 1, "profiles": {"p": {"client_system": {"edits": [
            {"op": "remove_regex", "pattern": pattern}]}}}}
        try:
            prompt_policy.compile_document(document, tmp_path, rev=1)
        except prompt_policy.PromptConfigError:
            return False
        return True

    # the regex module is installed (requirements.txt) and a plain pattern is taken
    assert accepted("a{2,3}") and accepted(r"\bfoo\b")
    # a brace that is no repeat count starts a fuzzy match in `regex`: refused, and the escaped one is taken
    assert not accepted("a{e<=1}") and accepted(r"a\{e<=1\}")
    assert not accepted("(a+)+")                                                   # nested quantifiers
    assert not accepted("(?:a{1000}){1000}") and accepted("(?:a{100}){100}")       # the weight of nested counts
    assert not accepted("a" * (prompt_policy.MAX_PATTERN_CHARS + 1))
    assert prompt_policy.MAX_PATTERN_CHARS == 512 and 0 < prompt_policy.REGEX_TIMEOUT <= prompt_policy.REGEX_BUDGET


def test_the_text_file_rules_the_guide_states_hold(tmp_path):
    import os
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_text("hello", encoding="utf-8")
    (tmp_path / "prompts" / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    def accepted(name):
        document = {"version": 1, "texts": {"t": {"file": name}}, "profiles": {"p": {"head": "{{text:t}}"}}}
        try:
            prompt_policy.compile_document(document, tmp_path, rev=1)
        except prompt_policy.PromptConfigError:
            return False
        return True

    assert accepted("prompts/a.md")
    for name in ("config.json", "../config.json", "prompts/../config.json", "prompts/b.json", "/prompts/a.md",
                 "C:\\prompts\\a.md", "prompts/a.md:stream", "a.md"):
        assert not accepted(name), name
    try:
        os.symlink(tmp_path / "config.json", tmp_path / "prompts" / "link.md")
    except (OSError, NotImplementedError):
        pass                                           # no right to make a link here: the rest of the rule is checked above
    else:
        assert not accepted("prompts/link.md")         # a link that leads out of prompts/
    assert prompt_policy.TEXT_DIR == "prompts" and prompt_policy.TEXT_SUFFIXES == (".md", ".txt")


def test_the_edit_rules_the_guide_states_hold(tmp_path):
    from test_prompt_policy_common import compose, document

    def head(edit, system):
        return compose(document(profiles={"p": {"head": "{{client_system}}", "client_system": {"edits": [edit]}}}),
                       tmp_path, system)[0]

    # replace_text: every occurrence unless all is false; `with` is required and an empty one deletes
    assert head({"op": "replace_text", "find": "a", "with": "b"}, "aaa") == "bbb"
    assert head({"op": "replace_text", "find": "a", "with": "b", "all": False}, "aaa") == "baa"
    assert head({"op": "replace_text", "find": "cut ", "with": ""}, "keep cut this") == "keep this"
    with pytest.raises(prompt_policy.PromptConfigError) as raised:
        head({"op": "replace_text", "find": "a"}, "aaa")
    assert raised.value.path.endswith(".with")
    # a tag block: the opening tag begins a line, the attributes are at most 512 characters long
    cut = {"op": "remove_tag", "tag": "env"}
    assert head(cut, "a\n<env>\nx\n</env>\nb") == "a\nb"
    assert head(cut, "see <env>x</env> here") == "see <env>x</env> here"
    assert head(cut, "a\n<env %s>x</env>\nb" % ("k" * 500)) == "a\nb"
    assert head(cut, "a\n<env %s>x</env>\nb" % ("k" * 600)).startswith("a\n<env k")


def hide_run(tmp_path, hidden, *, called=(), choice="auto", names=("bash", "read")):
    from test_prompt_policy_common import compile_doc, document
    snapshot = compile_doc(document(profiles={"p": {"tools": {"edits": [{"tool": hidden, "hide": True}]}}}), tmp_path)
    messages = [{"role": "user", "content": "go"}]
    for number, name in enumerate(called):
        messages += [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "c%d" % number, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c%d" % number, "content": "ok"}]
    body = {"model": "claude-opus-5-5", "messages": messages, "tool_choice": choice,
            "tools": [{"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object"}}}
                      for name in names]}
    out, changes = prompt_body.rewrite(body, "chat", snapshot.profile("p"), snapshot)
    return [tool["function"]["name"] for tool in out["tools"]], changes


def test_hide_keeps_the_tools_the_guide_says_it_keeps(tmp_path):
    names, changes = hide_run(tmp_path, "read")
    assert names == ["bash"] and not any(change.get("skipped") for change in changes)       # an ordinary hide
    for case in (dict(called=("bash",)),                                                       # the history called it
                 dict(choice={"type": "function", "function": {"name": "bash"}}),             # tool_choice names it
                 dict(choice="required", names=("bash",))):                                    # nothing would be left
        names, changes = hide_run(tmp_path, "bash", **case)
        assert "bash" in names, case
        assert [change["skipped"] for change in changes] == [True], case


def test_the_store_judges_the_rules_in_the_order_the_guide_lists(tmp_path):
    from test_prompt_policy_common import document, inp, make_store
    profiles = {name: {"head": "{{client_system}}"} for name in ("p", "v", "d")}
    rules = [{"name": "mine", "if": {"client": "opencode"}, "use": "p"},
             {"name": "override", "if": {"purpose": "session-title"}, "use": "p", "override_protected": True}]
    store = make_store(tmp_path, document(profiles=profiles, rules=rules, default_profile="d"))
    protected, virtual = prompt_policy.PROTECTED_RULES[0]["name"], prompt_policy.VIRTUAL_RULE_NAME
    assert store.plan(inp(purpose="session-title", tools=False)).rule == "override"      # 1: before the protected rules
    assert store.plan(inp(purpose="client-service", tools=False)).rule == protected      # 2: the protected rules
    assert store.plan(inp(virtual_profile="v")).rule == virtual                          # 3: before the user's rules
    assert store.plan(inp()).rule == "mine"                                              # 4: the user's rules
    assert store.plan(inp(client="codex")).rule == prompt_policy.DEFAULT_RULE_NAME       # 5: default_profile
    bare = make_store(tmp_path / "bare", document(profiles=profiles, rules=rules))
    assert bare.plan(inp(client="codex")).legacy                                         # 6: the legacy path
    assert bare.plan(inp(purpose="client-service", tools=False)).protected              # a service request is protected


def test_saving_leaves_the_previous_file_beside_the_new_one_as_a_bak(tmp_path):
    from test_prompt_policy_common import document, make_store
    first = document(profiles={"a": {"head": "one"}})
    store = make_store(tmp_path, first)
    store.save(document(profiles={"a": {"head": "two"}}), store.snapshot().rev)
    assert json.loads((tmp_path / "prompts.json.bak").read_text(encoding="utf-8")) == first
    assert "`prompts.json.bak`" in guide_text()


def test_the_codex_catalog_offers_no_persistent_effort_so_persistent_instructions_stay_idle():
    import models
    catalog = models.codex_catalog()["models"]
    levels = {level["effort"] for entry in catalog for level in entry["supported_reasoning_levels"]}
    assert "persistent" not in levels
    assert {entry["model_messages"]["persistent_instructions"] for entry in catalog} == {""}      # off unless a document sets it
    text = guide_text()
    assert "`persistent_instructions` почти не применим" in text and "около 5 минут" in text


def test_a_tool_edit_that_names_bash_names_shell_too():
    # OpenCode 2.x calls the command tool `shell`, 1.x `bash`: a profile that writes one of them reaches half of the clients
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    documents = [document for _line, document, _recipe in guide_documents()] + [example] + [
        document for _line, document in readme_documents()]
    seen = 0
    for document in documents:
        for name, profile in (document.get("profiles") or {}).items():
            for edit in (profile.get("tools") or {}).get("edits", []):
                tools = [edit["tool"]] if isinstance(edit["tool"], str) else edit["tool"]
                if "bash" in tools:
                    seen += 1
                    assert "shell" in tools, f"profile {name} names bash without shell"
    assert seen >= 3


def test_the_guide_and_the_readme_say_where_the_regex_module_comes_from():
    import regex  # noqa: F401 - the engine refuses a document with a regular expression without it
    assert re.search(r"(?m)^regex==", (ROOT / "requirements.txt").read_text(encoding="utf-8"))
    assert "pip install -r requirements.txt" in README.read_text(encoding="utf-8")
    assert "`regex`" in README.read_text(encoding="utf-8") and "`regex`" in guide_text()


def test_the_guide_names_the_header_a_browser_may_send_and_the_version_of_the_hint_headers():
    text = guide_text()
    assert "`x-proxi-profile` входит в `access-control-allow-headers`" in text
    assert "2.1.273" in text
    allowed = (ROOT / "proxy.py").read_text(encoding="utf-8").split("access-control-allow-headers")[1][:700]
    assert "x-proxi-profile" in allowed


def test_the_historical_plan_marks_what_the_final_engine_no_longer_does():
    # The plan is history and is not rewritten; what it got wrong is marked and points at the guide.
    text = PLAN.read_text(encoding="utf-8")
    assert text.count("Устарело") >= 4
    for promise in ("модуль `regex`", "только папка `prompts/`", "prompt-pins", "override_protected"):
        assert promise in text, promise
