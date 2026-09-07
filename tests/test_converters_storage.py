"""Public behavior tests for the storage XHTML converter."""

import html

import pytest

from as_engine.converters.placeholders import encode
from as_engine.converters.storage import markdown_to_xhtml, xhtml_to_markdown


def test_basic_legacy_formatting_is_available_by_default() -> None:
    source = '<h2>Title</h2><p>Hello world</p><strong>bold</strong><a href="/x">link</a>'

    assert xhtml_to_markdown(source) == "## Title\n\nHello world\n**bold**[link](/x)"
    assert (
        markdown_to_xhtml("## Title\n\nHello **world**")
        == "<h2>Title</h2><p>Hello <strong>world</strong></p>"
    )


@pytest.mark.parametrize(
    "source",
    [
        '<ac:structured-macro ac:name="jira" ac:macro-id="one"><ac:parameter ac:name="key">JAS-38</ac:parameter></ac:structured-macro>',
        '<ac:link><ri:user ri:account-id="abc" /></ac:link>',
        '<table class="confluenceTable" data-layout="wide"><tr><th>A</th></tr><tr><td>B</td></tr></table>',
        '<ac:structured-macro ac:name="code" ac:macro-id="one"><ac:parameter ac:name="language">python</ac:parameter><ac:plain-text-body><![CDATA[print("<tag>")]]></ac:plain-text-body></ac:structured-macro>',
    ],
)
def test_lossless_storage_constructs_round_trip_exactly(source: str) -> None:
    markdown = xhtml_to_markdown(source, placeholders=True)

    assert "{{as:1:storage:" in markdown
    assert markdown_to_xhtml(markdown) == source


def test_plain_code_macro_retains_legacy_fence_when_no_attributes_are_lost() -> None:
    source = (
        '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[print(1)]]></ac:plain-text-body></ac:structured-macro>"
    )

    assert xhtml_to_markdown(source) == "```python\nprint(1)\n```"
    assert xhtml_to_markdown(source, placeholders=True) == "```python\nprint(1)\n```"


def test_foreign_adf_placeholder_is_rejected() -> None:
    foreign = encode({"type": "mention", "attrs": {"id": "abc"}}, kind="mention")

    with pytest.raises(ValueError, match="non-storage"):
        markdown_to_xhtml(foreign)


def test_escaped_and_code_fence_tokens_are_literal() -> None:
    token = encode(
        '<ac:structured-macro ac:name="jira"/>', source="storage", placement="block", kind="jira"
    )

    assert markdown_to_xhtml("\\" + token) == f"<p>{html.escape(token)}</p>"
    assert markdown_to_xhtml(f"```\n{token}\n```") == f"<pre>{html.escape(token)}</pre>"


def test_plain_table_keeps_legacy_markdown_display() -> None:
    source = "<table><tr><th>One</th><th>Two</th></tr><tr><td>A</td><td>B</td></tr></table>"

    assert xhtml_to_markdown(source) == "| One | Two |\n| --- | --- |\n| A | B |"


@pytest.mark.parametrize(
    "source",
    [
        '<p class="intro">under <u>line</u> and <a href="/x" title="more">link</a></p>',
        '<table><tr><td data-cell="retained">A</td></tr></table>',
    ],
)
def test_lossless_reader_falls_back_when_markdown_cannot_recreate_xml(source: str) -> None:
    markdown = xhtml_to_markdown(source, placeholders=True)

    assert markdown.startswith("{{as:1:storage:block:")
    assert markdown_to_xhtml(markdown) == source


def test_lossless_reader_escapes_literal_token_text() -> None:
    source = "<p>literal {{as:1:storage:inline:thing:not-base64}}</p>"

    markdown = xhtml_to_markdown(source, placeholders=True)
    assert markdown == "literal \\{{as:1:storage:inline:thing:not-base64}}"
    assert markdown_to_xhtml(markdown) == source


def test_markdown_reserved_tokens_are_validated_and_code_spans_stay_literal() -> None:
    token = encode(
        '<ac:link><ri:user ri:account-id="abc" /></ac:link>', source="storage", kind="mention"
    )

    assert markdown_to_xhtml(f"`{token}`") == f"<p><code>{html.escape(token)}</code></p>"
    with pytest.raises(ValueError):
        markdown_to_xhtml("{{as:1:storage:inline:bad:not-base64}}")
    block = encode(
        '<ac:structured-macro ac:name="toc"/>', source="storage", placement="block", kind="toc"
    )
    with pytest.raises(ValueError, match="block storage"):
        markdown_to_xhtml("before " + block)


def test_cross_format_opaque_nodes_fail_closed() -> None:
    from as_engine.converters.storage import adf_to_xhtml, xhtml_to_adf

    with pytest.raises(ValueError):
        xhtml_to_adf('<ac:structured-macro ac:name="jira"/>')
    with pytest.raises(ValueError):
        adf_to_xhtml(
            {"type": "doc", "version": 1, "content": [{"type": "mention", "attrs": {"id": "abc"}}]}
        )


@pytest.mark.parametrize(
    "source",
    [
        " \n<p>before</p><!-- note --><p>after</p>\n",
        '<p>before <ac:link><ri:user ri:account-id="123" /></ac:link> after</p>',
        "<p>literal *stars* and `ticks`</p>",
        '<ac:structured-macro ac:name="custom weird macro"/>',
        "<p><code>{{as:broken}}</code></p>",
    ],
)
def test_public_storage_lossless_read_write(source):
    from as_engine.converters import convert, render

    assert convert(render(source, source="storage"), target="storage") == source


def test_multibacktick_code_span_is_literal():
    token = encode("<ac:link/>", source="storage", kind="mention")
    assert (
        markdown_to_xhtml("``one ` " + token + "``")
        == "<p><code>one ` " + html.escape(token) + "</code></p>"
    )


def test_cdata_terminator_roundtrip():
    from as_engine.converters import convert, render

    storage = convert('```python\na = "]]>"\n```', target="storage")
    assert "]]]]><![CDATA[>" in storage
    assert convert(render(storage, source="storage"), target="storage") == storage


def test_storage_label_edit_is_informational_and_atomic():
    import json

    source = '<ac:structured-macro ac:name="jira"/>'
    markdown = xhtml_to_markdown(source, placeholders=True)
    assert ':jira:"jira":' in markdown
    edited = markdown.replace('"jira"', json.dumps('Label `code` {{as:broken}} " quote'))
    assert markdown_to_xhtml(edited) == source
