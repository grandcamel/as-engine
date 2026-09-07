"""Atlassian rich-text conversion, isolated from the generic engine.

File/argv handling and representation envelopes belong to the transform layer.
"""

import json
from typing import Any

from .adf import adf_to_markdown, markdown_to_adf
from .placeholders import TOKEN_RE, decode
from .schema import validate_adf

__all__ = ["check_document", "convert", "render", "validate_adf"]


def convert(text: str, source: str = "markdown", target: str = "adf") -> Any:
    """Convert literal Markdown to an ADF object or storage-format string."""
    if source != "markdown" or target not in {"adf", "storage"}:
        raise ValueError(f"Unsupported conversion: {source} -> {target}")
    if not isinstance(text, str):
        raise TypeError("Markdown input must be a string")
    token = TOKEN_RE.fullmatch(text.strip())
    if token and token.group(3) == "doc":
        _, placement, _, value = decode(token[0], source=target)
        if placement != "block":
            raise ValueError("Document placeholder must be a block")
        return value
    if target == "adf":
        return markdown_to_adf(text)
    from .storage import markdown_to_xhtml

    return markdown_to_xhtml(text)


def render(
    document: Any, source: str = "adf", target: str = "markdown", placeholders: bool = True
) -> str:
    """Render ADF (object or JSON string) or storage with lossless tokens by default."""
    if target != "markdown" or source not in {"adf", "storage"}:
        raise ValueError(f"Unsupported rendering: {source} -> {target}")
    if source == "adf":
        if isinstance(document, str):
            document = json.loads(document)
        if not isinstance(document, dict):
            raise TypeError("ADF input must be an object or JSON object string")
        return adf_to_markdown(document, placeholders=placeholders)
    if not isinstance(document, str):
        raise TypeError("Storage input must be a string")
    from .storage import xhtml_to_markdown

    return xhtml_to_markdown(document, placeholders=placeholders)


def check_document(document: Any, source: str = "adf") -> None:
    """Check wire document structure without loading the optional ADF schema.

    This inexpensive check is used by tagged transforms for encoded inputs.
    Full schema conformance remains the explicit validate_adf API. Invalid
    encoded data uses ValueError consistently with the Surface usage contract.
    """
    if source == "storage":
        if not isinstance(document, str):
            raise ValueError("Storage document must be a string")
        return
    if source != "adf":
        raise ValueError("Unsupported document representation")
    if not isinstance(document, dict):
        raise ValueError("ADF document must be an object")  # noqa: TRY004
    if (
        document.get("type") != "doc"
        or type(document.get("version")) is not int
        or document["version"] != 1
    ):
        raise ValueError("ADF document must have type doc and version 1")
    if not isinstance(document.get("content"), list):
        raise ValueError("ADF document content must be a list")  # noqa: TRY004

    def walk(node: Any) -> None:
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise ValueError("ADF nodes must be objects with a type")  # noqa: TRY004
        if node.get("type") == "text" and (
            not isinstance(node.get("text"), str) or not node["text"]
        ):
            raise ValueError("ADF text nodes must be nonempty")
        if "content" in node:
            if not isinstance(node["content"], list):
                raise ValueError("ADF node content must be a list")
            for child in node["content"]:
                walk(child)

    walk(document)
