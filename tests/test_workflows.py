"""Public indexed-read contracts over a non-product-specific compiled fixture.

These cases use the real Surface, parameter checker, scope guard and transforms.
Only the transport is controlled. Product CLI/configuration/wheel checks belong
in the product suite; no assertion here substitutes for that acceptance.
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from as_engine.compiler import compile_document
from as_engine.errors import SurfaceError
from as_engine.help import render_help
from as_engine.index import ProductIndexes
from as_engine.surface import Surface
from as_engine.transport import Response
from as_engine.workflows import (
    CAPABILITIES,
    Catalog,
    WorkflowError,
    normalize_inputs,
    render_result,
    result_document,
    run,
    validate_binding,
)


@pytest.fixture(autouse=True)
def forbid_http(monkeypatch):
    import requests

    def denied(*args, **kwargs):
        pytest.fail("workflow contract attempted real HTTP")

    monkeypatch.setattr(requests.Session, "send", denied)


def catalog_data():
    return {
        "schema_version": 1,
        "product": "inventory-as",
        "product_version": "1.2.3",
        "revision": 1,
        "workflows": [
            {
                "id": "list-assets",
                "revision": 1,
                "requires": ["indexed-read-v1"],
                "title": "List visible assets",
                "purpose": "See asset keys and names available to the current account.",
                "search_terms": ["assets", "inventory", "available", "visible"],
                "inputs": {
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "minimum": 0,
                        "maximum": 2147483647,
                    },
                },
                "prerequisites": ["Current account and operator scope must permit this read."],
                "binding": {
                    "kind": "indexed-read",
                    "document": "inventory",
                    "operation_id": "enumerateAssets",
                    "method": "GET",
                    "path": "/v1/assets",
                    "fixed_parameters": {"sort": "key", "permission": "view"},
                    "input_parameters": {"limit": "count", "offset": "start"},
                    "parameter_schemas": {
                        "count": {
                            "in": "query",
                            "schema": {"type": "integer", "format": "int32", "minimum": 1},
                        },
                        "start": {
                            "in": "query",
                            "schema": {"type": "integer", "format": "int32", "minimum": 0},
                        },
                        "sort": {"in": "query", "schema": {"type": "string", "enum": ["key"]}},
                        "permission": {
                            "in": "query",
                            "schema": {"type": "string", "enum": ["view"]},
                        },
                    },
                    "scope": {"in": "site"},
                },
                "projection": {
                    "id": "/uuid",
                    "key": "/code",
                    "name": "/label",
                    "canonical_url": {"pointer": "/self", "path": "/v1/assets/{identity}"},
                },
                "paging": {
                    "tag": {
                        "style": "offset/limit",
                        "itemsPath": "/entries",
                        "request": {
                            "offset": {"in": "query", "name": "start"},
                            "limit": {"in": "query", "name": "count"},
                        },
                        "response": {"totalPath": "/total"},
                    },
                    "evidence": {
                        "offset": "/start",
                        "limit": "/count",
                        "total": "/total",
                        "is_last": "/last",
                    },
                },
                "examples": [
                    {"kind": "invocation", "value": "inventory-as workflows run list-assets"},
                    {"kind": "json", "value": '{"limit":2,"offset":0}'},
                ],
            }
        ],
    }


def load(data=None):
    raw = json.dumps(catalog_data() if data is None else data).encode()
    return Catalog.load(raw, product="inventory-as", product_version="1.2.3")


def make_surface(tmp_path, pages, *, allow=True, mutate=None, factory_error=None):
    definition = catalog_data()["workflows"][0]
    binding = definition["binding"]
    fields = {
        "uuid": {"type": "string"},
        "code": {"type": "string"},
        "label": {"type": "string"},
        "self": {"type": "string", "format": "uri"},
    }
    page_schema = {
        "type": "object",
        "properties": {
            "entries": {"type": "array", "items": {"$ref": "#/components/schemas/Asset"}},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
            "total": {"type": "integer"},
            "last": {"type": "boolean"},
        },
    }
    operation = {
        "operationId": binding["operation_id"],
        "parameters": [
            {"name": key, **value} for key, value in binding["parameter_schemas"].items()
        ],
        "responses": {
            "200": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Page"}}}
            },
        },
        "x-as-scope": binding["scope"],
        "x-as-paging": definition["paging"]["tag"],
    }
    document = {
        "openapi": "3.0.3",
        "paths": {"/v1/assets": {"get": operation}},
        "components": {
            "schemas": {"Page": page_schema, "Asset": {"type": "object", "properties": fields}}
        },
    }
    if mutate:
        mutate(document)
    (tmp_path / "inventory.json").write_text(json.dumps(compile_document(document)))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "documents": [{"id": "inventory", "tier": "primary", "file": "inventory.json"}],
            }
        )
    )
    indexes = ProductIndexes(tmp_path)
    requests, factories, closed = [], [], []
    queue = list(pages)

    class ControlledTransport:
        def call(self, op, parameters, body):
            requests.append(
                {
                    "operation": op.operationId,
                    "method": op.method,
                    "parameters": dict(parameters),
                    "body": body,
                }
            )
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, Response) else Response(200, deepcopy(value), {})

        def close(self):
            closed.append(True)

    def factory(document_id, index):
        factories.append(document_id)
        assert index is indexes.get("inventory")
        if factory_error:
            raise factory_error
        return ControlledTransport()

    return (
        Surface(indexes, factory, scope_allow_site=allow, scope_allowlist=("AAA",)),
        requests,
        factories,
        closed,
    )


def asset(identity, key, name):
    return {
        "uuid": identity,
        "code": key,
        "label": name,
        "self": f"https://inventory.example/v1/assets/{identity}",
    }


ASSETS = [
    asset("10001", "AAA", "Alpha"),
    asset("10002", "BBB", "Shared"),
    asset("10003", "CCC", "Shared"),
]


def page(items=None, **metadata):
    return {"entries": deepcopy(ASSETS if items is None else items), **metadata}


def test_discovery_is_pure_and_bound_to_definition_bytes(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("discovery constructed execution machinery")

    monkeypatch.setattr(Surface, "__init__", denied)
    monkeypatch.setattr(ProductIndexes, "__init__", denied)
    data = catalog_data()
    raw = json.dumps(data).encode()
    catalog = Catalog.load(raw, product="inventory-as", product_version="1.2.3")
    assert "indexed-read-v1" in CAPABILITIES
    listing = catalog.list()
    assert [entry["id"] for entry in listing["entries"]] == ["list-assets"]
    assert listing["support"] is True and listing["availability"] == "unknown"
    assert listing["definition_digest"] == hashlib.sha256(raw).hexdigest()
    assert listing["engine_version"]
    assert "inventory-as 1.2.3" in catalog.hint()
    assert "catalog r1" in catalog.hint()
    assert "indexed-read-v1" in catalog.hint()
    assert 'Start: workflows search "task" --format json' in catalog.hint()
    assert "workflows describe ID includes a run example" in catalog.hint()
    assert "list-assets" not in catalog.hint()
    assert data["workflows"][0]["binding"]["operation_id"] not in catalog.hint()
    found = catalog.search("What inventory assets can I see?")
    assert [entry["id"] for entry in found["entries"]] == ["list-assets"]
    assert catalog.search("unrelated bananas")["entries"] == []
    assert catalog.search("What is the weather today?")["entries"] == []
    assert catalog.describe("list-assets")["inputs"]["limit"]["maximum"] == 100
    assert (
        catalog.describe("list-assets", examples=True)["examples"]
        == data["workflows"][0]["examples"]
    )
    assert catalog.describe("missing")["support"] is False
    assert catalog.describe("missing")["next_actions"] == [{"action": "search-catalog"}]
    other = Catalog.load(raw + b"\n", product="inventory-as", product_version="1.2.3")
    assert other.list()["definition_digest"] != listing["definition_digest"]
    # Returned structures cannot mutate the validated execution definition.
    catalog.describe("list-assets")["inputs"]["limit"]["maximum"] = 1000
    assert normalize_inputs(catalog.get("list-assets"), {}) == {"limit": 25, "offset": 0}


def test_default_description_includes_first_authored_invocation_detached():
    data = catalog_data()
    authored = data["workflows"][0]["examples"]
    authored.insert(0, authored.pop())  # JSON may precede the first invocation.
    authored.append(
        {"kind": "invocation", "value": "inventory-as workflows run list-assets --limit 2"}
    )
    catalog = load(data)
    described = catalog.describe("list-assets")
    assert described["examples"] == [authored[1]]
    assert catalog.describe("list-assets", examples=True)["examples"] == authored
    assert len(render_result(described) + "\n") <= 4800
    assert len(render_result(catalog.describe("list-assets", examples=True)) + "\n") <= 2400
    described["examples"][0]["value"] = "changed by caller"
    assert catalog.describe("list-assets")["examples"] == [authored[1]]
    assert catalog.get("list-assets").data["examples"] == authored


def test_default_description_does_not_invent_an_invocation():
    data = catalog_data()
    data["workflows"][0]["examples"] = [{"kind": "json", "value": '{"limit":2}'}]
    catalog = load(data)
    assert "examples" not in catalog.describe("list-assets")
    assert (
        catalog.describe("list-assets", examples=True)["examples"]
        == data["workflows"][0]["examples"]
    )


@pytest.mark.parametrize("query", ["", "   ", "x" * 513, True])
def test_search_rejects_invalid_query(query):
    result = load().search(query)
    assert result["status"] == "needs-input" and result["exit_code"] == 2


@pytest.mark.parametrize("query", ["visible assets", "visible assets 😀"])
def test_catalog_pagination_ranking_and_budgets(query):
    data = catalog_data()
    template = data["workflows"][0]
    data["workflows"] = [{**deepcopy(template), "id": f"assets-{n:02}"} for n in range(30)]
    catalog = load(data)
    seen = []
    offset = 0
    while True:
        result = catalog.search(query, offset=offset)
        assert result["status"] == "completed-read" and result["exit_code"] == 0
        assert result["query"] == query
        assert len(render_result(result) + "\n") <= 3200
        seen.extend(entry["id"] for entry in result["entries"])
        if result["complete"]:
            break
        offset = result["continuation"]["offset"]
        assert result["continuation"]["query"] == query
    assert seen == [f"assets-{n:02}" for n in range(30)]
    for examples, cap in [(False, 4800), (True, 2400)]:
        result = catalog.describe("assets-00", examples=examples)
        assert len(render_result(result) + "\n") <= cap
        doc = result_document(result)
        assert {"level", "title", "sections"} <= doc.keys()
        assert render_help(json.loads(render_help(doc, "json"))) == render_result(result)


@pytest.mark.parametrize("query", ["😀" * 512, "assets " + "😀" * 505])
def test_search_unicode_expansion_requests_shorter_query(query):
    catalog = load()
    assert len(query) == 512
    result = catalog.search(query)
    assert result["status"] == "needs-input" and result["exit_code"] == 2
    assert result["support"] is True and result["availability"] == "unknown"
    assert result["reason"]["code"] == "query-output-too-large"
    assert "shorter query" in result["reason"]["message"]
    assert result["next_actions"] == [{"action": "shorten-query"}]
    assert result["complete"] is None and result["continuation"] is None
    assert "query" not in result  # Explicit refusal, never an altered search query.
    assert len(render_result(result) + "\n") <= 3200
    assert json.loads(render_result(result, "json")) == result
    document = result_document(result)
    assert json.loads(document["sections"][0]["examples"][0]["value"]) == result
    assert catalog.search("visible assets")["status"] == "completed-read"


def test_search_unicode_continuation_budget_preserves_complete_queries():
    query = "assets " + "😀" * 110
    complete = load().search(query)
    assert complete["status"] == "completed-read" and complete["complete"] is True
    assert complete["query"] == query and complete["continuation"] is None
    assert [entry["id"] for entry in complete["entries"]] == ["list-assets"]
    assert len(render_result(complete) + "\n") <= 3200

    data = catalog_data()
    template = data["workflows"][0]
    data["workflows"] = [{**deepcopy(template), "id": f"assets-{n:02}"} for n in range(2)]
    complete = load(data).search(query)
    assert complete["status"] == "completed-read" and complete["complete"] is True
    assert complete["query"] == query and complete["continuation"] is None
    assert [entry["id"] for entry in complete["entries"]] == ["assets-00", "assets-01"]
    assert len(render_result(complete) + "\n") <= 3200

    data["workflows"] = [{**deepcopy(template), "id": f"assets-{n:02}"} for n in range(30)]
    catalog = load(data)
    result = catalog.search(query)
    assert result["status"] == "needs-input" and result["exit_code"] == 2
    assert result["reason"]["code"] == "query-output-too-large"
    assert result["continuation"] is None
    assert len(render_result(result) + "\n") <= 3200

    empty = catalog.search("😀" * 10)
    assert empty["status"] == "completed-read" and empty["entries"] == []
    assert empty["query"] == "😀" * 10 and empty["complete"] is True
    assert len(render_result(empty) + "\n") <= 3200


def test_oversized_definition_rendering_remains_load_time_incompatibility():
    data = catalog_data()
    data["workflows"][0]["examples"] = [
        {"kind": "json", "value": json.dumps("😀" * 300, ensure_ascii=False)}
    ]
    with pytest.raises(WorkflowError) as caught:
        load(data)
    assert caught.value.result["status"] == "blocked"
    assert caught.value.result["reason"]["code"] == "incompatible-definition-or-runtime"


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(product="wrong-as"),
        lambda d: d.update(product_version="1.2.4"),
        lambda d: d["workflows"].append(deepcopy(d["workflows"][0])),
        lambda d: d["workflows"][0].update(requires=["write-v1"]),
        lambda d: d["workflows"][0]["binding"].update(kind="procedure"),
        lambda d: d["workflows"][0]["binding"].update(method="POST"),
        lambda d: d["workflows"][0]["binding"].update(scope_allow_site=True),
        lambda d: d["workflows"][0].update(executable="echo"),
        lambda d: d["workflows"][0]["inputs"]["limit"].update(maximum=101),
        lambda d: d["workflows"][0]["inputs"]["offset"].update(default=True),
        lambda d: d["workflows"][0]["projection"].update(id="/bad~2path"),
        lambda d: d["workflows"][0].update(purpose="oversized " * 1000),
    ],
)
def test_incompatible_definitions_are_rejected_before_execution(change):
    data = catalog_data()
    change(data)
    with pytest.raises(WorkflowError) as caught:
        load(data)
    assert caught.value.result["status"] == "blocked"
    assert caught.value.result["reason"]["code"] == "incompatible-definition-or-runtime"
    assert caught.value.result["exit_code"] == 2


def test_duplicate_json_members_and_additive_descriptions():
    raw = json.dumps(catalog_data()).replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )
    with pytest.raises(WorkflowError):
        Catalog.load(raw.encode(), product="inventory-as", product_version="1.2.3")
    data = catalog_data()
    data["workflows"][0]["annotations"] = {"documentation": "More descriptive guidance"}
    assert load(data).list()["returned_count"] == 1


@pytest.mark.parametrize(
    "inputs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": "2"},
        {"offset": -1},
        {"offset": 2147483648},
        {"offset": False},
        {"site": "https://elsewhere.example"},
        [("limit", 2), ("limit", 3)],
    ],
)
def test_invalid_inputs_never_reach_factory(tmp_path, inputs):
    surface, calls, factories, _ = make_surface(tmp_path, [])
    result = run(load().get("list-assets"), inputs, surface)
    assert result["status"] == "needs-input" and result["exit_code"] == 2
    assert calls == factories == []


def test_two_pages_preserve_identity_and_advance_by_actual_count(tmp_path):
    surface, calls, factories, closed = make_surface(
        tmp_path,
        [
            page(ASSETS[:2], start=0, count=2, total=3, last=False),
            page(ASSETS[2:], start=2, count=2, total=3, last=True),
        ],
    )
    definition = load().get("list-assets")
    first = run(definition, {}, surface)
    assert first["status"] == "completed-read" and first["complete"] is False
    assert first["continuation"] == {
        "workflow": "list-assets",
        "inputs": {"limit": 25, "offset": 2},
    }
    last = run(definition, first["continuation"]["inputs"], surface)
    assert last["complete"] is True and last["continuation"] is None
    assert last["range"] == {"start": 2, "end": 3}
    assert [(r["id"], r["key"], r["name"]) for r in first["items"] + last["items"]] == [
        ("10001", "AAA", "Alpha"),
        ("10002", "BBB", "Shared"),
        ("10003", "CCC", "Shared"),
    ]
    assert [r["parameters"] for r in calls] == [
        {"start": 0, "count": 25, "sort": "key", "permission": "view"},
        {"start": 2, "count": 25, "sort": "key", "permission": "view"},
    ]
    assert all(
        r["operation"] == "enumerateAssets" and r["method"] == "GET" and r["body"] is None
        for r in calls
    )
    assert factories == ["inventory", "inventory"] and len(closed) == 2


@pytest.mark.parametrize(
    "body,status,complete,continuation",
    [
        (page([], start=0, total=0, last=True), "completed-read", True, False),
        (page([], start=0, total=3, last=False), "unknown", None, False),
        (page(ASSETS[:1], start=0), "completed-read", None, False),
        (page(ASSETS[:1], total=3), "completed-read", False, False),
        (page(ASSETS[:1], total=1), "completed-read", True, False),
        (page(ASSETS[:1], start=0, last=False), "completed-read", False, True),
        (page(ASSETS[:1], start=0, last=True), "completed-read", True, False),
        (page(ASSETS[:1], start=True, total=1), "unknown", None, False),
        (page(ASSETS[:1], start=1, total=1), "unknown", None, False),
        (page(ASSETS[:1], start=0, total=-1), "unknown", None, False),
        (page(ASSETS[:1], start=0, total=True), "unknown", None, False),
        (page(ASSETS[:1], start=0, total=1, last=False), "unknown", None, False),
        (page(ASSETS[:1], start=0, total=3, last=True), "unknown", None, False),
        (page(ASSETS[:1], start=0, last="true"), "unknown", None, False),
        (page(ASSETS[:1], start=0, count=0, total=1), "unknown", None, False),
        (page(ASSETS[:1], start=0, count=101, total=1), "unknown", None, False),
        (page(ASSETS[:2], start=0, count=1, total=2), "unknown", None, False),
        ({"entries": "bad"}, "unknown", None, False),
        ([], "unknown", None, False),
        (page([{"uuid": "1"}], start=0, total=1), "unknown", None, False),
        (page([ASSETS[0], ASSETS[0]], start=0, total=2), "unknown", None, False),
    ],
)
def test_paging_evidence_never_invents_exhaustiveness(
    tmp_path, body, status, complete, continuation
):
    surface, calls, _, _ = make_surface(tmp_path, [body])
    result = run(load().get("list-assets"), {}, surface)
    assert result["status"] == status and result["complete"] is complete
    assert bool(result["continuation"]) is continuation
    assert result["exit_code"] == (1 if status == "unknown" else 0)
    assert len(calls) == 1
    assert result["reason"]["code"]


def test_oversized_page_reports_dropped_entries(tmp_path):
    surface, _, _, _ = make_surface(tmp_path, [page(start=0, total=3)])
    result = run(load().get("list-assets"), {"limit": 2}, surface)
    assert result["status"] == "unknown" and result["complete"] is None
    assert result["continuation"] is None and result["returned_count"] <= 2
    assert result["evidence"]["received_count"] == 3
    assert result["evidence"]["omitted_count"] == 1


@pytest.mark.parametrize(
    "url,valid",
    [
        ("https://inventory.example/v1/assets/10001", True),
        ("https://inventory.example/v1/assets/AAA", True),
        ("https://inventory.example/documentation", False),
        ("http://inventory.example/v1/assets/10001", False),
        ("https://user:secret@inventory.example/v1/assets/10001", False),
        ("https://inventory.example/v1/assets/10001?token=secret", False),
        ("https://inventory.example/v1/assets/10001#fragment", False),
        ("https://inventory.example/v1/assets/99999", False),
        ("https://inventory.example/v1/assets/10001\n", False),
        (None, False),
    ],
)
def test_canonical_links_are_display_data_only(tmp_path, url, valid):
    item = {**ASSETS[0], "self": url, "url": "https://documentation.example"}
    surface, calls, _, _ = make_surface(tmp_path, [page([item], start=0, total=1)])
    result = run(load().get("list-assets"), {}, surface)
    assert result["items"][0]["url"] == (url if valid else None)
    assert result["items"][0]["url_source"] == ("provider-self" if valid else None)
    assert len(calls) == 1


def test_provider_text_cannot_create_markdown_commands_or_continuation(tmp_path):
    hostile = "Shared\n```\n$(evil) <script>\x1b[31m"
    item = {**ASSETS[0], "label": hostile}
    body = page([item], start=0, total=2, last=False)
    body["nextPage"] = "https://evil.example/?token=secret"
    surface, calls, _, _ = make_surface(tmp_path, [body])
    result = run(load().get("list-assets"), {}, surface)
    rendered = render_result(result)
    assert result["items"][0]["name"] == hostile
    assert "$(evil)" in rendered  # inert data inside a JSON fence, never an action
    assert "<script>" not in rendered and "\x1b" not in rendered
    assert rendered.count("```") == 2
    assert json.loads(render_result(result, "json")) == result
    data_block = result_document(result)["sections"][0]["examples"][0]["value"]
    assert json.loads(data_block) == result
    assert result["continuation"] == {
        "workflow": "list-assets",
        "inputs": {"limit": 25, "offset": 1},
    }
    assert "evil.example" not in rendered and len(calls) == 1


def test_scope_is_checked_at_run_time_even_after_discovery(tmp_path):
    catalog = load()
    assert catalog.describe("list-assets")["availability"] == "unknown"
    surface, calls, factories, _ = make_surface(tmp_path, [], allow=False)
    blocked = run(catalog.get("list-assets"), {}, surface)
    assert blocked["status"] == "blocked" and blocked["exit_code"] == 4
    assert calls == factories == []


@pytest.mark.parametrize(
    "status,exit_code,outcome",
    [
        (400, 2, "blocked"),
        (401, 3, "blocked"),
        (403, 4, "blocked"),
        (404, 5, "failed"),
        (409, 7, "failed"),
        (429, 6, "failed"),
        (503, 6, "failed"),
    ],
)
def test_surface_http_failure_meanings_and_sanitized_results(tmp_path, status, exit_code, outcome):
    surface, calls, _, closed = make_surface(
        tmp_path, [Response(status, {"message": "opaque-private-server-dump"}, {})]
    )
    result = run(load().get("list-assets"), {}, surface)
    assert result["status"] == outcome and result["exit_code"] == exit_code
    assert result["items"] == [] and result["complete"] is None
    assert "opaque-private-server-dump" not in render_result(result)
    assert len(calls) == len(closed) == 1


def test_factory_validation_and_transport_failure(tmp_path):
    surface, calls, _, _ = make_surface(
        tmp_path, [], factory_error=ValueError("private-config-value")
    )
    result = run(load().get("list-assets"), {}, surface)
    assert result["status"] == "blocked" and result["exit_code"] == 2
    assert "private-config-value" not in render_result(result) and calls == []
    surface, _, _, _ = make_surface(tmp_path, [SurfaceError(503, ["private-request-url"])])
    result = run(load().get("list-assets"), {}, surface)
    assert result["status"] == "failed" and result["exit_code"] == 6
    assert "private-request-url" not in render_result(result)


@pytest.mark.parametrize(
    "change",
    [
        lambda op: replace(op, operationId="otherOperation"),
        lambda op: replace(op, method="POST"),
        lambda op: replace(op, path="/v2/assets"),
        lambda op: replace(op, extensions={"x-as-paging": op.extensions["x-as-paging"]}),
        lambda op: replace(
            op, extensions={**op.extensions, "x-as-scope": {"in": "query", "name": "tenant"}}
        ),
        lambda op: replace(op, extensions={**op.extensions, "x-as-paging": {"style": "none"}}),
        lambda op: replace(
            op,
            extensions={
                **op.extensions,
                "x-as-version": {"target": {"in": "body", "path": "/version"}},
            },
        ),
        lambda op: replace(op, extensions={**op.extensions, "x-as-response": {"kind": "binary"}}),
        lambda op: replace(
            op,
            parameters=[
                {**p, "schema": {"type": "string"}} if p["name"] == "count" else p
                for p in op.parameters
            ],
        ),
    ],
)
def test_changed_index_is_incompatible_before_transport(tmp_path, change):
    surface, calls, factories, _ = make_surface(tmp_path, [])
    index = surface.indexes.get("inventory")
    index.operations["enumerateAssets"] = change(index.operations["enumerateAssets"])
    result = run(load().get("list-assets"), {}, surface)
    assert result["reason"]["code"] == "incompatible-definition-or-runtime"
    assert result["status"] == "blocked" and calls == factories == []


def test_removed_operation_alias_document_and_evidence_schema_are_not_substitutes(tmp_path):
    surface, calls, factories, _ = make_surface(tmp_path, [])
    definition = load().get("list-assets")
    validate_binding(definition, surface.indexes)
    index = surface.indexes.get("inventory")
    index.schemas["Page"]["properties"]["last"] = {"type": "string"}
    assert run(definition, {}, surface)["status"] == "blocked"
    index.schemas["Page"]["properties"]["last"] = {"type": "boolean"}
    data = catalog_data()
    data["workflows"][0]["binding"]["document"] = "another-document"
    assert run(load(data).get("list-assets"), {}, surface)["status"] == "blocked"
    op = index.operations.pop("enumerateAssets")
    index.operations["enumerate-assets"] = replace(op, operationId="enumerate-assets")
    assert run(definition, {}, surface)["status"] == "blocked"
    assert calls == factories == []


def test_capability_removed_after_discovery_is_incompatible(tmp_path, monkeypatch):
    from as_engine import workflows

    catalog = load()
    surface, calls, factories, _ = make_surface(tmp_path, [])
    monkeypatch.setattr(workflows, "CAPABILITIES", frozenset())
    result = run(catalog.get("list-assets"), {}, surface)
    assert result["reason"]["code"] == "incompatible-definition-or-runtime"
    assert calls == factories == []
    with pytest.raises(WorkflowError) as caught:
        load()
    assert caught.value.result["required_capabilities"] == ["indexed-read-v1"]
    assert caught.value.result["runtime_capabilities"] == []


def test_declared_bounds_and_required_parameters_must_match_index(tmp_path):
    surface, calls, factories, _ = make_surface(tmp_path, [])
    definition = load().get("list-assets")
    index = surface.indexes.get("inventory")
    op = index.operations["enumerateAssets"]
    index.operations[op.operationId] = replace(
        op,
        parameters=[
            *op.parameters,
            {"name": "tenant", "in": "query", "type": "string", "required": True},
        ],
    )
    assert run(definition, {}, surface)["status"] == "blocked"
    index.operations[op.operationId] = op
    data = catalog_data()
    data["workflows"][0]["inputs"]["offset"]["maximum"] = 2147483648
    result = run(load(data).get("list-assets"), {}, surface)
    assert result["reason"]["code"] == "incompatible-definition-or-runtime"
    assert calls == factories == []


def test_continuation_cannot_exceed_input_bounds(tmp_path):
    surface, calls, _, _ = make_surface(
        tmp_path,
        [
            page(ASSETS[:1], start=2147483647, total=2147483649, last=False),
        ],
    )
    result = run(load().get("list-assets"), {"offset": 2147483647}, surface)
    assert result["complete"] is False and result["continuation"] is None
    assert result["reason"]["code"] == "continuation-out-of-bounds"
    assert len(calls) == 1


@pytest.mark.parametrize("body", [page([], start=0, total=0), page(start=0, total=3), []])
def test_all_result_values_survive_human_json_round_trip(tmp_path, body):
    surface, _, _, _ = make_surface(tmp_path, [body])
    result = run(load().get("list-assets"), {}, surface)
    document = result_document(result)
    assert json.loads(document["sections"][0]["examples"][0]["value"]) == result
    assert json.loads(render_result(result, "json")) == result
    assert render_help(json.loads(render_help(document, "json"))) == render_result(result)
