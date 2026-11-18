"""Config: YAML and ``config.d`` merge, Pydantic models, ``PR_COMMENT_*`` overrides, per-repo merge.

Implements docs/interface.md "Config" and "Environment variables". The models mirror
``schemas/config.json`` (a test checks the two agree).

Layers, lowest to highest: built-in defaults, ``config.yaml`` and then ``config.d/*.yaml`` in
lexical order (maps merge, hook lists append, other lists replace), then the four scalar
environment variables. ``repos.<id>`` overrides are applied per repo by ``LoadedConfig.for_repo``,
and flags by the caller.

Every file is untrusted input (config can name commands that run): size-capped, opened without
blocking, regular files only, owned by the user or root and not world-writable, UTF-8 only, and
parsed by ``yamlstrict``. Any failure is ``ConfigError`` (exit 2 ``config_invalid``) naming the key
or the file; a message never repeats file content beyond a key.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import string
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
)

from . import grammars
from .yamlstrict import YamlRefusal, load_mapping

__all__ = [
    "Config",
    "ConfigError",
    "ConfigWarning",
    "HookEntry",
    "LoadedConfig",
    "RepoSettings",
    "config_hash",
    "load_config",
]

APP = "pr-comment"
ENV_PREFIX = "PR_COMMENT_"
MAX_FILE_BYTES = 256 * 1024
MAX_DROP_INS = 256
DEFAULT_HOOK_TIMEOUT = 30.0
FOOTER_MAX = 512
PLACEHOLDERS = frozenset({"user", "policy", "sha", "decision_id", "tool_version"})
DEFAULT_FOOTER_HUMAN = "Drafted with AI assistance · posted by {user} after review · {sha}"
DEFAULT_FOOTER_AUTO = "Posted automatically under policy {policy} · no human review · {sha}"


class ConfigError(Exception):
    """Invalid config (exit 2, code ``config_invalid``). ``key`` is the dotted key path, an
    environment variable name, or a file path; ``message`` holds no file content."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


def _invalid(key: str, message: str) -> ConfigError:
    return ConfigError(key, message)


# --- value grammars (the patterns of schemas/config.json, ASCII classes and fullmatch) ----------

_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}", re.ASCII)
_BIN_RE = re.compile(r"/.*|[^/]+", re.ASCII)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_MENTION_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", re.ASCII)
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_REPO_RE = re.compile(
    rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[0-9]{{1,5}})?(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+",
    re.ASCII,
)
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_FOOTER_FORBIDDEN = frozenset("<`[]\\  ")
_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([a-z_]+)\}|[{}]")


def _matches(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"not a valid {what}")
        return value

    return AfterValidator(check)


def _bin_name(value: str) -> str:
    """A bare name or an absolute path, not ``.`` or ``..``, no control character, no lone
    surrogate (an environment value can carry one from undecodable bytes)."""
    if not _BIN_RE.fullmatch(value) or value in (".", ".."):
        raise ValueError("must be a bare executable name or an absolute path")
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        raise ValueError("must not contain control characters or lone surrogates")
    return value


def _command(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    if "\x00" in value or _SURROGATE_RE.search(value):
        raise ValueError("must not contain NUL or a lone surrogate")
    return value


def _as_tuple(value: object) -> object:
    """A YAML list becomes a tuple, so the loaded config holds no mutable list."""
    if isinstance(value, list):
        return tuple(value)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, tuple):
        return value  # pyright: ignore[reportUnknownVariableType]
    raise ValueError("Input should be a valid list")


def _timeout_text(value: object) -> str:
    return grammars.check_timeout(value)


def _hook_seconds(value: object) -> float:
    return float(grammars.duration_seconds(value))


def _forgectl_config(value: object) -> str | None:
    return None if value is None else grammars.check_forgectl_config(value)


def _me_name(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _NAME_RE.fullmatch(value):
        raise ValueError("not a valid name: 1 to 64 of letters, digits and . _ @ -")
    return value


def check_footer_template(value: object, *, allow_false: bool) -> str | Literal[False]:
    """A footer template (interface.md, Footers and marker): one line of at most 512 characters,
    starting with an ASCII letter or ``{``, no Cc or Cf character, no forgectl separator, none of
    ``< ` [ ] \\``, only the closed placeholder set, braces written ``{{`` and ``}}``."""
    if value is False:
        if allow_false:
            return False
        raise ValueError("must be a non-empty string, never false")
    if type(value) is not str:
        raise ValueError("must be a string" + (" or false" if allow_false else ""))
    if not value or len(value) > FOOTER_MAX:
        raise ValueError(f"must be 1 to {FOOTER_MAX} characters")
    if not (value[0] in "{" or ("A" <= value[0] <= "Z") or ("a" <= value[0] <= "z")):
        raise ValueError("must start with an ASCII letter or {")
    for char in value:
        if char in _FOOTER_FORBIDDEN or unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            raise ValueError("contains a character a footer may not hold")
    for match in _PLACEHOLDER_RE.finditer(value):
        token = match[0]
        if token in ("{{", "}}"):
            continue
        if match[1] is None:
            raise ValueError("a lone { or } (write {{ and }} for literal braces)")
        if match[1] not in PLACEHOLDERS:
            raise ValueError(f"unknown placeholder {{{match[1]}}}")
    return value


def _footer_human(value: object) -> str | Literal[False]:
    return check_footer_template(value, allow_false=True)


def _footer_auto(value: object) -> str:
    result = check_footer_template(value, allow_false=False)
    assert result is not False
    return result


Duration = Annotated[str, BeforeValidator(_timeout_text)]
HookSeconds = Annotated[float, BeforeValidator(_hook_seconds)]
BinName = Annotated[str, AfterValidator(_bin_name)]
EnvName = Annotated[str, _matches(_ENV_NAME_RE, "variable name")]
MentionName = Annotated[str, _matches(_MENTION_RE, "name (a team as org/team)")]
Command = Annotated[str, AfterValidator(_command)]
FooterHuman = Annotated[Any, BeforeValidator(_footer_human)]
FooterAuto = Annotated[str, BeforeValidator(_footer_auto)]
MentionNames = Annotated[tuple[MentionName, ...], BeforeValidator(_as_tuple)]
EnvNames = Annotated[tuple[EnvName, ...], BeforeValidator(_as_tuple)]
PreHooks = Annotated[tuple["PreHook", ...], BeforeValidator(_as_tuple)]
PostHooks = Annotated[tuple["PostHook", ...], BeforeValidator(_as_tuple)]
ForgectlConfig = Annotated[str | None, BeforeValidator(_forgectl_config)]
MeName = Annotated[str | None, BeforeValidator(_me_name)]


def _read_only(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(value)


def _plain_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(value)


def _empty_repos() -> Mapping[str, Any]:
    return MappingProxyType({})


Policy = Literal["neutralize", "allow"]
ResolveBodyThread = Literal["non_blocking", "never", "always"]


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _fold(repo_id: str) -> str:
    """Lowercase ASCII letters only: ``str.lower`` would also match U+212A (Kelvin) to ``k``."""
    return repo_id.translate(_ASCII_LOWER)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=False)


class Me(_Model):
    name: MeName = None


class Footer(_Model):
    human: FooterHuman = DEFAULT_FOOTER_HUMAN
    auto: FooterAuto = DEFAULT_FOOTER_AUTO


class Mentions(_Model):
    policy: Policy = "neutralize"
    allow: MentionNames = ()


class Gitlab(_Model):
    resolve_body_thread: ResolveBodyThread = "non_blocking"


class PreHook(_Model):
    cmd: Command
    timeout: HookSeconds = DEFAULT_HOOK_TIMEOUT
    with_text: bool = False


class PostHook(_Model):
    cmd: Command
    timeout: HookSeconds = DEFAULT_HOOK_TIMEOUT
    async_: bool = Field(default=False, alias="async")
    with_text: bool = False


class Hooks(_Model):
    pre_post: PreHooks = Field(default=(), alias="pre-post")
    post_post: PostHooks = Field(default=(), alias="post-post")


# The override models use the global field types, so an explicit null is invalid; an unset key is
# told apart by ``model_fields_set`` (the defaults below are never read).
class FooterOverride(_Model):
    human: FooterHuman = None
    auto: FooterAuto = ""


class MentionsOverride(_Model):
    policy: Policy = "neutralize"
    allow: MentionNames = ()


class GitlabOverride(_Model):
    resolve_body_thread: ResolveBodyThread = "non_blocking"


class RepoOverride(_Model):
    footer: FooterOverride = Field(default_factory=FooterOverride)
    mentions: MentionsOverride = Field(default_factory=MentionsOverride)
    gitlab: GitlabOverride = Field(default_factory=GitlabOverride)
    allow_auto_approve: bool = False


RepoMap = Annotated[
    Mapping[str, RepoOverride], AfterValidator(_read_only), PlainSerializer(_plain_dict)
]


class Config(_Model):
    forgectl_bin: BinName = "forgectl"
    forgectl_config: ForgectlConfig = None
    timeout: Duration = "120s"
    me: Me = Field(default_factory=Me)
    footer: Footer = Field(default_factory=Footer)
    mentions: Mentions = Field(default_factory=Mentions)
    gitlab: Gitlab = Field(default_factory=Gitlab)
    allow_auto_approve: bool = False
    hook_env_deny: EnvNames = ()
    hooks: Hooks = Field(default_factory=Hooks)
    repos: RepoMap = Field(default_factory=_empty_repos)


# --- reading one file ---------------------------------------------------------------------------


def _reason(err: OSError) -> str:
    return f"cannot be read: {err.strerror or 'error'}"


def _entry_exists(path: Path) -> bool:
    """Whether a directory entry is there at all (a dangling symlink is one)."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as err:
        raise _invalid(str(path), _reason(err)) from None
    return True


def _check_mode(path: Path, info: os.stat_result, what: str) -> None:
    """The file or directory is ours (or root's) and nobody else can write it."""
    if info.st_uid not in (0, os.geteuid()):
        raise _invalid(str(path), f"{what} is owned by another user")
    if info.st_mode & stat.S_IWOTH:
        raise _invalid(str(path), f"{what} is writable by everyone")


def _read_bytes(path: Path, *, required: bool) -> bytes | None:
    """The file's bytes, or None when no entry exists at the path and it is not required.

    Opened non-blocking so a FIFO cannot hang the open, and never as a controlling terminal.
    Symbolic links are followed (dotfile managers use them) and the target is judged: a regular
    file, owned by the user or root, not world-writable, at most ``MAX_FILE_BYTES``. A dangling
    link is refused, never skipped.
    """
    if not required and not _entry_exists(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except FileNotFoundError:
        raise _invalid(str(path), "no such file, or a symbolic link to nothing") from None
    except OSError as err:
        raise _invalid(str(path), _reason(err)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _invalid(str(path), "not a regular file")
        _check_mode(path, info, "the file")
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as err:
        raise _invalid(str(path), _reason(err)) from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise _invalid(str(path), f"file too large (limit {MAX_FILE_BYTES // 1024} KiB)")
    return data


def _parse_layer(path: Path, *, required: bool) -> dict[str, Any] | None:
    data = _read_bytes(path, required=required)
    if data is None:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _invalid(str(path), "not valid UTF-8") from None
    try:
        return load_mapping(text)
    except YamlRefusal as err:
        at = f" at line {err.line}" if err.line is not None else ""
        if err.key is not None:
            raise _invalid(err.key, f"{err.phrase}{at}") from None
        raise _invalid(str(path), f"invalid YAML{at}: {err.phrase}") from None


def _drop_in_paths(directory: Path) -> list[Path]:
    """``*.yaml`` files of ``config.d`` in lexical (code point) order, hidden names skipped.

    No entry at the path means no drop-ins; an entry that is not a directory after following
    symlinks is refused, so drop-ins are never lost silently. The directory must be ours or
    root's and not world-writable: anyone who can add a drop-in can add a hook.
    """
    if not _entry_exists(directory):
        return []
    try:
        _check_mode(directory, os.stat(directory), "the directory")
        with os.scandir(directory) as entries:
            names = [e.name for e in entries if e.name.endswith(".yaml") and e.name[0] != "."]
    except (FileNotFoundError, NotADirectoryError):
        raise _invalid(str(directory), "not a directory") from None
    except OSError as err:
        raise _invalid(str(directory), _reason(err)) from None
    if len(names) > MAX_DROP_INS:
        raise _invalid(str(directory), f"too many drop-in files (limit {MAX_DROP_INS})")
    return [directory / name for name in sorted(names)]


# --- merging ------------------------------------------------------------------------------------


def _merge(base: Any, over: Any, path: tuple[str, ...] = ()) -> Any:
    """Maps merge; ``hooks.<name>`` lists append; every other value, lists included, replaces."""
    if isinstance(base, dict) and isinstance(over, dict):
        merged: dict[str, Any] = dict(base)  # pyright: ignore[reportUnknownArgumentType]
        for key, value in over.items():  # pyright: ignore[reportUnknownVariableType]
            merged[key] = (  # pyright: ignore[reportUnknownMemberType]
                _merge(base[key], value, (*path, key)) if key in base else value
            )
        return merged
    if isinstance(base, list) and isinstance(over, list) and len(path) == 2 and path[0] == "hooks":
        return [*base, *over]  # pyright: ignore[reportUnknownVariableType]
    return over


def _fold_repo_keys(layer: dict[str, Any]) -> None:
    """Lowercase (ASCII) the ``repos`` keys of one file, so spellings in different files merge.

    Two spellings in the same file are refused. A key that is not a valid repo id is left as
    written, so ``_validate`` names it as the user spelled it.
    """
    repos: Any = layer.get("repos")
    if not isinstance(repos, dict):
        return
    folded: dict[str, Any] = {}
    for key, value in repos.items():  # pyright: ignore[reportUnknownVariableType]
        name = str(key)  # pyright: ignore[reportUnknownArgumentType]
        new = _fold(name) if _REPO_RE.fullmatch(name) else name
        if new in folded:
            raise _invalid(f"repos.{name}", "differs from another repo key only by case")
        folded[new] = value
    layer["repos"] = folded


def _key_of(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "(config)"


def _first_error(err: ValidationError) -> ConfigError:
    first = err.errors(include_input=False, include_url=False)[0]
    message = first["msg"].removeprefix("Value error, ")
    return _invalid(_key_of(first["loc"]), message)


def _validate(raw: dict[str, Any]) -> Config:
    try:
        config = Config.model_validate(raw)
    except ValidationError as err:
        raise _first_error(err) from None
    except RecursionError:
        raise _invalid("(config)", "nested too deeply") from None
    for repo in config.repos:
        if not _REPO_RE.fullmatch(repo):
            raise _invalid(f"repos.{repo}", "not a valid repo id: host/path")
    return config


# --- the environment ----------------------------------------------------------------------------

_ENV_ADAPTERS: tuple[tuple[str, str, TypeAdapter[Any]], ...] = (
    ("PR_COMMENT_TIMEOUT", "timeout", TypeAdapter(Duration)),
    ("PR_COMMENT_FORGECTL_BIN", "forgectl_bin", TypeAdapter(BinName)),
    ("PR_COMMENT_FORGECTL_CONFIG", "forgectl_config", TypeAdapter(ForgectlConfig)),
    ("PR_COMMENT_ME__NAME", "me.name", TypeAdapter(MeName)),
)


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """The four ``PR_COMMENT_*`` scalar settings that are present, validated under the config's
    rules. Nothing else is read: lists, maps and ``allow_auto_approve`` cannot come from the
    environment, and the hook variables are not config keys. An empty value is refused rather
    than treated as unset."""
    found: dict[str, Any] = {}
    for name, field, adapter in _ENV_ADAPTERS:
        raw = env.get(name)
        if raw is None:
            continue
        try:
            found[field] = adapter.validate_python(raw)
        except ValidationError as err:
            first = err.errors(include_input=False, include_url=False)[0]
            raise _invalid(name, first["msg"].removeprefix("Value error, ")) from None
    return found


# --- locating -----------------------------------------------------------------------------------


def _config_home(env: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME`` when absolute (a relative or empty value is ignored, per the XDG
    spec), else ``~/.config``; the home directory is ``$HOME`` when absolute, else the account's."""
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg)
    home = env.get("HOME", "")
    if not (home and os.path.isabs(home)):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            raise _invalid("HOME", "cannot locate the home directory") from None
    return Path(home) / ".config"


# --- the result ---------------------------------------------------------------------------------


def _set_only(override: RepoOverride) -> dict[str, Any]:
    dumped = override.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return {k: v for k, v in dumped.items() if v != {}}


def config_hash(config: Config) -> str:
    """sha256 hex of the sorted-key, compact, ASCII-escaped JSON, in UTF-8, of the config after
    files, environment and defaults (no flags), with schema key spellings and durations in
    seconds, so ``60``, ``"60"``, ``"60s"`` and ``"1m"`` hash alike."""
    document = config.model_dump(mode="json", by_alias=True, exclude={"repos"})
    # A repo override hashes only what it sets: an unset key inherits the global value, which is
    # not the same as a key set to the default.
    document["repos"] = {repo: _set_only(override) for repo, override in config.repos.items()}
    document["timeout"] = float(grammars.duration_seconds(config.timeout))
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfigWarning:
    """A ``config_warning`` log line to write: ``file`` sets ``allow_auto_approve`` or a
    ``mentions.*`` key at ``keys`` (dotted paths, global or under ``repos.<id>``)."""

    file: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class HookEntry:
    """One hook to run: a configured entry, or a per-invocation ``--hook`` (the defaults)."""

    cmd: str
    timeout: float = DEFAULT_HOOK_TIMEOUT
    async_: bool = False
    with_text: bool = False


@dataclass(frozen=True)
class RepoSettings:
    """The per-repo-overridable settings in effect for one repo."""

    footer_human: str | Literal[False]
    footer_auto: str
    mentions_policy: Policy
    mentions_allow: tuple[str, ...]
    resolve_body_thread: ResolveBodyThread
    allow_auto_approve: bool


@dataclass(frozen=True)
class LoadedConfig:
    """The config in effect (files and environment), its hash, and the per-repo lookups."""

    config: Config
    config_hash: str
    config_path: str | None  # the main config file actually read; None when none exists
    warnings: tuple[ConfigWarning, ...] = ()

    @classmethod
    def defaults(cls) -> LoadedConfig:
        config = Config()
        return cls(config=config, config_hash=config_hash(config), config_path=None)

    def for_repo(self, repo_id: str) -> RepoSettings:
        """Global values with the ``repos.<id>`` override merged over them (key match ignores
        ASCII case; unset keys inherit; a list the override sets replaces the global one).

        TODO(#26): the forgectl reads call this with the canonical repo id from ``show``.
        TODO(#29): the review order takes the footer, mentions and approval settings from it.
        """
        cfg = self.config
        wanted = _fold(repo_id)
        over = next((r for key, r in cfg.repos.items() if _fold(key) == wanted), None)
        human: str | Literal[False] = cfg.footer.human
        auto = cfg.footer.auto
        policy = cfg.mentions.policy
        allow = cfg.mentions.allow
        resolve = cfg.gitlab.resolve_body_thread
        approve = cfg.allow_auto_approve
        if over is not None:
            footer, mentions, gitlab = over.footer, over.mentions, over.gitlab
            if "human" in footer.model_fields_set:
                human = footer.human
            if "auto" in footer.model_fields_set:
                auto = footer.auto
            if "policy" in mentions.model_fields_set:
                policy = mentions.policy
            if "allow" in mentions.model_fields_set:
                allow = mentions.allow
            if "resolve_body_thread" in gitlab.model_fields_set:
                resolve = gitlab.resolve_body_thread
            if "allow_auto_approve" in over.model_fields_set:
                approve = over.allow_auto_approve
        return RepoSettings(human, auto, policy, allow, resolve, approve)

    def hooks_for(self, name: str, cli_hooks: Sequence[str] = ()) -> tuple[HookEntry, ...]:
        """The hooks of one name: configured entries, then each ``--hook <name>=<cmd>`` (already
        checked by ``grammars.check_hook``) with the defaults."""
        if name == "pre-post":
            configured = tuple(
                HookEntry(h.cmd, h.timeout, False, h.with_text) for h in self.config.hooks.pre_post
            )
        elif name == "post-post":
            configured = tuple(
                HookEntry(h.cmd, h.timeout, h.async_, h.with_text)
                for h in self.config.hooks.post_post
            )
        else:
            raise ValueError(f"unknown hook {name!r}")
        extra = tuple(
            HookEntry(text.partition("=")[2])
            for text in cli_hooks
            if text.partition("=")[0] == name
        )
        return configured + extra


def _warning_keys(raw: dict[str, Any]) -> tuple[str, ...]:
    """The key paths of one file that interface.md says to warn about."""
    keys: list[str] = []

    def scan(prefix: str, section: Any) -> None:
        if not isinstance(section, dict):
            return
        if "allow_auto_approve" in section:
            keys.append(f"{prefix}allow_auto_approve")
        mentions: Any = section.get("mentions")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if isinstance(mentions, dict):
            keys.extend(f"{prefix}mentions.{name}" for name in mentions)  # pyright: ignore[reportUnknownVariableType]

    scan("", raw)
    repos: Any = raw.get("repos")
    if isinstance(repos, dict):
        for repo, section in repos.items():  # pyright: ignore[reportUnknownVariableType]
            scan(f"repos.{repo}.", section)
    return tuple(keys)


def load_config(config_path: str | None, *, env: Mapping[str, str]) -> LoadedConfig:
    """Load and validate the config; raise ``ConfigError`` on any problem.

    ``config_path`` is ``--config``: a file that must exist, whose directory's ``config.d`` holds
    the drop-ins. None reads ``<config home>/pr-comment/config.yaml`` (absent is fine) and its
    ``config.d``. ``env`` is the process environment (only ``HOME``, ``XDG_CONFIG_HOME`` and the
    four ``PR_COMMENT_*`` settings are read).
    """
    if config_path is None:
        main = _config_home(env) / APP / "config.yaml"
        required = False
    else:
        try:
            main = Path(os.path.abspath(config_path))
        except OSError as err:  # the working directory was removed and the path is relative
            raise _invalid(config_path, _reason(err)) from None
        required = True
    raw: dict[str, Any] = {}
    warnings: list[ConfigWarning] = []
    main_read = False
    sources = [(main, required)]
    sources += [(p, True) for p in _drop_in_paths(main.parent / "config.d")]
    for path, must_exist in sources:
        layer = _parse_layer(path, required=must_exist)
        if layer is None:
            continue
        main_read = main_read or path == main
        keys = _warning_keys(layer)
        _fold_repo_keys(layer)
        if keys:
            warnings.append(ConfigWarning(str(path), keys))
        raw = _merge(raw, layer)
    config = _validate(raw)
    overrides = _env_overrides(env)
    if overrides:
        update = dict(overrides)
        if "me.name" in update:
            update["me"] = config.me.model_copy(update={"name": update.pop("me.name")})
        config = config.model_copy(update=update)
    return LoadedConfig(
        config=config,
        config_hash=config_hash(config),
        config_path=str(main) if main_read else None,
        warnings=tuple(warnings),
    )
