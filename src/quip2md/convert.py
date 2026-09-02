"""HTML-to-Markdown conversion with Quip-specific fixups.

Pure, network-free, filesystem-free conversion of Quip's exported document
HTML into GitHub-Flavored Markdown. All image/blob resolution is delegated to
an injected `AssetResolver` callable so this module never touches the network
or disk.

Quip HTML quirks handled here (verified against `tests/fixtures/`, the real
HTML payloads recorded by T1 in `docs/API_NOTES.md`):

* Attributes are single-quoted (`<img src='...'>`); BeautifulSoup normalizes
  quote style on parse, so both styles are handled transparently.
* List nesting is **not** expressed via `<li><ul>...</ul></li>` (the normal,
  spec-valid way). Instead Quip emits sub-lists as `<ul>`/`<ol>` elements that
  are *siblings* of the `<li>` they logically belong to, e.g.:
      <ul><li class='parent'>A<br/></li><ul><li>B<br/></li></ul><li>C<br/></li></ul>
  Naive HTML->Markdown conversion (including markdownify's own list handling)
  flattens this because it is not valid nested-list HTML. `_fix_list_nesting`
  re-parents each such orphaned `<ul>`/`<ol>` into the immediately preceding
  `<li>` sibling before conversion. When no preceding `<li>` exists at all
  (observed once, in a fixture where indentation crosses a `<div>` boundary
  with no `class='parent'` marker) the orphaned sub-list's items are spliced
  in at the same level instead of being dropped, to guarantee no text loss.
* Blank lines are represented as `<p class='line'>​</p>` (a lone
  U+200B ZERO WIDTH SPACE) rather than an empty paragraph. These are stripped
  so they render as genuinely blank lines instead of an invisible character.
* `<img>` blob references use `/blob/{thread_id}/{blob_id}`; the `/-/blob/`
  variant and absolute `https://...` forms of the same path are also matched
  per `docs/API_NOTES.md`.
* Tables (both regular content tables and spreadsheet exports) are converted
  by hand rather than via markdownify's built-in (and pipe-unsafe) table
  support -- see `_shield_tables`.
* `<pre>` code blocks encode line breaks as `<br/>` between `<span>` runs,
  not literal newlines; `_shield_code_blocks` reconstructs the text before
  fencing it, bypassing markdownify entirely so code content is never
  markdown-escaped.

* A list's *kind* lives on the wrapping `<div data-section-style>`, never on
  the list itself -- Quip emits `<ul>` for bullet, numbered *and* checklist
  lists alike, and marks only checked checklist items with `class='checked'`.
  `_normalize_lists` recovers the kind from that wrapper; see its docstring.

Blockquotes, `<hr>` and Quip `@`-mention markup were not observed in any
sampled thread; mention handling is implemented defensively against plausible
Quip conventions and exercised only by synthetic unit tests. Blockquote/hr/
strikethrough are covered by markdownify's own (verified-by-unit-test)
defaults.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import ATX, MarkdownConverter

ZERO_WIDTH_SPACE = "​"

# Matches /blob/{thread_id}/{blob_id}, /-/blob/{thread_id}/{blob_id}, and the
# same paths with an absolute https://host prefix (docs/API_NOTES.md #4).
_BLOB_SRC_RE = re.compile(
    r"(?:https?://[^/]+)?/(?:-/)?blob/(?P<thread_id>[^/]+)/(?P<blob_id>[^/?#]+)"
)

_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|bmp)(?:[?#].*)?$", re.IGNORECASE)

_LANG_CLASS_RE = re.compile(r"^lang(?:uage)?-([\w+-]+)$", re.IGNORECASE)

# Unanchored, so searching the space-joined class attribute is equivalent to
# bs4's per-token class matching (a match can never straddle the joining space).
_MENTION_CLASS_RE = re.compile(r"mention", re.IGNORECASE)
_MENTION_DATE_RE = re.compile(r"date", re.IGNORECASE)
_CHECKED_CLASS_RE = re.compile(r"^checked$", re.IGNORECASE)
_CHECKLIST_CLASS_RE = re.compile(r"checklist|unchecked", re.IGNORECASE)

# Quip marks a list's *kind* on the wrapping `<div data-section-style>`, not on
# the `<ul>`/`<li>` themselves -- every list it emits is a `<ul>` regardless of
# how it renders. Values observed live across a 60-thread sample (T13 review):
_SECTION_STYLE_ATTR = "data-section-style"
_SECTION_STYLE_BULLET = "5"
_SECTION_STYLE_NUMBERED = "6"
_SECTION_STYLE_CHECKLIST = "7"

MAX_TABLE_COLUMNS_BEFORE_WIDE = 30

# Tags handled explicitly (by us or by markdownify's defaults) that should
# never be reported as "unknown" in ConversionResult.warnings. Includes the
# synthetic "p" wrapper used to shield code blocks/tables.
_KNOWN_TAGS = frozenset(
    {
        "html",
        "body",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "div",
        "span",
        "ul",
        "ol",
        "li",
        "br",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "del",
        "strike",
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "pre",
        "code",
        "blockquote",
        "hr",
    }
)


class AssetResolver(Protocol):
    """Resolves a Quip blob reference to a relative Markdown image path.

    Never called by this module for network or filesystem access -- purely
    injected so `convert.py` stays a pure function of its inputs.
    """

    def __call__(self, thread_id: str, blob_id: str, suggested_ext: str | None) -> str: ...


@dataclass(slots=True, frozen=True)
class ConversionResult:
    """Result of converting one Quip document's HTML to Markdown."""

    markdown: str
    warnings: tuple[str, ...]
    wide_table: bool


def html_to_markdown(html: str, asset_resolver: AssetResolver) -> ConversionResult:
    """Convert Quip document HTML to Markdown.

    Never raises on malformed/unexpected input: unrecognized elements unwrap
    to their text content (no text loss) and are counted in the returned
    warnings.
    """
    warnings: list[str] = []
    soup = BeautifulSoup(html, "lxml")

    _strip_zero_width_space(soup)
    _fix_list_nesting(soup)
    _normalize_lists(soup, warnings)
    _normalize_mentions(soup)
    _normalize_images(soup, asset_resolver, warnings)

    code_placeholders = _shield_code_blocks(soup)
    table_placeholders, wide_table = _shield_tables(soup)

    _record_unknown_tags(soup, warnings)

    converter = MarkdownConverter(heading_style=ATX, bullets="-")
    markdown = converter.convert_soup(soup)

    for placeholder, replacement in {**code_placeholders, **table_placeholders}.items():
        markdown = markdown.replace(placeholder, replacement)

    markdown = _tidy_blank_lines(markdown)

    return ConversionResult(markdown=markdown, warnings=tuple(warnings), wide_table=wide_table)


def build_frontmatter(
    *,
    quip_id: str,
    quip_url: str,
    title: str,
    created_usec: int,
    updated_usec: int,
    exported: datetime,
) -> str:
    """Build a YAML frontmatter block (including delimiting `---` lines).

    All scalar values are double-quoted (including timestamps, which contain
    YAML-significant colons) so no separate YAML dependency is needed to
    produce a safe, parseable document. `created_usec`/`updated_usec` are
    Quip's microseconds-since-epoch integers; `exported` is a UTC-or-naive
    datetime supplied by the caller (this function does not read the clock,
    to stay pure).
    """
    lines = [
        "---",
        f"quip_id: {_yaml_quote(quip_id)}",
        f"quip_url: {_yaml_quote(quip_url)}",
        f"title: {_yaml_quote(title)}",
        f"created: {_yaml_quote(_usec_to_iso8601(created_usec))}",
        f"updated: {_yaml_quote(_usec_to_iso8601(updated_usec))}",
        f"exported: {_yaml_quote(_datetime_to_iso8601(exported))}",
        "---",
    ]
    return "\n".join(lines) + "\n"


def _usec_to_iso8601(usec: int) -> str:
    dt = datetime.fromtimestamp(usec / 1_000_000, tz=UTC)
    return _datetime_to_iso8601(dt)


def _datetime_to_iso8601(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _strip_zero_width_space(soup: BeautifulSoup) -> None:
    for text_node in soup.find_all(string=True):
        if ZERO_WIDTH_SPACE in text_node:
            text_node.replace_with(str(text_node).replace(ZERO_WIDTH_SPACE, ""))


def _fix_list_nesting(soup: BeautifulSoup) -> None:
    """Re-parent Quip's sibling-style sub-lists into their owning `<li>`.

    See module docstring for the exact malformed shape being fixed.
    `find_all` recurses the whole tree before any mutation happens, so every
    orphaned sub-list at every nesting depth is captured in a single
    upfront snapshot. One pass over that snapshot then suffices -- for each
    list, each orphaned child is re-parented using its *live* preceding
    sibling at the moment it is processed, which already reflects any
    earlier re-parenting within the same list -- so no fixed-point restart
    is needed.
    """
    for list_tag in soup.find_all(["ul", "ol"]):
        for child in list(list_tag.children):
            if not isinstance(child, Tag) or child.name not in ("ul", "ol"):
                continue
            owner = _preceding_owner_li(child)
            if owner is not None:
                owner.append(child.extract())
            else:
                for sub_li in list(child.find_all("li", recursive=False)):
                    child.insert_before(sub_li.extract())
                child.decompose()


def _preceding_owner_li(node: Tag) -> Tag | None:
    """Nearest previous sibling tag, if it is a `<li>` (skipping whitespace)."""
    sibling = node.previous_sibling
    while isinstance(sibling, NavigableString) and not sibling.strip():
        sibling = sibling.previous_sibling
    if isinstance(sibling, Tag) and sibling.name == "li":
        return sibling
    return None


def _class_tokens(tag: Tag) -> list[str]:
    """Normalize a `class` attribute (bs4 types it as `str | list[str]`)."""
    value = tag.get("class")
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return value.split()
    return []


def _attr_text(value: str | list[str] | None) -> str:
    """Normalize any attribute value (bs4 types them as `str | list[str]`)."""
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _normalize_lists(soup: BeautifulSoup, warnings: list[str]) -> None:
    """Recover each list's kind from its wrapping `data-section-style` div.

    Quip emits every list as a `<ul>` and records what it actually is on the
    nearest enclosing `<div data-section-style>`: `5` bullets, `6` numbered,
    `7` checklist. Two consequences that are only visible from the wrapper:

    * a numbered list is indistinguishable from a bullet list at the `<ul>`
      level, so style `6` lists are retagged as `<ol>` here;
    * inside a checklist, only *checked* items carry a `checked` class token
      -- an unchecked item's class is empty, making it identical to an
      ordinary bullet. Every `<li>` under a style `7` list is therefore a
      checklist item, and the absence of `checked` means unchecked, not
      "not a checklist item".

    Nested sub-lists inherit their ancestor div's style (verified: Quip keeps
    the whole nested structure inside the one wrapper), which is why the style
    is resolved per list via `find_parent` rather than per wrapper div.

    A list under an unrecognized style is left as bullets and counted in
    `warnings`, so a Quip list kind we have never seen surfaces in the run
    report instead of silently degrading.

    Must run after `_fix_list_nesting`: it relies on sub-lists already living
    inside their owning `<li>` so that each `<li>` is visited exactly once, by
    exactly one list.
    """
    unknown: Counter[str] = Counter()
    for list_tag in soup.find_all(["ul", "ol"]):
        style = _nearest_section_style(list_tag)
        if style == _SECTION_STYLE_CHECKLIST:
            for li in list_tag.find_all("li", recursive=False):
                if _is_empty_item(li):
                    # Quip keeps empty checklist rows; marking one would turn an
                    # invisible spacer row into a stray empty checkbox. Leave it
                    # bare so it collapses away exactly as an empty bullet does.
                    continue
                checked = any(_CHECKED_CLASS_RE.match(c) for c in _class_tokens(li))
                li.insert(0, "[x] " if checked else "[ ] ")
        elif style == _SECTION_STYLE_NUMBERED:
            if list_tag.name == "ul":
                list_tag.name = "ol"
        elif style is None:
            # No Quip wrapper at all (hand-written or synthetic HTML): fall
            # back to the per-`<li>` class heuristic.
            for li in list_tag.find_all("li", recursive=False):
                _mark_checklist_item_by_class(li)
        elif style != _SECTION_STYLE_BULLET:
            unknown[style] += 1

    for style, count in sorted(unknown.items()):
        warnings.append(
            f"unrecognized Quip list section style {style!r} ({count} list(s)): "
            "converted as a bullet list"
        )


def _nearest_section_style(tag: Tag) -> str | None:
    """The `data-section-style` of `tag`'s nearest enclosing wrapper div."""
    ancestor = tag.find_parent(attrs={_SECTION_STYLE_ATTR: True})
    if ancestor is None:
        return None
    return _attr_text(ancestor.get(_SECTION_STYLE_ATTR)).strip() or None


def _is_empty_item(li: Tag) -> bool:
    """True when a list item carries no content of its own (sub-lists aside)."""
    for child in li.children:
        if isinstance(child, Tag):
            if child.name in ("ul", "ol"):
                continue
            if child.name == "img" or child.find("img") is not None:
                return False
            if child.get_text(strip=True):
                return False
        elif str(child).strip():
            return False
    return True


def _mark_checklist_item_by_class(li: Tag) -> None:
    classes = _class_tokens(li)
    if any(_CHECKED_CLASS_RE.match(c) for c in classes):
        li.insert(0, "[x] ")
    elif any(_CHECKLIST_CLASS_RE.search(c) for c in classes):
        li.insert(0, "[ ] ")


def _normalize_mentions(soup: BeautifulSoup) -> None:
    """Best-effort @-mention handling (unverified: no fixture has one).

    Person mentions collapse to plain text; document-link mentions (an
    element carrying a `quip.com` href) keep a `[title](url)` link; date
    mentions collapse to plain text. Any element not matching a `mention`
    class token is left untouched -- the common case for every real fixture.
    """
    for element in soup.find_all(None, attrs={"class": _MENTION_CLASS_RE}):
        classes = _class_tokens(element)
        text = element.get_text()
        href = _attr_text(element.get("href")) if element.name == "a" else ""
        is_date = any(_MENTION_DATE_RE.search(c) for c in classes)
        if not is_date and href and "quip.com" in href:
            link = soup.new_tag("a", href=href)
            link.string = text
            element.replace_with(link)
        else:
            element.replace_with(text)


def _normalize_images(
    soup: BeautifulSoup, asset_resolver: AssetResolver, warnings: list[str]
) -> None:
    for img in soup.find_all("img"):
        src = _attr_text(img.get("src"))
        match = _BLOB_SRC_RE.search(src)
        if match is None:
            warnings.append(f"image src did not match the Quip blob pattern: {src!r}")
            continue
        thread_id = match.group("thread_id")
        blob_id = match.group("blob_id")
        ext_match = _IMAGE_EXT_RE.search(src)
        suggested_ext = ext_match.group(1).lower() if ext_match else None
        img["src"] = asset_resolver(thread_id, blob_id, suggested_ext)
        if not _attr_text(img.get("alt")):
            img["alt"] = ""


def _new_placeholder(kind: str, index: int) -> str:
    return f"QUIP2MDPLACEHOLDER{kind}{index}ENDPLACEHOLDER"


def _shield_code_blocks(soup: BeautifulSoup) -> dict[str, str]:
    """Replace `<pre>` blocks with alnum-only placeholders, off to the side.

    Fencing is built by hand and restored via string replacement *after*
    markdownify runs, so code content is never subject to markdown escaping
    (e.g. underscores in code would otherwise become `\\_`).
    """
    placeholders: dict[str, str] = {}
    for index, pre in enumerate(soup.find_all("pre")):
        for br in pre.find_all("br"):
            br.replace_with("\n")
        code_text = pre.get_text().strip("\n")

        language = ""
        for token in _class_tokens(pre):
            lang_match = _LANG_CLASS_RE.match(token)
            if lang_match:
                language = lang_match.group(1)
                break

        placeholder = _new_placeholder("CODE", index)
        fenced = f"```{language}\n{code_text}\n```"
        placeholders[placeholder] = fenced

        replacement = soup.new_tag("p")
        replacement.string = placeholder
        pre.replace_with(replacement)
    return placeholders


def _shield_tables(soup: BeautifulSoup) -> tuple[dict[str, str], bool]:
    """Replace `<table>` elements with hand-built GFM pipe tables.

    markdownify's default table converter does not escape `|` inside cells,
    which silently corrupts column alignment for any Quip table/spreadsheet
    containing a literal pipe -- so tables are converted by hand instead.
    """
    placeholders: dict[str, str] = {}
    wide_table = False
    for index, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        if not rows:
            continue

        grid = [_table_row_cells(row) for row in rows]
        column_count = max(len(row) for row in grid)
        if column_count > MAX_TABLE_COLUMNS_BEFORE_WIDE:
            wide_table = True

        header, *body = grid
        header = _pad_row(header, column_count)
        gfm_lines = [
            _gfm_row(header),
            _gfm_row(["---"] * column_count),
        ]
        gfm_lines.extend(_gfm_row(_pad_row(row, column_count)) for row in body)
        gfm_table = "\n".join(gfm_lines)

        placeholder = _new_placeholder("TABLE", index)
        placeholders[placeholder] = gfm_table

        replacement = soup.new_tag("p")
        replacement.string = placeholder
        table.replace_with(replacement)
    return placeholders, wide_table


def _table_row_cells(row: Tag) -> list[str]:
    cells = row.find_all(["th", "td"], recursive=False)
    return [_table_cell_text(cell) for cell in cells]


def _table_cell_text(cell: Tag) -> str:
    text = cell.get_text(separator=" ", strip=True)
    text = text.replace("\n", " ").replace("|", "\\|")
    return text


def _pad_row(row: list[str], width: int) -> list[str]:
    return row + [""] * (width - len(row))


def _gfm_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _record_unknown_tags(soup: BeautifulSoup, warnings: list[str]) -> None:
    unknown: Counter[str] = Counter()
    for tag in soup.find_all(True):
        if tag.name not in _KNOWN_TAGS:
            unknown[tag.name] += 1
    for tag_name, count in sorted(unknown.items()):
        warnings.append(
            f"unrecognized element <{tag_name}> ({count} occurrence(s)): "
            "unwrapped to its text content"
        )


def _tidy_blank_lines(markdown: str) -> str:
    # Markdownify sometimes leaves whitespace-only lines (e.g. a trailing
    # hard-break "  " before a nested list); blank them out before collapsing
    # runs of blank lines so no whitespace-only lines survive.
    lines = [line if line.strip() else "" for line in markdown.split("\n")]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return collapsed.strip() + "\n"
