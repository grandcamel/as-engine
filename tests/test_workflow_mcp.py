"""Public MCP and owned-process contracts; synthetic CLI, no Jira or HTTP access.

SDK imports are mandatory. These tests are not native-app or live product proof.
The Jira phase will supply the shared product CLI/Surface scenario oracles.
"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from copy import deepcopy
from textwrap import dedent

import mcp_types
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from as_engine import workflow_mcp
from as_engine.workflow_mcp import create_server, load_profile
from as_engine.workflows import Catalog, failure_result


def no_network(*_args, **_kwargs):
    pytest.fail("unexpected network access")


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    for name in tuple(os.environ):
        if (name.startswith(("JIRA_", "OPENAI_")) or name in {
            "SITE_URL", "EMAIL", "API_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
            "SSL_CERT_FILE", "SSL_CERT_DIR",
        }):
            monkeypatch.delenv(name)
    for name, value in {
        "JIRA_SITE_URL": "https://fixture.atlassian.net", "JIRA_EMAIL": "fixture@example.test",
        "JIRA_API_TOKEN": "fixture-only-secret-marker", "JIRA_ALLOWED_PROJECTS": "",
        "JIRA_ALLOW_SITE_OPERATIONS": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


@pytest.fixture
def catalog():
    # Product-shaped data for the engine adapter contract; no product package import.
    document = {
        "schema_version": 1, "product": "jira-as", "product_version": "2.0.0rc1", "revision": 1,
        "workflows": [{
            "id": "list-projects", "revision": 1, "requires": ["indexed-read-v1"],
            "title": "List visible Jira projects", "purpose": "Find visible projects.",
            "search_terms": ["projects"], "prerequisites": ["Site-operation permission."],
            "inputs": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
                "offset": {"type": "integer", "minimum": 0,
                           "maximum": 9223372036854775807, "default": 0},
            },
            "binding": {
                "kind": "indexed-read", "document": "platform", "operation_id": "searchProjects",
                "method": "GET", "path": "/rest/api/3/project/search",
                "fixed_parameters": {"orderBy": "key", "action": "view"},
                "input_parameters": {"limit": "maxResults", "offset": "startAt"},
                "parameter_schemas": {
                    "startAt": {"in": "query", "schema": {"type": "integer", "default": 0,
                                                          "format": "int64"}},
                    "maxResults": {"in": "query", "schema": {"type": "integer", "default": 50,
                                                              "maximum": 100, "format": "int32"}},
                    "orderBy": {"in": "query", "schema": {"type": "string", "enum": ["key"]}},
                    "action": {"in": "query", "schema": {"type": "string", "enum": ["view"]}},
                },
                "scope": {"in": "site"},
            },
            "projection": {"id": "/id", "key": "/key", "name": "/name",
                           "canonical_url": {"pointer": "/self", "path": "/rest/api/3/project/{identity}"}},
            "paging": {
                "tag": {"style": "offset/limit", "itemsPath": "/values",
                        "request": {"offset": {"in": "query", "name": "startAt"},
                                    "limit": {"in": "query", "name": "maxResults"}},
                        "response": {"totalPath": "/total"}},
                "evidence": {"offset": "/startAt", "limit": "/maxResults", "total": "/total",
                             "is_last": "/isLast"},
            },
            "examples": [{"kind": "invocation", "value": "jira-as workflows run list-projects"},
                         {"kind": "json", "value": '{"limit":25,"offset":0}'}],
        }],
    }
    return Catalog.load(json.dumps(document).encode(), product="jira-as", product_version="2.0.0rc1")


def read_result(catalog, *, complete=True):
    result = failure_result(catalog.get("list-projects").provenance, code=0,
                            status="completed-read", reason="final-page")
    result.update(
        availability="available", view="run", inputs={"limit": 25, "offset": 0},
        limit=25, offset=0, complete=complete, range={"start": 0, "end": 1}, returned_count=1,
        items=[{"id": "10001", "key": "AAA", "name": "Alpha", "url": None, "url_source": None}],
        evidence={"binding": "indexed-read", "document": "platform", "operation_id": "searchProjects",
                  "method": "GET", "received_count": 1, "omitted_count": 0,
                  "coverage": "Only an offset0-to-final traversal covers the full set; reads are not a transactional snapshot.",
                  "metadata_present": ["offset", "limit", "total", "is_last"],
                  "metadata": {"offset": 0, "limit": 25, "total": 1, "is_last": True}},
        next_actions=[],
    )
    return result


@pytest.fixture
def pilot(tmp_path, catalog):
    root = tmp_path.resolve()
    executable = root / "fixed-jira"
    payloads = root / "payloads.json"
    calls = root / "calls.jsonl"
    values = {
        "list": catalog.list(), "search": catalog.search("projects"),
        "describe": catalog.describe("list-projects"),
        "examples": catalog.describe("list-projects", examples=True), "run": read_result(catalog),
    }
    payloads.write_text(json.dumps(values))
    executable.write_text(f"#!{sys.executable}\n" + dedent(f'''\
        import json
        import os
        import signal
        import sys
        import time
        from pathlib import Path

        values = json.loads(Path({str(payloads)!r}).read_text())
        action = sys.argv[2]
        with Path({str(calls)!r}).open("a") as stream:
            stream.write(json.dumps({{
                "argv": sys.argv[1:], "names": sorted(os.environ),
                "scope": os.environ.get("JIRA_ALLOWED_PROJECTS"),
                "site": os.environ.get("JIRA_ALLOW_SITE_OPERATIONS"),
                "transport": os.environ.get("JIRA_AS_TRANSPORT"),
            }}) + "\\n")
        mode = values.get("mode", "normal") if action == "run" else "normal"
        if mode in ("sleep", "ignore-term"):
            if mode == "ignore-term":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(30)
        if mode in ("stdout-overflow", "stderr-overflow"):
            target = sys.stdout if mode == "stdout-overflow" else sys.stderr
            target.write("x" * 16384)
            target.flush()
            time.sleep(30)
        if mode == "raw":
            target = sys.stderr if values.get("raw_stream") == "stderr" else sys.stdout
            target.buffer.write(bytes.fromhex(values["raw_hex"]))
            raise SystemExit(values.get("raw_exit", 0))
        if mode == "opposite":
            print("secret-marker unexpected diagnostic", file=sys.stderr)
        if mode == "exit":
            raise SystemExit(17)
        value = values["examples" if "--examples" in sys.argv else action]
        if action == "search":
            value["query"] = sys.argv[-1]
        code = value["exit_code"]
        print(json.dumps(value), file=sys.stderr if code else sys.stdout)
        raise SystemExit(code)
    '''))
    executable.chmod(0o700)
    p = catalog.provenance
    document = {
        "schema_version": 1, "adapter_id": "fixture-workflows", "product": "jira-as",
        "executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "expected": {**{key: p[key] for key in ("product_version", "engine_version", "schema_version",
                                                "catalog_revision", "definition_digest")},
                     "workflow_revisions": {"list-projects": 1}},
        "context": {"source": "approved-env-v1", "account_email": "fixture@example.test",
                    "site_url": "https://fixture.atlassian.net", "home": str(root),
                    "cwd": str(root), "tmpdir": str(root), "scope": {}},
        "limits": {"call_timeout_seconds": 1, "terminate_grace_seconds": 1,
                   "kill_grace_seconds": 1, "drain_grace_seconds": 1},
    }
    profile = root / "profile.json"
    profile.write_text(json.dumps(document))
    profile.chmod(0o600)
    return {"profile": profile, "document": document, "values": values, "payloads": payloads,
            "calls": calls, "executable": executable}


def save(pilot):
    pilot["payloads"].write_text(json.dumps(pilot["values"]))
    pilot["profile"].write_text(json.dumps(pilot["document"]))


def calls(pilot):
    if not pilot["calls"].exists():
        return []
    return [json.loads(line) for line in pilot["calls"].read_text().splitlines()]


def envelope(result):
    assert isinstance(result, mcp_types.CallToolResult)
    assert len(result.content) == 1 and result.content[0].type == "text"
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


def assert_child_observation(error, value, *, phase="execute"):
    # The fixed fixture prints json.dumps(value) plus one newline to the stream
    # selected by its exit code. Count those actual fixture bytes, not adapter state.
    size = len((json.dumps(value) + "\n").encode())
    code = int(value["exit_code"])
    assert error["phase"] == phase
    assert error["child_exit_code"] == code
    assert error["stdout_bytes"] == (0 if code else size)
    assert error["stderr_bytes"] == (size if code else 0)
    assert error["counts_complete"] is True
    assert error["cleanup"] == "reaped"


def assert_not_started(error):
    assert error["child_exit_code"] is None
    assert error["stdout_bytes"] == error["stderr_bytes"] == 0
    assert error["counts_complete"] is True
    assert error["cleanup"] == "not-started"


async def request(pilot, name="workflows_run", args=None):
    if args is None:
        args = {"workflow": "list-projects"}
    server = await create_server(load_profile(pilot["profile"]))
    async with Client(server, mode="legacy", raise_exceptions=True) as client:
        return await client.call_tool(name, args)


def test_public_discovery_schemas_and_exact_results(pilot, monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN")
    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy", raise_exceptions=True) as client:
            listing = await client.list_tools()
            assert [tool.name for tool in listing.tools] == [
                "workflows_list", "workflows_search", "workflows_describe", "workflows_run",
            ]
            for tool in listing.tools:
                Draft202012Validator.check_schema(tool.input_schema)
                Draft202012Validator.check_schema(tool.output_schema)
                assert tool.input_schema["additionalProperties"] is False
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
            run = listing.tools[-1].input_schema
            assert run["properties"]["inputs"]["additionalProperties"] is False
            assert run["properties"]["inputs"]["properties"] == pilot["values"]["describe"]["inputs"]
            for name, args, expected in (
                ("list", {}, "list"), ("search", {"query": "projects"}, "search"),
                ("describe", {"workflow": "list-projects"}, "describe"),
                ("describe", {"workflow": "list-projects", "examples": True}, "examples"),
            ):
                result = await client.call_tool("workflows_" + name, args)
                assert not result.is_error
                assert envelope(result) == pilot["values"][expected]
    asyncio.run(scenario())
    assert all("JIRA_API_TOKEN" not in row["names"] for row in calls(pilot))
    assert not any(row["argv"][1] == "run" for row in calls(pilot))


@pytest.mark.parametrize("name,value", [
    ("JIRA_AS_TRANSPORT", "socket"), ("JIRA_AS_TRANSPORT", "simulation"),
    ("JIRA_AS_TRANSPORT", "cassette"), ("JIRA_AS_TRANSPORT", "responder"),
    ("JIRA_AS_TRANSPORT", "HTTP"), ("JIRA_AS_TRANSPORT", ""),
    *((name, "https://routing.invalid/secret-marker") for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
    )),
])
def test_ambient_routing_refusals_do_not_launch_run(pilot, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    result = asyncio.run(request(pilot))
    assert result.is_error
    assert envelope(result)["error"]["code"] == "runtime-context-unavailable"
    assert_not_started(envelope(result)["error"])
    assert len(calls(pilot)) == 2
    assert all(row["argv"][1] != "run" for row in calls(pilot))
    assert "secret-marker" not in result.content[0].text


@pytest.mark.parametrize("missing", ["JIRA_API_TOKEN", "JIRA_EMAIL", "JIRA_SITE_URL",
                                      "JIRA_ALLOWED_PROJECTS", "JIRA_ALLOW_SITE_OPERATIONS"])
def test_missing_context_is_distinct_from_explicit_empty_scope(pilot, monkeypatch, missing):
    monkeypatch.delenv(missing)
    result = asyncio.run(request(pilot))
    assert envelope(result)["error"]["code"] == "runtime-context-unavailable"
    assert len(calls(pilot)) == 2


def test_empty_project_scope_and_false_site_permission_are_preserved(pilot, monkeypatch):
    monkeypatch.setenv("JIRA_AS_TRANSPORT", "http")
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "tunnel-secret-marker")
    monkeypatch.setenv("PYTHONPATH", "/not-forwarded")
    result = asyncio.run(request(pilot))
    assert envelope(result) == pilot["values"]["run"]
    executed = calls(pilot)[-1]
    assert executed["scope"] == "" and executed["site"] == "false"
    assert executed["transport"] == "http"
    assert not {"OPENAI_API_KEY", "PYTHONPATH", "HTTPS_PROXY"}.intersection(executed["names"])
    assert executed["argv"] == ["workflows", "run", "--limit=25", "--offset=0",
                                "--format=json", "--", "list-projects"]
    # This fixture emits a synthetic result. It does not assert site=false permits a real read.


@pytest.mark.parametrize("name,value", [
    ("JIRA_EMAIL", "another@example.test"), ("JIRA_SITE_URL", "https://other.atlassian.net"),
    ("JIRA_ALLOWED_PROJECTS", "SBX"), ("JIRA_ALLOW_SITE_OPERATIONS", "true"),
    ("JIRA_DEFAULT_PROJECT", "SBX"),
])
def test_identity_mismatch_has_no_run_child(pilot, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    result = asyncio.run(request(pilot))
    assert envelope(result)["error"]["code"] == "identity-binding-mismatch"
    assert len(calls(pilot)) == 2


@pytest.mark.parametrize("args", [
    {"workflow": "list-projects", "inputs": {"limit": True}},
    {"workflow": "list-projects", "inputs": {"limit": 2.0}},
    {"workflow": "list-projects", "inputs": {"limit": "2"}},
    {"workflow": "list-projects", "inputs": {"limit": 0}},
    {"workflow": "list-projects", "inputs": {"limit": 101}},
    {"workflow": "list-projects", "inputs": {"offset": -1}},
    {"workflow": "list-projects", "inputs": {"offset": 9223372036854775808}},
    {"workflow": "list-projects", "inputs": {"env": {"JIRA_ALLOW_SITE_OPERATIONS": "true"}}},
    {"workflow": "list-projects", "inputs": None},
    {"workflow": "list-projects", "site": "https://other.test"},
    {"workflow": "list-projects", "argv": ["--allow-site"]}, {},
])
def test_invalid_nested_and_authority_inputs_fail_before_launch(pilot, args):
    result = asyncio.run(request(pilot, args=args))
    assert envelope(result)["error"]["code"] == "invalid-input"
    assert len(calls(pilot)) == 2


def test_unknown_workflow_and_tool_are_distinct_public_errors(pilot):
    result = asyncio.run(request(pilot, args={"workflow": "write-anything"}))
    assert envelope(result)["error"]["code"] == "unsupported-workflow"
    assert len(calls(pilot)) == 2

    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy", raise_exceptions=True) as client:
            before = calls(pilot)
            assert len(before) == 4  # Two discovery children for each server bootstrap.
            # Assert at the public call seam, before Client task-group exit can
            # wrap a deliberately unhandled MCPError in an ExceptionGroup.
            with pytest.raises(MCPError, match="^Unknown workflow tool$") as exc:
                await client.call_tool("arbitrary_shell", {})
            assert type(exc.value) is MCPError
            assert exc.value.code == -32602
            assert exc.value.message == "Unknown workflow tool"
            assert exc.value.data is None
            assert calls(pilot) == before
        assert calls(pilot) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("query", ["", " ", "x" * 513, "projects\0"])
def test_search_bounds_before_child(pilot, query):
    result = asyncio.run(request(pilot, "workflows_search", {"query": query}))
    assert envelope(result)["error"]["code"] == "invalid-input"
    assert len(calls(pilot)) == 2


def test_leading_dash_search_is_a_single_positional_value(pilot):
    query = "--format=markdown $(not-a-command)"
    result = asyncio.run(request(pilot, "workflows_search", {"query": query}))
    assert envelope(result)["query"] == query
    assert calls(pilot)[-1]["argv"] == ["workflows", "search", "--offset=0", "--format=json", "--", query]


@pytest.mark.parametrize("code,status,reason", [
    (1, "unknown", "malformed-page"), (2, "blocked", "configuration-or-parameters"),
    (3, "blocked", "authentication-failed"), (4, "blocked", "scope-refused"),
    (5, "failed", "resource-not-found"), (6, "failed", "transport-failed"), (7, "failed", "conflict"),
])
def test_exact_product_stderr_error_mapping(pilot, catalog, code, status, reason):
    value = failure_result(catalog.get("list-projects").provenance, code=code,
                           status=status, reason=reason)
    value.update(view="run", inputs={"limit": 25, "offset": 0}, limit=25, offset=0)
    pilot["values"]["run"] = value
    save(pilot)
    result = asyncio.run(request(pilot))
    assert result.is_error and envelope(result) == value


@pytest.mark.parametrize("complete", [False, None])
def test_success_never_fabricates_exhaustive_result(pilot, complete):
    value = pilot["values"]["run"]
    value["complete"] = complete
    value["items"][0]["name"] = "Shared\n```\n<script>&\0"
    save(pilot)
    result = asyncio.run(request(pilot))
    assert not result.is_error and envelope(result) == value
    assert "<script>" not in result.content[0].text


@pytest.mark.parametrize("mutation", ["extra", "exit", "count", "capability", "digest", "bounds", "reason"])
def test_output_schema_or_identity_failure_blocks_following_dispatch(pilot, mutation):
    value = pilot["values"]["run"]
    if mutation == "extra":
        value["operator_env"] = {}
    elif mutation == "exit":
        value["exit_code"] = True
    elif mutation == "count":
        value["returned_count"] = 8
    elif mutation == "capability":
        value["runtime_capabilities"] = ["write-v1"]
    elif mutation == "digest":
        value["definition_digest"] = "f" * 64
    elif mutation == "bounds":
        value["input_bounds"]["limit"]["maximum"] = 101
    else:
        value["reason"]["message"] = "provider secret-marker"
    save(pilot)
    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert result.is_error
            assert envelope(result)["error"]["code"] == "incompatible-output"
            assert_child_observation(envelope(result)["error"], value)
            before = len(calls(pilot))
            assert before == 3
            again = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(again)["error"]["dispatch_blocked"] is True
            assert envelope(again) == envelope(result)
            invalid = await client.call_tool("workflows_run", {"workflow": "list-projects", "env": {}})
            assert envelope(invalid)["error"]["code"] == "invalid-input"
            assert_not_started(envelope(invalid)["error"])
            assert len(calls(pilot)) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("leak", ["fixture-only-secret-marker", base64.b64encode(
    b"fixture@example.test:fixture-only-secret-marker").decode()])
def test_secret_in_json_leaf_is_not_returned(pilot, leak):
    pilot["values"]["run"]["items"][0]["name"] = leak
    save(pilot)
    result = asyncio.run(request(pilot))
    assert envelope(result)["error"]["code"] == "unsafe-output"
    assert leak not in result.content[0].text


@pytest.mark.parametrize("raw", [b"not JSON secret-marker", b"\xff", b"[]", b"{} {}",
                                b'{"exit_code":0,"exit_code":0}', b'{"exit_code":NaN}',
                                b'{"a":' * 33 + b"0" + b"}" * 33])
def test_invalid_child_json_is_sanitized(pilot, raw):
    pilot["values"].update(mode="raw", raw_hex=raw.hex())
    save(pilot)
    result = asyncio.run(request(pilot))
    error = envelope(result)["error"]
    assert error["code"] == "invalid-json"
    assert error["stdout_bytes"] == len(raw) and error["counts_complete"]
    assert "secret-marker" not in result.content[0].text


@pytest.mark.parametrize("mode,code", [("opposite", "unexpected-stream"), ("exit", "child-exit"),
                                       ("stdout-overflow", "output-limit"),
                                       ("stderr-overflow", "output-limit"), ("sleep", "timeout"),
                                       ("ignore-term", "timeout")])
def test_bounded_process_errors_reap_owned_child(pilot, mode, code):
    pilot["values"]["mode"] = mode
    pilot["document"]["limits"].update(stdout_max_bytes=4096, stderr_max_bytes=4096)
    save(pilot)
    result = asyncio.run(request(pilot))
    error = envelope(result)["error"]
    assert error["code"] == code
    assert error["cleanup"] == "reaped" and not error["dispatch_blocked"]
    if mode == "ignore-term":
        assert error["child_exit_code"] == -9
    assert "secret-marker" not in result.content[0].text


async def wait_for_runs(pilot, count=1):
    for _ in range(200):
        if sum(row["argv"][1] == "run" for row in calls(pilot)) >= count:
            return
        await asyncio.sleep(0.01)
    pytest.fail("run child did not start within fixture deadline")


def test_one_inflight_and_public_cancellation(pilot):
    pilot["values"]["mode"] = "sleep"
    pilot["document"]["limits"]["call_timeout_seconds"] = 10
    save(pilot)
    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy") as client:
            pending = asyncio.create_task(client.call_tool("workflows_run", {"workflow": "list-projects"}))
            await wait_for_runs(pilot)
            busy = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(busy)["error"]["code"] == "busy"
            assert_not_started(envelope(busy)["error"])
            assert len(calls(pilot)) == 3
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            # Poll a credential-free call through the public interface, never a PID.
            for _ in range(100):
                result = await client.call_tool("workflows_list", {})
                if not result.is_error:
                    assert envelope(result) == pilot["values"]["list"]
                    break
                assert envelope(result)["error"]["code"] == "busy"
                await asyncio.sleep(0.05)
            else:
                pytest.fail("cancelled owned child did not settle")
    asyncio.run(scenario())


def test_unsettled_stream_refuses_next_dispatch_with_real_child(pilot, monkeypatch):
    original = asyncio.create_subprocess_exec
    async def spawn(*argv, **kwargs):
        process = await original(*argv, **kwargs)
        if argv[2] == "run":
            stream = process.stdout
            class DelayedEOF:
                async def read(self, n):
                    data = await stream.read(n)
                    if data:
                        return data
                    await asyncio.Event().wait()
            process.stdout = DelayedEOF()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            error = envelope(result)["error"]
            assert error["code"] == "cleanup-unresolved" and error["dispatch_blocked"]
            assert error["cleanup"] == "unresolved"
            again = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(again)["error"]["code"] == "cleanup-unresolved"
            assert len(calls(pilot)) == 3
    asyncio.run(scenario())


def test_reader_exception_is_consumed_and_refuses_admission(pilot, monkeypatch, capfd):
    original = asyncio.create_subprocess_exec

    async def spawn(*argv, **kwargs):
        process = await original(*argv, **kwargs)
        if argv[2] == "run":
            class FailedReader:
                async def read(self, _n):
                    raise RuntimeError("fixture-only-secret-marker reader details")
            process.stdout = FailedReader()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario():
        loop = asyncio.get_running_loop()
        diagnostics = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: diagnostics.append(context))
        try:
            server = await create_server(load_profile(pilot["profile"]))
            async with Client(server, mode="legacy", raise_exceptions=True) as client:
                result = await client.call_tool("workflows_run", {"workflow": "list-projects"})
                assert envelope(result)["error"]["code"] == "cleanup-unresolved"
                assert envelope(result)["error"]["counts_complete"] is False
                before = len(calls(pilot))
                again = await client.call_tool("workflows_list", {})
                assert envelope(again)["error"]["dispatch_blocked"] is True
                assert len(calls(pilot)) == before
            await asyncio.sleep(0)
            assert diagnostics == []
        finally:
            loop.set_exception_handler(previous)
    asyncio.run(scenario())
    stderr = capfd.readouterr().err
    assert "Task exception was never retrieved" not in stderr and "Traceback" not in stderr
    assert "fixture-only-secret-marker" not in stderr


def test_missing_executable_and_changed_executable_do_not_dispatch(pilot):
    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        pilot["executable"].write_text("changed executable bytes")
        async with Client(server, mode="legacy") as client:
            changed = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(changed)["error"]["code"] == "executable-changed"
            assert_not_started(envelope(changed)["error"])
            pilot["executable"].unlink()
            missing = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(missing)["error"]["code"] == "executable-unavailable"
            assert_not_started(envelope(missing)["error"])
        assert len(calls(pilot)) == 2
    asyncio.run(scenario())


def test_immutable_profile_and_environment_snapshot(pilot, monkeypatch):
    profile = load_profile(pilot["profile"])
    monkeypatch.setenv("JIRA_AS_TRANSPORT", "simulation")
    pilot["document"]["expected"]["definition_digest"] = "0" * 64
    save(pilot)
    async def scenario():
        server = await create_server(profile)
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(result) == pilot["values"]["run"]
    asyncio.run(scenario())


def test_run_environment_pins_scope_enforcement(pilot, monkeypatch):
    # An operator shell opt-out is not admitted; the pin also outranks any
    # settings file the child could read from its fixed working directory.
    monkeypatch.setenv("JIRA_SCOPE_ENFORCEMENT", "permissive")
    profile = load_profile(pilot["profile"])
    assert profile.environment(run=True)["JIRA_SCOPE_ENFORCEMENT"] == "enforcing"
    assert "JIRA_SCOPE_ENFORCEMENT" not in profile.environment(run=False)


@pytest.mark.parametrize("change", ["extra", "nested", "boolean", "symlink", "permissions", "duplicate"])
def test_operator_profile_fails_closed(pilot, change):
    if change == "extra":
        pilot["document"]["env"] = {"JIRA_API_TOKEN": "no"}
    elif change == "nested":
        pilot["document"]["context"]["scope"]["permit_all"] = True
    elif change == "boolean":
        pilot["document"]["schema_version"] = True
    save(pilot)
    if change == "symlink":
        alias = pilot["profile"].with_name("alias.json")
        alias.symlink_to(pilot["profile"])
        pilot["profile"] = alias
    elif change == "permissions":
        pilot["profile"].chmod(0o666)
    elif change == "duplicate":
        pilot["profile"].write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="^invalid-profile$"):
        load_profile(pilot["profile"])
    assert calls(pilot) == []


def test_real_stdio_initialization_and_serialized_fields(pilot):
    async def scenario():
        params = StdioServerParameters(command=sys.executable,
                                      args=["-m", "as_engine.workflow_mcp", "--profile", str(pilot["profile"])])
        async with Client(params, mode="legacy", read_timeout_seconds=15) as client:
            tools = await client.list_tools()
            wire = tools.model_dump(by_alias=True)
            assert "inputSchema" in wire["tools"][0] and "outputSchema" in wire["tools"][0]
            result = await client.call_tool("workflows_list", {})
            wire = result.model_dump(by_alias=True)
            assert wire["structuredContent"] == envelope(result) == pilot["values"]["list"]
            assert wire["isError"] is False
    asyncio.run(scenario())


def test_ordinary_engine_import_does_not_require_optional_sdk(tmp_path):
    # A fresh interpreter blocks optional imports; no package uninstall or test skip.
    probe = dedent('''\
        import importlib.abc
        import sys
        class Refuse(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in {"mcp", "mcp_types", "jsonschema"}:
                    raise AssertionError("ordinary import loaded optional MCP SDK")
        sys.meta_path.insert(0, Refuse())
        import as_engine
        import as_engine.workflows
        import as_engine.surface
        assert "as_engine.workflow_mcp" not in sys.modules
    ''')
    result = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("dependency", ["mcp", "mcp_types", "jsonschema"])
def test_explicit_module_launch_without_optional_dependency(tmp_path, dependency):
    probe = dedent('''\
        import importlib.abc
        import runpy
        import sys
        missing = sys.argv[1]
        class Refuse(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] == missing:
                    raise ModuleNotFoundError("fixture secret-marker", name=missing)
        sys.meta_path.insert(0, Refuse())
        sys.argv = ["as_engine.workflow_mcp", "--profile", "/not-read.json"]
        runpy.run_module("as_engine.workflow_mcp", run_name="__main__")
    ''')
    result = subprocess.run([sys.executable, "-I", "-c", probe, dependency], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "workflow-mcp: missing dependency; install as-engine[mcp]\n"


@pytest.mark.parametrize("action", ["list", "describe"])
@pytest.mark.parametrize("kind", ["incompatible", "absent-module", "absent-resource"])
def test_bootstrap_product_failure_remains_inspectable_without_run(pilot, catalog, action, kind):
    value = failure_result(catalog.provenance, reason="incompatible-definition-or-runtime")
    if kind != "incompatible":
        # Exact fallback shape in jira_as.cli.commands.workflows_cmds._unavailable;
        # fixture data only, distinct from a missing MCP SDK in this interpreter.
        value.update(schema_version=None, catalog_revision=None, support=None,
                     runtime_capabilities=[])
        if kind == "absent-resource":
            value["definition_digest"] = None
        value["reason"]["message"] = (
            "Workflow metadata or the optional engine capability is unavailable; "
            "check installed package alignment."
        )
    pilot["values"][action] = value
    save(pilot)

    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy", raise_exceptions=True) as client:
            listing = await client.list_tools()
            assert len(listing.tools) == 4
            run = listing.tools[-1]
            assert "enum" not in run.input_schema["properties"]["workflow"]
            assert run.input_schema["properties"]["inputs"]["properties"] == {}
            before = len(calls(pilot))
            for name, args in (
                ("workflows_list", {}), ("workflows_search", {"query": "projects"}),
                ("workflows_describe", {"workflow": "list-projects"}),
                ("workflows_run", {"workflow": "list-projects"}),
            ):
                result = await client.call_tool(name, args)
                assert result.is_error and envelope(result) == value
                tool = next(tool for tool in listing.tools if tool.name == name)
                Draft202012Validator(tool.output_schema).validate(envelope(result))
            assert len(calls(pilot)) == before == (1 if action == "list" else 2)
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["list", "run"])
def test_launch_failure_has_fixed_diagnostic(pilot, monkeypatch, action):
    original = asyncio.create_subprocess_exec

    async def spawn(*argv, **kwargs):
        if argv[2] == action:
            raise OSError("fixture secret-marker executable details")
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(request(pilot))
    error = envelope(result)["error"]
    assert error["code"] == "launch-failed" and error["cleanup"] == "not-started"
    assert error["phase"] == ("bootstrap" if action == "list" else "execute")
    assert len(calls(pilot)) == (0 if action == "list" else 2)
    assert "secret-marker" not in result.content[0].text


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_exact_child_stream_cap_boundary(pilot, catalog, stream, extra):
    value = pilot["values"]["run"] if stream == "stdout" else failure_result(
        catalog.get("list-projects").provenance, code=4, status="blocked", reason="scope-refused"
    )
    if stream == "stderr":
        value.update(view="run", inputs={"limit": 25, "offset": 0}, limit=25, offset=0,
                     evidence={"http_status": 403})
    raw = json.dumps(value).encode()
    cap = 4096
    assert len(raw) < cap - 1
    raw += b" " * (cap + extra - len(raw))
    pilot["values"].update(mode="raw", raw_hex=raw.hex(), raw_stream=stream,
                           raw_exit=value["exit_code"])
    pilot["document"]["limits"].update(stdout_max_bytes=cap, stderr_max_bytes=cap)
    save(pilot)
    result = asyncio.run(request(pilot))
    if extra <= 0:
        assert envelope(result) == value
        assert result.is_error is (stream == "stderr")
    else:
        error = envelope(result)["error"]
        assert error["code"] == "output-limit" and error["cleanup"] == "reaped"
        assert error[stream + "_bytes"] == cap + 1
        assert error[("stderr" if stream == "stdout" else "stdout") + "_bytes"] == 0
        assert error["counts_complete"] is True


@pytest.mark.parametrize("extra", [0, 1])
def test_serialized_result_cap_is_exact_and_never_truncates(pilot, extra):
    value = deepcopy(pilot["values"]["run"])

    def wire_size(name):
        value["items"][0]["name"] = name
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        text = text.replace("<", "\\u003c")
        result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)],
            structured_content=value, is_error=False,
        )
        return len(result.model_dump_json(by_alias=True).encode())

    # Count the SDK's actual serialized result independently of the adapter.
    # '<' expands in escaped text; an accented character permits an odd byte
    # delta while still fitting within the separate 1 MiB child-output bound.
    target = 4 * 1048576 + extra
    base = wire_size("")
    step = wire_size("<") - base
    letters, remainder = divmod(target - base, step)
    suffix = ""
    if remainder % 2:
        letters -= 1
        remainder += step
        suffix = "é"
        remainder -= wire_size(suffix) - base
    assert remainder >= 0 and remainder % 2 == 0
    name = "<" * letters + suffix + "x" * (remainder // 2)
    assert wire_size(name) == target
    assert len(json.dumps(value).encode()) + 1 <= 1048576
    pilot["values"]["run"] = value
    pilot["document"]["limits"]["call_timeout_seconds"] = 10
    save(pilot)
    result = asyncio.run(request(pilot))
    if extra == 0:
        assert not result.is_error and envelope(result) == value
        assert len(result.model_dump_json(by_alias=True).encode()) == target
    else:
        assert result.is_error and envelope(result)["error"]["code"] == "output-limit"
        assert_child_observation(envelope(result)["error"], value)
        assert envelope(result)["error"]["dispatch_blocked"] is False
        assert len(calls(pilot)) == 3
        assert "items" not in envelope(result)
        assert len(result.model_dump_json(by_alias=True).encode()) < 4096


@pytest.mark.parametrize("fault", ["validate-value", "validate-runtime", "serialize-value",
                                    "serialize-runtime", "result-schema"])
def test_postexecution_fallbacks_keep_local_observation(pilot, monkeypatch, fault):
    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        error_type = ValueError if fault.endswith("value") else RuntimeError
        if fault.startswith("validate"):
            def broken_check(*_args):
                raise error_type("fixture-only-secret-marker validation details")
            monkeypatch.setattr(workflow_mcp._Adapter, "_check", broken_check)
        elif fault.startswith("serialize"):
            original = mcp_types.CallToolResult.model_dump_json

            def broken_serialization(result, *args, **kwargs):
                if result.structured_content.get("status") != "adapter-error":
                    raise error_type("fixture-only-secret-marker serialization details")
                return original(result, *args, **kwargs)
            monkeypatch.setattr(mcp_types.CallToolResult, "model_dump_json", broken_serialization)
        else:
            original_valid = workflow_mcp._valid

            def reject_final_schema(schema, value):
                if "$schema" in schema and value.get("status") != "adapter-error":
                    return False
                return original_valid(schema, value)
            monkeypatch.setattr(workflow_mcp, "_valid", reject_final_schema)
        async with Client(server, mode="legacy", raise_exceptions=True) as client:
            result = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            error = envelope(result)["error"]
            assert error["code"] == ("incompatible-output" if fault == "result-schema" else "internal-error")
            assert_child_observation(error, pilot["values"]["run"])
            assert "fixture-only-secret-marker" not in result.content[0].text
            assert len(calls(pilot)) == 3
            invalid = await client.call_tool("workflows_run", {"workflow": "list-projects", "env": {}})
            assert envelope(invalid)["error"]["code"] == "invalid-input"
            assert_not_started(envelope(invalid)["error"])
            assert len(calls(pilot)) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["list", "describe"])
def test_bootstrap_catalog_rejection_preserves_its_child_evidence(pilot, action):
    value = pilot["values"][action]
    if action == "list":
        value["complete"] = False
    else:
        value["inputs"]["limit"]["default"] = 24
        value["input_bounds"]["limit"]["default"] = 24
    save(pilot)

    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy", raise_exceptions=True) as client:
            result = await client.call_tool("workflows_list", {})
            error = envelope(result)["error"]
            assert error["code"] == "incompatible-catalog" and error["dispatch_blocked"] is True
            assert_child_observation(error, value, phase="bootstrap")
            before = calls(pilot)
            assert len(before) == (1 if action == "list" else 2)
            again = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            assert envelope(again) == envelope(result)
            assert calls(pilot) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("evidence_kind", ["partial", "pre-items", "http", "empty"])
def test_unknown_and_failure_evidence_is_preserved_on_public_wire(pilot, catalog, evidence_kind):
    value = deepcopy(pilot["values"]["run"])
    value.update(status="unknown", exit_code=1, complete=None, continuation=None)
    value["reason"] = failure_result(catalog.provenance, reason="malformed-page")["reason"]
    value["next_actions"] = [{"action": "inspect-or-retry", "workflow": "list-projects",
                              "inputs": value["inputs"]}]
    if evidence_kind == "partial":
        # Range measures received rows, not retained valid identities. Invalid
        # provider metadata is present but its unsafe value has been omitted.
        value["range"]["end"] = 3
        value["evidence"].update(received_count=3, omitted_count=2,
                                 metadata_present=["offset", "total"], metadata={"offset": 0})
    elif evidence_kind == "pre-items":
        value.update(items=[], returned_count=0, range=None)
        value["evidence"].update(received_count=None, omitted_count=None)
        del value["evidence"]["metadata_present"]
        del value["evidence"]["metadata"]
    else:
        value = failure_result(catalog.get("list-projects").provenance,
                               code=6, status="failed", reason="transport-failed")
        value.update(inputs={"limit": 25, "offset": 0}, limit=25, offset=0, view="run")
        value["evidence"] = {"http_status": None} if evidence_kind == "http" else {}
    pilot["values"]["run"] = value
    save(pilot)

    async def scenario():
        server = await create_server(load_profile(pilot["profile"]))
        async with Client(server, mode="legacy") as client:
            listing = await client.list_tools()
            result = await client.call_tool("workflows_run", {"workflow": "list-projects"})
            wire = result.model_dump(by_alias=True)
            assert wire["isError"] is True and wire["structuredContent"] == value
            Draft202012Validator(listing.tools[-1].output_schema).validate(envelope(result))
    asyncio.run(scenario())


def test_real_stdio_raw_duplicate_keys_record_pinned_sdk_semantics(pilot, record_property):
    # SDK 2.2.0 stdio hands raw JSON to Pydantic validate_json. This test pins
    # last-key-wins at that boundary, not an adapter claim to recover lost keys.
    # Supervisor execution is the observation gate for this source-authored case.
    async def scenario():
        # Four fixed tools inline their closed success/error schemas, with two
        # describe views: the advertised tools/list frame exceeds asyncio's
        # default 64 KiB. Give that schema frame a separate 256 KiB test budget.
        tools_list_max_bytes = 256 * 1024
        # _result caps CallToolResult alone at 4 MiB, not tools/list or the
        # enclosing JSON-RPC frame. Reserve 4 KiB for this fixture's RPC wrapper.
        wire_max_bytes = max(tools_list_max_bytes, 4 * 1048576 + 4096)
        frame_sizes = {}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "as_engine.workflow_mcp", "--profile", str(pilot["profile"]),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=wire_max_bytes - 1,
        )
        try:
            async def exchange(frame, request_id, response_max_bytes=wire_max_bytes):
                process.stdin.write(frame + b"\n")
                await process.stdin.drain()
                for _ in range(10):
                    # Overflow remains a failing readline/length check; do not
                    # retry, discard a partial frame or grow the limit on error.
                    line = await asyncio.wait_for(process.stdout.readline(), 15)
                    assert line, "stdio closed before response"
                    assert line.endswith(b"\n"), "incomplete JSON-RPC frame"
                    assert len(line) <= response_max_bytes, "JSON-RPC frame exceeds test budget"
                    value = json.loads(line)
                    if value.get("id") == request_id:
                        frame_sizes[request_id] = len(line)
                        return value
                pytest.fail("response not found in bounded wire frames")

            initialized = await exchange(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": mcp_types.LATEST_PROTOCOL_VERSION,
                           "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}},
            }).encode(), 1)
            # SDK 2.2.0 runner counter-offers its latest handshake revision for
            # the 2026 per-request revision; types' overall latest is not an
            # initialize-handshake version. Pin the valid negotiated wire value.
            assert initialized["result"]["protocolVersion"] == LATEST_HANDSHAKE_VERSION == "2025-11-25"
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await process.stdin.drain()
            listed = await exchange(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
                                    2, tools_list_max_bytes)
            record_property("raw_stdio_tools_list_frame_bytes", frame_sizes[2])
            assert [row["name"] for row in listed["result"]["tools"]] == [
                "workflows_list", "workflows_search", "workflows_describe", "workflows_run",
            ]
            assert all("inputSchema" in row and "outputSchema" in row
                       for row in listed["result"]["tools"])
            tool = listed["result"]["tools"][0]
            assert tool["name"] == "workflows_list"
            response = await exchange(
                b'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
                b'"params":{"name":"workflows_list","arguments":{"offset":1,"offset":0}}}', 3
            )
            value = response["result"]
            assert value["isError"] is False
            assert value["structuredContent"] == pilot["values"]["list"]
            assert json.loads(value["content"][0]["text"]) == value["structuredContent"]
            Draft202012Validator(tool["outputSchema"]).validate(value["structuredContent"])
            assert calls(pilot)[-1]["argv"] == ["workflows", "list", "--offset=0", "--format=json"]
            assert len(calls(pilot)) == 3
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 15)
            assert process.returncode == 0
            assert await process.stderr.read() == b""
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["before-create", "after-create"])
@pytest.mark.parametrize("late", [False, True])
def test_public_cancellation_retains_unsettled_spawn_and_late_handle(pilot, monkeypatch, capfd, stage, late):
    pilot["values"]["mode"] = "sleep"
    pilot["document"]["limits"]["call_timeout_seconds"] = 10
    save(pilot)
    original = asyncio.create_subprocess_exec

    async def scenario():
        entered, release, transferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        handles = []
        launches = []
        diagnostics = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: diagnostics.append(context))

        async def spawn(*argv, **kwargs):
            if argv[2] != "run":
                return await original(*argv, **kwargs)
            launches.append(argv)
            if stage == "before-create":
                entered.set()
                await release.wait()
            process = await original(*argv, **kwargs)
            handles.append(process)
            if stage == "after-create":
                entered.set()
                await release.wait()
            transferred.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        try:
            server = await create_server(load_profile(pilot["profile"]))
            async with Client(server, mode="legacy", raise_exceptions=True) as client:
                pending = asyncio.create_task(client.call_tool("workflows_run", {"workflow": "list-projects"}))
                await asyncio.wait_for(entered.wait(), 3)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                if not late:
                    release.set()
                for _ in range(140):
                    result = await client.call_tool("workflows_list", {})
                    if late and envelope(result).get("error", {}).get("code") == "cleanup-unresolved":
                        assert envelope(result)["error"]["dispatch_blocked"] is True
                        break
                    if not late and not result.is_error:
                        break
                    assert envelope(result)["error"]["code"] == "busy"
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("spawn cancellation did not reach bounded public state")
                release.set()
                await asyncio.wait_for(transferred.wait(), 3)
                for process in handles:
                    await asyncio.wait_for(process.wait(), 5)
                    assert process.returncode is not None
                if late:
                    # Reaping a late handle never automatically reopens admission.
                    before = len(calls(pilot))
                    again = await client.call_tool("workflows_run", {"workflow": "list-projects"})
                    assert envelope(again)["error"]["code"] == "cleanup-unresolved"
                    assert envelope(again) == envelope(result)
                    assert len(calls(pilot)) == before
                assert len(launches) == 1
            await asyncio.sleep(0)
            assert diagnostics == []
        finally:
            release.set()
            for process in handles:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            loop.set_exception_handler(previous)
    asyncio.run(scenario())
    stderr = capfd.readouterr().err
    assert "Task exception was never retrieved" not in stderr
    assert "Traceback" not in stderr and "fixture-only-secret-marker" not in stderr
