import hashlib
import json
from pathlib import Path

import pytest

from as_engine.build import compile_product
from as_engine.enrichment import entry_cases, validate_overlay

FIXTURES = Path(__file__).parent / "fixtures"


def _action(**extra):
    action = {
        "target": "$.paths['/ping'].get",
        "update": {"x-added": True},
        "description": "Document the action.",
        "x-as-reason": "An explicit rationale.",
        "x-as-origin": "test fixture",
        "x-as-test": "ping-action",
        "x-as-evidence": {"url": "https://example.test/evidence", "date": "2026-09-06"},
    }
    action.update(extra)
    return action


def _spec_dir(tmp_path, overlays):
    base = (FIXTURES / "enrichment-base.json").read_bytes()
    (tmp_path / "api.json").write_bytes(base)
    for name, overlay in overlays.items():
        (tmp_path / name).write_text(json.dumps(overlay), encoding="utf-8")
    manifest = {
        "format_version": 1,
        "documents": [
            {
                "id": "api",
                "file": "api.json",
                "declared_version": "1.0.0",
                "sha256": hashlib.sha256(base).hexdigest(),
                "overlays": list(overlays),
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "field", ["description", "x-as-reason", "x-as-origin", "x-as-test", "x-as-evidence"]
)
def test_compile_product_rejects_missing_provenance_before_output(tmp_path, field):
    spec_dir = _spec_dir(tmp_path, {"one.overlay.json": {"actions": [_action()]}})
    overlay_path = spec_dir / "one.overlay.json"
    missing = _action()
    del missing[field]
    overlay_path.write_text(json.dumps({"actions": [missing]}), encoding="utf-8")
    with pytest.raises(
        ValueError, match=rf"one.overlay.json: target .+: missing required field {field}"
    ):
        compile_product(spec_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ({"url": "ftp://example.test", "date": "2026-09-06"}, "x-as-evidence.url"),
        ({"url": "https://example.test/a b", "date": "2026-09-06"}, "x-as-evidence.url"),
        ({"url": "https://example.test", "date": "2026-9-6"}, "x-as-evidence.date"),
    ],
)
def test_validate_overlay_rejects_malformed_evidence(evidence, message):
    with pytest.raises(ValueError, match=message):
        validate_overlay({"actions": [_action(**{"x-as-evidence": evidence})]}, source="bad.json")

    with pytest.raises(ValueError, match="x-as-evidence.url"):
        validate_overlay(
            {"actions": [_action(**{"x-as-evidence": {"url": "https://[", "date": "2026-09-06"}})]},
            source="bad.json",
        )


def test_duplicate_test_ids_are_rejected_product_wide_across_documents(tmp_path):
    spec_dir = _spec_dir(
        tmp_path,
        {
            "one.overlay.json": {"actions": [_action()]},
            "two.overlay.json": {"actions": []},
        },
    )
    base = (spec_dir / "api.json").read_bytes()
    (spec_dir / "api-two.json").write_bytes(base)
    manifest = json.loads((spec_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["documents"][0]["overlays"] = ["one.overlay.json"]
    manifest["documents"].append(
        {
            "id": "api-two",
            "file": "api-two.json",
            "declared_version": "1.0.0",
            "sha256": hashlib.sha256(base).hexdigest(),
            "overlays": ["two.overlay.json"],
        }
    )
    (spec_dir / "two.overlay.json").write_text(
        json.dumps({"actions": [_action(**{"x-as-origin": "second"})]}), encoding="utf-8"
    )
    (spec_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate x-as-test"):
        compile_product(spec_dir, tmp_path / "out")
    with pytest.raises(ValueError, match="duplicate x-as-test"):
        list(entry_cases(spec_dir))


def test_entry_cases_check_ordered_merge_remove_and_examples(tmp_path):
    base = json.loads((FIXTURES / "enrichment-base.json").read_text(encoding="utf-8"))
    overlay = json.loads((FIXTURES / "enrichment-overlay.json").read_text(encoding="utf-8"))
    spec_dir = _spec_dir(tmp_path, {"api.overlay.json": overlay})
    calls = []

    def validate_body(instance, schema, document):
        calls.append((instance, schema, document))
        assert instance == {"name": "Ada"}
        assert schema == base["components"]["schemas"]["Thing"]

    cases = list(entry_cases(spec_dir))
    assert [case.id for case in cases] == [
        "first-merge",
        "second-merge",
        "append-list",
        "replace-scalar",
        "remove-item",
    ]
    assert cases[1].before_document["paths"]["/ping"]["get"]["x-first"]["untouched"] == ["once"]
    assert cases[1].check(validate_body) == (
        ["confluence-as", "api", "call", "ping", "--body", "{}"],
        {"name": "Ada"},
    )
    assert calls and cases[0].check() == () and cases[2].check() == ()
    assert cases[3].check() == () and cases[4].check() == ()


def test_entry_case_rejects_bad_base_target_and_malformed_examples(tmp_path):
    bad_target = _action(target="$.paths['/absent']")
    spec_dir = _spec_dir(tmp_path, {"bad.overlay.json": {"actions": [bad_target]}})
    with pytest.raises(ValueError, match="does not exist"):
        next(entry_cases(spec_dir)).check()
    malformed = _action(**{"x-as-examples": [{"kind": "invocation", "value": "unterminated '"}]})
    spec_dir = _spec_dir(tmp_path, {"bad.overlay.json": {"actions": [malformed]}})
    with pytest.raises(ValueError, match="invalid shell syntax"):
        next(entry_cases(spec_dir)).check()
    generic = _action(
        **{"x-as-examples": [{"kind": "invocation", "value": "jira-as issue get JAS-35"}]}
    )
    spec_dir = _spec_dir(tmp_path, {"generic.overlay.json": {"actions": [generic]}})
    assert next(entry_cases(spec_dir)).check() == (["jira-as", "issue", "get", "JAS-35"],)


def test_entry_case_json_schema_is_local_resolvable_and_not_silently_skipped(tmp_path):
    action = _action(
        **{
            "x-as-examples": [
                {"kind": "json", "value": "{}", "schema": "https://example.test/schema"}
            ]
        }
    )
    spec_dir = _spec_dir(tmp_path, {"bad.overlay.json": {"actions": [action]}})
    with pytest.raises(ValueError, match="local reference"):
        next(entry_cases(spec_dir)).check(lambda *_: None)

    action = _action(
        **{
            "x-as-examples": [
                {"kind": "json", "value": "{}", "schema": "#/components/schemas/Thing"}
            ]
        }
    )
    spec_dir = _spec_dir(tmp_path, {"callback.overlay.json": {"actions": [action]}})
    with pytest.raises(ValueError, match="requires validate_body callback"):
        next(entry_cases(spec_dir)).check()


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", " "),
        ("x-as-reason", 2),
        ("x-as-origin", None),
        ("x-as-test", []),
        ("x-as-evidence", []),
        ("x-as-evidence", {"url": "https://example.test", "date": "2026-02-30"}),
    ],
)
def test_invalid_provenance_names_field(field, value):
    with pytest.raises(ValueError, match=field):
        validate_overlay({"actions": [_action(**{field: value})]}, source="invalid.json")


@pytest.mark.parametrize(
    "example",
    [
        {"kind": "json", "value": "{"},
        {"kind": "json", "value": "{}", "schema": None},
        {"kind": "json", "value": "{}", "schema": "#/components/schemas/Missing"},
        {"kind": "invocation", "value": "   "},
        {"kind": "invocation", "value": "command", "schema": "#/unused"},
        {"kind": "unknown", "value": "anything"},
    ],
)
def test_bad_examples_fail_entry_check(tmp_path, example):
    spec_dir = _spec_dir(
        tmp_path, {"one.json": {"actions": [_action(**{"x-as-examples": [example]})]}}
    )
    with pytest.raises(ValueError):
        next(entry_cases(spec_dir)).check(lambda *_: None)


def test_schema_example_sees_enriched_context(tmp_path):
    action = _action(
        target="$.components.schemas",
        update={"New": {"type": "string"}},
        **{
            "x-as-examples": [
                {"kind": "json", "value": '"valid"', "schema": "#/components/schemas/New"}
            ]
        },
    )
    spec_dir = _spec_dir(tmp_path, {"one.json": {"actions": [action]}})
    seen = []
    next(entry_cases(spec_dir)).check(lambda body, schema, doc: seen.append((body, schema)))
    assert seen == [("valid", {"type": "string"})]


def test_entry_cases_refuses_source_escape(tmp_path):
    spec_dir = _spec_dir(tmp_path, {})
    manifest = json.loads((spec_dir / "manifest.json").read_text())
    manifest["documents"][0]["file"] = "../outside.json"
    (spec_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="escapes spec directory"):
        list(entry_cases(spec_dir))


def test_compile_product_does_not_import_jsonschema(tmp_path):
    import subprocess
    import sys

    spec_dir = _spec_dir(tmp_path, {"one.json": {"actions": [_action()]}})
    code = (
        "import sys; from as_engine.build import compile_product; "
        "compile_product(sys.argv[1],sys.argv[2]); "
        "assert 'jsonschema' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(spec_dir), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
