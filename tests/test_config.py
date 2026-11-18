"""The config loader: layers, merge rules, environment, per-repo lookup and hostile files."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from pr_comment import config as config_module
from pr_comment import yamlstrict
from pr_comment.config import (
    Config,
    ConfigError,
    HookEntry,
    LoadedConfig,
    config_hash,
    load_config,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schemas" / "config.json").read_text(encoding="utf-8"))
SECRET = "SECRET-marker-9f3a"

Write = Callable[..., Path]


@pytest.fixture
def cfg_dir(private_home: Path) -> Path:
    path = private_home / ".config" / "pr-comment"
    path.mkdir(parents=True, mode=0o700)
    return path


@pytest.fixture
def env(private_home: Path) -> dict[str, str]:
    return {"HOME": str(private_home), "XDG_CONFIG_HOME": str(private_home / ".config")}


@pytest.fixture
def write() -> Write:
    def _write(path: Path, content: str | bytes | Mapping[str, Any], mode: int = 0o644) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Mapping):
            content = json.dumps(content)  # JSON is YAML
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        path.chmod(mode)
        return path

    return _write


def error_of(path: Path | None, env: Mapping[str, str]) -> ConfigError:
    with pytest.raises(ConfigError) as caught:
        load_config(None if path is None else str(path), env=env)
    return caught.value


def invalid_key(
    write: Write, tmp_path: Path, env: Mapping[str, str], content: str | Mapping[str, Any]
) -> str:
    return error_of(write(tmp_path / "bad.yaml", content), env).key


# --- defaults -----------------------------------------------------------------------------------


def test_no_files_gives_the_defaults(env: dict[str, str]) -> None:
    loaded = load_config(None, env=env)
    assert loaded.config == Config()
    assert loaded.config_path is None
    assert loaded.config_hash == LoadedConfig.defaults().config_hash


def test_example_file_equals_the_defaults(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    loaded = load_config(str(write(tmp_path / "example.yaml", example)), env=env)
    assert loaded.config == Config()


def test_model_defaults_equal_the_schema_defaults() -> None:
    dumped = Config().model_dump(mode="json", by_alias=True)
    for key, spec in SCHEMA["properties"].items():
        if "default" in spec and spec.get("type") != "object":
            assert dumped[key] == spec["default"], key
    assert (
        dumped["footer"]["human"]
        == SCHEMA["properties"]["footer"]["properties"]["human"]["default"]
    )
    assert (
        dumped["footer"]["auto"] == SCHEMA["properties"]["footer"]["properties"]["auto"]["default"]
    )
    assert dumped["mentions"] == {"policy": "neutralize", "allow": []}
    assert dumped["gitlab"] == {"resolve_body_thread": "non_blocking"}
    assert dumped["hooks"] == {"pre-post": [], "post-post": []}


def test_empty_file_and_comment_only_file_give_defaults(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    for content in ("", "# nothing\n", "\ufeff# bom\n", "---\n"):
        loaded = load_config(str(write(tmp_path / "c.yaml", content)), env=env)
        assert loaded.config == Config()


# --- the schema and the loader agree ------------------------------------------------------------

VALID_DOCS: list[dict[str, Any]] = [
    {"timeout": 30},
    {"timeout": 0.5},
    {"timeout": "30s"},
    {"timeout": "90"},
    {"timeout": "250ms"},
    {"forgectl_bin": "/usr/local/bin/forgectl"},
    {"forgectl_config": None},
    {"forgectl_config": "/etc/forgectl.yaml"},
    {"me": {"name": "jane.doe@host"}},
    {"me": {"name": None}},
    {"footer": {"human": False}},
    {"footer": {"human": "Posted by {user} at {sha}"}},
    {"footer": {"auto": "{policy} {{literal}} {decision_id} {tool_version}"}},
    {"mentions": {"policy": "allow", "allow": ["a", "org/team", "x.y-z_1"]}},
    {"gitlab": {"resolve_body_thread": "never"}},
    {"allow_auto_approve": True},
    {"hook_env_deny": ["MY_VAR", "_x1"]},
    {"hooks": {"pre-post": [{"cmd": "gate", "timeout": "5s", "with_text": True}]}},
    {"hooks": {"post-post": [{"cmd": "n", "async": True}]}},
    {"repos": {"gitlab.example.com/group/sub/project": {"allow_auto_approve": True}}},
    {"repos": {"example.test:8443/o/r": {"footer": {"human": "x {user}"}}}},
]

INVALID_DOCS: list[dict[str, Any]] = [
    {"timeout": 0},
    {"timeout": -5},
    {"timeout": "-5s"},
    {"timeout": "0s"},
    {"timeout": "5x"},
    {"timeout": True},
    {"timeout": None},
    {"forgectl_bin": "./forgectl"},
    {"forgectl_bin": 3},
    {"forgectl_config": "-x"},
    {"forgectl_config": ""},
    {"me": {"name": "a b"}},
    {"me": {"name": "x" * 65}},
    {"me": {"extra": 1}},
    {"footer": {"human": True}},
    {"footer": {"human": None}},
    {"footer": {"human": "{bogus}"}},
    {"footer": {"human": "{"}},
    {"footer": {"human": "1 starts with a digit"}},
    {"footer": {"human": "has `code`"}},
    {"footer": {"human": "a" * 513}},
    {"footer": {"auto": False}},
    {"footer": {"auto": ""}},
    {"footer": {"other": "x"}},
    {"mentions": {"policy": "maybe"}},
    {"mentions": {"allow": ["@x"]}},
    {"mentions": {"allow": "x"}},
    {"gitlab": {"resolve_body_thread": "sometimes"}},
    {"allow_auto_approve": "yes"},
    {"allow_auto_approve": 1},
    {"hook_env_deny": ["1BAD"]},
    {"hook_env_deny": "X"},
    {"hooks": {"pre-post": [{"cmd": ""}]}},
    {"hooks": {"pre-post": [{"cmd": "x", "async": True}]}},
    {"hooks": {"pre-merge": []}},
    {"hooks": {"post-post": [{"timeout": 5}]}},
    {"repos": {"not-a-repo-id": {}}},
    {"repos": {"example.test/o/r": {"me": {"name": "x"}}}},
    {"repos": {"example.test/o/r": {"hooks": {}}}},
    {"repos": {"example.test/o/..": {}}},
    {"repos": {"Example.test/o/r": {}}},
    {"unknown_key": 1},
]


def _schema_valid(document: Mapping[str, Any]) -> bool:
    return Draft202012Validator(SCHEMA).is_valid(document)


@pytest.mark.parametrize("document", VALID_DOCS, ids=lambda d: json.dumps(d)[:60])
def test_valid_documents_pass_schema_and_loader(
    document: dict[str, Any], write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    assert _schema_valid(document)
    load_config(str(write(tmp_path / "c.yaml", document)), env=env)


@pytest.mark.parametrize("document", INVALID_DOCS, ids=lambda d: json.dumps(d)[:60])
def test_invalid_documents_fail_schema_and_loader(
    document: dict[str, Any], write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    assert not _schema_valid(document)
    error = error_of(write(tmp_path / "c.yaml", document), env)
    assert error.key
    assert str(error).startswith(error.key + ": ")


@pytest.mark.parametrize(
    ("document", "key"),
    [
        ({"timeout": "-5s"}, "timeout"),
        ({"forgectl_config": "-x"}, "forgectl_config"),
        ({"footer": {"human": "{bogus}"}}, "footer.human"),
        ({"footer": {"auto": ""}}, "footer.auto"),
        ({"footer": {"auto": False}}, "footer.auto"),
        ({"footer": {"nope": 1}}, "footer.nope"),
        ({"mentions": {"policy": "x"}}, "mentions.policy"),
        ({"mentions": {"allow": ["ok", "@bad"]}}, "mentions.allow.1"),
        (
            {"hooks": {"post-post": [{"cmd": "a"}, {"cmd": "b", "timeout": 0}]}},
            "hooks.post-post.1.timeout",
        ),
        ({"hooks": {"pre-post": [{"cmd": "a", "async": True}]}}, "hooks.pre-post.0.async"),
        ({"repos": {"bad": {}}}, "repos.bad"),
        (
            {"repos": {"example.test/o/r": {"footer": {"human": "{x}"}}}},
            "repos.example.test/o/r.footer.human",
        ),
        ({"me": {"name": "a b"}}, "me.name"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_error_names_the_key(
    document: dict[str, Any], key: str, write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    assert invalid_key(write, tmp_path, env, document) == key


# --- footer templates (checks the schema leaves to code) ----------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "Posted\u200b by {user}",  # Cf: zero width space
        "Posted\u202e by {user}",  # Cf: right-to-left override
        "Posted\u2028 by {user}",  # LS
        "Posted\u2029 by {user}",  # PS
        "Posted\x0b by",  # VT
        "Posted\x1c by",  # FS
        "Posted\x85 by",  # NEL
        "Posted\nby",
        "Posted\tby",
        "Posted \\ by",
        "Posted [x] by",
        "Posted <b> by",
        "Posted } by",
        "Posted {user by",
        "Posted {{{user}} by",
        "Posted {USER} by",
        "Posted {user } by",
        "Posted {} by",
        "\u00c9 not ascii start",
        " leading space",
        "Posted \ud800 by",
    ],
)
def test_bad_footer_templates_are_refused(
    template: str, write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    for side in ("human", "auto"):
        document = {"footer": {side: template}}
        assert invalid_key(write, tmp_path, env, document) == f"footer.{side}"


def test_footer_may_start_with_a_placeholder_or_escaped_brace(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    document = {"footer": {"human": "{user} posted {{this}}", "auto": "{{x}} {policy}"}}
    loaded = load_config(str(write(tmp_path / "c.yaml", document)), env=env)
    assert loaded.config.footer.human == "{user} posted {{this}}"


def test_footer_human_false_is_kept_and_auto_never_false(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    loaded = load_config(str(write(tmp_path / "c.yaml", {"footer": {"human": False}})), env=env)
    assert loaded.config.footer.human is False
    assert loaded.for_repo("example.test/o/r").footer_human is False


def test_footer_length_boundary(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    ok = {"footer": {"human": "a" * 512}}
    load_config(str(write(tmp_path / "ok.yaml", ok)), env=env)
    assert invalid_key(write, tmp_path, env, {"footer": {"human": "a" * 513}}) == "footer.human"


# --- layers and merge rules ---------------------------------------------------------------------


def test_drop_ins_merge_in_lexical_order(cfg_dir: Path, write: Write, env: dict[str, str]) -> None:
    write(cfg_dir / "config.yaml", {"timeout": "10s", "gitlab": {"resolve_body_thread": "never"}})
    write(cfg_dir / "config.d" / "20-b.yaml", {"timeout": "20s"})
    write(cfg_dir / "config.d" / "10-a.yaml", {"timeout": "99s"})
    write(cfg_dir / "config.d" / "2-c.yaml", {"mentions": {"policy": "allow"}})
    loaded = load_config(None, env=env)
    # lexical order is 10-a, 2-c, 20-b: the last writer of `timeout` is 20-b.
    assert loaded.config.timeout == "20s"
    assert loaded.config.gitlab.resolve_body_thread == "never"
    assert loaded.config.mentions.policy == "allow"


def test_drop_in_overrides_main_and_a_later_drop_in_overrides_an_earlier_one(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.yaml", {"me": {"name": "main"}})
    write(cfg_dir / "config.d" / "a.yaml", {"me": {"name": "first"}})
    write(cfg_dir / "config.d" / "b.yaml", {"me": {"name": "second"}})
    assert load_config(None, env=env).config.me.name == "second"


def test_hook_lists_append_other_lists_replace(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(
        cfg_dir / "config.yaml",
        {
            "hooks": {"pre-post": [{"cmd": "one"}], "post-post": [{"cmd": "p1"}]},
            "mentions": {"allow": ["a", "b"]},
            "hook_env_deny": ["X1"],
        },
    )
    write(
        cfg_dir / "config.d" / "10.yaml",
        {
            "hooks": {"pre-post": [{"cmd": "two"}]},
            "mentions": {"allow": ["c"]},
            "hook_env_deny": ["X2"],
        },
    )
    write(cfg_dir / "config.d" / "20.yaml", {"hooks": {"pre-post": [{"cmd": "three"}]}})
    config = load_config(None, env=env).config
    assert [h.cmd for h in config.hooks.pre_post] == ["one", "two", "three"]
    assert [h.cmd for h in config.hooks.post_post] == ["p1"]
    assert config.mentions.allow == ("c",)
    assert config.hook_env_deny == ("X2",)


def test_hooks_for_adds_cli_hooks_after_configured_ones(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(
        cfg_dir / "config.yaml",
        {
            "hooks": {
                "pre-post": [{"cmd": "gate", "timeout": "5s", "with_text": True}],
                "post-post": [{"cmd": "tell", "async": True}],
            }
        },
    )
    loaded = load_config(None, env=env)
    cli = ["pre-post=extra gate", "post-post=ping", "pre-post=second"]
    assert loaded.hooks_for("pre-post", cli) == (
        HookEntry("gate", 5.0, False, True),
        HookEntry("extra gate"),
        HookEntry("second"),
    )
    assert loaded.hooks_for("post-post", cli) == (
        HookEntry("tell", 30.0, True, False),
        HookEntry("ping"),
    )
    assert loaded.hooks_for("post-post") == (HookEntry("tell", 30.0, True, False),)
    with pytest.raises(ValueError):
        loaded.hooks_for("pre-merge")


def test_hook_defaults(cfg_dir: Path, write: Write, env: dict[str, str]) -> None:
    write(cfg_dir / "config.yaml", {"hooks": {"post-post": [{"cmd": "x"}]}})
    hook = load_config(None, env=env).config.hooks.post_post[0]
    assert (hook.timeout, hook.async_, hook.with_text) == (30.0, False, False)


def test_invalid_drop_in_fails_the_whole_load(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.yaml", {"timeout": "10s"})
    write(cfg_dir / "config.d" / "bad.yaml", {"timeout": "-5s"})
    assert error_of(None, env).key == "timeout"


def test_config_flag_reads_drop_ins_next_to_it_not_the_default_ones(
    cfg_dir: Path, write: Write, env: dict[str, str], tmp_path: Path
) -> None:
    write(cfg_dir / "config.d" / "x.yaml", {"me": {"name": "default-place"}})
    other = tmp_path / "other"
    write(other / "main.yaml", {"timeout": "7s"})
    write(other / "config.d" / "x.yaml", {"me": {"name": "next-to-it"}})
    loaded = load_config(str(other / "main.yaml"), env=env)
    assert (loaded.config.timeout, loaded.config.me.name) == ("7s", "next-to-it")
    assert loaded.config_path == str(other / "main.yaml")


def test_config_flag_does_not_read_the_default_main_file(
    cfg_dir: Path, write: Write, env: dict[str, str], tmp_path: Path
) -> None:
    write(cfg_dir / "config.yaml", {"timeout": "1s"})
    loaded = load_config(str(write(tmp_path / "o" / "c.yaml", {})), env=env)
    assert loaded.config.timeout == "120s"


def test_config_flag_file_must_exist(tmp_path: Path, env: dict[str, str]) -> None:
    error = error_of(tmp_path / "missing.yaml", env)
    assert error.key == str(tmp_path / "missing.yaml")


def test_relative_config_flag_is_resolved(
    write: Write, tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "rel" / "c.yaml", {"me": {"name": "rel"}})
    monkeypatch.chdir(tmp_path / "rel")
    assert load_config("c.yaml", env=env).config.me.name == "rel"


def test_hidden_and_non_yaml_drop_ins_are_skipped(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    for name in (".hidden.yaml", "note.txt", "x.yml", "x.yaml.bak", "x.YAML"):
        write(cfg_dir / "config.d" / name, {"timeout": "-1s"})
    assert load_config(None, env=env).config == Config()


def test_xdg_config_home_is_used_when_absolute(
    write: Write, tmp_path: Path, private_home: Path
) -> None:
    write(tmp_path / "xdg" / "pr-comment" / "config.yaml", {"me": {"name": "xdg"}})
    write(private_home / ".config" / "pr-comment" / "config.yaml", {"me": {"name": "home"}})
    env = {"HOME": str(private_home), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert load_config(None, env=env).config.me.name == "xdg"


@pytest.mark.parametrize("xdg", ["", "relative/dir"])
def test_relative_or_empty_xdg_config_home_is_ignored(
    xdg: str, write: Write, private_home: Path
) -> None:
    write(private_home / ".config" / "pr-comment" / "config.yaml", {"me": {"name": "home"}})
    env = {"HOME": str(private_home), "XDG_CONFIG_HOME": xdg}
    assert load_config(None, env=env).config.me.name == "home"


def test_repo_config_in_the_working_directory_is_never_read(
    write: Write, tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    for name in ("config.yaml", ".pr-comment.yaml", "pr-comment.yaml"):
        write(checkout / name, {"allow_auto_approve": True})
    write(checkout / "config.d" / "x.yaml", {"allow_auto_approve": True})
    monkeypatch.chdir(checkout)
    assert load_config(None, env=env).config.allow_auto_approve is False


# --- environment --------------------------------------------------------------------------------


def test_environment_overrides_config(cfg_dir: Path, write: Write, env: dict[str, str]) -> None:
    write(
        cfg_dir / "config.yaml",
        {
            "timeout": "10s",
            "forgectl_bin": "from-file",
            "forgectl_config": "/f",
            "me": {"name": "file"},
        },
    )
    env.update(
        PR_COMMENT_TIMEOUT="30",
        PR_COMMENT_FORGECTL_BIN="/opt/forgectl",
        PR_COMMENT_FORGECTL_CONFIG="/env/forgectl.yaml",
        PR_COMMENT_ME__NAME="env.user",
    )
    config = load_config(None, env=env).config
    assert config.timeout == "30"
    assert config.forgectl_bin == "/opt/forgectl"
    assert config.forgectl_config == "/env/forgectl.yaml"
    assert config.me.name == "env.user"


def test_environment_applies_without_any_file(env: dict[str, str]) -> None:
    env["PR_COMMENT_TIMEOUT"] = "2m"
    assert load_config(None, env=env).config.timeout == "2m"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PR_COMMENT_TIMEOUT", "-5s"),
        ("PR_COMMENT_TIMEOUT", "0"),
        ("PR_COMMENT_TIMEOUT", ""),
        ("PR_COMMENT_TIMEOUT", "30s\n"),
        ("PR_COMMENT_TIMEOUT", "1e3"),
        ("PR_COMMENT_TIMEOUT", "\u0663\u0660"),  # Arabic-Indic digits
        ("PR_COMMENT_TIMEOUT", "99999999999999999999d"),
        ("PR_COMMENT_FORGECTL_BIN", "./x"),
        ("PR_COMMENT_FORGECTL_BIN", "."),
        ("PR_COMMENT_FORGECTL_BIN", ""),
        ("PR_COMMENT_FORGECTL_BIN", "bad\x01name"),
        ("PR_COMMENT_FORGECTL_BIN", "bad\udcffname"),
        ("PR_COMMENT_FORGECTL_CONFIG", "-x"),
        ("PR_COMMENT_FORGECTL_CONFIG", ""),
        ("PR_COMMENT_FORGECTL_CONFIG", "a\udcffb"),
        ("PR_COMMENT_ME__NAME", ""),
        ("PR_COMMENT_ME__NAME", "has space"),
        ("PR_COMMENT_ME__NAME", "x" * 65),
        ("PR_COMMENT_ME__NAME", "name\n"),
    ],
)
def test_invalid_environment_value_names_the_variable(
    name: str, value: str, env: dict[str, str]
) -> None:
    env[name] = value
    assert error_of(None, env).key == name


def test_environment_cannot_set_lists_maps_or_allow_auto_approve(
    env: dict[str, str],
) -> None:
    env.update(
        PR_COMMENT_ALLOW_AUTO_APPROVE="true",
        PR_COMMENT_MENTIONS__POLICY="allow",
        PR_COMMENT_MENTIONS__ALLOW="x",
        PR_COMMENT_HOOKS__PRE_POST="evil",
        PR_COMMENT_HOOK_ENV_DENY="X",
        PR_COMMENT_REPOS="x",
        PR_COMMENT_FOOTER__HUMAN="x",
        PR_COMMENT_HOOK="pre-post",
        PR_COMMENT_ID="x",
        PR_COMMENT_OBJECT_TYPE="pr",
        USER="somebody",
        LOGNAME="somebody",
    )
    assert load_config(None, env=env).config == Config()


def test_environment_only_in_the_given_mapping_not_the_process(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "-5s")
    assert load_config(None, env=env).config.timeout == "120s"


def test_repo_override_outranks_environment_for_the_keys_both_could_set(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    # The environment sets scalar top-level keys only, so a repo override never competes with it;
    # the repo value stays in force whatever the environment holds.
    write(cfg_dir / "config.yaml", {"repos": {"example.test/o/r": {"allow_auto_approve": True}}})
    env["PR_COMMENT_TIMEOUT"] = "9"
    loaded = load_config(None, env=env)
    assert loaded.for_repo("example.test/o/r").allow_auto_approve is True
    assert loaded.config.timeout == "9"


# --- per-repo merge -----------------------------------------------------------------------------


REPO = "gitlab.example.com/group/sub/project"


def test_repo_overrides_merge_over_global_and_inherit(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(
        cfg_dir / "config.yaml",
        {
            "footer": {"human": "global {user}", "auto": "global auto {policy}"},
            "mentions": {"policy": "neutralize", "allow": ["g1", "g2"]},
            "gitlab": {"resolve_body_thread": "always"},
            "allow_auto_approve": False,
            "repos": {
                REPO: {
                    "footer": {"human": False},
                    "mentions": {"allow": ["r1"]},
                    "allow_auto_approve": True,
                }
            },
        },
    )
    loaded = load_config(None, env=env)
    repo = loaded.for_repo(REPO)
    assert repo.footer_human is False
    assert repo.footer_auto == "global auto {policy}"
    assert repo.mentions_policy == "neutralize"
    assert repo.mentions_allow == ("r1",)
    assert repo.resolve_body_thread == "always"
    assert repo.allow_auto_approve is True
    other = loaded.for_repo("gitlab.example.com/group/other")
    assert other.footer_human == "global {user}"
    assert other.mentions_allow == ("g1", "g2")
    assert other.allow_auto_approve is False


def test_repo_lookup_ignores_case_of_the_canonical_id(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(
        cfg_dir / "config.yaml",
        {"repos": {"example.test/Org/Repo": {"gitlab": {"resolve_body_thread": "never"}}}},
    )
    loaded = load_config(None, env=env)
    for spelling in ("example.test/Org/Repo", "example.test/org/repo", "EXAMPLE.TEST/ORG/REPO"):
        assert loaded.for_repo(spelling).resolve_body_thread == "never"
    assert loaded.for_repo("example.test/org/repo2").resolve_body_thread == "non_blocking"


def test_repo_keys_differing_only_by_case_are_refused(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.yaml", {"repos": {"example.test/o/R": {}, "example.test/o/r": {}}})
    assert error_of(None, env).key == "repos.example.test/o/r"


def test_repo_sections_merge_across_files(cfg_dir: Path, write: Write, env: dict[str, str]) -> None:
    write(
        cfg_dir / "config.yaml",
        {"repos": {REPO: {"footer": {"human": "a {user}"}, "allow_auto_approve": True}}},
    )
    write(cfg_dir / "config.d" / "1.yaml", {"repos": {REPO: {"footer": {"auto": "b {policy}"}}}})
    repo = load_config(None, env=env).for_repo(REPO)
    assert (repo.footer_human, repo.footer_auto, repo.allow_auto_approve) == (
        "a {user}",
        "b {policy}",
        True,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"footer": {"human": None}},
        {"footer": {"auto": None}},
        {"mentions": {"policy": None}},
        {"mentions": {"allow": None}},
        {"gitlab": {"resolve_body_thread": None}},
        {"allow_auto_approve": None},
    ],
)
def test_repo_override_null_is_not_an_unset(
    override: dict[str, Any], write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    assert invalid_key(write, tmp_path, env, {"repos": {REPO: override}}).startswith(
        f"repos.{REPO}"
    )


def test_repo_override_cannot_set_unlisted_keys(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    for key in ("timeout", "forgectl_bin", "hooks", "me", "hook_env_deny", "repos"):
        assert (
            invalid_key(write, tmp_path, env, {"repos": {REPO: {key: {}}}}) == f"repos.{REPO}.{key}"
        )


# --- warnings -----------------------------------------------------------------------------------


def test_files_setting_guarded_keys_are_reported_by_file_and_key(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    main = write(cfg_dir / "config.yaml", {"timeout": "5s", "allow_auto_approve": False})
    quiet = write(cfg_dir / "config.d" / "10-quiet.yaml", {"footer": {"human": "x {user}"}})
    drop = write(
        cfg_dir / "config.d" / "20.yaml",
        {
            "mentions": {"policy": "allow", "allow": ["a"]},
            "repos": {REPO: {"allow_auto_approve": True, "mentions": {"allow": []}}},
        },
    )
    warnings = {w.file: w.keys for w in load_config(None, env=env).warnings}
    assert str(quiet) not in warnings
    assert warnings[str(main)] == ("allow_auto_approve",)
    assert warnings[str(drop)] == (
        "mentions.policy",
        "mentions.allow",
        f"repos.{REPO}.allow_auto_approve",
        f"repos.{REPO}.mentions.allow",
    )


def test_no_warning_without_guarded_keys(env: dict[str, str]) -> None:
    assert load_config(None, env=env).warnings == ()


# --- config hash --------------------------------------------------------------------------------


def test_hash_ignores_how_a_duration_is_written(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    hashes = {
        load_config(str(write(tmp_path / f"{i}.yaml", {"timeout": value})), env=env).config_hash
        for i, value in enumerate([60, 60.0, "60", "60s", "1m", "60000ms"])
    }
    assert len(hashes) == 1


def test_hash_changes_with_the_config_and_is_a_sha256(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    base = LoadedConfig.defaults().config_hash
    assert len(base) == 64 and set(base) <= set("0123456789abcdef")
    for i, document in enumerate(
        [
            {"timeout": "61s"},
            {"footer": {"human": "x"}},
            {"allow_auto_approve": True},
            {"repos": {REPO: {}}},
        ]
    ):
        assert (
            load_config(str(write(tmp_path / f"{i}.yaml", document)), env=env).config_hash != base
        )


def test_hash_follows_the_environment(env: dict[str, str]) -> None:
    env["PR_COMMENT_TIMEOUT"] = "60"
    assert load_config(None, env=env).config_hash != LoadedConfig.defaults().config_hash
    env["PR_COMMENT_TIMEOUT"] = "120"
    assert load_config(None, env=env).config_hash == LoadedConfig.defaults().config_hash
    assert config_hash(Config()) == LoadedConfig.defaults().config_hash


# --- hostile YAML -------------------------------------------------------------------------------

HOSTILE_YAML = [
    pytest.param("a: &x 1\nb: *x\n", id="anchor-and-alias"),
    pytest.param("timeout: &t 5s\n", id="anchor-only"),
    pytest.param("base: &b {timeout: 5s}\n<<: *b\n", id="merge-key"),
    pytest.param("<<: {timeout: 5s}\n", id="merge-key-literal"),
    pytest.param("timeout: 5s\ntimeout: 6s\n", id="duplicate-key"),
    pytest.param("footer: {human: a}\nfooter: {human: b}\n", id="duplicate-map"),
    pytest.param("1: x\n", id="int-key"),
    pytest.param("true: x\n", id="bool-key"),
    pytest.param("null: x\n", id="null-key"),
    pytest.param("~: x\n", id="tilde-key"),
    pytest.param("? [a]\n: x\n", id="sequence-key"),
    pytest.param("? {a: b}\n: x\n", id="map-key"),
    pytest.param("timeout: !!python/object/apply:os.system [id]\n", id="python-object"),
    pytest.param("timeout: !!python/name:os.system\n", id="python-name"),
    pytest.param("timeout: !!binary aGk=\n", id="binary"),
    pytest.param("timeout: !!set {a}\n", id="set"),
    pytest.param("timeout: !!omap [a: 1]\n", id="omap"),
    pytest.param("timeout: !custom x\n", id="custom-tag"),
    pytest.param("timeout: !!int abc\n", id="mistyped-int"),
    pytest.param("%TAG !e! tag:example.com,2000:\n---\ntimeout: !e!x 5\n", id="tag-directive"),
    pytest.param("a: 1\n---\nb: 2\n", id="two-documents"),
    pytest.param("- a\n- b\n", id="top-level-list"),
    pytest.param("just a string\n", id="top-level-scalar"),
    pytest.param("[1, 2\n", id="syntax-error"),
    pytest.param("a: b: c\n", id="syntax-error-2"),
    pytest.param('timeout: "a\\0b"\n', id="nul"),
    pytest.param('timeout: "a\\ud800b"\n', id="lone-surrogate"),
    pytest.param('"ke\\0y": 1\n', id="nul-in-key"),
    pytest.param("timeout: \x01\n", id="control-char"),
    pytest.param("{" * 40 + "}" * 40 + "\n", id="deep-flow-map"),
    pytest.param("[" * 5000 + "]" * 5000 + "\n", id="very-deep-flow-seq"),
    pytest.param("a:\n" + "".join(f"{' ' * i}b:\n" for i in range(1, 50)), id="deep-block-map"),
    pytest.param("[" + ", ".join(["1"] * 10_100) + "]\n", id="too-many-nodes"),
    pytest.param("a: " + "x" * 300_000 + "\n", id="too-large"),
    pytest.param(
        "a: &a [x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nd: &d [*c, *c]\ne: [*d, *d]\n",
        id="alias-bomb",
    ),
]


@pytest.mark.parametrize("content", HOSTILE_YAML)
def test_hostile_yaml_is_config_invalid_and_never_echoed(
    content: str, write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    path = write(tmp_path / "hostile.yaml", content)
    error = error_of(path, env)
    assert error.key
    assert "python" not in str(error)
    assert "os.system" not in str(error)


def test_invalid_utf8_is_refused(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    path = write(tmp_path / "c.yaml", b"me: {name: \xff\xfe}\n")
    assert error_of(path, env).key == str(path)


def test_utf16_file_is_refused(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    path = write(tmp_path / "c.yaml", "timeout: 5s\n".encode("utf-16"))
    assert error_of(path, env).key == str(path)


def test_file_size_boundary(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    limit = config_module.MAX_FILE_BYTES
    header = "# "
    ok = write(tmp_path / "ok.yaml", header + "x" * (limit - len(header)))
    assert ok.stat().st_size == limit
    load_config(str(ok), env=env)
    big = write(tmp_path / "big.yaml", header + "x" * (limit - len(header) + 1))
    assert "too large" in error_of(big, env).message


def test_node_limit_boundary(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    # One mapping plus one key and value per entry; the check is on nodes, not on size.
    limit = yamlstrict.MAX_NODES
    # A mapping node, then a key node and a value node per entry.
    ok = "{" + ", ".join(f"k{i}: 1" for i in range((limit - 1) // 2)) + "}"
    over = "{" + ", ".join(f"k{i}: 1" for i in range((limit - 1) // 2 + 1)) + "}"
    # Unknown keys fail validation, which proves the parse itself succeeded at the limit.
    assert error_of(write(tmp_path / "ok.yaml", ok), env).key == "k0"
    error = error_of(write(tmp_path / "over.yaml", over), env)
    assert error.key == str(tmp_path / "over.yaml") and "too many nodes" in error.message


def test_yaml_11_forms_are_strings_not_booleans_or_numbers(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    # PyYAML (YAML 1.1) would read `yes` as true and `1:30` as the integer 90.
    assert invalid_key(write, tmp_path, env, "allow_auto_approve: yes\n") == "allow_auto_approve"
    assert invalid_key(write, tmp_path, env, "allow_auto_approve: on\n") == "allow_auto_approve"
    assert invalid_key(write, tmp_path, env, "timeout: 1:30\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "timeout: 0x10\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "timeout: .inf\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "timeout: .nan\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "timeout: 1_000\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "allow_auto_approve: True1\n") == "allow_auto_approve"


def test_core_schema_scalars_are_read(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    content = "allow_auto_approve: TRUE\ntimeout: 1.5e1\nme: {name: ~}\nforgectl_config: null\n"
    config = load_config(str(write(tmp_path / "c.yaml", content)), env=env).config
    assert config.allow_auto_approve is True
    assert config.timeout == "15"
    assert config.me.name is None


def test_huge_numbers_do_not_crash(write: Write, tmp_path: Path, env: dict[str, str]) -> None:
    path = write(tmp_path / "bad.yaml", "timeout: " + "9" * 5000 + "\n")
    assert error_of(path, env).key == str(path)  # beyond Python's int digit limit: a refused file
    assert invalid_key(write, tmp_path, env, "timeout: " + "9" * 400 + "\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "timeout: 1e999\n") == "timeout"
    assert invalid_key(write, tmp_path, env, "timeout: 1e-999\n") == "timeout"


def test_errors_never_repeat_file_content(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    documents = [
        f"timeout: {SECRET}\n",
        f"me: {{name: '{SECRET} x'}}\n",
        f"{SECRET}: 1\n",
        f"footer: {{human: '{{{SECRET}}}'}}\n",
        f"timeout: !{SECRET} x\n",
        f"a: &{SECRET} 1\n",
        f"hooks: {{{SECRET}: []}}\n",
        f"forgectl_bin: ['{SECRET}']\n",
        f"repos: {{'{SECRET}': {{}}}}\n",
        f"x: [\n{SECRET}\n",
    ]
    for document in documents:
        error = error_of(write(tmp_path / "c.yaml", document), env)
        # Only a key may be named (the offending unknown key), never a value or a tag.
        assert SECRET not in error.message, document


# --- hostile files and directories --------------------------------------------------------------


def test_fifo_is_refused_without_blocking(
    cfg_dir: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    fifo = tmp_path / "fifo.yaml"
    os.mkfifo(fifo)
    assert "not a regular file" in error_of(fifo, env).message
    os.mkfifo(cfg_dir / "config.yaml")
    assert "not a regular file" in error_of(None, env).message


def test_fifo_drop_in_is_refused_without_blocking(cfg_dir: Path, env: dict[str, str]) -> None:
    (cfg_dir / "config.d").mkdir(mode=0o700)
    os.mkfifo(cfg_dir / "config.d" / "x.yaml")
    assert "not a regular file" in error_of(None, env).message


def test_directory_named_like_a_config_is_refused(
    cfg_dir: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    (tmp_path / "d.yaml").mkdir()
    assert "not a regular file" in error_of(tmp_path / "d.yaml", env).message
    (cfg_dir / "config.d" / "sub.yaml").mkdir(parents=True, mode=0o700)
    assert "not a regular file" in error_of(None, env).message


def test_device_file_is_refused(env: dict[str, str]) -> None:
    assert "not a regular file" in error_of(Path("/dev/null"), env).message


def test_symlinked_config_is_followed_to_a_good_target(
    cfg_dir: Path, write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    target = write(tmp_path / "dotfiles" / "main.yaml", {"me": {"name": "linked"}})
    (cfg_dir / "config.yaml").symlink_to(target)
    drop = write(tmp_path / "dotfiles" / "drop.yaml", {"timeout": "8s"})
    (cfg_dir / "config.d").mkdir(mode=0o700)
    (cfg_dir / "config.d" / "10.yaml").symlink_to(drop)
    config = load_config(None, env=env).config
    assert (config.me.name, config.timeout) == ("linked", "8s")


def test_symlink_to_world_writable_target_is_refused(
    cfg_dir: Path, write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    target = write(tmp_path / "t.yaml", {}, mode=0o666)
    (cfg_dir / "config.yaml").symlink_to(target)
    assert "writable by everyone" in error_of(None, env).message


def test_dangling_symlinks_are_refused_not_skipped(
    cfg_dir: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    (cfg_dir / "config.yaml").symlink_to(tmp_path / "nowhere.yaml")
    assert "symbolic link to nothing" in error_of(None, env).message
    (cfg_dir / "config.yaml").unlink()
    (cfg_dir / "config.d").mkdir(mode=0o700)
    (cfg_dir / "config.d" / "x.yaml").symlink_to(tmp_path / "nowhere.yaml")
    assert "symbolic link to nothing" in error_of(None, env).message


def test_symlink_loop_is_refused(cfg_dir: Path, env: dict[str, str]) -> None:
    (cfg_dir / "config.yaml").symlink_to(cfg_dir / "config.yaml")
    assert error_of(None, env).key == str(cfg_dir / "config.yaml")


@pytest.mark.parametrize("mode", [0o666, 0o646, 0o602, 0o777])
def test_world_writable_files_are_refused(
    mode: int, cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.yaml", {}, mode=mode)
    error = error_of(None, env)
    assert "writable by everyone" in error.message
    assert error.key == str(cfg_dir / "config.yaml")


def test_world_writable_drop_in_is_refused(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.d" / "x.yaml", {}, mode=0o666)
    assert "writable by everyone" in error_of(None, env).message


def test_world_writable_drop_in_directory_is_refused(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.d" / "x.yaml", {})
    (cfg_dir / "config.d").chmod(0o777)
    assert "writable by everyone" in error_of(None, env).message


def test_group_writable_and_group_readable_files_are_accepted(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.yaml", {}, mode=0o664)
    write(cfg_dir / "config.d" / "x.yaml", {}, mode=0o640)
    load_config(None, env=env)


def test_drop_in_directory_that_is_a_file_is_refused(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    write(cfg_dir / "config.d", "x")
    assert "not a directory" in error_of(None, env).message


def test_dangling_drop_in_directory_is_refused(
    cfg_dir: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    (cfg_dir / "config.d").symlink_to(tmp_path / "nowhere")
    assert error_of(None, env).key == str(cfg_dir / "config.d")


def test_files_owned_by_another_user_are_refused(
    cfg_dir: Path, write: Write, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg_dir / "config.yaml", {})
    other = os.geteuid() + 1
    monkeypatch.setattr(config_module.os, "geteuid", lambda: other)
    error = error_of(None, env)
    assert "owned by another user" in error.message


def test_directory_owned_by_another_user_is_refused(
    cfg_dir: Path, write: Write, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfg_dir / "config.d" / "x.yaml", {})
    other = os.geteuid() + 1
    monkeypatch.setattr(config_module.os, "geteuid", lambda: other)
    assert "owned by another user" in error_of(None, env).message


def test_too_many_drop_ins_are_refused(cfg_dir: Path, env: dict[str, str]) -> None:
    drops = cfg_dir / "config.d"
    drops.mkdir(mode=0o700)
    for i in range(config_module.MAX_DROP_INS + 1):
        (drops / f"{i:04}.yaml").write_text("{}\n", encoding="utf-8")
    assert "too many drop-in files" in error_of(None, env).message


def test_drop_in_limit_boundary(cfg_dir: Path, env: dict[str, str]) -> None:
    drops = cfg_dir / "config.d"
    drops.mkdir(mode=0o700)
    for i in range(config_module.MAX_DROP_INS):
        (drops / f"{i:04}.yaml").write_text("{}\n", encoding="utf-8")
    load_config(None, env=env)


def test_unreadable_file_is_config_invalid(
    cfg_dir: Path, write: Write, env: dict[str, str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = write(cfg_dir / "config.yaml", {}, mode=0o000)
    assert error_of(None, env).key == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0


def test_filename_with_odd_characters_is_a_plain_key(
    write: Write, tmp_path: Path, env: dict[str, str]
) -> None:
    path = write(tmp_path / "we\u202eird\n.yaml", {"timeout": 0})
    error = error_of(path, env)
    assert error.key == "timeout"


def test_returned_config_is_immutable(env: dict[str, str]) -> None:
    config = load_config(None, env=env).config
    with pytest.raises(ValidationError):
        config.timeout = "1s"  # type: ignore[misc]
    assert isinstance(config.mentions.allow, tuple)
    assert isinstance(config.hooks.pre_post, tuple)


def test_yaml_loader_is_safe_loader_only() -> None:
    assert issubclass(yamlstrict._StrictLoader, yaml.SafeLoader)  # pyright: ignore[reportPrivateUsage]
    assert not any("python" in tag for tag in yamlstrict._StrictLoader.yaml_constructors if tag)  # pyright: ignore[reportPrivateUsage]
