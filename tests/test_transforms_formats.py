"""Scalar tags use the shared parser registry through ordinary public calls."""

import pytest
from test_transforms_richtext import surface_for

from as_engine.errors import SurfaceError


def operation():
    return {
        "parameters": [
            {
                "name": "elapsed",
                "in": "query",
                "schema": {"type": "integer", "format": "int64", "maximum": 10000},
            }
        ],
        "x-as-format": [
            {"target": {"in": "query", "name": "elapsed"}, "format": "duration"},
            {"target": {"in": "body", "path": "/dueDate"}, "format": "date"},
        ],
    }


def test_duration_query_and_date_body_do_not_modify_caller(tmp_path):
    surface, wire = surface_for(tmp_path, {"write": ("post", operation())})
    params, body = {"elapsed": "2h30m"}, {"dueDate": "2028-02-29"}
    surface.call("write", params, body)
    assert wire.requests == [("write", {"elapsed": 9000}, {"dueDate": "2028-02-29"})]
    assert params == {"elapsed": "2h30m"} and body == {"dueDate": "2028-02-29"}
    surface.call("write", {}, {})
    assert wire.requests[-1] == ("write", {}, {})


@pytest.mark.parametrize(
    "params,body",
    [
        ({"elapsed": "3h"}, {}),
        ({"elapsed": "-1"}, {}),
        ({"elapsed": True}, {}),
        ({"elapsed": "1h1h"}, {}),
        ({}, {"dueDate": "2027-02-29"}),
        ({}, {"dueDate": "2028-02-29T00:00:00Z"}),
    ],
)
def test_invalid_scalar_values_never_send(tmp_path, params, body):
    surface, wire = surface_for(tmp_path, {"write": ("post", operation())})
    with pytest.raises(SurfaceError) as caught:
        surface.call("write", params, body)
    assert caught.value.code == 2 and wire.requests == []


@pytest.mark.parametrize(
    "tag",
    [
        None,
        {},
        [{"target": {"in": "body", "path": "/date"}, "format": "workday"}],
        [{"target": {"in": "query", "name": "missing"}, "format": "duration"}],
        [{"target": {"in": "body", "path": "bad"}, "format": "date"}],
    ],
)
def test_bad_scalar_tags_fail_before_transport(tmp_path, tag):
    op = operation()
    op["x-as-format"] = tag
    surface, wire = surface_for(tmp_path, {"write": ("post", op)})
    with pytest.raises(SurfaceError):
        surface.call("write", {}, {})
    assert wire.requests == []
