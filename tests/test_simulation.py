"""Stateful simulation tests through the public transport seam."""

from __future__ import annotations

import subprocess
import sys

from as_engine.index import Operation
from as_engine.simulation import Simulation, SimulationStore


def operation(name: str) -> Operation:
    return Operation(name, "GET", "/simulation", [], None, None, [], None, None, {}, [])


def test_default_seed_snapshot_and_call_log_are_detached():
    store = SimulationStore()
    simulation = Simulation(store)
    response = simulation.call(operation("getPageById"), {"id": "1"}, None)
    assert response.status == 200 and response.body["title"] == "First"
    snapshot = store.snapshot()
    snapshot["pages"][0]["title"] = "changed"
    assert store.snapshot()["pages"][0]["title"] == "First"
    params = {"id": "1"}
    simulation.call(operation("getPageById"), params, {"include": ["body"]})
    params["id"] = "2"
    assert store.calls[-1] == ("getPageById", {"id": "1"}, {"include": ["body"]})
    assert simulation.close() is None


def test_page_mutation_reads_tree_and_reports_version_conflict():
    simulation = Simulation(SimulationStore())
    created = simulation.call(
        operation("createPage"), {}, {"spaceId": "55", "parentId": "2", "title": "Third"}
    )
    assert created.status == 201 and created.body["id"] == "3"
    assert [p["id"] for p in simulation.call(operation("getPageDescendants"), {"id": "1"}, None).body["results"]] == ["2", "3"]
    conflict = simulation.call(
        operation("updatePage"), {"id": "3"}, {"title": "Other", "version": {"number": 9}}
    )
    assert conflict.status == 409 and conflict.body["message"] == "Version conflict"
    updated = simulation.call(operation("updatePage"), {"id": "3"}, {"title": "Other"})
    assert updated.body["version"] == {"number": 2}
    assert simulation.call(operation("deletePage"), {"id": "404"}, None).status == 404


def test_cql_labels_and_unknown_operation_are_explicit():
    simulation = Simulation(SimulationStore())
    assert simulation.call(operation("addLabelsToContent"), {"id": "2"}, [{"name": "reviewed"}]).status == 200
    found = simulation.call(operation("searchByCQL"), {"cql": "space=DOCS AND type IN (page) AND label=reviewed"}, None)
    assert found.status == 200 and [item["id"] for item in found.body["results"]] == ["2"]
    assert simulation.call(operation("removeLabelFromContent"), {"id": "2", "label": "reviewed"}, None).status == 204
    unsupported = simulation.call(operation("searchByCQL"), {"cql": "text ~ 'needle'"}, None)
    assert unsupported.status == 400 and "supports equality" in unsupported.body["message"]
    unknown = simulation.call(operation("unmodelledOperation"), {}, None)
    assert unknown.status == 501 and "unmodelledOperation" in unknown.body["message"]


def test_cql_created_order_honors_lowercase_direction():
    simulation = Simulation(
        SimulationStore(
            {
                "pages": [
                    {"id": "1", "spaceId": "55", "title": "Older", "created": "2026-01-01"},
                    {"id": "2", "spaceId": "55", "title": "Newer", "created": "2026-02-01"},
                ]
            }
        )
    )
    descending = simulation.call(
        operation("searchByCQL"), {"cql": "space=DOCS ORDER BY created desc"}, None
    )
    ascending = simulation.call(
        operation("searchByCQL"), {"cql": "space=DOCS ORDER BY created asc"}, None
    )
    assert [item["id"] for item in descending.body["results"]] == ["2", "1"]
    assert [item["id"] for item in ascending.body["results"]] == ["1", "2"]


def test_surface_and_cassette_import_without_http_stack_until_constructed():
    program = """
import sys
import as_engine.surface
import as_engine.cassette
assert 'requests' not in sys.modules
from as_engine.transport import HTTPTransport
from as_engine import transport
assert HTTPTransport('https://offline.invalid').error_handler is transport.handle_api_error
assert 'requests' in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, check=False, text=True
    )
    assert result.returncode == 0, result.stderr


def test_attachment_upload_update_download_and_paging_through_simulation(tmp_path):
    import base64
    from dataclasses import replace

    store = SimulationStore({"attachments": []})
    sim = Simulation(store)
    file = tmp_path / "payload.bin"
    content = b"\x00\xff\xfe\n"
    file.write_bytes(content)
    upload = replace(operation("createAttachment"), request_media_types=["multipart/form-data"])
    created = sim.call(upload, {"id": "1"}, {"file": f"@{file}", "minorEdit": True})
    attachment = created.body["results"][0]
    assert attachment["id"] == "att1" and attachment["fileSize"] == len(content)
    assert "data_base64" not in attachment
    assert store.snapshot()["attachments"][0]["data_base64"] == base64.b64encode(content).decode()
    assert store.wire_requests[0]["headers"] == {"X-Atlassian-Token": "nocheck"}
    assert store.wire_requests[0]["parts"][0]["size"] == len(content)
    metadata = sim.call(operation("getAttachmentById"), {"id": "att1"}, None)
    assert metadata.body["pageId"] == "1" and "data_base64" not in metadata.body
    download = replace(operation("downloadAttatchment"), extensions={"x-as-response": {"kind": "binary"}})
    destination = tmp_path / "download.bin"
    assert sim.call(download, {"id": "1", "attachmentId": "att1"}, None, output=destination).body["bytes"] == len(content)
    assert destination.read_bytes() == content
    file.write_bytes(b"updated")
    update = replace(operation("updateAttachmentData"), request_media_types=["multipart/form-data"])
    changed = sim.call(update, {"id": "1", "attachmentId": "att1"}, {"file": f"@{file}"})
    assert changed.body["version"]["number"] == 2
    sim.call(download, {"id": "1", "attachmentId": "att1"}, None, output=destination)
    assert destination.read_bytes() == b"updated"
    sim.call(upload, {"id": "1"}, {"file": f"@{file}"})
    first = sim.call(operation("getPageAttachments"), {"id": "1", "limit": 1}, None)
    assert len(first.body["results"]) == 1 and first.body["_links"]["next"] == "?cursor=1"
    second = sim.call(operation("getPageAttachments"), {"id": "1", "limit": 1, "cursor": "1"}, None)
    assert len(second.body["results"]) == 1 and second.body["_links"] == {}
    assert sim.call(download, {"id": "2", "attachmentId": "att1"}, None, output=tmp_path / "wrong").status == 404
    assert not (tmp_path / "wrong").exists()
    assert sim.call(operation("getAttachmentById"), {"id": "absent"}, None).status == 404
