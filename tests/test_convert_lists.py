"""List-kind recovery in `convert._normalize_lists` and its neighbours.

Quip emits bullet, numbered and checklist lists as the same `<ul>`, and records
which is which on the nearest enclosing `div[data-section-style]`. Getting that
lookup wrong is what lost 5,857 unchecked items and 734 numbered items in the
first export, so these tests pin each style value, the fallbacks for markup
with no wrapper at all, and the nesting rule (nearest wrapper wins, not the
outermost).
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from quip2md.convert import _attr_text, _class_tokens, html_to_markdown


def _resolver(thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
    ext = f".{suggested_ext}" if suggested_ext else ""
    return f"_assets/{thread_id}/{blob_id}{ext}"


def _markdown(html: str) -> str:
    return html_to_markdown(html, _resolver).markdown


def _warnings(html: str) -> tuple[str, ...]:
    return html_to_markdown(html, _resolver).warnings


def _wrap(style: str, inner: str) -> str:
    return f"<div data-section-style='{style}'>{inner}</div>"


_ONE_ITEM = "<ul><li>a</li></ul>"


# --- Known styles -----------------------------------------------------------


def test_style_5_is_a_bullet_list() -> None:
    assert _markdown(_wrap("5", _ONE_ITEM)) == "- a\n"


def test_style_6_retags_the_ul_as_an_ol() -> None:
    assert _markdown(_wrap("6", "<ul><li>a</li><li>b</li></ul>")) == "1. a\n2. b\n"


def test_style_6_leaves_a_list_that_is_already_an_ol_alone() -> None:
    assert _markdown(_wrap("6", "<ol><li>a</li></ol>")) == "1. a\n"


def test_style_7_marks_every_item_by_its_class_not_its_presence() -> None:
    """An unchecked item's `class` is empty, which is why the wrapper decides."""
    html = _wrap("7", "<ul><li>plain</li><li class='checked'>done</li></ul>")
    assert _markdown(html) == "- [ ] plain\n- [x] done\n"


def test_style_7_leaves_an_empty_spacer_row_unmarked() -> None:
    """Marking one would turn an invisible Quip spacer into a stray empty checkbox."""
    html = _wrap("7", "<ul><li>real</li><li><span>​</span></li></ul>")
    assert _markdown(html) == "- [ ] real\n"


def test_style_7_keeps_an_item_whose_only_content_is_an_image() -> None:
    html = _wrap("7", "<ul><li><img src='/blob/THREAD0013/BLOB0001'/></li></ul>")
    assert _markdown(html) == "- [ ] ![](_assets/THREAD0013/BLOB0001)\n"


# --- Unknown and absent styles ----------------------------------------------


@pytest.mark.parametrize("style", ["0", "4", "9", "11", "banana"])
def test_an_unrecognized_style_degrades_to_bullets_with_a_warning(style: str) -> None:
    result = html_to_markdown(_wrap(style, _ONE_ITEM), _resolver)
    assert result.markdown == "- a\n"
    assert any(f"section style {style!r}" in warning for warning in result.warnings)


def test_only_a_style_outside_the_known_set_produces_a_warning() -> None:
    assert _warnings(_wrap("5", _ONE_ITEM)) == ()
    assert _warnings(_wrap("6", _ONE_ITEM)) == ()
    assert _warnings(_wrap("7", _ONE_ITEM)) == ()
    assert len(_warnings(_wrap("9", _ONE_ITEM))) == 1


def test_the_warning_names_the_style_and_counts_the_lists() -> None:
    html = _wrap("9", _ONE_ITEM) + _wrap("9", "<ul><li>b</li></ul>")
    assert _warnings(html) == (
        "unrecognized Quip list section style '9' (2 list(s)): converted as a bullet list",
    )


def test_several_unknown_styles_are_reported_in_sorted_order() -> None:
    html = _wrap("9", _ONE_ITEM) + _wrap("4", "<ul><li>b</li></ul>")
    warnings = _warnings(html)
    assert [warning.split("style ")[1].split(" ")[0] for warning in warnings] == ["'4'", "'9'"]


def test_a_blank_section_style_falls_back_to_the_per_item_class_heuristic() -> None:
    html = _wrap("   ", "<ul><li class='checklist'>a</li></ul>")
    assert _markdown(html) == "- [ ] a\n"


def test_no_wrapper_at_all_falls_back_to_the_per_item_class_heuristic() -> None:
    html = "<ul><li>plain</li><li class='checked'>done</li><li class='unchecked'>open</li></ul>"
    assert _markdown(html) == "- plain\n- [x] done\n- [ ] open\n"


# --- Nesting ----------------------------------------------------------------


def test_a_nested_list_inherits_its_ancestors_style() -> None:
    html = _wrap("7", "<ul><li>parent<ul><li class='checked'>child</li></ul></li></ul>")
    assert _markdown(html) == "- [ ] parent\n  - [x] child\n"


def test_the_nearest_wrapper_wins_over_an_outer_one() -> None:
    """A style-6 wrapper inside a style-7 list makes its own list numbered."""
    inner = _wrap("6", "<ul><li>numbered child</li></ul>")
    html = _wrap("7", f"<ul><li>task<ul><li>{inner}</li></ul></li></ul>")
    assert _markdown(html) == "- [ ] task\n  - [ ]\n\n    1. numbered child\n"


def test_a_sibling_style_sublist_separated_by_whitespace_is_still_re_parented() -> None:
    """Quip's real markup puts a newline between the `<li>` and its sub-list."""
    html = _wrap("5", "<ul><li>a</li>\n  <ul><li>b</li></ul>\n</ul>")
    assert _markdown(html) == "- a\n  - b\n"


def test_a_sibling_sublist_with_no_owning_li_is_spliced_in_rather_than_dropped() -> None:
    html = _wrap("5", "<ul><ul><li>orphan</li></ul><li>a</li></ul>")
    assert _markdown(html) == "- orphan\n- a\n"


# --- Attribute normalization ------------------------------------------------


def test_a_multi_valued_class_attribute_is_read_token_by_token() -> None:
    html = "<ul><li class='foo checked bar'>done</li></ul>"
    assert _markdown(html) == "- [x] done\n"


def test_an_existing_image_alt_is_preserved() -> None:
    html = "<img src='/blob/THREAD0013/BLOB0001' alt='Alice Example at the whiteboard'/>"
    assert _markdown(html) == "![Alice Example at the whiteboard](_assets/THREAD0013/BLOB0001)\n"


def test_an_image_without_a_blob_src_is_reported_and_left_alone() -> None:
    warnings = _warnings("<img src='https://cdn.example.com/logo.png'/>")
    assert any("did not match the Quip blob pattern" in warning for warning in warnings)


# --- Private normalizers, called directly ------------------------------------


def test_class_tokens_splits_a_string_valued_class_attribute() -> None:
    """`html.parser` hands `class` back as a string where `lxml` gives a list."""
    tag = BeautifulSoup("<li class='foo checked'>x</li>", "html.parser").li
    assert tag is not None
    tag.attrs["class"] = "foo checked"
    assert _class_tokens(tag) == ["foo", "checked"]


def test_class_tokens_is_empty_for_an_unclassed_tag() -> None:
    tag = BeautifulSoup("<li>x</li>", "html.parser").li
    assert tag is not None
    assert _class_tokens(tag) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [(["a", "b"], "a b"), ("a", "a"), (None, ""), ([], "")],
)
def test_attr_text_normalizes_every_shape_bs4_can_hand_back(
    value: str | list[str] | None, expected: str
) -> None:
    assert _attr_text(value) == expected


def test_a_table_with_no_rows_is_left_for_markdownify() -> None:
    """`_shield_tables` cannot build a GFM table without a header row."""
    assert html_to_markdown("<table></table>", _resolver).markdown.strip() == ""
