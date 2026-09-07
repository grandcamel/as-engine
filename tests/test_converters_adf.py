"""Public converter contract: schema-valid writes and lossless editable reads."""

import hashlib
import json
import subprocess
import sys
from importlib.resources import files

import pytest

from as_engine.converters import adf, convert, render, validate_adf
from as_engine.converters.formats import parse
from as_engine.converters.placeholders import decode, encode


def document(*nodes):
    return {"type": "doc", "version": 1, "content": list(nodes)}


def paragraph(text="hello"):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


@pytest.mark.parametrize(
    "markdown",
    [
        "",
        " ",
        "#",
        "# ",
        "## ",
        "#no-space",
        "####### heading",
        "- ",
        "1. ",
        "```\n```",
        "```python\n```",
        "> ",
        "# Heading\n\nHello **world**",
        "## Two\n### Three\n#### Four\n##### Five\n###### Six",
        "---\n\n> **quote**\n> second line",
        "- **one**\n- two",
        "3. one\n4. two",
        "- parent\n  - child\n    2. inner\n- sibling",
        "plain\ncontinued\n\nnext",
        "**bold** __bold__ *italic* _italic_ `code` ~~strike~~",
        '[label](https://example.test "title")',
        r"\*literal\* and \{braces\}",
        '```c++\nif (x < y) { a = "]]>"; }\n```',
        "````\n```literal```\n````",
    ],
)
def test_markdown_outputs_validate_and_roundtrip(markdown):
    converted = convert(markdown)
    assert validate_adf(converted)
    assert convert(render(converted)) == converted


MENTION = {
    "type": "mention",
    "attrs": {"id": "account:123", "text": "@Jane", "userType": "DEFAULT"},
}


@pytest.mark.parametrize(
    "node",
    [
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [paragraph()]},
        {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [
                {"type": "media", "attrs": {"type": "file", "id": "file-id", "collection": "files"}}
            ],
        },
        {
            "type": "table",
            "attrs": {"layout": "wide", "width": 800},
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableHeader",
                            "attrs": {"colspan": 2, "background": "#ffffff"},
                            "content": [paragraph()],
                        }
                    ],
                }
            ],
        },
        {
            "type": "layoutSection",
            "content": [
                {"type": "layoutColumn", "attrs": {"width": 50}, "content": [paragraph()]},
                {"type": "layoutColumn", "attrs": {"width": 50}, "content": [paragraph("other")]},
            ],
        },
        {
            "type": "taskList",
            "attrs": {"localId": "list"},
            "content": [
                {
                    "type": "taskItem",
                    "attrs": {"localId": "item", "state": "TODO"},
                    "content": [{"type": "text", "text": "Task"}],
                }
            ],
        },
        {"type": "expand", "attrs": {"title": "Details"}, "content": [paragraph()]},
        {"type": "blockCard", "attrs": {"url": "https://example.test/card"}},
    ],
)
def test_opaque_block_retains_entire_subtree(node):
    original = document(node, paragraph("after"))
    assert validate_adf(original)
    markdown = render(original)
    assert f":block:{node['type']}:" in markdown
    restored = convert(markdown.replace("after", "edited"))
    assert restored["content"][0] == node
    assert validate_adf(restored)
    assert restored["content"][1] == paragraph("edited")


def test_mention_survives_adjacent_edits_and_serialized_adf():
    original = document(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Hi "},
                MENTION,
                {"type": "text", "text": ", welcome."},
            ],
        }
    )
    markdown = render(json.dumps(original))
    assert ":inline:mention:" in markdown
    restored = convert(markdown.replace("welcome", "thanks"))
    assert restored["content"][0]["content"][1] == MENTION
    assert validate_adf(restored)
    assert convert(render(original)) == original


@pytest.mark.parametrize(
    "marks",
    [
        [{"type": "underline"}],
        [{"type": "textColor", "attrs": {"color": "#ff0000"}}],
        [{"type": "link", "attrs": {"href": "https://example.test", "id": "identity"}}],
        [{"type": "strong"}, {"type": "em"}],
        [{"type": "em"}, {"type": "strong"}],
        [{"type": "code"}],
        [{"type": "strike"}],
    ],
)
def test_marks_and_literal_punctuation_are_lossless(marks):
    original = document(
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "*literal* `tick` [x] \\ end", "marks": marks}],
        }
    )
    assert validate_adf(original)
    assert convert(render(original)) == original


def test_literal_token_and_code_are_inert():
    token = encode(MENTION, kind="mention")
    for text in [token, "\\" + token, "{{as:broken}}"]:
        original = document(paragraph(text))
        assert convert(render(original)) == original
    converted = convert("```\n" + token + "\n```")
    assert converted["content"][0]["content"][0]["text"] == token
    assert validate_adf(converted)
    assert convert("`" + token + "`")["content"][0]["content"][0]["text"] == token


def test_empty_document_and_empty_paragraph_remain_distinct():
    for original in [
        document(),
        document({"type": "paragraph"}),
        document({"type": "paragraph", "content": []}),
    ]:
        assert convert(render(original)) == original
        assert validate_adf(convert(render(original)))


@pytest.mark.parametrize(
    "value",
    [
        document({"type": "paragraph", "content": [{"type": "text", "text": ""}]}),
        {"type": "doc", "content": []},
        document({"type": "madeUp"}),
    ],
)
def test_pinned_validator_rejects_bad_adf(value):
    with pytest.raises(ValueError, match="Invalid ADF"):
        validate_adf(value)


@pytest.mark.parametrize(
    "token", ["{{as:broken}}", "{{as:1:adf:inline:mention:eA}}", "{{as:1:adf:inline:mention:====}}"]
)
def test_malformed_reserved_tokens_fail(token):
    with pytest.raises(ValueError):
        convert(token)


def test_foreign_tokens_and_wrong_placement_refused():
    with pytest.raises(ValueError):
        convert(encode("<ac:link/>", source="storage", kind="link"))
    with pytest.raises(ValueError):
        convert("before " + encode({"type": "rule"}, placement="block", kind="rule"))
    with pytest.raises(ValueError):
        decode(encode({"type": "text", "text": ""}, kind="text"))


def test_schema_pin_and_import_cost_boundary():
    root = files("as_engine.converters.schema")
    manifest = json.loads(root.joinpath("MANIFEST.json").read_text())
    assert (
        hashlib.sha256(root.joinpath(manifest["file"]).read_bytes()).hexdigest()
        == manifest["sha256"]
    )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                'import sys; import as_engine.converters as c; c.convert("hello"); '
                'assert "jsonschema" not in sys.modules; print("jsonschema absent")'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "jsonschema absent"


@pytest.mark.parametrize("text", ["", "plain", "*Commit:* [abc|https://example.test]", "one\ntwo"])
def test_jira_wiki_adapter(text):
    assert validate_adf(adf.wiki_markup_to_adf(text))


@pytest.mark.parametrize(
    "name,value,expected",
    [
        ("date", "2024-02-29", "2024-02-29"),
        ("duration", "1w 2d 3h 4m 5s", 788645),
        ("duration", "2h30m", 9000),
        ("duration", 0, 0),
        ("duration", "90", 90),
    ],
)
def test_scalar_formats(name, value, expected):
    assert parse(value, name) == expected


@pytest.mark.parametrize(
    "name,value",
    [
        ("date", "2023-02-29"),
        ("date", "2024-1-1"),
        ("date", "2024-01-01T00:00:00Z"),
        ("duration", True),
        ("duration", "-1h"),
        ("duration", "1h1h"),
        ("duration", "1m1h"),
        ("duration", ""),
        ("unknown", "1"),
    ],
)
def test_scalar_formats_refuse_ambiguous_values(name, value):
    with pytest.raises(ValueError):
        parse(value, name)


def test_exported_builders_are_schema_valid():
    nodes = [
        adf.create_paragraph(),
        adf.create_paragraph(text=""),
        adf.create_heading("", level=99),
        adf.create_heading("Title", level=-1),
        adf.create_code_block(""),
        adf.create_code_block("x", "python"),
        adf.create_bullet_list(["one", ""]),
        adf.create_ordered_list(["two", ""], start=3),
        adf.create_blockquote("quote"),
        adf.create_rule(),
        adf.create_table([["A", "B"], ["", "D"]]),
        adf.create_table([["value"]], header=False),
        adf.create_paragraph(content=[adf.create_link("label", "https://example.test")]),
        adf.create_adf_paragraph("marked", bold=True, italic=True, link="https://example.test"),
        adf.create_adf_paragraph(""),
    ]
    for node in nodes:
        assert validate_adf(adf.create_adf_doc([node]))
    with pytest.raises(ValueError, match="nonempty"):
        adf.create_text("")


@pytest.mark.parametrize("value", ["", "   ", "one\ntwo\n\nthree"])
def test_plain_text_adapter(value):
    result = adf.text_to_adf(value)
    assert validate_adf(result)
    assert convert(render(result)) == result


def test_legacy_display_preserves_all_pinned_node_renderings():
    rich = document(
        adf.create_heading("Title", 2),
        adf.create_bullet_list(["one", "two"]),
        adf.create_ordered_list(["three"], 3),
        adf.create_code_block("code", "python"),
        adf.create_blockquote("quote"),
        adf.create_table([["head"], ["cell"]]),
        adf.create_rule(),
        {
            "type": "paragraph",
            "content": [
                adf.create_text("strong", [{"type": "strong"}]),
                adf.create_text("em", [{"type": "em"}]),
                adf.create_text("code", [{"type": "code"}]),
                adf.create_link("link", "https://example.test"),
                adf.create_text("strike", [{"type": "strike"}]),
                {"type": "hardBreak"},
                MENTION,
            ],
        },
        {"type": "expand", "content": [paragraph("unknown container")], "attrs": {"title": "More"}},
    )
    assert validate_adf(rich)
    markdown = render(rich, placeholders=False)
    for expected in [
        "## Title",
        "- one",
        "3. three",
        "```python",
        "> quote",
        "| head |",
        "---",
        "**strong**",
        "*em*",
        "`code`",
        "[link]",
        "~~strike~~",
        "unknown container",
    ]:
        assert expected in markdown
    text = adf.adf_to_text(rich)
    for expected in ["Title", "one", "three", "code", "quote", "head", "cell", "strong"]:
        assert expected in text
    assert adf.adf_to_text({}) == ""
    assert render({}, placeholders=False) == ""


@pytest.mark.parametrize("kwargs", [{"source": "text"}, {"target": "wiki"}])
def test_unsupported_conversion_refused(kwargs):
    with pytest.raises(ValueError):
        convert("hello", **kwargs)


def test_bad_input_types_refused():
    with pytest.raises(TypeError):
        convert({})
    with pytest.raises(TypeError):
        render([])
    with pytest.raises(TypeError):
        render({}, source="storage")
    with pytest.raises(ValueError):
        render({}, source="wiki")
    with pytest.raises(ValueError):
        render({"type": "doc", "version": 2, "content": []})


@pytest.mark.parametrize("markdown", ["**`code`**", "*`code`*", "~~`code`~~"])
def test_inline_code_takes_precedence_over_schema_forbidden_emphasis(markdown):
    result = convert(markdown)
    assert validate_adf(result)
    assert result["content"][0]["content"] == [
        {"type": "text", "text": "code", "marks": [{"type": "code"}]}
    ]
    assert convert(render(result)) == result


@pytest.mark.parametrize(
    "label",
    [
        "Renamed person",
        'Quote " and : colon',
        "Unicode José / \\ backslash",
        "Literal `code` and {{as:broken}}",
    ],
)
def test_placeholder_label_edits_do_not_change_adf(label):
    original = document({"type": "paragraph", "content": [MENTION]})
    markdown = render(original)
    assert ':mention:"@Jane":' in markdown
    edited = markdown.replace('"@Jane"', json.dumps(label, ensure_ascii=False))
    assert convert(edited) == original
    assert validate_adf(convert(edited))


def test_panel_and_media_labels_are_readable():
    panel = {
        "type": "panel",
        "attrs": {"panelType": "info"},
        "content": [paragraph("Read these notes")],
    }
    media = {
        "type": "mediaSingle",
        "attrs": {"layout": "center"},
        "content": [
            {
                "type": "media",
                "attrs": {
                    "type": "external",
                    "url": "https://example.test/x",
                    "alt": "Architecture diagram",
                },
            }
        ],
    }
    original = document(panel, media)
    markdown = render(original)
    assert ':panel:"info: Read these notes":' in markdown
    assert ':mediaSingle:"Architecture diagram":' in markdown
    assert convert(markdown) == original
    assert validate_adf(convert(markdown))
