"""prompts_file: where the prompts document lives, how config.load resolves it and what stays out of git."""
import json
import pathlib

import pytest

import apierr
import config
import prompt_policy

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RETOOL_THREAD_ID",
                 "RELAY_SESSION_MODE", "RELAY_HOST", "RELAY_PORT", "RELAY_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "synthetic-key")


def write_config(tmp_path, **extra):
    folder = tmp_path / "relay"
    folder.mkdir(exist_ok=True)
    path = folder / "config.json"
    path.write_text(json.dumps({"base": "https://a.invalid", "page_uuid": "page-a", "ua": "test",
                                "cookie": "accessToken=synthetic; xsrfToken=x", "xsrf": "x",
                                "workspace_root": str(tmp_path), **extra}), encoding="utf-8")
    return path


def load(tmp_path, **extra):
    path = write_config(tmp_path, **extra)
    return config.load(str(path)), path.parent


def test_without_a_setting_the_file_is_prompts_json_beside_config_json(tmp_path):
    cfg, folder = load(tmp_path)
    assert cfg["prompts_file"] == str(folder / "prompts.json")
    assert config.DEFAULTS["prompts_file"] == "" and config.PROMPTS_FILE == prompt_policy.DEFAULT_FILE


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_a_blank_setting_means_the_default_file(tmp_path, value):
    cfg, folder = load(tmp_path, prompts_file=value)
    assert cfg["prompts_file"] == str(folder / "prompts.json")


@pytest.mark.parametrize("value,expected", [
    ("mine.json", "relay/mine.json"),
    ("./mine.json", "relay/mine.json"),
    ("  mine.json  ", "relay/mine.json"),
    ("sub/dir/mine.json", "relay/sub/dir/mine.json"),
    ("../shared/mine.json", "shared/mine.json"),
])
def test_a_relative_path_starts_in_the_folder_of_config_json(tmp_path, value, expected):
    cfg, _ = load(tmp_path, prompts_file=value)
    assert pathlib.Path(cfg["prompts_file"]) == tmp_path / pathlib.Path(expected)


def test_an_absolute_path_is_kept_and_the_result_is_always_absolute(tmp_path):
    target = tmp_path / "elsewhere" / "prompts.json"
    cfg, _ = load(tmp_path, prompts_file=str(target))
    assert cfg["prompts_file"] == str(target)
    for value in ("", "a.json", "../a.json", str(target)):
        assert pathlib.Path(load(tmp_path, prompts_file=value)[0]["prompts_file"]).is_absolute()


def test_the_file_may_be_missing_and_is_never_created_by_loading(tmp_path):
    cfg, folder = load(tmp_path, prompts_file="not-yet/prompts.json")
    assert not pathlib.Path(cfg["prompts_file"]).exists() and not (folder / "not-yet").exists()


@pytest.mark.parametrize("value", [None, 5, True, 1.5, ["prompts.json"], {"file": "prompts.json"}])
def test_a_setting_that_is_not_a_string_stops_loading(tmp_path, value):
    path = write_config(tmp_path, prompts_file=value)
    before = path.read_bytes()
    with pytest.raises(apierr.ApiError, match="prompts_file must be a string"):
        config.load(str(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ["prompts\x00.json", "\x00", "a/\x00/b.json"])
def test_a_path_with_a_nul_character_stops_loading(tmp_path, value):
    with pytest.raises(apierr.ApiError, match="NUL"):
        load(tmp_path, prompts_file=value)


def test_an_unreasonably_long_path_stops_loading_and_the_limit_itself_loads(tmp_path):
    limit = config.PROMPTS_FILE_MAX_CHARS
    assert limit >= 1024
    with pytest.raises(apierr.ApiError, match="at most %d characters" % limit):
        load(tmp_path, prompts_file="p" * (limit + 1))
    assert load(tmp_path, prompts_file="p" * limit)[0]["prompts_file"].endswith("p" * limit)


def test_the_store_reads_the_path_that_was_loaded(tmp_path):
    cfg, folder = load(tmp_path, prompts_file="sub/mine.json")
    assert prompt_policy.PromptStore(cfg).path == folder / "sub" / "mine.json"
    default, folder = load(tmp_path)
    assert prompt_policy.PromptStore(default).path == folder / "prompts.json"
    # Built without config.load (as the tests do), the empty default resolves the same way.
    assert prompt_policy.PromptStore(dict(config.DEFAULTS, _config_path=str(folder / "config.json"))).path == \
        folder / "prompts.json"


def test_without_a_file_the_store_holds_no_revision(tmp_path):
    cfg, folder = load(tmp_path)
    assert not (folder / "prompts.json").exists() and prompt_policy.PromptStore(cfg).snapshot() is None


def test_the_example_config_names_the_setting_and_keeps_it_empty():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["prompts_file"] == "" and example["prompts_file_note"]


def test_personal_prompt_files_are_ignored_by_git_but_the_shared_sample_is_not():
    lines = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "/prompts.json" in lines and "/prompts/" in lines
    # Anchored to the repository root: web/src/prompts and the sample stay visible.
    assert not any(line in ("prompts.json", "prompts/", "prompts.example.json", "*.json", "prompts*")
                   for line in lines)
