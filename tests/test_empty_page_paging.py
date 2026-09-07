"""Explicit empty-page/requested-size contracts for bare-array endpoints."""

import json

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.index import ProductIndexes
from as_engine.responder import Responder
from as_engine.surface import Surface


def fixture(tmp_path, *, change=None, default=2):
    tag = {
        "style": "offset/limit",
        "request": {
            "offset": {"in": "query", "name": "startAt"},
            "limit": {"in": "query", "name": "maxResults"},
        },
        "itemsPath": "",
        "termination": "emptyPage",
        "advance": "requested",
    }
    tag.update(change or {})
    schema = {
        "type": "integer",
        "format": "int32",
        **({"default": default} if default is not None else {}),
    }
    operation = {
        "operationId": "getUsers",
        "parameters": [
            {"in": "query", "name": "startAt", "schema": {"type": "integer"}},
            {"in": "query", "name": "maxResults", "schema": schema},
        ],
        "responses": {"200": {"description": "ok"}},
        "x-as-paging": tag,
    }
    (tmp_path / "index.json").write_text(
        json.dumps(compile_document({"openapi": "3.0.1", "paths": {"/users": {"get": operation}}}))
    )
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "test", "tier": "primary", "file": "index.json"}],
            }
        )
    )
    indexes = ProductIndexes(tmp_path)
    wire = Responder(indexes.get("test"))
    return Surface(indexes, lambda *_: wire), wire


@pytest.mark.parametrize("parameters", [{}, {"maxResults": 2}])
def test_nonempty_short_page_continues_at_requested_offset(tmp_path, parameters):
    surface, wire = fixture(tmp_path)
    wire.seed("getUsers", [[1, 2], [3], []])
    assert surface.call("getUsers", parameters, all_pages=True).body == [1, 2, 3]
    assert [call[1] for call in wire.requests] == [
        {"maxResults": 2, "startAt": n} for n in (0, 2, 4)
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"advance": None},
        {"termination": None},
        {"termination": "shortPage"},
        {"advance": "count"},
        {"style": "start/limit"},
    ],
)
def test_only_explicit_paired_contract_is_accepted(tmp_path, change):
    surface, wire = fixture(tmp_path, change=change)
    with pytest.raises(SurfaceError, match="emptyPage termination requires"):
        surface.call("getUsers", {}, all_pages=True)
    assert not wire.requests


@pytest.mark.parametrize("parameters", [{}, {"maxResults": 0}, {"maxResults": -1}])
def test_request_step_must_be_known_and_positive(tmp_path, parameters):
    surface, wire = fixture(tmp_path, default=None)
    with pytest.raises(SurfaceError):
        surface.call("getUsers", parameters, all_pages=True)
    assert not wire.requests


def test_one_page_and_aggregate_cap_do_not_send_exhaustion_probe(tmp_path):
    surface, wire = fixture(tmp_path)
    wire.seed("getUsers", [[1, 2], [3]])
    assert surface.call("getUsers", {}).body == [1, 2]
    assert len(wire.requests) == 1
    wire.seed("getUsers", [[1, 2]])
    assert surface.call("getUsers", {}, all_pages=True, limit=1).body == [1]
    assert len(wire.requests) == 2
