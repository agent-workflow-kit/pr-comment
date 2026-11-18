"""A strict YAML reader for config files, which are untrusted input.

It resolves scalars under the YAML 1.2 core schema (PyYAML implements 1.1, whose implicit rules
read ``yes`` as true, ``010`` as 8 and ``1:30`` as 90; here those are strings), caps the node
count and the nesting depth, and refuses anchors, aliases (so no alias bombs), merge keys,
duplicate and non-string keys, NUL, lone surrogates, and every tag outside YAML's core types.
Only ``yaml.SafeLoader`` is ever subclassed. A refusal carries a fixed phrase and a line, never
file content: PyYAML's own messages can quote tags and handles from the file.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.parser import ParserError
from yaml.scanner import ScannerError

MAX_NODES = 10_000  # per file: bounds parse time, far above any real config
MAX_DEPTH = 32

_CORE = "tag:yaml.org,2002:"
_ALLOWED_TAGS = frozenset(
    f"{_CORE}{name}" for name in ("str", "int", "float", "bool", "null", "seq", "map")
)

# The core schema's forms, numbers decimal only and without leading zeros.
_CORE_INT = r"[-+]?(?:0|[1-9][0-9]*)"
_CORE_FLOAT = r"[-+]?(?:\.[0-9]+|(?:0|[1-9][0-9]*)(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
_CORE_INF = r"[-+]?\.(?:inf|Inf|INF)"
_CORE_NAN = r"\.(?:nan|NaN|NAN)"
_CORE_TRUE = ("true", "True", "TRUE")
_CORE_FALSE = ("false", "False", "FALSE")
_INT_RE = re.compile(_CORE_INT, re.ASCII)
_FLOAT_RE = re.compile(_CORE_FLOAT, re.ASCII)
_INF_RE = re.compile(_CORE_INF, re.ASCII)
_NAN_RE = re.compile(_CORE_NAN, re.ASCII)

_MISTYPED = "a value does not match its type"


class YamlRefusal(Exception):
    """The file is refused. ``phrase`` is fixed text, ``line`` 1-based or None, ``key`` the dotted
    path of a key problem, or None when the problem concerns the file."""

    def __init__(self, phrase: str, line: int | None = None, key: str | None = None) -> None:
        super().__init__(phrase)
        self.phrase = phrase
        self.line = line
        self.key = key


class _Refusal(Exception):
    def __init__(self, phrase: str, mark: Any, key: str | None = None) -> None:
        super().__init__(phrase)
        self.phrase = phrase
        self.line: int | None = mark.line + 1 if mark is not None else None
        self.key = key


def _check_text(text: str, path: tuple[str, ...], mark: Any) -> None:
    key = ".".join(path) or None
    if "\x00" in text:
        raise _Refusal("contains a NUL character", mark, key)
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        raise _Refusal("contains a lone surrogate", mark, key)


def _check_tree(node: yaml.Node, path: tuple[str, ...]) -> None:
    """Keys are plain strings without duplicates or merge keys; no string holds NUL or a lone
    surrogate. A problem is named by its key path, or by the file at the top level."""
    if isinstance(node, yaml.ScalarNode):
        _check_text(node.value, path, node.start_mark)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _check_tree(item, (*path, str(index)))
    elif isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if key_node.tag == f"{_CORE}merge":
                raise _Refusal("merge keys are not allowed", key_node.start_mark)
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != f"{_CORE}str":
                raise _Refusal("keys must be strings", key_node.start_mark)
            name: str = key_node.value
            _check_text(name, path, key_node.start_mark)
            if name in seen:
                raise _Refusal("duplicate key", key_node.start_mark, ".".join((*path, name)))
            seen.add(name)
            _check_tree(value_node, (*path, name))


def _scalar(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise _Refusal(_MISTYPED, node.start_mark)
    return loader.construct_scalar(node)


def _construct_int(loader: yaml.SafeLoader, node: yaml.Node) -> int:
    text = _scalar(loader, node)
    if not _INT_RE.fullmatch(text):
        raise _Refusal(_MISTYPED, node.start_mark)
    try:
        return int(text)
    except ValueError:  # beyond Python's limit on the digits of an int
        raise _Refusal(_MISTYPED, node.start_mark) from None


def _construct_float(loader: yaml.SafeLoader, node: yaml.Node) -> float:
    text = _scalar(loader, node)
    if _INF_RE.fullmatch(text):
        return float("-inf") if text[0] == "-" else float("inf")
    if _NAN_RE.fullmatch(text):
        return float("nan")
    if not _FLOAT_RE.fullmatch(text):
        raise _Refusal(_MISTYPED, node.start_mark)
    return float(text)


def _construct_bool(loader: yaml.SafeLoader, node: yaml.Node) -> bool:
    text = _scalar(loader, node)
    if text in _CORE_TRUE:
        return True
    if text in _CORE_FALSE:
        return False
    raise _Refusal(_MISTYPED, node.start_mark)


class _StrictLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: dict[Any, Any] = {}  # this class's own; SafeLoader's are untouched
    yaml_constructors = {
        tag: constructor
        for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
        if tag is None or tag in _ALLOWED_TAGS  # None: the constructor that refuses unknown tags
    }
    _nodes = 0
    _depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        event: Any = self.peek_event()
        mark: Any = event.start_mark
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _Refusal("anchors and aliases are not allowed", mark)
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise _Refusal(f"too many nodes (limit {MAX_NODES})", mark)
        self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise _Refusal(f"nested too deeply (limit {MAX_DEPTH})", mark)
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def get_single_node(self) -> Any:
        node = super().get_single_node()
        if node is not None:
            _check_tree(node, ())
        return node

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise _Refusal(_MISTYPED, node.start_mark)
        return yaml.constructor.BaseConstructor.construct_mapping(self, node, deep=True)


for _tag, _pattern, _first in (
    ("bool", "|".join(_CORE_TRUE + _CORE_FALSE), "tTfF"),
    ("int", _CORE_INT, "-+0123456789"),
    ("float", f"{_CORE_FLOAT}|{_CORE_INF}|{_CORE_NAN}", "-+.0123456789"),
    ("null", "~|null|Null|NULL|", ["~", "n", "N", ""]),
    ("merge", "<<", "<"),  # resolved only to be refused
):
    _StrictLoader.add_implicit_resolver(
        f"{_CORE}{_tag}", re.compile(rf"(?:{_pattern})\Z", re.ASCII), list(_first)
    )
_StrictLoader.add_constructor(f"{_CORE}int", _construct_int)
_StrictLoader.add_constructor(f"{_CORE}float", _construct_float)
_StrictLoader.add_constructor(f"{_CORE}bool", _construct_bool)


def load_mapping(text: str) -> dict[str, Any]:
    """The document's top-level mapping (an empty document is ``{}``).

    Raises ``YamlRefusal`` for anything refused or malformed.
    """
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - the strict loader above
    except _Refusal as err:
        raise YamlRefusal(err.phrase, err.line, err.key) from None
    except yaml.reader.ReaderError as err:
        line = text.count("\n", 0, err.position) + 1
        raise YamlRefusal("a character YAML does not allow", line) from None
    except yaml.MarkedYAMLError as err:
        if isinstance(err, ScannerError | ParserError):
            phrase = "syntax error"
        elif isinstance(err, ComposerError):
            phrase = "more than one document"
        elif isinstance(err, ConstructorError):
            phrase = "unsupported tag, or a value that does not match its tag"
        else:
            phrase = "invalid YAML"
        mark: Any = err.problem_mark or err.context_mark
        raise YamlRefusal(phrase, mark.line + 1 if mark is not None else None) from None
    except yaml.YAMLError:
        raise YamlRefusal("invalid YAML") from None
    except RecursionError:
        raise YamlRefusal("nested too deeply") from None
    except (ValueError, IndexError, KeyError, OverflowError, TypeError):  # a backstop
        raise YamlRefusal(_MISTYPED) from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise YamlRefusal("the top level must be a mapping")
    return document  # pyright: ignore[reportUnknownVariableType]
