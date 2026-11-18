from __future__ import annotations

import json

import pytest
from jsonschema import ValidationError

from tests.schema_harness import (
    SCHEMAS_DIR,
    build_registry,
    iter_refs,
    load_schemas,
    resolve_all_refs,
    schema_files,
    validate,
)


def test_schemas_directory_has_files() -> None:
    names = {p.relative_to(SCHEMAS_DIR).as_posix() for p in schema_files()}
    assert {"defs.json", "config.json", "cli/error.json"} <= names


def test_every_schema_file_loads_into_registry_by_id() -> None:
    registry = build_registry()
    loaded = load_schemas()
    assert len(loaded) == len(schema_files())
    for schema_id in loaded:
        assert registry.get(schema_id) is not None, schema_id


def test_schema_ids_follow_the_path() -> None:
    for path in schema_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["$id"] == f"pr-comment/{path.relative_to(SCHEMAS_DIR).as_posix()}"


def test_every_ref_resolves() -> None:
    assert resolve_all_refs() == []


def test_ref_walk_finds_refs() -> None:
    assert sum(1 for doc in load_schemas().values() for _ in iter_refs(doc)) > 20


def test_unresolvable_ref_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = {
        "$id": "pr-comment/broken.json",
        "$ref": "defs.json#/$defs/does_not_exist",
    }
    monkeypatch.setattr(
        "tests.schema_harness.load_schemas",
        lambda: {**load_schemas(), broken["$id"]: broken},
    )
    monkeypatch.setattr("tests.schema_harness.build_registry", _registry_with(broken))
    failures = resolve_all_refs()
    assert len(failures) == 1
    assert failures[0].startswith("pr-comment/broken.json: defs.json#/$defs/does_not_exist")


def _registry_with(extra: dict[str, str]):  # noqa: ANN202
    from referencing import Resource
    from referencing.jsonschema import DRAFT202012

    registry = build_registry().with_resource(
        extra["$id"], Resource.from_contents(extra, default_specification=DRAFT202012)
    )
    return lambda: registry


def test_validate_accepts_a_valid_fragment() -> None:
    validate("pr-comment/defs.json#/$defs/pr", "github.com/o/r#12")


@pytest.mark.parametrize("bad", ["", "github.com/o/r#0", "github.com/o/r", 5, None, "x" * 5000])
def test_validate_rejects_an_invalid_fragment(bad: object) -> None:
    with pytest.raises(ValidationError):
        validate("pr-comment/defs.json#/$defs/pr", bad)


def test_validate_follows_cross_file_refs() -> None:
    # cli/unresolve.json refs ../defs.json for `pr`; a bad pr must fail through the ref.
    with pytest.raises(ValidationError):
        validate("pr-comment/cli/unresolve.json", {"pr": "not a pr", "action": "unresolve"})


def test_validate_unknown_schema_raises_key_error() -> None:
    with pytest.raises(KeyError):
        validate("pr-comment/nope.json", {})
