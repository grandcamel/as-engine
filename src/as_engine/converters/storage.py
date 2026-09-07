"""Confluence storage XHTML <-> Markdown conversion.

Storage XHTML is not ordinary HTML: it contains namespaced elements, macros,
and CDATA bodies.  The default reader therefore keeps anything Markdown cannot
represent in a self-contained storage placeholder.  ``placeholders=False`` is
the compatible, display-oriented conversion used by older callers.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from .markdown_parser import MarkdownBlock, parse_markdown
from .placeholders import TOKEN_RE, decode, encode

_VOID = frozenset({"br", "hr", "img", "meta", "link", "input"})
_BLOCKS = frozenset(
    {"p", "div", "section", "article", "table", "ul", "ol", "li", "pre", "blockquote"}
)
_SAFE_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "del",
        "code",
        "a",
        "br",
        "hr",
        "pre",
        "blockquote",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "th",
        "td",
    }
)


# The compatibility reader below is intentionally the established XHTML helper
# algorithm, with its product import replaced by this stdlib-only equivalent.
# It remains separate from the lossless tree renderer so old callers retain
# their display output byte-for-byte.
_LEGACY_LITERAL_RE = re.compile(r"\x01(\d+)\x02")
_LEGACY_MACRO_RE = re.compile(
    r"<structured-macro(?=[\s/>])(?P<attrs>[^>]*?)(?:/>|>(?P<body>(?:(?!<structured-macro[\s/>]).)*?)</structured-macro>)",
    re.DOTALL,
)
_LEGACY_PANELS = frozenset({"info", "warning", "note", "tip", "panel"})
_LEGACY_CELL_BREAK = "\x00"


def _legacy_strip_tags(value: str, collapse_whitespace: bool = False) -> str:
    result = re.sub(r"<[^>]+>", "", value)
    if collapse_whitespace:
        result = re.sub(r"\s+", " ", result)
    return result.strip()


def _legacy_stash(value: str, literals: list[str]) -> str:
    literals.append(value)
    return f"\x01{len(literals) - 1}\x02"


def _legacy_clean(value: str) -> str:
    return _legacy_strip_tags(value, collapse_whitespace=True)


def _legacy_parameter(block: str, name: str) -> str | None:
    match = re.search(rf'<parameter[^>]*name="{name}"[^>]*>([^<]*)</parameter>', block)
    return match.group(1) if match else None


def _legacy_rich_text(body: str) -> str:
    return "".join(re.findall(r"<rich-text-body[^>]*>(.*?)</rich-text-body>", body, re.DOTALL))


def _legacy_render_macro(match: re.Match[str], literals: list[str]) -> str:
    attrs = match.group("attrs") or ""
    body = match.group("body") or ""
    block = match.group(0)
    name_match = re.search(r'name="([^"]*)"', attrs)
    name = name_match.group(1) if name_match else ""
    if name == "code":
        language = _legacy_parameter(block, "language")
        if language is None:
            attr_language = re.search(r'language="([^"]*)"', attrs)
            language = attr_language.group(1) if attr_language else ""
        content_match = re.search(r"<plain-text-body[^>]*>(.*?)</plain-text-body>", body, re.DOTALL)
        content = content_match.group(1) if content_match else ""
        token = _LEGACY_LITERAL_RE.fullmatch(content.strip())
        code = literals[int(token.group(1))] if token else html.unescape(content)
        return _legacy_stash(f"\n```{language}\n{code.strip(chr(10))}\n```\n", literals)
    if name in _LEGACY_PANELS:
        return f"\n> **{name.title()}:** {_legacy_clean(_legacy_rich_text(body))}\n"
    if name == "status":
        title = _legacy_parameter(block, "title")
        return f"`{title}`" if title else ""
    if name == "toc":
        return "\n[Table of Contents]\n"
    if name == "expand":
        title = html.unescape(_legacy_parameter(block, "title") or "Details")
        content = html.unescape(_legacy_clean(_legacy_rich_text(body)))
        return _legacy_stash(
            f"\n<details>\n<summary>{title}</summary>\n\n{content}\n</details>\n", literals
        )
    return _legacy_rich_text(body)


def _legacy_macros(value: str, literals: list[str]) -> str:
    while match := _LEGACY_MACRO_RE.search(value):
        value = (
            value[: match.start()] + _legacy_render_macro(match, literals) + value[match.end() :]
        )
    return value


def _legacy_lists(value: str) -> str:
    def unordered(match: re.Match[str]) -> str:
        items = re.findall(r"<li[^>]*>(.*?)</li>", match.group(1), re.DOTALL)
        return "\n" + "\n".join(f"- {_legacy_clean(item)}" for item in items) + "\n"

    def ordered(match: re.Match[str]) -> str:
        items = re.findall(r"<li[^>]*>(.*?)</li>", match.group(1), re.DOTALL)
        return (
            "\n"
            + "\n".join(f"{index}. {_legacy_clean(item)}" for index, item in enumerate(items, 1))
            + "\n"
        )

    return re.sub(
        r"<ol[^>]*>(.*?)</ol>",
        ordered,
        re.sub(r"<ul[^>]*>(.*?)</ul>", unordered, value, flags=re.DOTALL),
        flags=re.DOTALL,
    )


def _legacy_table_cell(value: str) -> str:
    value = re.sub(r"</p>\s*<p[^>]*>", _LEGACY_CELL_BREAK, value)
    value = re.sub(r"<br\s*/?\s*>", _LEGACY_CELL_BREAK, value)
    value = re.sub(r"\s*\n\s*", _LEGACY_CELL_BREAK, value.strip())
    value = _legacy_clean(value).replace("|", "\\|")
    value = re.sub(rf"\A(?:\s*{_LEGACY_CELL_BREAK})+\s*", "", value)
    value = re.sub(rf"(?:{_LEGACY_CELL_BREAK}\s*)+\Z", "", value)
    return re.sub(rf"\s*(?:{_LEGACY_CELL_BREAK}\s*)+", _LEGACY_CELL_BREAK, value)


def _legacy_tables(value: str) -> str:
    def table(match: re.Match[str]) -> str:
        rows: list[str] = []
        for index, row in enumerate(re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(0), re.DOTALL)):
            cells = [
                _legacy_table_cell(cell)
                for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.DOTALL)
            ]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
                if index == 0:
                    rows.append("| " + " | ".join("---" for _ in cells) + " |")
        return "\n" + "\n".join(rows) + "\n" if rows else ""

    return re.sub(r"<table[^>]*>.*?</table>", table, value, flags=re.DOTALL)


def _legacy_heading(match: re.Match[str], level: int) -> str:
    return f"\n{'#' * level} {_legacy_clean(match.group(1))}\n"


def _legacy_heading_replacer(level: int):
    def replace(match: re.Match[str]) -> str:
        return _legacy_heading(match, level)

    return replace


def _legacy_xhtml_to_markdown(xhtml: str) -> str:
    literals: list[str] = []
    value = re.sub(
        r"<!\[CDATA\[(.*?)\]\]>",
        lambda match: _legacy_stash(match.group(1), literals),
        xhtml,
        flags=re.DOTALL,
    )
    value = re.sub(r"<\?xml[^>]*\?>", "", value)
    value = re.sub(r"<(/?)ac:", r"<\1", value)
    value = re.sub(r"<(/?)ri:", r"<\1", value)
    value = _legacy_macros(value, literals)
    for level in range(1, 7):
        value = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            _legacy_heading_replacer(level),
            value,
            flags=re.DOTALL,
        )
    value = re.sub(
        r"<p[^>]*>(.*?)</p>",
        lambda match: f"\n{_legacy_clean(match.group(1))}\n",
        value,
        flags=re.DOTALL,
    )
    value = re.sub(r"<br\s*/?\s*>", "  \n", value)
    for pattern, replacement in (
        (r"<strong[^>]*>(.*?)</strong>", r"**\1**"),
        (r"<b[^>]*>(.*?)</b>", r"**\1**"),
        (r"<em[^>]*>(.*?)</em>", r"*\1*"),
        (r"<i[^>]*>(.*?)</i>", r"*\1*"),
        (r"<u[^>]*>(.*?)</u>", r"_\1_"),
        (r"<s[^>]*>(.*?)</s>", r"~~\1~~"),
        (r"<del[^>]*>(.*?)</del>", r"~~\1~~"),
        (r"<code[^>]*>(.*?)</code>", r"`\1`"),
    ):
        value = re.sub(pattern, replacement, value, flags=re.DOTALL)
    value = re.sub(
        r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        lambda match: f"[{_legacy_clean(match.group(2))}]({match.group(1)})",
        value,
        flags=re.DOTALL,
    )
    value = re.sub(r'<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*/?>', r"![\2](\1)", value)
    value = re.sub(r'<img[^>]*src="([^"]*)"[^>]*/?>', r"![](\1)", value)
    value = re.sub(
        r"<pre[^>]*>(.*?)</pre>",
        lambda match: f"\n```\n{_legacy_clean(match.group(1))}\n```\n",
        value,
        flags=re.DOTALL,
    )
    value = _legacy_lists(value)
    value = _legacy_tables(value)
    value = re.sub(
        r"<blockquote[^>]*>(.*?)</blockquote>",
        lambda match: (
            "\n"
            + "\n".join(f"> {line}" for line in _legacy_clean(match.group(1)).split("\n"))
            + "\n"
        ),
        value,
        flags=re.DOTALL,
    )
    value = re.sub(r"<hr\s*/?\s*>", "\n---\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace(_LEGACY_CELL_BREAK, "<br>")
    for _ in range(len(literals) + 1):
        if not _LEGACY_LITERAL_RE.search(value):
            break
        value = _LEGACY_LITERAL_RE.sub(
            lambda match: (
                literals[int(match.group(1))] if int(match.group(1)) < len(literals) else ""
            ),
            value,
        )
    return re.sub(r"\n{3,}", "\n\n", value).strip()


class _Node:
    def __init__(
        self, name: str, start: str = "", attrs: list[tuple[str, str | None]] | None = None
    ) -> None:
        self.name = name.lower()
        self.start = start
        self.attrs = attrs or []
        self.children: list[_Node | str] = []
        self.end = ""

    def attr(self, local_name: str) -> str | None:
        for key, value in self.attrs:
            if key.lower().split(":")[-1] == local_name:
                return value
        return None

    def raw(self) -> str:
        return (
            self.start
            + "".join(child.raw() if isinstance(child, _Node) else child for child in self.children)
            + self.end
        )


class _StorageParser(HTMLParser):
    """A small XML-aware tree builder which retains each lexical XML fragment.

    ``HTMLParser`` handles nesting for us while ``get_starttag_text`` retains
    original attribute spelling and quoting.  Character references are kept
    unexpanded so an opaque subtree can be emitted exactly as received.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _Node("#root")
        self.stack = [self.root]
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, self.get_starttag_text() or f"<{tag}>", attrs)
        self.stack[-1].children.append(node)
        if tag.lower() not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, self.get_starttag_text() or f"<{tag}/>", attrs))

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].name == lowered:
                if index != len(self.stack) - 1:
                    self.errors.append(f"Unexpected closing tag </{tag}>")
                node = self.stack[index]
                node.end = f"</{tag}>"
                del self.stack[index:]
                return
        self.errors.append(f"Unexpected closing tag </{tag}>")
        self.stack[-1].children.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        self.stack[-1].children.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.stack[-1].children.append(f"&#{name};")

    def handle_decl(self, decl: str) -> None:
        self.stack[-1].children.append(f"<!{decl}>")

    def unknown_decl(self, data: str) -> None:
        # HTMLParser passes CDATA payload as ``CDATA[...``.  Keeping it as a
        # string makes it inert during conversion and exact in opaque tokens.
        self.stack[-1].children.append(f"<![{data}]]>")

    def handle_pi(self, data: str) -> None:
        self.stack[-1].children.append(f"<?{data}>")


def _parse(xhtml: str) -> _Node:
    parser = _StorageParser()
    parser.feed(xhtml)
    parser.close()
    return parser.root


def _text(node: _Node | str) -> str:
    if isinstance(node, str):
        if node.startswith("<![CDATA[") and node.endswith("]]>"):
            return node[9:-3]
        return html.unescape(node)
    return "".join(_text(child) for child in node.children)


def _escape_reserved(value: str) -> str:
    """Keep storage-looking prose from becoming an executable placeholder."""
    return re.sub(r"(?<!\\)\{\{as:", r"\\{{as:", value)


def _plain(node: _Node | str) -> str:
    return re.sub(r"\s+", " ", _text(node)).strip()


def _has_meaningful_attrs(node: _Node) -> bool:
    return bool(node.attrs)


def _is_opaque(node: _Node) -> bool:
    if node.name == "ac:structured-macro" and _safe_legacy_code(node):
        return False
    if node.name.startswith(("ac:", "ri:")):
        return True
    if node.name == "img":
        return True
    if node.name == "table" and _has_meaningful_attrs(node):
        return True
    return node.name not in _SAFE_TAGS


def _safe_legacy_code(node: _Node) -> bool:
    """Whether a code macro has only the syntax represented by a fence."""
    if node.attr("name") != "code" or len(node.attrs) != 1:
        return False
    parameters = _descendants(node, "ac:parameter")
    bodies = _descendants(node, "ac:plain-text-body")
    if len(bodies) != 1 or any(
        len(item.attrs) != 1 or item.attr("name") != "language" for item in parameters
    ):
        return False
    return not any(body.attrs for body in bodies)


def _placement(node: _Node, parent: _Node | None) -> str:
    if node.name in _BLOCKS or (parent is not None and parent.name == "#root"):
        return "block"
    return "inline"


def _kind(node: _Node) -> str:
    if node.name == "ac:structured-macro":
        return node.attr("name") or "macro"
    if node.name in {"ac:link", "ri:user"}:
        return "mention"
    if node.name in {"ac:image", "img", "ri:attachment", "ri:url"}:
        return "image"
    return node.name.replace(":", "-")


def _placeholder(node: _Node, parent: _Node | None) -> str:
    kind = re.sub(r"[^A-Za-z0-9_-]", "-", _kind(node))
    if not kind or not kind[0].isalpha():
        kind = "node-" + kind
    return encode(node.raw(), source="storage", placement=_placement(node, parent), kind=kind)


def _inline(node: _Node | str, placeholders: bool, parent: _Node | None = None) -> str:
    if isinstance(node, str):
        value = html.unescape(node)
        return _escape_reserved(value) if placeholders else value
    if placeholders and _is_opaque(node):
        return _placeholder(node, parent)
    body = "".join(_inline(child, placeholders, node) for child in node.children)
    if node.name in {"strong", "b"}:
        return f"**{body}**"
    if node.name in {"em", "i"}:
        return f"*{body}*"
    if node.name == "u":
        return f"_{body}_"
    if node.name in {"s", "del"}:
        return f"~~{body}~~"
    if node.name == "code":
        return f"`{body}`"
    if node.name == "a":
        href = node.attr("href")
        return f"[{body}]({href})" if href else body
    if node.name == "br":
        return "  \n"
    return body


def _table(node: _Node, placeholders: bool) -> str:
    if placeholders and _has_meaningful_attrs(node):
        return _placeholder(node, None)
    rows = [child for child in node.children if isinstance(child, _Node) and child.name == "tr"]
    # tbody/thead are harmless wrappers in old storage and are flattened here.
    if not rows:
        rows = [
            row
            for group in node.children
            if isinstance(group, _Node)
            for row in group.children
            if isinstance(row, _Node) and row.name == "tr"
        ]
    rendered: list[str] = []
    for index, row in enumerate(rows):
        cells = [
            child
            for child in row.children
            if isinstance(child, _Node) and child.name in {"th", "td"}
        ]
        if not cells:
            continue
        values = [
            _inline(cell, placeholders, row).strip().replace("|", "\\|").replace("\n", "<br>")
            for cell in cells
        ]
        rendered.append("| " + " | ".join(values) + " |")
        if index == 0:
            rendered.append("| " + " | ".join("---" for _ in values) + " |")
    return "\n".join(rendered)


def _descendants(node: _Node, name: str) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        if isinstance(child, _Node):
            if child.name == name:
                found.append(child)
            found.extend(_descendants(child, name))
    return found


def _legacy_macro(node: _Node) -> str:
    """Retain the established display rendering for common storage macros."""
    name = node.attr("name") or ""
    parameters = _descendants(node, "ac:parameter")
    parameter = {item.attr("name"): _text(item) for item in parameters}
    if name == "code":
        language = parameter.get("language", "")
        bodies = _descendants(node, "ac:plain-text-body")
        code = _text(bodies[0]).strip("\n") if bodies else ""
        return f"```{language}\n{code}\n```"
    if name in {"info", "warning", "note", "tip", "panel"}:
        bodies = _descendants(node, "ac:rich-text-body")
        return f"> **{name.title()}:** {_plain(bodies[0]) if bodies else ''}".rstrip()
    if name == "status":
        return f"`{parameter['title']}`" if parameter.get("title") else ""
    if name == "toc":
        return "[Table of Contents]"
    if name == "expand":
        title = parameter.get("title", "Details")
        bodies = _descendants(node, "ac:rich-text-body")
        return f"<details>\n<summary>{title}</summary>\n\n{_plain(bodies[0]) if bodies else ''}\n</details>"
    bodies = _descendants(node, "ac:rich-text-body")
    return "\n\n".join(_block(body, False, node) for body in bodies)


def _block(node: _Node | str, placeholders: bool, parent: _Node | None = None) -> str:
    if isinstance(node, str):
        return html.unescape(node)
    if placeholders and _is_opaque(node):
        return _placeholder(node, parent)
    if node.name == "ac:structured-macro":
        return _legacy_macro(node)
    if node.name == "p":
        return _inline(node, placeholders, parent).strip()
    if re.fullmatch(r"h[1-6]", node.name):
        return "#" * int(node.name[1]) + " " + _inline(node, placeholders, parent).strip()
    if node.name == "hr":
        return "---"
    if node.name == "pre":
        return "```\n" + _text(node).strip("\n") + "\n```"
    if node.name == "blockquote":
        return "\n".join("> " + line for line in _plain(node).splitlines() if line) or ">"
    if node.name in {"ul", "ol"}:
        return _list(node, placeholders)
    if node.name == "table":
        return _table(node, placeholders)
    return _inline(node, placeholders, parent).strip()


def _list(node: _Node, placeholders: bool, indent: int = 0) -> str:
    lines: list[str] = []
    number = 1
    for child in node.children:
        if not isinstance(child, _Node) or child.name != "li":
            continue
        nested = [
            part for part in child.children if isinstance(part, _Node) and part.name in {"ul", "ol"}
        ]
        text = "".join(
            _inline(part, placeholders, child) for part in child.children if part not in nested
        ).strip()
        marker = f"{number}. " if node.name == "ol" else "- "
        lines.append(" " * indent + marker + text)
        for item in nested:
            lines.extend(_list(item, placeholders, indent + 2).splitlines())
        number += 1
    return "\n".join(lines)


def xhtml_to_markdown(xhtml: str, placeholders: bool = False) -> str:
    """Convert storage XHTML to Markdown.

    The historical default remains lossy for compatibility.  Product rendering
    opts into ``placeholders=True`` to retain storage-only constructs.
    """
    if not xhtml:
        return ""
    if not placeholders:
        return _legacy_xhtml_to_markdown(xhtml)
    root = _parse(xhtml)
    blocks: list[str] = []
    for child in root.children:
        if isinstance(child, str):
            blocks.append(_escape_reserved(html.unescape(child)))
            continue
        candidate = _block(child, True, root)
        # A display conversion is allowed only when it reconstructs the exact
        # lexical subtree.  Otherwise retain its smallest complete XML node.
        try:
            identical = markdown_to_xhtml(candidate) == child.raw()
        except ValueError:
            identical = False
        if not identical:
            candidate = _placeholder(child, root)
        blocks.append(candidate)
    result = "\n\n".join(part for part in blocks if part.strip())
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    # Top-level whitespace, comments and processing instructions have no
    # Markdown representation.  A document token is the only lossless form.
    try:
        identical = markdown_to_xhtml(result) == xhtml
    except ValueError:
        identical = False
    if not identical:
        return encode(xhtml, source="storage", placement="block", kind="document")
    return result


def _cdata(value: str) -> str:
    """Emit a CDATA section without allowing its terminator to end it early."""
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _restore_tokens(text: str) -> list[str | tuple[str, str]]:
    """Split unescaped storage tokens, rejecting tokens from another source."""
    pieces: list[str | tuple[str, str]] = []
    marker = 0
    while (marker := text.find("{{as:", marker)) != -1:
        slashes = 0
        index = marker - 1
        while index >= 0 and text[index] == "\\":
            slashes += 1
            index -= 1
        token = TOKEN_RE.match(text, marker)
        if slashes % 2 == 0 and token is None:
            raise ValueError("Malformed rich-text placeholder")
        marker = token.end() if token else marker + 5
    position = 0
    for match in TOKEN_RE.finditer(text):
        # An odd count of preceding backslashes escapes the reserved syntax.
        slashes = 0
        index = match.start() - 1
        while index >= 0 and text[index] == "\\":
            slashes += 1
            index -= 1
        if slashes % 2:
            continue
        if match.start() > position:
            pieces.append(text[position : match.start()])
        source, placement, _kind_value, value = decode(match.group(0))
        if source != "storage":
            raise ValueError("storage XHTML cannot restore a non-storage placeholder")
        if placement != "inline":
            raise ValueError("block storage placeholder cannot appear inside a paragraph")
        if not isinstance(value, str):
            raise TypeError("storage placeholder payload must be XML text")
        pieces.append(("token", value))
        position = match.end()
    if position < len(text):
        pieces.append(text[position:])
    return pieces


def _inline_to_xhtml(text: str) -> str:
    protected: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        if match.group(5) is None:
            return match.group(0)
        protected.append(match.group(6))
        return f"\x02{len(protected) - 1}\x03"

    # Code spans are literal Markdown, including token-looking text.
    text = re.sub(TOKEN_RE.pattern + r"|(`+)(.*?)\5(?!`)", stash_code, text, flags=re.DOTALL)
    rendered: list[str] = []
    for piece in _restore_tokens(text):
        if isinstance(piece, tuple):
            rendered.append(piece[1])
            continue
        escaped = html.escape(piece)
        escaped = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)", r'<ac:image><ri:url ri:value="\2" /></ac:image>', escaped
        )
        escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
        escaped = re.sub(
            r"\*\*(.+?)\*\*|__(.+?)__",
            lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>",
            escaped,
        )
        escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped)
        escaped = re.sub(
            r"(?<!\*)\*([^*]+)\*|(?<!_)_([^_]+)_",
            lambda m: f"<em>{m.group(1) or m.group(2)}</em>",
            escaped,
        )
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        # A backslash reserves a literal token in Markdown; do not retain it
        # in the resulting prose once it has served that escaping purpose.
        escaped = escaped.replace("\\{{as:1:", "{{as:1:")
        rendered.append(escaped)
    value = "".join(rendered)
    for index, code in enumerate(protected):
        value = value.replace(f"\x02{index}\x03", f"<code>{html.escape(code)}</code>")
    return value


def _whole_block_placeholder(text: str) -> str | None:
    """Restore an opaque block outside a paragraph when it is the whole line."""
    match = TOKEN_RE.fullmatch(text)
    if match is None:
        return None
    source, placement, _kind, value = decode(text)
    if source != "storage":
        raise ValueError("storage XHTML cannot restore a non-storage placeholder")
    if placement == "block" and isinstance(value, str):
        return value
    return None


def _render_list(block: MarkdownBlock, ordered: bool) -> str:
    items = block.get("items", [])
    children = block.get("children", [])
    rendered: list[str] = []
    for index, item in enumerate(items):
        tail = ""
        if index < len(children):
            tail = "".join(_render_block(child) for child in children[index])
        rendered.append(f"<li>{_inline_to_xhtml(item)}{tail}</li>")
    return ("<ol>" if ordered else "<ul>") + "".join(rendered) + ("</ol>" if ordered else "</ul>")


def _render_block(block: MarkdownBlock) -> str:
    block_type = block["type"]
    if block_type == "heading":
        level = block["level"]
        return f"<h{level}>{_inline_to_xhtml(block['content'])}</h{level}>"
    if block_type == "horizontal_rule":
        return "<hr />"
    if block_type == "code_block":
        language = block.get("language")
        content = block["content"]
        if language:
            return (
                '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">'
                + html.escape(language)
                + "</ac:parameter><ac:plain-text-body>"
                + _cdata(content)
                + "</ac:plain-text-body></ac:structured-macro>"
            )
        return "<pre>" + html.escape(content) + "</pre>"
    if block_type == "blockquote":
        return (
            "<blockquote><p>"
            + _inline_to_xhtml(block["content"].replace("\n", " "))
            + "</p></blockquote>"
        )
    if block_type == "bullet_list":
        return _render_list(block, False)
    if block_type == "ordered_list":
        return _render_list(block, True)
    if block_type == "placeholder":
        whole = _whole_block_placeholder(block["content"])
        if whole is None:
            raise ValueError("Expected block storage placeholder")
        return whole
    whole = _whole_block_placeholder(block["content"])
    if whole is not None:
        return whole
    return "<p>" + _inline_to_xhtml(block["content"]) + "</p>"


def markdown_to_xhtml(markdown: str) -> str:
    """Convert Markdown to storage XHTML, restoring only storage tokens."""
    if not markdown:
        return ""
    return "".join(_render_block(block) for block in parse_markdown(markdown))


def xhtml_to_adf(xhtml: str) -> dict[str, object]:
    """Legacy bridge through Markdown; storage-only nodes must stay explicit."""
    from .adf import markdown_to_adf

    return markdown_to_adf(xhtml_to_markdown(xhtml, placeholders=True))


def adf_to_xhtml(adf: dict[str, object]) -> str:
    """Legacy bridge through Markdown, rejecting foreign opaque placeholders."""
    from .adf import adf_to_markdown

    return markdown_to_xhtml(adf_to_markdown(adf, placeholders=True))


def extract_text_from_xhtml(xhtml: str) -> str:
    """Return a display-only text extraction without interpreting XML markup."""
    return _plain(_parse(xhtml))


def wrap_in_storage_format(content: str) -> str:
    """Storage bodies do not require a separate document wrapper."""
    return content


def validate_xhtml(xhtml: str) -> tuple[bool, str | None]:
    """Perform a balanced-tag validation suitable for local input feedback."""
    parser = _StorageParser()
    try:
        parser.feed(xhtml)
        parser.close()
    except ValueError as error:
        return False, str(error)
    if parser.errors:
        return False, parser.errors[0]
    if len(parser.stack) != 1:
        return False, "Unclosed tags: " + ", ".join(node.name for node in parser.stack[1:])
    return True, None


__all__ = [
    "adf_to_xhtml",
    "extract_text_from_xhtml",
    "markdown_to_xhtml",
    "validate_xhtml",
    "wrap_in_storage_format",
    "xhtml_to_adf",
    "xhtml_to_markdown",
]
