from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


@pytest.mark.parametrize("bad", ["not-a-date", "2025-13-45T99:99:99Z", "2025-01-01", ""])
def test_ts_format_date_time_is_enforced(bad: str) -> None:
    with pytest.raises(ValidationError):
        validate("pr-comment/defs.json#/$defs/ts", bad)


def test_ts_accepts_a_real_timestamp() -> None:
    validate("pr-comment/defs.json#/$defs/ts", "2025-01-02T03:04:05Z")


def test_refs_inside_const_enum_examples_default_are_data() -> None:
    doc = {
        "$id": "pr-comment/x.json",
        "const": {"$ref": "nope.json"},
        "enum": [{"$ref": "nope.json"}],
        "examples": [{"$ref": "nope.json"}],
        "default": {"$ref": "nope.json"},
    }
    assert list(iter_refs(doc)) == []


def test_property_named_ref_is_not_a_reference() -> None:
    doc = {
        "properties": {"$ref": {"type": "string"}},
        "$defs": {"$ref": {"type": "string"}},
        "patternProperties": {"^x": {"$ref": "defs.json#/$defs/pr"}},
    }
    assert list(iter_refs(doc)) == ["defs.json#/$defs/pr"]


def test_refs_are_found_in_subschema_positions() -> None:
    doc = {
        "allOf": [{"$ref": "a"}],
        "items": {"$ref": "b"},
        "properties": {"p": {"$ref": "c"}},
        "$defs": {"d": {"$ref": "d"}},
        "if": {"$ref": "e"},
        "not": {"$ref": "f"},
        "$dynamicRef": "#g",
    }
    assert sorted(iter_refs(doc)) == ["#g", "a", "b", "c", "d", "e", "f"]


def test_nested_id_changes_the_base_for_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    # `$defs/inner` re-bases to pr-comment/sub/inner.json, so `x.json` means sub/x.json, which
    # does not exist; resolved against the root id it would wrongly hit pr-comment/x.json.
    root = {
        "$id": "pr-comment/root.json",
        "$defs": {"inner": {"$id": "sub/inner.json", "$ref": "x.json"}},
    }
    other = {"$id": "pr-comment/x.json", "type": "string"}
    monkeypatch.setattr(
        "tests.schema_harness.load_schemas",
        lambda: {root["$id"]: root, other["$id"]: other},
    )
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    registry: Registry[Any] = Registry()
    for doc in (root, other):
        registry = registry.with_resource(
            doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)
        )
    monkeypatch.setattr("tests.schema_harness.build_registry", lambda: registry)
    failures = resolve_all_refs()
    assert len(failures) == 1
    assert "x.json" in failures[0]


def test_dynamic_ref_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = {"$id": "pr-comment/dyn.json", "$dynamicRef": "missing.json#thing"}
    monkeypatch.setattr("tests.schema_harness.load_schemas", lambda: {broken["$id"]: broken})
    monkeypatch.setattr("tests.schema_harness.build_registry", _registry_with(broken))
    assert len(resolve_all_refs()) == 1


@pytest.fixture
def schema_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr("tests.schema_harness.SCHEMAS_DIR", tmp_path)
    load_schemas.cache_clear()
    yield tmp_path
    load_schemas.cache_clear()


def test_invalid_metaschema_is_rejected(schema_dir: Path) -> None:
    (schema_dir / "bad.json").write_text('{"$id": "pr-comment/bad.json", "type": "strin"}')
    with pytest.raises(ValueError, match="bad.json"):
        load_schemas()


def test_invalid_json_names_the_file(schema_dir: Path) -> None:
    (schema_dir / "broken.json").write_text("{not json")
    with pytest.raises(ValueError, match="broken.json"):
        load_schemas()


def test_top_level_non_object_names_the_file(schema_dir: Path) -> None:
    (schema_dir / "list.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="list.json"):
        load_schemas()


def test_non_json_file_under_schemas_fails(schema_dir: Path) -> None:
    (schema_dir / "ok.json").write_text('{"$id": "pr-comment/ok.json"}')
    (schema_dir / "stray.yaml").write_text("a: 1")
    with pytest.raises(ValueError, match="stray.yaml"):
        load_schemas()
