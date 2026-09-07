"""Pinned first-party ADF schema. Validation is an explicit, opt-in operation."""

import json
from importlib.resources import files
from typing import Any


def validate_adf(document: Any) -> bool:
    """Validate Draft 4 ADF v1, raising ValueError with the failing JSON path.

    Requires the dev extra (jsonschema); never imported on the conversion path.
    The vendored schema has local references only; validation performs no I/O
    other than reading this package's pinned resource.
    """
    if (
        isinstance(document, dict)
        and "content" in document
        and not isinstance(document["content"], list)
    ):
        raise ValueError("Invalid ADF: content must be a list")

    from jsonschema import Draft4Validator  # type: ignore[import-untyped]

    schema = json.loads(files(__package__).joinpath("full-57.3.4.json").read_text())
    errors = list(Draft4Validator(schema).iter_errors(document))
    if errors:
        error = errors[0]
        # anyOf wrappers obscure the useful nested diagnostic; include leaf paths.
        leaves = []
        pending = [error]
        while pending:
            item = pending.pop()
            if item.context:
                pending.extend(item.context)
            else:
                path = "/" + "/".join(str(part) for part in item.absolute_path)
                leaves.append(f"{path}: {item.message}")
        raise ValueError("Invalid ADF: " + "; ".join(leaves))
    return True
