"""Pure product-owned catalogs and one bounded, guarded indexed-read binding.

Discovery imports only standard-library modules. Execution uses the existing
Surface and never supplies policy overrides, follows links, or aggregates pages.
See docs/workflows.md for schema 1 and the adapter/error contract.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from .index import OperationIndex, ProductIndexes
    from .surface import Surface

SCHEMA_VERSION = 1
CAPABILITIES = frozenset({"indexed-read-v1"})
_MISSING = object()
_ID = re.compile(r"[a-z][a-z0-9-]*\Z")
_WORDS = re.compile(r"\w+", re.UNICODE)
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "show",
        "that",
        "the",
        "their",
        "this",
        "to",
        "what",
        "which",
        "with",
        "you",
    ]
)
_INCOMPATIBLE = "incompatible-definition-or-runtime"
_REASONS = {
    _INCOMPATIBLE: "The definition and installed runtime/index are incompatible; check package alignment.",
    "unsupported-workflow": "This catalog does not support that workflow; search the catalog.",
    "invalid-input": "Supply only the declared integer inputs within their bounds.",
    "invalid-discovery-input": "Use a nonempty query of at most 512 characters and a nonnegative offset.",
    "query-output-too-large": "The escaped query and search result exceed the discovery output budget; retry with a shorter query.",
    "catalog-read": "Support is declared; account, configuration and scope have not been checked.",
    "scope-refused": "Current scope or account permission blocks this read.",
    "authentication-failed": "Authentication failed; inspect operator credentials.",
    "configuration-or-parameters": "Configuration, credentials or indexed parameters prevent this read.",
    "resource-not-found": "The resource was not found; this is not evidence of an empty result.",
    "transport-failed": "The transport failed or exhausted its existing read retry policy.",
    "conflict": "The service reported a conflict; inspect before retrying.",
    "read-failed": "The read failed; inspect the configuration and service before retrying.",
    "malformed-page": "The page is malformed, contradictory or oversized; inspect before retrying.",
    "no-progress": "The service reports more results but returned no items; no continuation is safe.",
    "completion-unknown": "No completion signal establishes whether another page follows this range.",
    "offset-unestablished": "More results exist, but response offset evidence is absent; inspect or retry.",
    "continuation-out-of-bounds": "More results exist, but the next offset exceeds the declared bounds.",
    "more-results": "Another page follows this range; continuation advances by the returned count.",
    "final-page": "Consistent evidence establishes that no page follows this returned range.",
}


class WorkflowError(ValueError):
    """A sanitized, JSON-ready failure for adapters to render and exit with."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(result["reason"]["message"])


def _reason(code: str) -> dict[str, str]:
    return {"code": code, "message": _REASONS[code]}


def _engine_version() -> str:
    try:
        return version("as-engine")
    except PackageNotFoundError:
        return "unknown"


def _base(provenance: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **provenance,
        "support": True,
        "availability": "unknown",
        "status": "completed-read",
        "exit_code": 0,
        "inputs": {},
        "items": [],
        "returned_count": 0,
        "limit": None,
        "offset": None,
        "range": None,
        "complete": None,
        "continuation": None,
        "evidence": {},
        "reason": _reason("catalog-read"),
        "next_actions": [],
    }


def failure_result(
    provenance: Mapping[str, Any],
    *,
    code: int = 2,
    reason: str = "configuration-or-parameters",
    status: str = "blocked",
    support: bool = True,
) -> dict[str, Any]:
    """Build a safe adapter failure without including exception/server text.

    Callers use known reason codes from this module, never raw exception messages.
    A product's absent-module fallback must construct its own equivalent envelope.
    """
    result = _base(provenance)
    result.update(status=status, exit_code=code, support=support, reason=_reason(reason))
    result["availability"] = "blocked" if status == "blocked" else "unknown"
    result["next_actions"] = [{"action": "inspect-configuration-or-definition"}]
    return result


def _require(condition: Any) -> None:
    if not condition:
        raise ValueError("incompatible workflow definition")


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    _require(isinstance(value, dict))
    _require(required <= value.keys() and value.keys() <= required | (optional or set()))


def _text(value: Any, maximum: int = 512) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _parts(path: Any) -> list[str]:
    _require(isinstance(path, str) and path.startswith("/") and len(path) <= 256)
    _require(not re.search(r"~(?![01])", path))
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    _require(all(parts))
    return parts


def _pointer(value: Any, path: str) -> Any:
    for part in _parts(path):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _path(value: Any) -> bool:
    return (
        _text(value, 256)
        and value.startswith("/")
        and not value.startswith("//")
        and not any(c.isspace() or ord(c) < 32 or c in "?#\\" for c in value)
        and all(part not in {".", ".."} for part in value.split("/"))
    )


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-JSON numeric constant")


def _validate_definition(data: Any) -> None:
    _keys(
        data,
        {
            "id",
            "revision",
            "requires",
            "title",
            "purpose",
            "search_terms",
            "inputs",
            "prerequisites",
            "binding",
            "projection",
            "paging",
            "examples",
        },
        {"annotations"},
    )
    _require(isinstance(data["id"], str) and _ID.fullmatch(data["id"]) and len(data["id"]) <= 80)
    _require(_integer(data["revision"], 1))
    _require(data["requires"] == ["indexed-read-v1"])
    _require(set(data["requires"]) <= CAPABILITIES)
    _require(_text(data["title"], 120) and _text(data["purpose"], 512))
    for field in ("search_terms", "prerequisites"):
        _require(isinstance(data[field], list) and 1 <= len(data[field]) <= 20)
        _require(all(_text(item, 256) for item in data[field]))
    if "annotations" in data:
        _require(isinstance(data["annotations"], dict))
    _keys(data["inputs"], {"limit", "offset"})
    for name, rule in data["inputs"].items():
        _keys(rule, {"type", "default", "minimum", "maximum"}, {"description"})
        _require(rule["type"] == "integer")
        _require(all(_integer(rule[k]) for k in ("default", "minimum", "maximum")))
        _require(rule["minimum"] <= rule["default"] <= rule["maximum"])
        if "description" in rule:
            _require(_text(rule["description"], 256))
        if name == "limit":
            _require(rule["minimum"] == 1 and rule["default"] == 25 and rule["maximum"] == 100)
        else:
            _require(rule["minimum"] == rule["default"] == 0 and rule["maximum"] >= 100)
    binding = data["binding"]
    _keys(
        binding,
        {
            "kind",
            "document",
            "operation_id",
            "method",
            "path",
            "fixed_parameters",
            "input_parameters",
            "parameter_schemas",
            "scope",
        },
    )
    _require(binding["kind"] == "indexed-read" and binding["method"] == "GET")
    _require(_text(binding["document"], 80) and _text(binding["operation_id"], 160))
    _require(_path(binding["path"]) and "{" not in binding["path"] and "}" not in binding["path"])
    _keys(binding["input_parameters"], {"limit", "offset"})
    names = list(binding["input_parameters"].values())
    _require(all(_text(name, 80) for name in names) and len(set(names)) == 2)
    fixed = binding["fixed_parameters"]
    _require(isinstance(fixed, dict) and len(fixed) <= 20)
    _require(
        all(_text(k, 80) and (_text(v, 256) or type(v) in (int, bool)) for k, v in fixed.items())
    )
    _require(not set(names).intersection(fixed))
    descriptors = binding["parameter_schemas"]
    _keys(descriptors, set(names) | fixed.keys())
    for descriptor in descriptors.values():
        _keys(descriptor, {"in", "schema"})
        _require(descriptor["in"] == "query" and isinstance(descriptor["schema"], dict))
    for name in names:
        _require(descriptors[name]["schema"].get("type") == "integer")
    # This capability requires direct scope, with no resolver/lookup procedure.
    scope = binding["scope"]
    _require(isinstance(scope, dict))
    if scope.get("in") == "site":
        _keys(scope, {"in"})
    else:
        _keys(scope, {"in", "name"})
        _require(scope["in"] == "query" and scope["name"] in fixed)
    projection = data["projection"]
    _keys(projection, {"id", "key", "name", "canonical_url"})
    for field in ("id", "key", "name"):
        _parts(projection[field])
    _require(len({projection[field] for field in ("id", "key", "name")}) == 3)
    link = projection["canonical_url"]
    _keys(link, {"pointer", "path"})
    _parts(link["pointer"])
    _require(_path(link["path"]) and link["path"].endswith("/{identity}"))
    _require(link["path"].count("{") == link["path"].count("}") == 1)
    paging = data["paging"]
    _keys(paging, {"tag", "evidence"})
    tag = paging["tag"]
    _keys(tag, {"style", "itemsPath", "request", "response"})
    _require(tag["style"] == "offset/limit")
    _parts(tag["itemsPath"])
    _keys(tag["request"], {"limit", "offset"})
    for name in ("limit", "offset"):
        _require(tag["request"][name] == {"in": "query", "name": binding["input_parameters"][name]})
    _keys(tag["response"], set(), {"totalPath", "isLastPath", "offsetPath", "limitPath"})
    evidence = paging["evidence"]
    _keys(evidence, {"offset", "limit"}, {"total", "is_last"})
    _require("total" in evidence or "is_last" in evidence)
    for path in evidence.values():
        _parts(path)
    _require(len(set(evidence.values())) == len(evidence))
    for role, key in {
        "total": "totalPath",
        "is_last": "isLastPath",
        "offset": "offsetPath",
        "limit": "limitPath",
    }.items():
        if key in tag["response"]:
            _require(tag["response"][key] == evidence.get(role))
    _require(isinstance(data["examples"], list) and 1 <= len(data["examples"]) <= 10)
    for example in data["examples"]:
        _keys(example, {"kind", "value"}, {"schema"})
        _require(example["kind"] in {"invocation", "json"} and _text(example["value"], 1200))


@dataclass(frozen=True)
class Definition:
    """Detached serialized definition and provenance returned by Catalog.get.

    Treat this as a catalog value, not a way to fabricate execution authority.
    Reading data/provenance returns new objects and cannot mutate the catalog.
    """

    _data: str
    _provenance: str

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self._data)

    @property
    def provenance(self) -> dict[str, Any]:
        return json.loads(self._provenance)


class Catalog:
    """Validated installed definition bytes; no config/index/transport access."""

    def __init__(self, provenance: dict[str, Any], definitions: dict[str, Definition]):
        self._provenance = _json(provenance)
        self._definitions = dict(definitions)

    @property
    def provenance(self) -> dict[str, Any]:
        return json.loads(self._provenance)

    @classmethod
    def load(cls, raw: bytes, *, product: str, product_version: str) -> Catalog:
        provenance: dict[str, Any] = {
            "product": product,
            "product_version": product_version,
            "engine_version": _engine_version(),
            "schema_version": SCHEMA_VERSION,
            "runtime_capabilities": sorted(CAPABILITIES),
            "required_capabilities": [],
            "required_schema_version": None,
            "definition_digest": None,
            "catalog_revision": None,
            "workflow": None,
            "revision": None,
        }
        try:
            _require(isinstance(raw, bytes) and len(raw) <= 1_048_576)
            provenance["definition_digest"] = hashlib.sha256(raw).hexdigest()
            data = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            _keys(
                data,
                {"schema_version", "product", "product_version", "revision", "workflows"},
                {"annotations"},
            )
            if type(data["schema_version"]) is int:
                provenance["required_schema_version"] = data["schema_version"]
            _require(
                type(data["schema_version"]) is int and data["schema_version"] == SCHEMA_VERSION
            )
            _require(_text(product, 80) and _ID.fullmatch(product))
            _require(
                _text(product_version, 80)
                and data["product"] == product
                and data["product_version"] == product_version
            )
            _require(_integer(data["revision"], 1))
            if "annotations" in data:
                _require(isinstance(data["annotations"], dict))
            provenance["catalog_revision"] = data["revision"]
            _require(isinstance(data["workflows"], list) and 1 <= len(data["workflows"]) <= 1000)
            required = [
                cap
                for row in data["workflows"]
                if isinstance(row, dict)
                for cap in row.get("requires", [])
                if isinstance(cap, str)
            ]
            provenance["required_capabilities"] = sorted(set(required))
            _require(set(required) <= CAPABILITIES)
            definitions = {}
            for row in data["workflows"]:
                _validate_definition(row)
                _require(row["id"] not in definitions)
                identity = {
                    **provenance,
                    "workflow": row["id"],
                    "revision": row["revision"],
                    "required_capabilities": row["requires"],
                    "input_bounds": row["inputs"],
                }
                definitions[row["id"]] = Definition(_json(row), _json(identity))
            catalog = cls(provenance, definitions)
            # A single entry, description or examples page must never be silently cut.
            for definition in definitions.values():
                for examples, cap in ((False, 4800), (True, 2400)):
                    _require(
                        len(
                            render_result(
                                catalog.describe(definition.data["id"], examples=examples)
                            )
                            + "\n"
                        )
                        <= cap
                    )
                _require(len(render_result(catalog._page([definition], 0, None, 1)) + "\n") <= 3200)
            return catalog
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise WorkflowError(failure_result(provenance, reason=_INCOMPATIBLE)) from None

    def get(self, workflow: str) -> Definition:
        if not isinstance(workflow, str) or workflow not in self._definitions:
            result = failure_result(
                self.provenance, reason="unsupported-workflow", status="needs-input", support=False
            )
            result["next_actions"] = [{"action": "search-catalog"}]
            raise WorkflowError(result)
        return self._definitions[workflow]

    def hint(self) -> str:
        p = self.provenance
        return (
            f"Workflows ({p['product']} {p['product_version']}, catalog r{p['catalog_revision']}, "
            'indexed-read-v1): Start: workflows search "task" --format json; '
            "workflows describe ID includes a run example."
        )

    def describe(self, workflow: str, *, examples: bool = False) -> dict[str, Any]:
        try:
            definition = self.get(workflow)
        except WorkflowError as exc:
            return exc.result
        result = _base(definition.provenance)
        data = definition.data
        result["view"] = "examples" if examples else "describe"
        if examples:
            result["examples"] = data["examples"]
        else:
            result.update(
                {
                    key: data[key]
                    for key in (
                        "title",
                        "purpose",
                        "search_terms",
                        "inputs",
                        "prerequisites",
                        "binding",
                        "projection",
                        "paging",
                    )
                }
            )
            for example in data["examples"]:
                if example["kind"] == "invocation":
                    result["examples"] = [example]
                    break
        return result

    def list(self, *, offset: int = 0) -> dict[str, Any]:
        return self._discover(None, offset)

    def search(self, query: str, *, offset: int = 0) -> dict[str, Any]:
        if not _text(query, 512):
            return failure_result(
                self.provenance, reason="invalid-discovery-input", status="needs-input"
            )
        return self._discover(query, offset)

    def _discover(self, query: str | None, offset: int) -> dict[str, Any]:
        if not _integer(offset):
            return failure_result(
                self.provenance, reason="invalid-discovery-input", status="needs-input"
            )
        definitions = sorted(self._definitions.values(), key=lambda item: item.data["id"])
        if query is not None:
            words = set(_WORDS.findall(query.casefold())) - _STOP_WORDS
            ranked = []
            for definition in definitions:
                data = definition.data
                vocabulary = set(
                    _WORDS.findall(
                        " ".join(
                            [
                                data["title"],
                                data["purpose"],
                                *data["search_terms"],
                            ]
                        ).casefold()
                    )
                )
                score = len(words & vocabulary)
                if score:
                    ranked.append((-score, data["id"], definition))
            definitions = [item[2] for item in sorted(ranked)]
        if offset > len(definitions):
            return failure_result(
                self.provenance, reason="invalid-discovery-input", status="needs-input"
            )
        count = 0
        result = self._page(definitions, offset, query, count)
        for count in range(1, len(definitions) - offset + 1):
            candidate = self._page(definitions, offset, query, count)
            if len(render_result(candidate) + "\n") > 3200:
                if query is not None:
                    # A final page drops continuation's repeated query, so it
                    # can fit even when a smaller nonfinal candidate cannot.
                    final = self._page(definitions, offset, query, len(definitions) - offset)
                    if len(render_result(final) + "\n") <= 3200:
                        result = final
                        break
                if not result["entries"]:
                    result = candidate
                break
            result = candidate
        # Include empty matches and continuation's repeated query in the budget.
        # A zero-entry provisional page can exceed the cap even when a final
        # one-entry page fits, so check the selected envelope after pagination.
        if len(render_result(result) + "\n") > 3200:
            if query is None:
                return failure_result(self.provenance, reason=_INCOMPATIBLE)
            result = failure_result(
                self.provenance, reason="query-output-too-large", status="needs-input"
            )
            result["next_actions"] = [{"action": "shorten-query"}]
        return result

    def _page(
        self, definitions: Sequence[Definition], offset: int, query: str | None, count: int
    ) -> dict[str, Any]:
        result = _base(self.provenance)
        entries = [
            {
                **{key: d.data[key] for key in ("id", "revision", "title", "purpose")},
                "support": True,
                "availability": "unknown",
            }
            for d in definitions[offset : offset + count]
        ]
        complete = offset + len(entries) >= len(definitions)
        result.update(
            view="search" if query is not None else "list",
            entries=entries,
            returned_count=len(entries),
            offset=offset,
            complete=complete,
        )
        if query is not None:
            result["query"] = query
        if not complete:
            result["continuation"] = {"action": result["view"], "offset": offset + len(entries)}
            if query is not None:
                result["continuation"]["query"] = query
        return result


def normalize_inputs(definition: Definition, inputs: Mapping[str, Any]) -> dict[str, int]:
    """Accept only already parsed integers. CLI duplicate-option checks stay in argv."""
    try:
        _require(isinstance(inputs, Mapping) and not set(inputs) - {"limit", "offset"})
        rules = definition.data["inputs"]
        values = {}
        for name, rule in rules.items():
            value = inputs.get(name, rule["default"])
            _require(type(value) is int and rule["minimum"] <= value <= rule["maximum"])
            values[name] = value
        return values
    except (ValueError, TypeError, KeyError):
        raise WorkflowError(
            failure_result(definition.provenance, reason="invalid-input", status="needs-input")
        ) from None


def _schema(
    schema: Any, index: OperationIndex, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    _require(isinstance(schema, dict))
    if "$ref" in schema:
        ref = schema["$ref"]
        _require(
            isinstance(ref, str) and ref.startswith("#/components/schemas/") and ref not in seen
        )
        name = ref[len("#/components/schemas/") :].replace("~1", "/").replace("~0", "~")
        return _schema(index.schemas.get(name), index, seen | {ref})
    # This first capability requires unambiguous property types, not union inference.
    _require(not any(key in schema for key in ("oneOf", "anyOf", "allOf")))
    return schema


def _schema_at(schema: Any, path: str, index: OperationIndex) -> dict[str, Any]:
    current = _schema(schema, index)
    for part in _parts(path):
        _require(current.get("type") == "object")
        current = _schema(current.get("properties", {}).get(part), index)
    return current


def _parameters(data: dict[str, Any], inputs: dict[str, int]) -> dict[str, Any]:
    binding = data["binding"]
    return {
        **binding["fixed_parameters"],
        **{binding["input_parameters"][key]: value for key, value in inputs.items()},
    }


def validate_binding(definition: Definition, indexes: ProductIndexes) -> None:
    """Check the current installed index with no Surface/config/factory construction.

    Exact canonical resolution must agree with Surface's subsequent resolver.
    Schemas, direct scope and the complete paging tag are pinned by the product.
    """
    from .params import validate_parameters

    try:
        data = definition.data
        _validate_definition(data)
        binding = data["binding"]
        document, index, operation = indexes.find(binding["operation_id"])
        _require(
            document == binding["document"] and operation.operationId == binding["operation_id"]
        )
        _require(operation.method == "GET" and operation.path == binding["path"])
        _require(not operation.requestBody and not operation.request_body_required)
        _require(operation.extensions.get("x-as-scope") == binding["scope"])
        _require(_json(operation.extensions.get("x-as-paging")) == _json(data["paging"]["tag"]))
        # These tags can introduce lookup calls, a body, file I/O or representation
        # changes. They need a separately supported capability, never silent reuse.
        _require(
            not any(
                tag in operation.extensions
                for tag in (
                    "x-as-prerequisites",
                    "x-as-version",
                    "x-as-format",
                    "x-as-richtext",
                    "x-as-response",
                )
            )
        )
        descriptors = binding["parameter_schemas"]
        for name, descriptor in descriptors.items():
            matches = [p for p in operation.parameters if p.get("name") == name]
            _require(len(matches) == 1 and matches[0].get("in") == descriptor["in"])
            parameter = matches[0]
            schema = parameter.get("schema")
            if not isinstance(schema, dict):
                schema = {
                    key: parameter[key]
                    for key in (
                        "type",
                        "enum",
                        "minimum",
                        "maximum",
                        "items",
                        "properties",
                    )
                    if key in parameter
                }
            _require(_json(schema) == _json(descriptor["schema"]))
        defaults = {name: rule["default"] for name, rule in data["inputs"].items()}
        for name, rule in data["inputs"].items():
            schema = descriptors[binding["input_parameters"][name]]["schema"]
            if schema.get("format") in {"int32", "int64"}:
                bits = 32 if schema["format"] == "int32" else 64
                _require(rule["maximum"] <= 2 ** (bits - 1) - 1)
            for bound in ("minimum", "maximum"):
                values = _parameters(data, {**defaults, name: rule[bound]})
                _require(validate_parameters(operation, values, index.schemas) == values)
        response = operation.response_schema
        if response is None and operation.response_200 is not None:
            response = index.schemas.get(operation.response_200)
        response = _schema(response, index)
        _require(response.get("type") == "object")
        items = _schema_at(response, data["paging"]["tag"]["itemsPath"], index)
        _require(items.get("type") == "array")
        item_schema = _schema(items.get("items"), index)
        for name in ("id", "key", "name"):
            _require(
                _schema_at(item_schema, data["projection"][name], index).get("type") == "string"
            )
        link = data["projection"]["canonical_url"]
        _require(_schema_at(item_schema, link["pointer"], index).get("type") == "string")
        for name, path in data["paging"]["evidence"].items():
            expected = "boolean" if name == "is_last" else "integer"
            _require(_schema_at(response, path, index).get("type") == expected)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
        raise WorkflowError(failure_result(definition.provenance, reason=_INCOMPATIBLE)) from None


def _canonical_url(raw: Any, rule: dict[str, str], item: dict[str, Any]) -> str | None:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    decoded = unquote(raw)
    if re.search(r"%(?![0-9a-fA-F]{2})", raw):
        return None
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "\\?#" for c in decoded):
        return None
    try:
        url = urlsplit(raw)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            return None
        if url.port is not None and not 1 <= url.port <= 65535:
            return None
        if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", url.netloc):
            return None
        host = url.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.rstrip(".").split(".")
        ):
            return None
        expected = {rule["path"].replace("{identity}", item[key]) for key in ("id", "key")}
        if unquote(url.path) not in expected:
            return None
        return raw
    except ValueError:
        return None


def _page_result(definition: Definition, inputs: dict[str, int], body: Any) -> dict[str, Any]:
    data = definition.data
    result = _base(definition.provenance)
    limit, offset = inputs["limit"], inputs["offset"]
    result.update(inputs=inputs, limit=limit, offset=offset, availability="available", view="run")
    evidence: dict[str, Any] = {
        "binding": "indexed-read",
        "document": data["binding"]["document"],
        "operation_id": data["binding"]["operation_id"],
        "method": "GET",
        "received_count": None,
        "omitted_count": None,
        "coverage": "Only an offset0-to-final traversal covers the full set; reads are not a transactional snapshot.",
    }
    result["evidence"] = evidence

    def finish(reason: str, *, unknown: bool = False) -> dict[str, Any]:
        result["reason"] = _reason(reason)
        if unknown:
            result.update(status="unknown", exit_code=1, complete=None, continuation=None)
        if result["complete"] is not True and result["continuation"] is None:
            result["next_actions"] = [
                {"action": "inspect-or-retry", "workflow": data["id"], "inputs": inputs}
            ]
        return result

    if not isinstance(body, dict):
        return finish("malformed-page", unknown=True)
    rows = _pointer(body, data["paging"]["tag"]["itemsPath"])
    if not isinstance(rows, list):
        return finish("malformed-page", unknown=True)
    evidence["received_count"] = len(rows)
    malformed = len(rows) > limit
    items = []
    ids: set[str] = set()
    keys: set[str] = set()
    for row in rows[:limit]:
        item = {field: _pointer(row, data["projection"][field]) for field in ("id", "key", "name")}
        valid = all(isinstance(v, str) and bool(v.strip()) for v in item.values())
        if valid:
            valid = all(
                not any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in item[k])
                for k in ("id", "key")
            )
        if not valid or item["id"] in ids or item["key"] in keys:
            malformed = True
            continue
        ids.add(item["id"])
        keys.add(item["key"])
        rule = data["projection"]["canonical_url"]
        item["url"] = _canonical_url(_pointer(row, rule["pointer"]), rule, item)
        item["url_source"] = "provider-self" if item["url"] else None
        items.append(item)
    evidence["omitted_count"] = len(rows) - len(items)
    result.update(
        items=items, returned_count=len(items), range={"start": offset, "end": offset + len(rows)}
    )
    metadata = {role: _pointer(body, path) for role, path in data["paging"]["evidence"].items()}
    evidence["metadata_present"] = [key for key, value in metadata.items() if value is not _MISSING]
    # Only validated numeric/boolean evidence is retained; never echo arbitrary provider metadata.
    evidence["metadata"] = {}
    for role, value in metadata.items():
        if value is _MISSING:
            continue
        valid = (
            type(value) is bool
            if role == "is_last"
            else _integer(value, 1 if role == "limit" else 0)
        )
        if not valid:
            malformed = True
        else:
            evidence["metadata"][role] = value
    if malformed:
        return finish("malformed-page", unknown=True)
    start, size = metadata.get("offset", _MISSING), metadata.get("limit", _MISSING)
    total, last = metadata.get("total", _MISSING), metadata.get("is_last", _MISSING)
    end = offset + len(rows)
    if (
        start is not _MISSING
        and start != offset
        or size is not _MISSING
        and not len(rows) <= size <= limit
        or total is not _MISSING
        and total < end
    ):
        return finish("malformed-page", unknown=True)
    signals = []
    if total is not _MISSING:
        signals.append(total == end)
    if last is not _MISSING:
        signals.append(last)
    if signals and any(signal != signals[0] for signal in signals):
        return finish("malformed-page", unknown=True)
    if not signals:
        return finish("completion-unknown")
    if signals[0]:
        result["complete"] = True
        return finish("final-page")
    if not rows:
        return finish("no-progress", unknown=True)
    result["complete"] = False
    if start is _MISSING:
        return finish("offset-unestablished")
    if end > data["inputs"]["offset"]["maximum"]:
        return finish("continuation-out-of-bounds")
    result["continuation"] = {"workflow": data["id"], "inputs": {**inputs, "offset": end}}
    result["next_actions"] = [{"action": "continue", **result["continuation"]}]
    return finish("more-results")


def run(definition: Definition, inputs: Mapping[str, Any], surface: Surface) -> dict[str, Any]:
    """Perform one logical indexed GET through the caller's current guarded Surface.

    Return a result for expected input/index/Surface failures. Unexpected coding
    errors are not caught as a successful compatibility fallback.
    """
    from .errors import SurfaceError

    try:
        normalized = normalize_inputs(definition, inputs)
    except WorkflowError as exc:
        exc.result["view"] = "run"
        return exc.result
    try:
        validate_binding(definition, surface.indexes)
    except WorkflowError as exc:
        exc.result.update(
            inputs=normalized, limit=normalized["limit"], offset=normalized["offset"], view="run"
        )
        return exc.result
    try:
        data = definition.data
        response = surface.call(
            data["binding"]["operation_id"], _parameters(data, normalized), None
        )
    except SurfaceError as exc:
        reason = {
            2: "configuration-or-parameters",
            3: "authentication-failed",
            4: "scope-refused",
            5: "resource-not-found",
            6: "transport-failed",
            7: "conflict",
        }.get(exc.code, "read-failed")
        result = failure_result(
            definition.provenance,
            code=exc.code,
            reason=reason,
            status="blocked" if exc.code in {2, 3, 4} else "failed",
        )
        result.update(
            inputs=normalized, limit=normalized["limit"], offset=normalized["offset"], view="run"
        )
        result["evidence"] = {"http_status": exc.status}
        return result
    return _page_result(definition, normalized, response.body)


def result_document(result: Mapping[str, Any]) -> dict[str, Any]:
    """A help-shaped Markdown document containing identical, inert JSON values.

    Provider strings never occupy headings, examples of shell invocations or
    action slots. Escaping backticks/HTML/control bytes prevents fence injection;
    decoding the JSON data section recovers every original result value.
    """
    lines = ["  " + _json(key) + ": " + _json(value) for key, value in result.items()]
    payload = "{\n" + ",\n".join(lines) + "\n}"
    for character, escaped in (
        ("`", "\\u0060"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
    ):
        payload = payload.replace(character, escaped)
    view = result.get("view")
    level = 3 if view == "examples" else 2 if view in {"describe", "run"} else 1
    return {
        "level": level,
        "title": "Workflow result",
        "sections": [{"examples": [{"kind": "json", "value": payload}]}],
    }


def render_result(result: Mapping[str, Any], format: str = "markdown") -> str:
    """Return one result without a trailing newline; adapter chooses stdout/stderr."""
    if format == "json":
        return _json(result)
    if format != "markdown":
        raise ValueError("workflow format must be markdown or json")
    from .help import render_help

    return render_help(result_document(result))
