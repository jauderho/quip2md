"""Tests for quip2md.enex.

These assert the *fidelity contract* the Apple Notes import now promises:
hyperlinks survive as links, checklist state survives as `<en-todo>`, list
nesting matches the Markdown archive, and unsafe URLs never reach Notes.

The checklist assertions deliberately expect a **flat** run of `<en-todo>`
paragraphs plus a recorded depth: Notes' importer discards checklist
indentation no matter how it is marked up (13 shapes tested live), so the
depth is carried in `NoteEnml.checklist` for `notes_indent` to reapply.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path

import pytest

from quip2md.enex import (
    ChecklistItem,
    NoteEnml,
    _cdata,
    _safe_url,
    build_enex,
    markdown_to_enml,
)


def _render(
    markdown: str,
    *,
    title: str = "Doc",
    quip_url: str | None = None,
    md_dir: Path | None = None,
) -> NoteEnml:
    return markdown_to_enml(
        title=title,
        quip_url=quip_url,
        markdown_text=markdown,
        md_dir=md_dir or Path("."),
    )


# --- Hyperlinks -------------------------------------------------------------


def test_provenance_url_is_a_real_link_labelled_with_the_url() -> None:
    note = _render("body", quip_url="https://quip.com/THREAD0001")
    assert (
        '<div>Source: <a href="https://quip.com/THREAD0001">'
        "https://quip.com/THREAD0001</a></div>" in note.enml
    )


def test_a_blank_line_separates_the_source_link_from_the_document() -> None:
    """Otherwise the note opens with the link welded to its first heading."""
    note = _render("# Heading\n\nbody\n", title="Doc", quip_url="https://quip.com/THREAD0001")
    assert (
        '<a href="https://quip.com/THREAD0001">https://quip.com/THREAD0001</a></div>'
        "<div><br/></div>" in note.enml
    )


def test_a_document_without_a_source_url_gains_no_blank_line() -> None:
    note = _render("body\n", quip_url=None)
    assert not note.enml.startswith(note.enml[: note.enml.index("<en-note>") + 9] + "<div><br/>")


def test_inline_link_keeps_its_descriptive_label() -> None:
    note = _render("See [the docs](https://example.com/page) now.")
    assert '<a href="https://example.com/page">the docs</a>' in note.enml
    assert "(https://example.com/page)" not in note.enml


def test_autolink_does_not_gain_a_redundant_parenthetical() -> None:
    note = _render("<https://example.com/page>")
    assert note.enml.count("https://example.com/page") == 2  # href + label


@pytest.mark.parametrize(
    "scheme", ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd"]
)
def test_unsafe_link_schemes_never_reach_notes_as_links(scheme: str) -> None:
    """CommonMark already refuses these; the payload must contain no such href."""
    note = _render(f"[click]({scheme})")
    assert "<a " not in note.enml
    assert "click" in note.enml


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "file:///x", "", "  "])
def test_safe_url_rejects_anything_outside_the_allowlist(url: str) -> None:
    """Second line of defence, in case a link ever arrives pre-parsed."""
    assert _safe_url(url) is None


def test_unsupported_provenance_scheme_degrades_to_text_with_a_warning() -> None:
    note = _render("body", quip_url="javascript:alert(1)")
    assert "<a " not in note.enml
    assert "javascript:alert(1)" in note.enml
    assert any("unsupported scheme" in warning for warning in note.warnings)


@pytest.mark.parametrize("url", ["https://a.example", "http://a.example", "mailto:a@b.com"])
def test_supported_link_schemes_survive(url: str) -> None:
    note = _render(f"[x]({url})")
    assert f'<a href="{url}">x</a>' in note.enml


# --- Checklists -------------------------------------------------------------


def test_checklist_states_become_en_todo_elements() -> None:
    note = _render("- [x] done\n- [ ] todo\n")
    assert '<div><en-todo checked="true"/>done</div>' in note.enml
    assert "<div><en-todo/>todo</div>" in note.enml
    assert "[x]" not in note.enml
    assert "[ ]" not in note.enml


def test_nested_checklist_is_flattened_but_its_depth_is_recorded() -> None:
    note = _render("- [ ] parent\n  - [x] child\n    - [ ] grandchild\n")
    assert note.checklist == (
        ChecklistItem(text="parent", checked=False, depth=0),
        ChecklistItem(text="child", checked=True, depth=1),
        ChecklistItem(text="grandchild", checked=False, depth=2),
    )
    # Flat in the payload: no <li> wrapping, in document order.
    assert re.findall(r"<en-todo[^>]*/>([^<]+)", note.enml) == ["parent", "child", "grandchild"]
    assert note.needs_indent_pass is True


def test_flat_checklist_needs_no_indent_pass() -> None:
    note = _render("- [x] a\n- [ ] b\n")
    assert note.needs_indent_pass is False


def test_literal_brackets_in_prose_are_not_a_checkbox() -> None:
    note = _render("A line mentioning [x] in passing.\n\n- item [x] mid-text\n")
    assert "en-todo" not in note.enml
    assert note.checklist == ()


def test_checklist_item_keeps_an_inline_link() -> None:
    note = _render("- [ ] see [docs](https://example.com/d)\n")
    assert '<en-todo/>see <a href="https://example.com/d">docs</a>' in note.enml


def test_mixed_checklist_and_plain_items_keeps_the_plain_one_a_bullet() -> None:
    note = _render("- [x] task\n- plain\n")
    assert "<ul><li>plain</li></ul>" in note.enml
    assert any("mixes checklist and plain" in warning for warning in note.warnings)


def test_nested_plain_item_among_checkboxes_keeps_its_depth() -> None:
    """Depth is expressed by nesting through an `<li>`: `<ul>` inside `<ul>` is invalid."""
    note = _render("- [x] task\n  - [ ] sub task\n  - plain sub\n")
    assert "<ul><li><ul><li>plain sub</li></ul></li></ul>" in note.enml


def test_a_plain_item_two_levels_down_nests_through_two_list_items() -> None:
    note = _render("- [x] task\n  - [ ] sub task\n    - [x] sub sub\n    - plain sub sub\n")
    assert "<ul><li><ul><li><ul><li>plain sub sub</li></ul></li></ul></li></ul>" in note.enml
    assert "<ul><ul>" not in note.enml


def test_task_marker_inside_inline_code_is_a_literal_not_a_checkbox() -> None:
    """GFM only accepts a marker that is the item's own leading text."""
    note = _render("- `[x]` literal\n")
    assert "en-todo" not in note.enml
    assert "<ul><li><code>[x]</code> literal</li></ul>" in note.enml
    assert note.checklist == ()


def test_a_loose_task_item_keeps_every_block_it_carries() -> None:
    """Continuation paragraphs and code inside a task item are text, not debris."""
    note = _render("- [ ] first\n\n  second\n\n  ```\n  code\n  ```\n")
    assert len(note.checklist) == 1
    item = note.checklist[0]
    assert item.checked is False
    for word in ("first", "second", "code"):
        assert word in note.enml
    assert any("nested block flattened" in warning for warning in note.warnings)


def test_a_multi_block_task_item_is_identified_by_its_first_line() -> None:
    """Notes splits the item across paragraphs; only the first holds the box.

    `ChecklistItem.text` is what `notes_indent` matches a note's lines against,
    so it has to name the line that actually carries the checkbox. Matching on
    the whole item made five real notes unfindable, and the pass refused them.
    """
    note = _render("- [ ] first\n\n  second\n\n  ```\n  code\n  ```\n")
    assert note.checklist[0].text == "first"


def test_an_item_whose_text_starts_on_the_next_line_skips_the_empty_first() -> None:
    """Quip emits `[ ]` then a break; the first *visible* line is the name."""
    note = _render("- [ ]\n  **Reason 1**\n\n  Reason 2\n")
    assert note.checklist[0].text == "Reason 1"


def test_angle_bracketed_placeholders_survive_verbatim() -> None:
    """`<param>` in prose is a placeholder, not a tag: raw HTML is disabled."""
    note = _render("run with --filter <param> to allow <url>\n")
    assert "&amp;lt;param&amp;gt;" in note.enml
    assert "&amp;lt;url&amp;gt;" in note.enml
    assert "<param>" not in note.enml


def test_angle_brackets_are_escaped_twice_so_notes_cannot_eat_them() -> None:
    """Notes decodes the content twice, so one level of escaping is not enough.

    Verified live: a note carrying `&lt;profile name&gt;` loses the whole
    placeholder, because the second decode turns it into an element. Only
    `&amp;lt;` arrives as literal text.
    """
    note = _render("pf=<profile name>\n")
    # First decode: parsing the ENML as XML. It must still be an entity here,
    # or Notes' own second decode would turn it into an element and eat it.
    once = ET.fromstring(note.enml).findtext("div") or ""
    assert once == "pf=&lt;profile name&gt;"
    # Second decode: the one Notes performs. Now it is literal text again.
    assert unescape(once) == "pf=<profile name>"


# --- List nesting -----------------------------------------------------------


def test_plain_nested_lists_stay_nested() -> None:
    note = _render("- a\n  - b\n    - c\n")
    assert "<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li></ul>" in note.enml


def test_ordered_list_survives_as_ol() -> None:
    note = _render("1. first\n2. second\n")
    assert "<ol><li>first</li><li>second</li></ol>" in note.enml


def test_commonmark_nesting_beats_the_python_markdown_shape() -> None:
    """The regression that put 29.4% of corpus list items at the wrong depth."""
    markdown = (
        "- top\n\n"
        "  - middle\n\n"
        "    - a long paragraph item that used to swallow its own siblings\n\n"
        "  - sibling one\n\n"
        "  - sibling two\n"
    )
    note = _render(markdown)
    # "sibling one"/"sibling two" belong to the same <ul> as "middle", not to
    # the long item's subtree -- python-markdown put them one level deeper.
    assert "</ul></li><li>sibling one</li><li>sibling two</li></ul>" in note.enml


# --- Headings, code, tables, quotes, rules ---------------------------------


def test_headings_clamp_to_the_three_levels_notes_has() -> None:
    note = _render("# One\n\n## Two\n\n### Three\n\n#### Four\n", title="Doc")
    assert "<h1>One</h1>" in note.enml
    assert "<h2>Two</h2>" in note.enml
    assert note.enml.count("<h3>") == 2  # Three and the clamped Four


def test_leading_h1_matching_the_title_is_dropped() -> None:
    note = _render("# Doc\n\nbody\n", title="Doc")
    assert "<h1>" not in note.enml
    assert "<div>body</div>" in note.enml


def test_code_block_becomes_pre() -> None:
    note = _render("```\ndef f():\n    return 1\n```\n")
    assert "<pre>def f():\n    return 1\n</pre>" in note.enml


def test_table_survives() -> None:
    note = _render("| A | B |\n| --- | --- |\n| 1 | 2 |\n")
    assert "<th>A</th>" in note.enml
    assert "<td>1</td>" in note.enml


def test_blockquote_and_rule_are_kept_or_dropped_with_a_warning() -> None:
    note = _render("> quoted\n\n---\n")
    assert "quoted" in note.enml
    assert any("blockquote" in warning for warning in note.warnings)
    assert any("horizontal rule" in warning for warning in note.warnings)


def test_inline_emphasis_survives() -> None:
    note = _render("**bold** and *ital* and `code` and ~~struck~~\n")
    for tag in ("<strong>bold</strong>", "<em>ital</em>", "<code>code</code>", "<s>struck</s>"):
        assert tag in note.enml


# --- Images -----------------------------------------------------------------


def test_local_image_becomes_en_media_plus_resource(tmp_path: Path) -> None:
    assets = tmp_path / "_assets" / "THREAD"
    assets.mkdir(parents=True)
    png = assets / "blob.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    note = _render("![](_assets/THREAD/blob.png)", md_dir=tmp_path)

    assert len(note.resources) == 1
    resource = note.resources[0]
    assert f'<en-media hash="{resource.md5}" type="image/png"/>' in note.enml
    assert resource.data == png.read_bytes()


def test_missing_image_degrades_to_a_warning(tmp_path: Path) -> None:
    note = _render("![](_assets/THREAD/gone.png)", md_dir=tmp_path)
    assert "[missing image: gone.png]" in note.enml
    assert any("missing image" in warning for warning in note.warnings)


# --- .enex assembly ---------------------------------------------------------


def test_build_enex_wraps_every_note() -> None:
    notes = [_render("- [x] a\n", title="One"), _render("body\n", title="Two")]
    document = build_enex(notes)
    assert document.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert document.count("<note>") == 2
    assert "<title>One</title>" in document
    assert "<title>Two</title>" in document
    assert document.rstrip().endswith("</en-export>")


def test_markdown_cannot_smuggle_a_cdata_terminator_into_the_archive() -> None:
    """`]]>` in body text is HTML-escaped long before it reaches the CDATA."""
    document = build_enex([_render("a ]]> b\n")])
    assert "]]&amp;gt;" in document
    assert document.count("]]>") == 1  # only the CDATA section's own terminator


def test_cdata_splits_an_embedded_terminator() -> None:
    """Defence in depth for any payload that does contain a literal `]]>`."""
    assert _cdata("a]]>b") == "<![CDATA[a]]]]><![CDATA[>b]]>"


def test_frontmatter_timestamps_are_converted_to_enex_form() -> None:
    note = markdown_to_enml(
        title="T",
        quip_url=None,
        markdown_text="body",
        md_dir=Path("."),
        created="2013-11-18T15:39:54Z",
        updated="2025-01-15T23:03:41Z",
    )
    document = build_enex([note])
    assert "<created>20131118T153954Z</created>" in document
    assert "<updated>20250115T230341Z</updated>" in document


def test_title_is_xml_escaped() -> None:
    note = _render("body", title="A & B <c>")
    document = build_enex([note])
    assert "<title>A &amp; B &amp;lt;c&amp;gt;</title>" in document
    # Parsed once by XML, once by Notes: back to exactly what was written.
    title = ET.fromstring(document).findtext("./note/title") or ""
    assert unescape(title) == "A & B <c>"


def test_task_marker_without_a_trailing_space_is_still_a_task() -> None:
    """Quip items whose text starts on the next line yield a bare `[ ]` node."""
    note = _render("- [ ]\n  **Reason 1**\n")
    assert "<en-todo/>" in note.enml
    assert "<strong>Reason 1</strong>" in note.enml
    assert len(note.checklist) == 1


def test_empty_task_item_emits_no_checkbox() -> None:
    note = _render("- [x] real\n- [ ]\n")
    assert note.enml.count("en-todo") == 1
    assert len(note.checklist) == 1


def test_tel_links_survive() -> None:
    note = _render("[call](tel:9255551212)")
    assert '<a href="tel:9255551212">call</a>' in note.enml


def test_control_characters_are_stripped_so_the_archive_stays_parseable() -> None:
    """One XML-illegal byte anywhere would make the whole `.enex` unreadable."""
    note = _render("a \x0c body\n", title="Bad\x01Title")
    document = build_enex([note])
    root = ET.fromstring(document)
    assert root.findtext("./note/title") == "BadTitle"
    content = root.findtext("./note/content")
    assert content is not None
    assert "\x0c" not in content
    assert "body" in content
    assert any("control characters removed" in warning for warning in note.warnings)


def test_a_clean_document_reports_no_control_character_warning() -> None:
    assert _render("plain body\n", title="Fine").warnings == ()


def test_marker_without_whitespace_separator_is_not_a_task() -> None:
    note = _render("- [x]nospace\n")
    assert "en-todo" not in note.enml
