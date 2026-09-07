"""Atlassian rich-text conversion, isolated from the generic engine.

File/argv handling and representation envelopes belong to the transform layer.
"""

import json
from typing import Any

from .adf import adf_to_markdown, markdown_to_adf
from .placeholders import TOKEN_RE, decode
from .schema import validate_adf

__all__ = ["convert", "render", "validate_adf"]


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
