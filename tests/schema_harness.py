"""Schema test harness: a ``referencing`` registry over ``schemas/`` keyed by ``$id``.

Test-only. Runtime code never imports ``jsonschema`` or ``referencing``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def schema_files() -> list[Path]:
    """Every schema file under ``schemas/``, sorted."""
    return sorted(SCHEMAS_DIR.rglob("*.json"))


@cache
def load_schemas() -> dict[str, Any]:
    """Map each schema's ``$id`` to its parsed document; an id must be present and unique."""
    by_id: dict[str, Any] = {}
    for path in schema_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
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


def iter_refs(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` string in a schema document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_refs(item)


def resolve_all_refs() -> list[str]:
    """Resolve every ``$ref`` of every schema; return the refs that fail, as ``id: ref``."""
    registry = build_registry()
    failures: list[str] = []
    for schema_id, doc in load_schemas().items():
        resolver = registry.resolver(base_uri=schema_id)
        for ref in iter_refs(doc):
            try:
                resolver.lookup(ref)
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
