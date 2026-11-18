"""Schema test harness: a ``referencing`` registry over ``schemas/`` keyed by ``$id``.

Test-only. Runtime code never imports ``jsonschema`` or ``referencing``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def schema_files() -> list[Path]:
    """Every schema file under ``schemas/``, sorted."""
    return sorted(SCHEMAS_DIR.rglob("*.json"))


@cache
def load_schemas() -> dict[str, Any]:
    """Map each schema's ``$id`` to its parsed document.

    Every file under ``schemas/`` must be a ``.json`` object that is valid against the 2020-12
    metaschema, with a unique ``$id``. Errors name the offending file.
    """
    stray = sorted(p for p in SCHEMAS_DIR.rglob("*") if p.is_file() and p.suffix != ".json")
    if stray:
        raise ValueError(f"{stray[0]}: not a .json file; schemas/ holds only .json schemas")
    by_id: dict[str, Any] = {}
    for path in schema_files():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{path}: invalid JSON ({exc})") from exc
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: top level must be a JSON object")
        try:
            Draft202012Validator.check_schema(doc)
        except SchemaError as exc:
            raise ValueError(f"{path}: not a valid 2020-12 schema ({exc.message})") from exc
        schema_id = doc.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise ValueError(f"{path}: schema has no $id")
        if schema_id in by_id:
            raise ValueError(f"{path}: duplicate $id {schema_id}")
        by_id[schema_id] = doc
    return by_id


@cache
def build_registry() -> Registry[Any]:
    """A registry holding every schema under ``schemas/``, keyed by ``$id``."""
    registry: Registry[Any] = Registry()
    for schema_id, doc in load_schemas().items():
        registry = registry.with_resource(
            schema_id, Resource.from_contents(doc, default_specification=DRAFT202012)
        )
    return registry


# Keywords whose value is one subschema, a list of subschemas, or a map of name -> subschema.
# Everything else (const, enum, examples, default, ...) is data and is never walked.
_SCHEMA_KEYWORDS = (
    "additionalProperties",
    "items",
    "contains",
    "if",
    "then",
    "else",
    "not",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAP_KEYWORDS = (
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
)


def _children(schema: dict[str, Any]) -> Iterator[Any]:
    for key in _SCHEMA_KEYWORDS:
        if key in schema:
            yield schema[key]
    for key in _SCHEMA_LIST_KEYWORDS:
        if isinstance(schema.get(key), list):
            yield from schema[key]
    for key in _SCHEMA_MAP_KEYWORDS:
        if isinstance(schema.get(key), dict):
            yield from schema[key].values()


def iter_ref_sites(node: Any, base: str) -> Iterator[tuple[str, str]]:
    """Yield ``(base_uri, ref)`` for every ``$ref`` and ``$dynamicRef`` in subschema positions.

    The base URI follows nested ``$id`` values. A property or definition named ``$ref`` is a
    name, not a reference, and ``const``/``enum``/``examples``/``default`` are never walked.
    """
    if not isinstance(node, dict):
        return
    nested_id = node.get("$id")
    if isinstance(nested_id, str):
        base = urljoin(base, nested_id)
    for key in ("$ref", "$dynamicRef"):
        if isinstance(node.get(key), str):
            yield base, node[key]
    for child in _children(node):
        yield from iter_ref_sites(child, base)


def iter_refs(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` and ``$dynamicRef`` string in a schema document."""
    for _base, ref in iter_ref_sites(node, ""):
        yield ref


def resolve_all_refs() -> list[str]:
    """Resolve every ``$ref`` of every schema; return the refs that fail, as ``id: ref``."""
    registry = build_registry()
    failures: list[str] = []
    for schema_id, doc in load_schemas().items():
        for base, ref in iter_ref_sites(doc, ""):
            try:
                registry.resolver(base_uri=base).lookup(ref)
            except Exception as exc:  # referencing raises several unresolvable types
                failures.append(f"{schema_id}: {ref} ({type(exc).__name__})")
    return failures


def validate(name: str, document: Any) -> None:
    """Validate ``document`` against a named schema.

    ``name`` is a schema ``$id`` (``pr-comment/log.json``), optionally with a JSON-pointer
    fragment (``pr-comment/defs.json#/$defs/pr``). Raises ``jsonschema.ValidationError`` on
    the first violation and ``KeyError`` for an unknown schema.
    """
    base = name.split("#", 1)[0]
    if base not in load_schemas():
        raise KeyError(f"unknown schema {base!r}")
    Draft202012Validator(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": name},
        registry=build_registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(document)
