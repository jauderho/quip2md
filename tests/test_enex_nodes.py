"""Node-by-node coverage of the Markdown -> ENML renderer.

`tests/test_enex.py` pins the *fidelity contract* (links survive, checklists
become `<en-todo>`, unsafe schemes never reach Notes). This module covers the
renderer's remaining shapes and its degradation paths: bare text nodes between
blocks, unknown elements, `<br/>`, remote and empty images, a task marker with
no text node of its own, and the `.enex` assembly's timestamp/resource
handling.

The renderer parses Markdown with raw HTML *disabled*, so the block walker's
non-Markdown branches can no longer be reached through `markdown_to_enml`.
They are still live -- the walker is handed whatever soup it is given -- so the
tests for them drive `_render_block` over a fragment built with `bs4` directly
(`_render_html`) instead of smuggling the markup through the parser.

Every assertion that concerns the archive as a *document* parses it with
`xml.etree.ElementTree` rather than matching substrings: an `.enex` that Notes
cannot parse is worthless no matter what its bytes contain.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from quip2md.enex import (
    EnexResource,
    NoteEnml,
    _attr,
    _clone,
    _enex_timestamp,
    _first_text_node,
    _normalize_timestamp,
    _render_block,
    _render_inline_node,
    _RenderState,
    build_enex,
    markdown_to_enml,
)


def _render(markdown: str, *, title: str = "Doc", md_dir: Path | None = None) -> NoteEnml:
    return markdown_to_enml(
        title=title,
        quip_url=None,
        markdown_text=markdown,
        md_dir=md_dir or Path("."),
    )


def _body(note: NoteEnml) -> str:
    """The `<en-note>` payload, without the prologue."""
    return note.enml.split("<en-note>", 1)[1].removesuffix("</en-note>")


def _render_html(fragment: str) -> _RenderState:
    """Walk `fragment` through the block renderer, bypassing the Markdown parser."""
    state = _RenderState(md_dir=Path("."))
    for child in BeautifulSoup(fragment, "html.parser").contents:
        _render_block(child, state, depth=0)
    return state


# --- Block-level node types -------------------------------------------------


def test_bare_text_between_blocks_becomes_its_own_paragraph() -> None:
    """A text node directly under the root is a paragraph of its own."""
    state = _render_html("<div>a</div>\nbare tail text\n")
    assert "<div>bare tail text</div>" in state.blocks


def test_unknown_block_element_degrades_to_a_div() -> None:
    state = _render_html("<section>raw block</section>")
    assert state.blocks == ["<div>raw block</div>"]


def test_render_block_ignores_a_node_that_is_neither_tag_nor_string() -> None:
    """Defensive branch: the walker must never raise on an unexpected node."""
    state = _RenderState(md_dir=Path("."))
    _render_block(object(), state, depth=0)
    assert state.blocks == []


def test_render_inline_ignores_a_node_that_is_neither_tag_nor_string() -> None:
    state = _RenderState(md_dir=Path("."))
    assert _render_inline_node(object(), state) == ""


def test_whitespace_only_text_node_produces_no_paragraph() -> None:
    state = _render_html("<div>a</div>\n   \n")
    assert state.blocks == ["<div>a</div>"]


def test_hard_break_becomes_a_br_element() -> None:
    note = _render("line one  \nline two\n")
    assert "<br/>" in note.enml
    assert "line one" in note.enml
    assert "line two" in note.enml


def test_inline_block_containers_are_unwrapped_not_duplicated() -> None:
    """A `<div>`/`<ul>` nested inside a paragraph keeps its text, loses its tag."""
    state = _render_html("<p>para <div>inner</div> end</p>")
    assert state.blocks == ["<div>para inner end</div>"]


def test_inline_anchor_with_an_unsafe_scheme_is_stripped_with_a_warning() -> None:
    """The allowlist is the only guard on an anchor the walker is handed."""
    state = _render_html('<p>a <a href="javascript:alert(1)">x</a> b</p>')
    assert state.blocks == ["<div>a x b</div>"]
    assert any("unsupported scheme" in warning for warning in state.warnings)


def test_angle_bracketed_prose_is_escaped_rather_than_parsed_as_a_tag() -> None:
    """Raw HTML is off: `<param>` in prose is a placeholder, not an element."""
    note = _render("run with --filter <param> to allow <url>\n")
    assert "<div>run with --filter &amp;lt;param&amp;gt; to allow &amp;lt;url&amp;gt;</div>" in (
        note.enml
    )
    root = ET.fromstring(note.enml)
    # Escaped twice on purpose: Notes decodes the content a second time.
    assert root.findtext("div") == "run with --filter &lt;param&gt; to allow &lt;url&gt;"
    assert unescape(root.findtext("div") or "") == "run with --filter <param> to allow <url>"


@pytest.mark.parametrize(
    "node",
    [
        Comment("hidden"),
        BeautifulSoup("<!DOCTYPE html>", "html.parser").contents[0],
    ],
)
def test_markup_declaration_nodes_are_dropped_from_blocks(node: NavigableString) -> None:
    state = _RenderState(md_dir=Path("."))
    _render_block(node, state, depth=0)
    assert state.blocks == []


def test_a_comment_inside_a_paragraph_is_not_rendered() -> None:
    soup = BeautifulSoup("<p>before after</p>", "html.parser")
    paragraph = soup.p
    assert paragraph is not None
    paragraph.insert(1, Comment(" secret "))
    state = _RenderState(md_dir=Path("."))
    _render_block(paragraph, state, depth=0)
    assert state.blocks == ["<div>before after</div>"]
    assert "secret" not in "".join(state.blocks)


# --- Task-marker edge cases -------------------------------------------------


@pytest.mark.parametrize(
    ("markdown", "checked"),
    [
        ("- [X] upper\n", True),
        ("- [x] lower\n", True),
        ("- [ ] open\n", False),
    ],
)
def test_marker_case_is_normalized(markdown: str, checked: bool) -> None:
    note = _render(markdown)
    assert [item.checked for item in note.checklist] == [checked]


def test_marker_followed_by_a_tab_is_a_task() -> None:
    note = _render("- [ ]\ttabbed\n")
    assert [item.text for item in note.checklist] == ["tabbed"]


def test_marker_immediately_followed_by_a_line_break_is_a_task() -> None:
    """GFM accepts any whitespace after the marker, a line break included."""
    note = _render("- [x]\n  text on the next line\n")
    assert [(item.text, item.checked) for item in note.checklist] == [
        ("text on the next line", True)
    ]
    assert '<en-todo checked="true"/>text on the next line' in note.enml
    assert "[x]" not in note.enml


def test_a_list_item_that_opens_with_a_sublist_is_not_a_task() -> None:
    """`_first_text_node` stops at a nested list rather than borrowing its text."""
    note = _render("- \n  - [x] child\n")
    assert "<ul><li></li></ul>" in note.enml
    assert [item.text for item in note.checklist] == ["child"]


def test_task_inside_a_blockquote_is_still_recorded() -> None:
    note = _render("> - [ ] quoted task\n")
    assert "<blockquote><div><en-todo/>quoted task</div></blockquote>" in note.enml
    assert [item.text for item in note.checklist] == ["quoted task"]


def test_nested_task_under_a_plain_bullet_records_its_depth() -> None:
    note = _render("- plain parent\n  - [x] nested task\n")
    depths = {item.text: item.depth for item in note.checklist}
    assert depths == {"nested task": 1}


# --- Images -----------------------------------------------------------------


def test_remote_image_degrades_to_a_link_rather_than_a_dangling_reference() -> None:
    note = _render("![alt](https://cdn.example.com/i.png)")
    assert '<a href="https://cdn.example.com/i.png">alt</a>' in note.enml
    assert note.resources == ()


def test_remote_image_without_alt_text_is_labelled_with_its_url() -> None:
    note = _render("![](https://cdn.example.com/i.png)")
    assert '<a href="https://cdn.example.com/i.png">https://cdn.example.com/i.png</a>' in note.enml


def test_image_with_an_empty_src_is_reported() -> None:
    note = _render("![](  )")
    assert "[missing image: (empty src)]" in note.enml
    assert any("empty src" in warning for warning in note.warnings)


def test_mime_type_is_guessed_from_the_file_extension(tmp_path: Path) -> None:
    asset = tmp_path / "_assets" / "THREAD0013"
    asset.mkdir(parents=True)
    (asset / "blob.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    note = _render("![](_assets/THREAD0013/blob.jpg)", md_dir=tmp_path)
    assert [resource.mime for resource in note.resources] == ["image/jpeg"]


def test_an_unrecognized_extension_falls_back_to_octet_stream(tmp_path: Path) -> None:
    asset = tmp_path / "_assets" / "THREAD0013"
    asset.mkdir(parents=True)
    (asset / "blob.quipblob").write_bytes(b"opaque")
    note = _render("![](_assets/THREAD0013/blob.quipblob)", md_dir=tmp_path)
    assert [resource.mime for resource in note.resources] == ["application/octet-stream"]
    assert any("blob.quipblob" in warning for warning in note.warnings)


@pytest.mark.parametrize(
    ("payload", "mime"),
    [
        (b"\x89PNG\r\n\x1a\nIHDR", "image/png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
        (b"GIF87a\x01\x00", "image/gif"),
        (b"GIF89a\x01\x00", "image/gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"BM\x36\x00\x00\x00", "image/bmp"),
    ],
)
def test_an_extensionless_image_is_identified_by_its_magic_bytes(
    tmp_path: Path, payload: bytes, mime: str
) -> None:
    """Quip blob names often carry no extension at all."""
    asset = tmp_path / "_assets" / "THREAD0013"
    asset.mkdir(parents=True)
    (asset / "blob").write_bytes(payload)
    note = _render("![](_assets/THREAD0013/blob)", md_dir=tmp_path)
    assert [resource.mime for resource in note.resources] == [mime]
    assert f'type="{mime}"' in note.enml
    assert note.warnings == ()


def test_an_extensionless_non_image_stays_a_binary_blob_with_a_warning(tmp_path: Path) -> None:
    asset = tmp_path / "_assets" / "THREAD0013"
    asset.mkdir(parents=True)
    (asset / "blob").write_bytes(b"not an image at all")
    note = _render("![](_assets/THREAD0013/blob)", md_dir=tmp_path)
    assert [resource.mime for resource in note.resources] == ["application/octet-stream"]
    assert any("unrecognized image type" in warning for warning in note.warnings)


def test_the_same_image_twice_yields_one_resource_keyed_by_its_hash(tmp_path: Path) -> None:
    asset = tmp_path / "_assets" / "THREAD0013"
    asset.mkdir(parents=True)
    (asset / "blob.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    note = _render(
        "![](_assets/THREAD0013/blob.png)\n\n![](_assets/THREAD0013/blob.png)\n",
        md_dir=tmp_path,
    )
    assert len(note.resources) == 1
    assert note.enml.count("<en-media") == 2


def test_a_directory_where_an_image_should_be_is_a_missing_image(tmp_path: Path) -> None:
    (tmp_path / "_assets" / "THREAD0013" / "blob.png").mkdir(parents=True)
    note = _render("![](_assets/THREAD0013/blob.png)", md_dir=tmp_path)
    assert "[missing image: blob.png]" in note.enml
    assert note.resources == ()


# --- Small helpers ----------------------------------------------------------


def test_attr_joins_a_multi_valued_attribute() -> None:
    """bs4 types some attributes as lists; the renderer must not stringify one."""
    tag = BeautifulSoup('<a rel="noopener noreferrer">x</a>', "html.parser").a
    assert tag is not None
    assert _attr(tag, "rel") == "noopener noreferrer"
    assert _attr(tag, "href") == ""


def test_clone_deep_copies_without_touching_the_source() -> None:
    soup = BeautifulSoup("<span>a<b>c</b></span>", "html.parser")
    original = soup.span
    assert original is not None
    copy = _clone(original)
    assert isinstance(copy, Tag)
    assert str(copy) == str(original)
    copy.append(NavigableString("mutated"))
    assert "mutated" not in str(original)


# --- `.enex` assembly -------------------------------------------------------


def test_build_enex_is_well_formed_xml_with_one_note_element_per_note() -> None:
    notes = [_render("- [x] a\n", title="One"), _render("body\n", title="Two")]
    root = ET.fromstring(build_enex(notes))
    assert root.tag == "en-export"
    assert [note.findtext("title") for note in root.findall("note")] == ["One", "Two"]


def test_each_notes_enml_payload_is_itself_well_formed_xml() -> None:
    markdown = (
        "# Heading\n\n"
        "text with **bold** and a [link](https://example.com/p)\n\n"
        "- [x] done\n- [ ] todo\n  - [ ] child\n\n"
        "1. one\n2. two\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "```\ncode & <angle>\n```\n\n"
        "> quoted\n\n---\n"
    )
    note = _render(markdown)
    root = ET.fromstring(note.enml)
    assert root.tag == "en-note"


def test_a_resource_is_emitted_as_base64_with_its_filename(tmp_path: Path) -> None:
    asset = tmp_path / "_assets" / "THREAD0013"
    asset.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\nfake-bytes"
    (asset / "blob.png").write_bytes(payload)

    note = _render("![](_assets/THREAD0013/blob.png)", md_dir=tmp_path)
    root = ET.fromstring(build_enex([note]))
    resource = root.find("./note/resource")
    assert resource is not None
    data = resource.findtext("data")
    assert data is not None
    assert base64.b64decode(data) == payload
    assert resource.findtext("mime") == "image/png"
    assert resource.findtext("./resource-attributes/file-name") == "blob.png"


def test_a_cdata_terminator_in_a_resource_free_note_still_parses() -> None:
    root = ET.fromstring(build_enex([_render("a ]]> b\n")]))
    content = root.findtext("./note/content")
    assert content is not None
    assert "]]&amp;gt;" in content or "]]>" in content
    ET.fromstring(content)


def test_an_unparseable_frontmatter_timestamp_falls_back_to_the_export_stamp() -> None:
    note = markdown_to_enml(
        title="T",
        quip_url=None,
        markdown_text="body",
        md_dir=Path("."),
        created="not-a-timestamp",
        updated=None,
    )
    root = ET.fromstring(build_enex([note], exported=datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)))
    assert root.findtext("./note/created") == "20260304T050607Z"
    assert root.findtext("./note/updated") == "20260304T050607Z"


def test_updated_defaults_to_created_when_only_created_is_known() -> None:
    note = markdown_to_enml(
        title="T",
        quip_url=None,
        markdown_text="body",
        md_dir=Path("."),
        created="2013-11-18T15:39:54Z",
    )
    root = ET.fromstring(build_enex([note]))
    assert root.findtext("./note/updated") == "20131118T153954Z"


def test_a_naive_export_datetime_is_stamped_verbatim() -> None:
    """No timezone means no conversion -- guessing one would shift every note."""
    assert _enex_timestamp(datetime(2026, 3, 4, 5, 6, 7)) == "20260304T050607Z"


def test_an_aware_export_datetime_is_converted_to_utc() -> None:
    aware = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    assert _enex_timestamp(aware) == "20260304T050607Z"


@pytest.mark.parametrize("value", [None, "", "2013-11-18", "2013-11-18T15:39:54+00:00"])
def test_normalize_timestamp_rejects_anything_but_the_exporters_own_form(value: str | None) -> None:
    assert _normalize_timestamp(value) is None


def test_an_empty_document_still_yields_a_body() -> None:
    """Notes shows nothing at all for an empty `<en-note>`, so emit a blank line."""
    note = _render("")
    assert "<en-note><div><br/></div></en-note>" in note.enml


def test_needs_indent_pass_is_false_without_a_checklist() -> None:
    assert NoteEnml(title="T", enml="<x/>").needs_indent_pass is False


def test_resource_equality_is_by_value() -> None:
    """`EnexResource` is frozen, so dedup by hash is safe across notes."""
    a = EnexResource(md5="d", mime="image/png", filename="f.png", data=b"x")
    b = EnexResource(md5="d", mime="image/png", filename="f.png", data=b"x")
    assert a == b


def test_first_text_node_is_none_for_an_item_with_no_text_at_all() -> None:
    """An image-only item has nothing to match a task marker against."""
    li = BeautifulSoup("<li><img src='x.png'/></li>", "html.parser").li
    assert li is not None
    assert _first_text_node(li) is None


def test_an_anchor_with_an_empty_href_is_dropped_without_a_warning() -> None:
    """There is no URL to report, so the label is simply kept as text."""
    state = _render_html('<p>a <a href="">label</a> b</p>')
    assert state.blocks == ["<div>a label b</div>"]
    assert state.warnings == []


def test_a_task_item_whose_sublist_comes_first_keeps_the_sublist_separate() -> None:
    note = _render("- [x] task\n  - sub one\n  - sub two\n")
    assert '<en-todo checked="true"/>task' in note.enml
    assert "sub one" in note.enml
    assert "sub two" in note.enml
    assert [item.text for item in note.checklist] == ["task"]


def test_an_empty_unknown_block_adds_nothing_to_the_body() -> None:
    """A whitespace-only element must not become a stray blank paragraph."""
    assert _render_html("<section>   </section>").blocks == []
