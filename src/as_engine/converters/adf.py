"""
Atlassian Document Format (ADF) Helper

Provides utilities for working with ADF, the JSON-based document format
used by Confluence Cloud API v2.

Features:
- Convert plain text to ADF
- Convert Markdown to ADF
- Convert ADF to plain text
- Convert ADF to Markdown

ADF Documentation:
https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/

Usage:
    from confluence_as import text_to_adf, markdown_to_adf, adf_to_text

    # Create ADF from text
    adf = text_to_adf("Hello, world!")

    # Create ADF from Markdown
    adf = markdown_to_adf("# Heading\n\nParagraph with **bold** text.")

    # Convert ADF back to text
    text = adf_to_text(adf)
"""

import re
from typing import Any

from .markdown_parser import is_block_start, parse_markdown
from .placeholders import TOKEN_RE, decode, encode
from .schema import validate_adf

__all__ = ["validate_adf"]


def create_adf_doc(content: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Create an ADF document wrapper.

    Args:
        content: List of ADF block nodes

    Returns:
        Complete ADF document
    """
    return {"type": "doc", "version": 1, "content": content}


def create_paragraph(
    content: list[dict[str, Any]] | None = None, text: str | None = None
) -> dict[str, Any]:
    """
    Create an ADF paragraph node.

    Args:
        content: List of inline nodes (text, marks, etc.)
        text: Simple text content (creates a text node automatically)

    Returns:
        ADF paragraph node
    """
    if text is not None:
        content = [create_text(text)] if text else []
    elif content is None:
        content = []

    return {"type": "paragraph", "content": content}


def create_text(text: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """
    Create an ADF text node.

    Args:
        text: The text content
        marks: List of marks (bold, italic, link, etc.)

    Returns:
        ADF text node
    """
    if not text:
        raise ValueError("ADF text nodes must be nonempty")
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def create_heading(text: str, level: int = 1) -> dict[str, Any]:
    """
    Create an ADF heading node.

    Args:
        text: Heading text
        level: Heading level (1-6)

    Returns:
        ADF heading node
    """
    level = max(1, min(6, level))
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [create_text(text)] if text else [],
    }


def create_bullet_list(items: list[str]) -> dict[str, Any]:
    """
    Create an ADF bullet list.

    Args:
        items: List of text items

    Returns:
        ADF bulletList node
    """
    if not items:
        raise ValueError("ADF bullet lists require at least one item")
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [create_paragraph(text=item)]} for item in items
        ],
    }


def create_ordered_list(items: list[str], start: int = 1) -> dict[str, Any]:
    """
    Create an ADF ordered list.

    Args:
        items: List of text items
        start: Starting number

    Returns:
        ADF orderedList node
    """
    if not items:
        raise ValueError("ADF ordered lists require at least one item")
    return {
        "type": "orderedList",
        "attrs": {"order": start},
        "content": [
            {"type": "listItem", "content": [create_paragraph(text=item)]} for item in items
        ],
    }


def create_code_block(code: str, language: str | None = None) -> dict[str, Any]:
    """
    Create an ADF code block.

    Args:
        code: Code content
        language: Programming language for syntax highlighting

    Returns:
        ADF codeBlock node
    """
    node: dict[str, Any] = {"type": "codeBlock", "content": [create_text(code)] if code else []}
    if language:
        node["attrs"] = {"language": language}
    return node


def create_blockquote(text: str) -> dict[str, Any]:
    """
    Create an ADF blockquote.

    Args:
        text: Quote text

    Returns:
        ADF blockquote node
    """
    return {"type": "blockquote", "content": [create_paragraph(text=text)]}


def create_rule() -> dict[str, Any]:
    """
    Create an ADF horizontal rule.

    Returns:
        ADF rule node
    """
    return {"type": "rule"}


def create_table(rows: list[list[str]], header: bool = True) -> dict[str, Any]:
    """
    Create an ADF table.

    Args:
        rows: List of rows, each row is a list of cell contents
        header: If True, first row is treated as header

    Returns:
        ADF table node
    """
    if not rows or any(not row for row in rows):
        raise ValueError("ADF tables require nonempty rows")
    table_rows = []

    for i, row in enumerate(rows):
        cells = []
        cell_type = "tableHeader" if (header and i == 0) else "tableCell"

        for cell_text in row:
            cells.append({"type": cell_type, "content": [create_paragraph(text=str(cell_text))]})

        table_rows.append({"type": "tableRow", "content": cells})

    return {"type": "table", "content": table_rows}


def create_link(text: str, url: str) -> dict[str, Any]:
    """
    Create an ADF text node with link mark.

    Args:
        text: Link text
        url: Link URL

    Returns:
        ADF text node with link mark
    """
    return create_text(text, marks=[{"type": "link", "attrs": {"href": url}}])


def text_to_adf(text: str) -> dict[str, Any]:
    """
    Convert plain text to ADF.

    Handles:
    - Paragraph breaks (double newlines)
    - Preserves single line breaks within paragraphs

    Args:
        text: Plain text content

    Returns:
        ADF document
    """
    if not text:
        return create_adf_doc([create_paragraph(text="")])

    # Split on double newlines for paragraphs
    paragraphs = re.split(r"\n\n+", text.strip())
    content = []

    for para in paragraphs:
        if para.strip():
            content.append(create_paragraph(text=para.strip()))

    return create_adf_doc(content if content else [create_paragraph(text="")])


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    """
    Convert Markdown to ADF.

    Supports:
    - Headings (# to ######)
    - Bold (**text** or __text__)
    - Italic (*text* or _text_)
    - Code (`code` and code blocks)
    - Links [text](url)
    - Bullet lists (- or *)
    - Ordered lists (1. 2. etc.)
    - Blockquotes (>)
    - Horizontal rules (---)

    Args:
        markdown: Markdown content

    Returns:
        ADF document
    """

    def nodes(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for block in blocks:
            kind = block["type"]
            if kind == "placeholder":
                _, placement, _, value = decode(block["content"], source="adf")
                if placement != "block":
                    raise ValueError("Expected block placeholder")
                result.append(value)
            elif kind == "heading":
                result.append(
                    {
                        "type": "heading",
                        "attrs": {"level": block["level"]},
                        "content": _parse_inline_markdown(block["content"]),
                    }
                )
            elif kind == "horizontal_rule":
                result.append(create_rule())
            elif kind == "code_block":
                result.append(create_code_block(block["content"], block.get("language")))
            elif kind == "blockquote":
                result.append(
                    {
                        "type": "blockquote",
                        "content": nodes(parse_markdown(block["content"])) or [create_paragraph()],
                    }
                )
            elif kind in {"bullet_list", "ordered_list"}:
                items = []
                for i, text in enumerate(block["items"]):
                    children = block.get("children", [[]] * len(block["items"]))[i]
                    items.append(
                        {
                            "type": "listItem",
                            "content": [
                                create_paragraph(content=_parse_inline_markdown(text)),
                                *nodes(children),
                            ],
                        }
                    )
                node: dict[str, Any] = {
                    "type": "orderedList" if kind == "ordered_list" else "bulletList",
                    "content": items,
                }
                if kind == "ordered_list":
                    node["attrs"] = {"order": block.get("start", 1)}
                result.append(node)
            else:
                result.append(create_paragraph(content=_parse_inline_markdown(block["content"])))
        return result

    content = nodes(parse_markdown(markdown))
    if len(content) == 1 and content[0].get("type") == "doc":
        return content[0]
    if any(node.get("type") == "doc" for node in content):
        raise ValueError("Document placeholder must be the entire document")
    return create_adf_doc(content or [create_paragraph()])


# Re-export from shared module for backward compatibility
is_markdown_block_start = is_block_start

# Alias for internal use
_is_block_element = is_block_start


def _parse_inline_markdown(text: str) -> list[dict[str, Any]]:
    """Inline parser with escapes, literal code and lossless node tokens."""
    nodes: list[dict[str, Any]] = []
    plain = ""

    def flush() -> None:
        nonlocal plain
        if plain:
            nodes.append(create_text(plain))
            plain = ""

    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            plain += text[i + 1]
            i += 2
            continue
        if text.startswith("{{as:", i):
            token = TOKEN_RE.match(text, i)
            if token is None:
                raise ValueError("Malformed rich-text placeholder")
            _, placement, _, value = decode(token[0], source="adf")
            if placement != "inline":
                raise ValueError("Block placeholder must be on its own line")
            flush()
            nodes.append(value)
            i = token.end()
            continue
        if text[i] == "`":
            fence = re.match(r"`+", text[i:])
            assert fence is not None
            marker = fence[0]
            end = text.find(marker, i + len(marker))
            if end >= 0:
                flush()
                value = text[i + len(marker) : end]
                if value:
                    nodes.append(create_text(value, [{"type": "code"}]))
                i = end + len(marker)
                continue
        link = re.match(r'\[([^\]]+)\]\(([^\s)]*)(?: "([^"\n]*)")?\)', text[i:])
        if link:
            flush()
            attrs = {"href": link[2]}
            if link[3] is not None:
                attrs["title"] = link[3]
            nodes.append(create_text(link[1], [{"type": "link", "attrs": attrs}]))
            i += len(link[0])
            continue
        matched = False
        for marker, mark in [
            ("**", "strong"),
            ("__", "strong"),
            ("~~", "strike"),
            ("*", "em"),
            ("_", "em"),
        ]:
            if not text.startswith(marker, i):
                continue
            end = text.find(marker, i + len(marker))
            if end <= i + len(marker):
                continue
            flush()
            children = _parse_inline_markdown(text[i + len(marker) : end])
            for child in children:
                if child.get("type") != "text":
                    raise ValueError("Cannot apply Markdown marks to an opaque inline node")
                # ADF code text admits only code/link/annotation marks.
                # Keep literal code when Markdown wraps it in emphasis.
                if not any(existing.get("type") == "code" for existing in child.get("marks", [])):
                    child.setdefault("marks", []).append({"type": mark})
            nodes.extend(children)
            i = end + len(marker)
            matched = True
            break
        if not matched:
            plain += text[i]
            i += 1
    flush()
    return nodes


def adf_to_text(adf: dict[str, Any]) -> str:
    """
    Convert ADF to plain text.

    Args:
        adf: ADF document

    Returns:
        Plain text content
    """
    if not adf:
        return ""

    def extract_text(node: dict[str, Any]) -> str:
        """Recursively extract text from a node."""
        if node.get("type") == "text":
            return node.get("text", "")

        content = node.get("content", [])
        if not content:
            return ""

        texts = []
        for child in content:
            child_text = extract_text(child)
            if child_text:
                texts.append(child_text)

        node_type = node.get("type", "")

        if node_type == "paragraph" or node_type == "heading":
            return "".join(texts)
        elif node_type == "bulletList" or node_type == "orderedList":
            return "\n".join(f"- {t}" for t in texts)
        elif node_type == "listItem" or node_type == "codeBlock":
            return "".join(texts)
        elif node_type == "blockquote":
            return "> " + "".join(texts)
        elif node_type == "table":
            return "\n".join(texts)
        elif node_type == "tableRow":
            return " | ".join(texts)
        elif node_type in ("tableCell", "tableHeader"):
            return "".join(texts)
        elif node_type == "hardBreak":
            return "\n"
        elif node_type == "rule":
            return "---"
        else:
            return "".join(texts)

    lines = []
    for node in adf.get("content", []):
        text = extract_text(node)
        if text:
            lines.append(text)

    return "\n\n".join(lines)


def _legacy_adf_to_markdown(adf: dict[str, Any]) -> str:
    """
    Convert ADF to Markdown.

    Args:
        adf: ADF document

    Returns:
        Markdown content
    """
    if not adf:
        return ""

    def convert_node(node: dict[str, Any], indent: str = "") -> str:
        """Convert a single ADF node to Markdown."""
        node_type = node.get("type", "")
        content = node.get("content", [])
        attrs = node.get("attrs", {})

        if node_type == "text":
            text = node.get("text", "")
            marks = node.get("marks", [])

            for mark in marks:
                mark_type = mark.get("type", "")
                if mark_type == "strong":
                    text = f"**{text}**"
                elif mark_type == "em":
                    text = f"*{text}*"
                elif mark_type == "code":
                    text = f"`{text}`"
                elif mark_type == "link":
                    url = mark.get("attrs", {}).get("href", "")
                    text = f"[{text}]({url})"
                elif mark_type == "strike":
                    text = f"~~{text}~~"

            return text

        elif node_type == "paragraph":
            return "".join(convert_node(c) for c in content)

        elif node_type == "heading":
            level = attrs.get("level", 1)
            text = "".join(convert_node(c) for c in content)
            return "#" * level + " " + text

        elif node_type == "bulletList":
            items = []
            for item in content:
                item_text = convert_node(item, indent + "  ")
                items.append(f"{indent}- {item_text}")
            return "\n".join(items)

        elif node_type == "orderedList":
            items = []
            start = attrs.get("order", 1)
            for i, item in enumerate(content):
                item_text = convert_node(item, indent + "   ")
                items.append(f"{indent}{start + i}. {item_text}")
            return "\n".join(items)

        elif node_type == "listItem":
            return "".join(convert_node(c, indent) for c in content)

        elif node_type == "codeBlock":
            language = attrs.get("language", "")
            code = "".join(convert_node(c) for c in content)
            return f"```{language}\n{code}\n```"

        elif node_type == "blockquote":
            text = "".join(convert_node(c) for c in content)
            lines = text.split("\n")
            return "\n".join(f"> {line}" for line in lines)

        elif node_type == "rule":
            return "---"

        elif node_type == "table":
            rows = []
            for row_node in content:
                row = convert_node(row_node)
                rows.append(row)
            if rows:
                # Add header separator after first row
                first_row_cells = content[0].get("content", []) if content else []
                separator = "| " + " | ".join(["---"] * len(first_row_cells)) + " |"
                rows.insert(1, separator)
            return "\n".join(rows)

        elif node_type == "tableRow":
            cells = [convert_node(c) for c in content]
            return "| " + " | ".join(cells) + " |"

        elif node_type in ("tableCell", "tableHeader"):
            return "".join(convert_node(c) for c in content)

        elif node_type == "hardBreak":
            return "  \n"

        else:
            return "".join(convert_node(c, indent) for c in content)

    blocks = []
    for node in adf.get("content", []):
        block = convert_node(node)
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


def wiki_markup_to_adf(text: str) -> dict[str, Any]:
    """
    Convert JIRA wiki markup to ADF format.

    Supports:
    - *bold* -> strong text
    - [text|url] -> linked text
    - Plain text paragraphs

    This is commonly used for formatting commit/PR comments that use
    wiki-style markup like "*Field:* [link_text|url]".

    Args:
        text: Text with wiki markup

    Returns:
        ADF document dictionary

    Example:
        >>> wiki_markup_to_adf("*Commit:* [abc123|https://github.com/org/repo/commit/abc123]")
        {
            "version": 1,
            "type": "doc",
            "content": [...]
        }
    """
    if not text:
        return {"version": 1, "type": "doc", "content": []}

    lines = text.split("\n")
    content_blocks = []

    for line in lines:
        if line.strip():
            content_blocks.append({"type": "paragraph", "content": _parse_wiki_inline(line)})

    return {
        "version": 1,
        "type": "doc",
        "content": content_blocks if content_blocks else [],
    }


def _parse_wiki_inline(text: str) -> list[dict[str, Any]]:
    """
    Parse wiki-style inline formatting.

    Handles:
    - *bold text* -> strong
    - [text|url] -> link

    Args:
        text: Text with wiki inline formatting

    Returns:
        List of ADF text nodes with formatting
    """
    if not text:
        return []

    result: list[dict[str, Any]] = []
    remaining = text

    # Patterns for wiki markup
    # *bold* - matches *text* but not ** (empty bold)
    bold_pattern = r"\*([^*]+)\*"
    # [text|url] - wiki-style links
    link_pattern = r"\[([^\]|]+)\|([^\]]+)\]"

    while remaining:
        bold_match = re.search(bold_pattern, remaining)
        link_match = re.search(link_pattern, remaining)

        # Collect valid matches
        matches: list[tuple[re.Match[str], str]] = []
        if bold_match:
            matches.append((bold_match, "bold"))
        if link_match:
            matches.append((link_match, "link"))

        if not matches:
            # No more matches, add remaining text
            if remaining:
                result.append({"type": "text", "text": remaining})
            break

        # Process the match that appears first
        matches.sort(key=lambda x: x[0].start())
        match, match_type = matches[0]

        # Add any text before the match
        if match.start() > 0:
            result.append({"type": "text", "text": remaining[: match.start()]})

        if match_type == "bold":
            result.append({"type": "text", "text": match.group(1), "marks": [{"type": "strong"}]})
        elif match_type == "link":
            result.append(
                {
                    "type": "text",
                    "text": match.group(1),
                    "marks": [{"type": "link", "attrs": {"href": match.group(2)}}],
                }
            )

        remaining = remaining[match.end() :]

    return result


def adf_to_markdown(adf: dict[str, Any], placeholders: bool = True) -> str:
    """Render editable Markdown; retain complete nodes whenever syntax loses data.

    A candidate is used only if parsing it reconstructs the exact node. This
    includes attributes, mark ordering and text-node boundaries. The fallback
    is self-contained, so changing adjacent prose cannot lose opaque content.
    """
    if not placeholders:
        return _legacy_adf_to_markdown(adf)
    if not adf:
        return ""
    if adf.get("type") != "doc" or adf.get("version") != 1:
        raise ValueError("Expected ADF version 1 document")
    if set(adf) - {"type", "version", "content"}:
        raise ValueError("Unsupported ADF document properties")

    def inline(node: dict[str, Any]) -> str:
        kind = node.get("type", "node")
        if kind != "text":
            return encode(node, kind=kind)
        text = node.get("text", "")
        if not text:
            raise ValueError("ADF text nodes must be nonempty")
        candidate = re.sub(r"([\\`*_\[\]{}~])", r"\\\1", text)
        for mark in node.get("marks", []):
            mark_type = mark.get("type")
            marker = {"strong": "**", "em": "*", "strike": "~~"}.get(mark_type)
            if marker:
                candidate = marker + candidate + marker
            elif mark_type == "code":
                fence = "`" * (max((len(x) for x in re.findall(r"`+", text)), default=0) + 1)
                candidate = fence + text + fence
            elif mark_type == "link":
                attrs = mark.get("attrs", {})
                title = f' "{attrs["title"]}"' if "title" in attrs else ""
                candidate = f"[{text}]({attrs.get('href', '')}{title})"
        try:
            if _parse_inline_markdown(candidate) == [node]:
                return candidate
        except ValueError:
            pass
        return encode(node, kind=kind)

    def block(node: dict[str, Any]) -> str:
        kind = node.get("type", "node")
        content = node.get("content", [])
        attrs = node.get("attrs", {})
        candidate: str | None = None
        if kind in {"paragraph", "heading"}:
            candidate = "".join(inline(child) for child in content)
            if kind == "heading":
                candidate = "#" * attrs.get("level", 1) + " " + candidate
        elif kind == "rule":
            candidate = "---"
        elif kind == "codeBlock":
            code = "".join(child.get("text", "") for child in content)
            fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", code)), default=3))
            candidate = f"{fence}{attrs.get('language', '')}\n{code}\n{fence}"
        elif kind == "blockquote":
            candidate = "\n".join(
                "> " + line for line in "\n\n".join(block(child) for child in content).split("\n")
            )
        elif kind in {"bulletList", "orderedList"}:
            rows = []
            for i, item in enumerate(content):
                parts = [block(child) for child in item.get("content", [])]
                prefix = "- " if kind == "bulletList" else f"{attrs.get('order', 1) + i}. "
                value = "\n".join(parts)
                lines = value.split("\n")
                rows.append(
                    prefix
                    + lines[0]
                    + "".join("\n" + " " * len(prefix) + line for line in lines[1:])
                )
            candidate = "\n".join(rows)
        if candidate is not None:
            try:
                if markdown_to_adf(candidate)["content"] == [node]:
                    return candidate
            except ValueError:
                pass
        return encode(node, placement="block", kind=kind)

    # Empty documents differ from a document containing an empty paragraph.
    if adf.get("content") == []:
        return encode(adf, placement="block", kind="doc")
    return "\n\n".join(block(node) for node in adf.get("content", []))


create_adf_heading = create_heading
create_adf_code_block = create_code_block


def create_adf_paragraph(text: str, **marks: Any) -> dict[str, Any]:
    """Jira helper compatibility without any environment-dependent field policy."""
    applied: list[dict[str, Any]] = [
        {"type": kind}
        for name, kind in [("bold", "strong"), ("italic", "em"), ("code", "code")]
        if marks.get(name)
    ]
    if marks.get("link"):
        applied.append({"type": "link", "attrs": {"href": marks["link"]}})
    return create_paragraph(content=[create_text(text, applied)] if text else [])
