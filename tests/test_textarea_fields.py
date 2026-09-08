"""Explicit and cached textarea IDs use the public body and transport seams."""

from copy import deepcopy

import pytest

from as_engine.converters import convert, validate_adf
from as_engine.errors import SurfaceError
from as_engine.params import build_body
from as_engine.surface import parse_call_flags
from tests.test_transforms_richtext import surface_for


def product(tmp_path, *, bulk=False):
    request = {"path": "/fields/description", "shape": "value", "nullable": True}
    if bulk:
        request["itemsPath"] = "/issueUpdates"
    tag = {
        "customFields": "textarea",
        "request": request,
        "representations": {"adf": {"converter": "adf", "encoding": "object"}},
    }
    return surface_for(
        tmp_path,
        {"write": ("post", {"x-as-richtext": [tag], "x-as-representation": {"default": "adf"}})},
    )


@pytest.mark.parametrize("source", ["adf_fields", "textarea_fields"])
def test_selected_textarea_on_wire_is_per_call_and_does_not_mutate_input(tmp_path, source):
    surface, wire = product(tmp_path)
    body = {"fields": {"customfield_10010": "**Ready**", "customfield_10011": "literal"}}
    before = deepcopy(body)
    surface.call("write", {}, body, **{source: ["customfield_10010"]})
    sent = wire.requests[-1][2]
    assert validate_adf(sent["fields"]["customfield_10010"])
    assert sent["fields"]["customfield_10011"] == "literal"
    assert body == before
    surface.call("write", {}, body)
    assert wire.requests[-1][2] == before


def test_cached_and_explicit_ids_share_file_input_and_shorthand(tmp_path):
    surface, wire = product(tmp_path)
    _, _, operation = surface.resolve("write")
    note = tmp_path / "note.md"
    note.write_text("**Ready**", encoding="utf-8")
    _, options = parse_call_flags(operation, ["--adf-field", "customfield_10010"])
    body = build_body(
        None, [f"customfield_10010=@{note}", "customfield_10011=123"],
        operation=operation, adf_fields=options["adf_fields"],
    )
    assert body == {"fields": {"customfield_10010": "**Ready**", "customfield_10011": 123}}
    surface.call("write", {}, body, adf_fields=options["adf_fields"])
    assert validate_adf(wire.requests[-1][2]["fields"]["customfield_10010"])
    cached = build_body(None, [f"fields.customfield_10010=@{note}"], operation=operation,
                        textarea_fields=["customfield_10010"])
    assert cached["fields"]["customfield_10010"] == "**Ready**"


def test_preencoded_and_null_custom_values_are_preserved_in_bulk(tmp_path):
    surface, wire = product(tmp_path, bulk=True)
    doc = convert("**Ready**", target="adf")
    body = {"issueUpdates": [
        {"fields": {"customfield_10010": doc}},
        {"fields": {"customfield_10010": None}},
        {"fields": {"customfield_10010": "**Next**"}},
    ]}
    surface.call("write", {}, body, textarea_fields=["customfield_10010"],
                 adf_fields=["customfield_10010"])
    sent = wire.requests[-1][2]["issueUpdates"]
    assert sent[0]["fields"]["customfield_10010"] == doc
    assert sent[1]["fields"]["customfield_10010"] is None
    assert validate_adf(sent[2]["fields"]["customfield_10010"])


@pytest.mark.parametrize("value", ["description", "customfield_", "customfield_1/escape"])
def test_invalid_override_refuses_before_transport(tmp_path, value):
    surface, wire = product(tmp_path)
    _, _, operation = surface.resolve("write")
    with pytest.raises(ValueError, match="customfield"):
        parse_call_flags(operation, ["--adf-field", value])
    with pytest.raises(SurfaceError, match="customfield"):
        surface.call("write", {}, {}, adf_fields=[value])
    assert wire.requests == []


def test_shorthand_collision_refuses(tmp_path):
    surface, _ = product(tmp_path)
    _, _, operation = surface.resolve("write")
    with pytest.raises(ValueError, match="collides"):
        build_body(None, ["customfield_10010=x", "fields.customfield_10010=y"], operation=operation)
