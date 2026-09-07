"""Self-contained, versioned lossless tokens; no process-local sidecar state."""

import base64
import binascii
import html
import json
import re
from typing import Any

_INLINE_ADF = frozenset(
    {
        "text",
        "hardBreak",
        "mention",
        "emoji",
        "date",
        "status",
        "placeholder",
        "inlineCard",
        "inlineExtension",
        "mediaInline",
    }
)

TOKEN_RE = re.compile(
    r"\{\{as:1:(adf|storage):(inline|block):([A-Za-z][A-Za-z0-9_-]*):"
    r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*":'
    r"([A-Za-z0-9_-]+)\}\}"
)


def encode(
    value: Any,
    source: str = "adf",
    placement: str = "inline",
    kind: str = "node",
    label: str | None = None,
) -> str:
    """Encode a complete ADF node or verbatim storage fragment."""
    if source not in {"adf", "storage"} or placement not in {"inline", "block"}:
        raise ValueError("Invalid placeholder source or placement")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", kind):
        raise ValueError("Invalid placeholder kind")
    if source == "adf":
        if not isinstance(value, dict) or value.get("type") != kind:
            raise ValueError("ADF placeholder kind must match node type")
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    else:
        if not isinstance(value, str):
            raise ValueError("Storage placeholder must contain text")
        raw = value
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    display = json.dumps(
        label if label is not None else _label(value, source, kind), ensure_ascii=False
    )
    return f"{{{{as:1:{source}:{placement}:{kind}:{display}:{payload}}}}}"


def _label(value: Any, source: str, kind: str) -> str:
    """Informational display text; decode deliberately never interprets it."""
    if source == "storage":
        # Keep macro kind visible. Resource attributes supply useful identity
        # when storage has no display text for an image or mention.
        match = re.search(r'(?:filename|alt|account-id|username)=["\']([^"\']*)', value)
        return (kind + ": " + html.unescape(match[1]))[:100] if match else kind

    def words(node: dict[str, Any]) -> str:
        attrs = node.get("attrs", {})
        own = node.get("text") or attrs.get("text") or attrs.get("alt") or attrs.get("filename")
        if own:
            return str(own)
        children = " ".join(words(child) for child in node.get("content", []))
        return children or str(attrs.get("title") or attrs.get("id") or "")

    text = " ".join(words(value).split())[:80]
    panel = value.get("attrs", {}).get("panelType")
    if panel:
        return f"{panel}: {text}" if text else str(panel)
    return text or kind


def decode(token: str, source: str | None = None) -> tuple[str, str, str, Any]:
    """Decode strictly; foreign-format tokens cannot be silently converted."""
    match = TOKEN_RE.fullmatch(token)
    if not match:
        raise ValueError("Malformed rich-text placeholder")
    fmt, placement, kind, payload = match.groups()
    if source is not None and fmt != source:
        raise ValueError(f"Cannot restore {fmt} placeholder into {source}")
    try:
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        value = raw.decode("utf-8")
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != payload:
            raise ValueError("Noncanonical placeholder encoding")
        if fmt == "adf":
            value = json.loads(value)
            if not isinstance(value, dict) or value.get("type") != kind:
                raise ValueError("Placeholder kind does not match node")
            if (placement == "inline") != (kind in _INLINE_ADF):
                raise ValueError("ADF placeholder placement does not match node kind")
            _nonempty_text(value)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid rich-text placeholder payload") from exc
    return fmt, placement, kind, value


def _nonempty_text(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "text" and not value.get("text"):
            raise ValueError("ADF text nodes must be nonempty")
        for child in value.values():
            _nonempty_text(child)
    elif isinstance(value, list):
        for child in value:
            _nonempty_text(child)
