"""Shared block IR derived from confluence-as's Markdown parser.

The deliberately small Markdown dialect is extended with nested lists and
lossless opaque blocks. Unrecognized syntax is always consumed as prose.
"""

import re
from typing import Any

from .placeholders import TOKEN_RE

MarkdownBlock = dict[str, Any]
_LIST = re.compile(r"^( *)([-*+] |\d+\. )(.*)$")
_HEADING = re.compile(r"^(#{1,6})(?:\s+(.*)|$)")
_FENCE = re.compile(r"^(`{3,}|~{3,})([^`~]*)$")


def is_block_start(line: str) -> bool:
    line = line.strip()
    return bool(
        _HEADING.fullmatch(line)
        or _FENCE.fullmatch(line)
        or line.startswith(">")
        or _LIST.match(line)
        or re.fullmatch(r"[-*_]{3,}", line)
        or TOKEN_RE.fullmatch(line)
    )


def parse_markdown(markdown: str) -> list[MarkdownBlock]:
    """Parse blocks, preserving code bytes and ordered-list start numbers."""
    blocks: list[MarkdownBlock] = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        token = TOKEN_RE.fullmatch(stripped)
        if token and token.group(2) == "block":
            blocks.append({"type": "placeholder", "content": stripped})
            i += 1
            continue
        heading = _HEADING.fullmatch(stripped)
        fence = _FENCE.fullmatch(stripped)
        item = _LIST.match(lines[i])
        if heading:
            blocks.append(
                {"type": "heading", "level": len(heading[1]), "content": heading[2] or ""}
            )
            i += 1
        elif re.fullmatch(r"[-*_]{3,}", stripped):
            blocks.append({"type": "horizontal_rule"})
            i += 1
        elif fence:
            code = []
            i += 1
            while i < len(lines) and not re.fullmatch(
                re.escape(fence[1][0]) + "{" + str(len(fence[1])) + ",}", lines[i].strip()
            ):
                code.append(lines[i])
                i += 1
            blocks.append(
                {
                    "type": "code_block",
                    "content": "\n".join(code),
                    "language": fence[2].strip() or None,
                }
            )
            i += 1
        elif stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^> ?", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "blockquote", "content": "\n".join(quote)})
        elif item:
            ordered = item[2][0].isdigit()
            indent = len(item[1])
            items: list[str] = []
            children: list[list[MarkdownBlock]] = []
            start = int(item[2].split(".")[0]) if ordered else 1
            while i < len(lines):
                current = _LIST.match(lines[i])
                if not current or len(current[1]) != indent or current[2][0].isdigit() != ordered:
                    break
                items.append(current[3])
                i += 1
                nested = []
                while i < len(lines) and lines[i].strip():
                    depth = len(lines[i]) - len(lines[i].lstrip(" "))
                    if depth <= indent:
                        break
                    nested.append(lines[i])
                    i += 1
                if nested:
                    dedent = min(len(line) - len(line.lstrip(" ")) for line in nested)
                    children.append(parse_markdown("\n".join(line[dedent:] for line in nested)))
                else:
                    children.append([])
            blocks.append(
                {
                    "type": "ordered_list" if ordered else "bullet_list",
                    "items": items,
                    "children": children,
                    "start": start,
                }
            )
        else:
            # Consume the first line even when it resembles unsupported syntax.
            paragraph = [lines[i].strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not is_block_start(lines[i]):
                paragraph.append(lines[i].strip())
                i += 1
            blocks.append({"type": "paragraph", "content": " ".join(paragraph)})
    return blocks
