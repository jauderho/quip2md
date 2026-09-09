"""Tests for quip2md.convert.

Golden-file tests run against every real Quip HTML fixture in
`tests/fixtures/` (captured live by T1's recon script, see
`docs/API_NOTES.md`); their expected `.md` outputs in `tests/golden/` were
eyeballed against the source HTML for fidelity before being committed as the
regression baseline. Targeted unit tests below exercise individual element
types with minimal, single-quoted, Quip-style HTML snippets -- including a
few element types (ordered lists, checklists, `@`-mentions) that do not
appear in any real fixture and are therefore unverified against ground
truth; see the module docstring in `convert.py` for details.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from quip2md.convert import (
    ZERO_WIDTH_SPACE,
    AssetResolver,
    build_frontmatter,
    html_to_markdown,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_DIR = Path(__file__).parent / "golden"

FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*.html"))

# Matches a backslash-escaped markdown-special character, the inverse of
# markdownify's own escaping -- used to normalize text for substring
# comparison in the text-preservation property test.
_UNESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def _default_resolver(thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
    ext = f".{suggested_ext}" if suggested_ext else ""
    return f"_assets/{thread_id}/{blob_id}{ext}"


class RecordingResolver:
    """An AssetResolver that records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
        self.calls.append((thread_id, blob_id, suggested_ext))
        return _default_resolver(thread_id, blob_id, suggested_ext)


def _normalize_for_comparison(text: str) -> str:
    text = text.replace(ZERO_WIDTH_SPACE, "")
    text = _UNESCAPE_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# --- Golden-file tests: one per real Quip HTML fixture --------------------


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_golden_output_matches(fixture_path: Path) -> None:
    html = fixture_path.read_text(encoding="utf-8")
    result = html_to_markdown(html, _default_resolver)

    golden_path = GOLDEN_DIR / f"{fixture_path.stem}.md"
    expected = golden_path.read_text(encoding="utf-8")

    assert result.markdown == expected


# --- Text-preservation property test ---------------------------------------


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_no_text_loss(fixture_path: Path) -> None:
    html = fixture_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    result = html_to_markdown(html, _default_resolver)

    normalized_markdown = _normalize_for_comparison(result.markdown)

    missing = []
    for node in soup.find_all(string=True):
        normalized_node = _normalize_for_comparison(str(node))
        if normalized_node and normalized_node not in normalized_markdown:
            missing.append(normalized_node)

    assert not missing, f"visible text dropped from {fixture_path.name}: {missing[:10]!r}"


# --- Resolver-call assertions for the image fixture -------------------------


def test_image_fixture_calls_resolver_with_correct_ids() -> None:
    html = (FIXTURES_DIR / "doc_sample_THREAD0003.html").read_text(encoding="utf-8")
    resolver = RecordingResolver()

    result = html_to_markdown(html, resolver)

    assert resolver.calls == [("THREAD0003", "BLOB0000000000000001", None)]
    assert "![](_assets/THREAD0003/BLOB0000000000000001)" in result.markdown
    assert result.warnings == ()


# --- Targeted element unit tests --------------------------------------------


def test_heading_levels() -> None:
    html = "<h1 id='a'>Title</h1><h2 id='b'>Sub</h2><h3 id='c'>SubSub</h3>"
    result = html_to_markdown(html, _default_resolver)
    assert "# Title" in result.markdown
    assert "## Sub" in result.markdown
    assert "### SubSub" in result.markdown


def test_nested_mixed_lists_quip_sibling_style() -> None:
    """Quip's actual nesting shape: sub-<ul> as a sibling of the owning <li>."""
    html = (
        "<div data-section-style='5'><ul id='x'>"
        "<li id='1' class='' style='' value='1'><span id='1'>Top</span><br/></li>"
        "<li id='2' class='parent' style=''><span id='2'>Parent</span><br/></li>"
        "<ul><li id='3' class='' style=''><span id='3'>Child</span><br/></li></ul>"
        "<li id='4' class='' style=''><span id='4'>After</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    # Compare line-by-line with trailing whitespace stripped: markdownify
    # emits a GFM hard-break ("  ") at the end of "Parent" because the
    # source <li> ends in <br/>, which is cosmetic and does not change the
    # nesting structure below.
    lines = [line.rstrip() for line in result.markdown.splitlines()]
    assert lines == ["- Top", "- Parent", "  - Child", "- After"]


def test_ordered_list() -> None:
    html = "<ol><li>first</li><li>second</li></ol>"
    result = html_to_markdown(html, _default_resolver)
    assert "1. first" in result.markdown
    assert "2. second" in result.markdown


def test_numbered_list_is_recovered_from_section_style_6() -> None:
    """Quip emits a numbered list as a <ul>; only the wrapper says otherwise."""
    html = (
        "<div class='' data-section-style='6' style=''><ul id='x'>"
        "<li class='' id='1' style='' value='1'><span id='1'>Step one</span><br/></li>"
        "<li class='' id='2' style=''><span id='2'>Step two</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    lines = [line.rstrip() for line in result.markdown.splitlines() if line.strip()]
    assert lines == ["1. Step one", "2. Step two"]


def test_checklist_from_section_style_7_marks_both_states() -> None:
    """Real Quip markup: only checked items carry a class; unchecked are bare."""
    html = (
        "<div class='' data-section-style='7' style=''><ul id='x'>"
        "<li class='checked' id='1' style='' value='1'><span id='1'>First task</span><br/></li>"
        "<li class='' id='2' style=''><span id='2'>Second task</span><br/></li>"
        "<li class='checked' id='3' style=''><span id='3'>Third task</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    lines = [line.rstrip() for line in result.markdown.splitlines() if line.strip()]
    assert lines == ["- [x] First task", "- [ ] Second task", "- [x] Third task"]


def test_nested_checklist_marks_every_depth() -> None:
    """Sub-lists inherit the wrapper's style, in Quip's sibling-<ul> shape."""
    html = (
        "<div class='' data-section-style='7' style=''><ul id='x'>"
        "<li class='parent' id='1' style='' value='1'><span id='1'>Parent item</span><br/></li>"
        "<ul>"
        "<li class='checked' id='2' style=''><span id='2'>Child item</span><br/></li>"
        "<li class='' id='3' style=''><span id='3'>Kenji</span><br/></li>"
        "<li class='checked parent' id='4' style=''><span id='4'>Fee</span><br/></li>"
        "<ul><li class='checked' id='5' style=''><span id='5'>Paid</span><br/></li></ul>"
        "</ul>"
        "<li class='' id='6' style=''><span id='6'>NTTD</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    lines = [line.rstrip() for line in result.markdown.splitlines() if line.strip()]
    assert lines == [
        "- [ ] Parent item",
        "  - [x] Child item",
        "  - [ ] Kenji",
        "  - [x] Fee",
        "    - [x] Paid",
        "- [ ] NTTD",
    ]


def test_bullet_list_section_style_5_is_left_alone() -> None:
    html = (
        "<div data-section-style='5'><ul id='x'>"
        "<li class='' id='1' style=''><span id='1'>Plain</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert [line.rstrip() for line in result.markdown.splitlines() if line.strip()] == ["- Plain"]
    assert result.warnings == ()


def test_unknown_list_section_style_warns_but_keeps_text() -> None:
    html = (
        "<div data-section-style='99'><ul id='x'>"
        "<li class='' id='1' style=''><span id='1'>Mystery</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "- Mystery" in result.markdown
    assert any("section style '99'" in warning for warning in result.warnings)


def test_checklist_item_is_marked_once_only() -> None:
    """A `checked` <li> inside a style-7 wrapper must not get a double marker."""
    html = (
        "<div data-section-style='7'><ul id='x'>"
        "<li class='checked' id='1' style=''><span id='1'>Done</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert result.markdown.count("[x]") == 1
    assert "[ ]" not in result.markdown


def test_checklist_checked_and_unchecked() -> None:
    html = (
        "<ul>"
        "<li class='checked'><span>Done thing</span></li>"
        "<li class='checklist'><span>Todo thing</span></li>"
        "</ul>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "- [x] Done thing" in result.markdown
    assert "- [ ] Todo thing" in result.markdown


def test_code_block_with_language_hint() -> None:
    html = "<pre class='prettyprint lang-python'>def f():<br/>    return 1<br/></pre>"
    result = html_to_markdown(html, _default_resolver)
    assert "```python\ndef f():\n    return 1\n```" in result.markdown


def test_code_block_without_language_hint() -> None:
    html = "<pre class='prettyprint'>echo hi<br/></pre>"
    result = html_to_markdown(html, _default_resolver)
    assert "```\necho hi\n```" in result.markdown


def test_code_block_preserves_two_blank_lines() -> None:
    """Two blank lines inside a fenced code block (PEP8 spacing) must survive.

    Regression for the bug where `_tidy_blank_lines` ran *after* code-block
    placeholders were substituted back, so its `\n{3,} -> \n\n` collapse ate
    the second blank line of PEP8-style spacing inside the restored fence.
    """
    html = (
        "<pre class='prettyprint lang-python'>def f():<br/>    pass<br/>"
        "<br/><br/>def g():<br/>    pass<br/></pre>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "```python\ndef f():\n    pass\n\n\ndef g():\n    pass\n```" in result.markdown


def test_code_block_preserves_whitespace_only_line() -> None:
    """A whitespace-only code line (indentation only) must be preserved verbatim.

    Regression for the bug where `_tidy_blank_lines` ran *after* code-block
    placeholders were substituted back, so its `line if line.strip() else ""`
    branch blanked indentation-only lines inside the restored fence.
    """
    html = "<pre class='prettyprint'>a<br/>    <br/>b<br/></pre>"
    result = html_to_markdown(html, _default_resolver)
    assert "```\na\n    \nb\n```" in result.markdown


def test_code_block_blank_lines_not_collapsed_amid_prose() -> None:
    """Blank-line tidy still applies to surrounding prose with a code block.

    Guarantees the reorder left the prose-cleaning behaviour intact: a run of
    blank lines *outside* a fenced code block is still collapsed to a single
    blank line, while the code block's own interior spacing is untouched.
    """
    html = (
        "<p>before</p><p><br/></p><p><br/></p>"
        "<pre class='prettyprint'>x<br/><br/><br/>y<br/></pre>"
        "<p><br/></p><p><br/></p><p>after</p>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert result.markdown == "before\n\n```\nx\n\n\ny\n```\n\nafter\n"


def test_blockquote() -> None:
    html = "<blockquote>Quoted text</blockquote>"
    result = html_to_markdown(html, _default_resolver)
    assert "> Quoted text" in result.markdown


def test_horizontal_rule() -> None:
    html = "<p>Above</p><hr/><p>Below</p>"
    result = html_to_markdown(html, _default_resolver)
    assert "---" in result.markdown


def test_bold_italic_strikethrough_links() -> None:
    html = (
        "<p><b>bold</b> <i>italic</i> <del>gone</del> <s>also gone</s> "
        "<a href='https://example.com'>link</a></p>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "**bold**" in result.markdown
    assert "*italic*" in result.markdown
    assert "~~gone~~" in result.markdown
    assert "~~also gone~~" in result.markdown
    assert "[link](https://example.com)" in result.markdown


def test_table_escapes_pipe_in_cells() -> None:
    html = (
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>has | pipe</td><td>plain</td></tr></tbody></table>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "| has \\| pipe | plain |" in result.markdown
    assert "| A | B |" in result.markdown
    assert "| --- | --- |" in result.markdown


def test_wide_table_flag_set_over_30_columns() -> None:
    headers = "".join(f"<th>C{i}</th>" for i in range(31))
    cells = "".join(f"<td>{i}</td>" for i in range(31))
    html = f"<table><thead><tr>{headers}</tr></thead><tbody><tr>{cells}</tr></tbody></table>"
    result = html_to_markdown(html, _default_resolver)
    assert result.wide_table is True


def test_narrow_table_does_not_set_wide_flag() -> None:
    html = "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>"
    result = html_to_markdown(html, _default_resolver)
    assert result.wide_table is False


def test_person_mention_becomes_plain_text() -> None:
    html = (
        "<p>Assigned to <span class='mention-inline' data-mention-type='user'>Jane Doe</span></p>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "Jane Doe" in result.markdown
    assert "mention" not in result.markdown.lower()


def test_document_mention_becomes_link() -> None:
    html = (
        "<p>See <a class='mention-inline' data-mention-type='link' "
        "href='https://quip.com/AbCdEfGhIjKl'>Design Doc</a></p>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "[Design Doc](https://quip.com/AbCdEfGhIjKl)" in result.markdown


def test_date_mention_becomes_plain_text() -> None:
    html = "<p>Due <span class='mention-inline date-mention'>Jan 1, 2026</span></p>"
    result = html_to_markdown(html, _default_resolver)
    assert "Jan 1, 2026" in result.markdown
    assert "mention" not in result.markdown.lower()


def test_image_blob_variants_resolved() -> None:
    resolver = RecordingResolver()
    html = (
        "<img src='/blob/AAA/bbb'>"
        "<img src='/-/blob/CCC/ddd'>"
        "<img src='https://platform.quip.com/blob/EEE/fff'>"
    )
    html_to_markdown(html, resolver)
    assert resolver.calls == [
        ("AAA", "bbb", None),
        ("CCC", "ddd", None),
        ("EEE", "fff", None),
    ]


def test_image_extension_hint_inferred_when_present() -> None:
    resolver = RecordingResolver()
    html = "<img src='/blob/AAA/bbb.png'>"
    html_to_markdown(html, resolver)
    assert resolver.calls == [("AAA", "bbb.png", "png")]


def test_unrecognized_image_src_does_not_crash_and_warns() -> None:
    html = "<img src='https://example.com/not-a-blob.png'>"
    result = html_to_markdown(html, _default_resolver)
    assert len(result.warnings) == 1
    assert "blob pattern" in result.warnings[0]


def test_unknown_element_unwraps_and_warns() -> None:
    html = "<p>Type @ to <control id='x'>insert</control></p>"
    result = html_to_markdown(html, _default_resolver)
    assert "Type @ to insert" in result.markdown
    assert len(result.warnings) == 1
    assert "<control>" in result.warnings[0]


def test_zero_width_space_stripped() -> None:
    html = f"<p class='line'>{ZERO_WIDTH_SPACE}</p><p>real text</p>"
    result = html_to_markdown(html, _default_resolver)
    assert ZERO_WIDTH_SPACE not in result.markdown
    assert "real text" in result.markdown


def test_malformed_input_does_not_raise() -> None:
    html = "<ul><li>unclosed<div><span>broken<p>nesting</ul>"
    result = html_to_markdown(html, _default_resolver)
    assert "unclosed" in result.markdown
    assert "broken" in result.markdown
    assert "nesting" in result.markdown


# --- build_frontmatter -------------------------------------------------------


def test_build_frontmatter_basic_fields() -> None:
    frontmatter = build_frontmatter(
        quip_id="AbCdEfGhIjKl",
        quip_url="https://quip.com/AbCdEfGhIjKl",
        title="My Doc",
        created_usec=1_700_000_000_000_000,
        updated_usec=1_700_100_000_000_000,
        exported=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    assert frontmatter.startswith("---\n")
    assert frontmatter.endswith("---\n")
    assert 'quip_id: "AbCdEfGhIjKl"' in frontmatter
    assert 'quip_url: "https://quip.com/AbCdEfGhIjKl"' in frontmatter
    assert 'title: "My Doc"' in frontmatter
    assert 'exported: "2024-01-15T12:00:00Z"' in frontmatter


def test_build_frontmatter_usec_conversion() -> None:
    # 1_700_000_000 seconds -> 2023-11-14T22:13:20Z
    frontmatter = build_frontmatter(
        quip_id="x",
        quip_url="https://quip.com/x",
        title="t",
        created_usec=1_700_000_000_000_000,
        updated_usec=1_700_000_000_000_000,
        exported=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert 'created: "2023-11-14T22:13:20Z"' in frontmatter
    assert 'updated: "2023-11-14T22:13:20Z"' in frontmatter


def test_build_frontmatter_escapes_yaml_hostile_title() -> None:
    frontmatter = build_frontmatter(
        quip_id="x",
        quip_url="https://quip.com/x",
        title='Title: "quoted" \\ backslash',
        created_usec=0,
        updated_usec=0,
        exported=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert 'title: "Title: \\"quoted\\" \\\\ backslash"' in frontmatter

    # Round-trips through Python's own YAML-compatible-ish parsing isn't
    # available without a YAML dependency (by design); instead assert the
    # produced line is a single well-formed double-quoted scalar: an even
    # number of unescaped double quotes bounding the value.
    title_line = next(line for line in frontmatter.splitlines() if line.startswith("title:"))
    value = title_line.removeprefix("title: ")
    assert value.startswith('"') and value.endswith('"')


def test_build_frontmatter_naive_datetime_treated_as_utc() -> None:
    frontmatter = build_frontmatter(
        quip_id="x",
        quip_url="https://quip.com/x",
        title="t",
        created_usec=0,
        updated_usec=0,
        exported=datetime(2024, 6, 1, 8, 30, 0),
    )
    assert 'exported: "2024-06-01T08:30:00Z"' in frontmatter


def test_asset_resolver_is_a_runtime_checkable_protocol_shape() -> None:
    def resolver(thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
        return f"{thread_id}/{blob_id}"

    resolver_typed: AssetResolver = resolver
    assert resolver_typed("t", "b", None) == "t/b"


def test_empty_checklist_item_gets_no_marker() -> None:
    """An empty Quip row must not become a stray empty checkbox."""
    html = (
        "<div data-section-style='7'><ul id='x'>"
        "<li class='checked' id='1' style=''><span id='1'>Real</span><br/></li>"
        "<li class='' id='2' style=''><span id='2'>​</span><br/></li>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    lines = [line.rstrip() for line in result.markdown.splitlines() if line.strip()]
    assert lines == ["- [x] Real"]


def test_empty_parent_item_still_keeps_its_children() -> None:
    html = (
        "<div data-section-style='7'><ul id='x'>"
        "<li class='parent' id='1' style=''><span id='1'>​</span><br/></li>"
        "<ul><li class='checked' id='2' style=''><span id='2'>Child</span><br/></li></ul>"
        "</ul></div>"
    )
    result = html_to_markdown(html, _default_resolver)
    assert "[x] Child" in result.markdown
