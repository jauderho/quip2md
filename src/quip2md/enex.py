"""Markdown -> Evernote-archive (`.enex`) rendering for the Apple Notes import.

Apple Notes' AppleScript `body` writer silently discards every `<a href>` and
every form of checkbox markup, so it cannot express two of this migration's
three requirements. Notes' *Evernote archive importer* can: it produces real
clickable hyperlinks and real native checklist items (see
`docs/NOTES_API_NOTES.md` for the live capability matrix). This module renders
the exported Markdown archive into that format.

Design notes, all keyed to behaviour observed live on macOS 26.6.2:

* **CommonMark, not `python-markdown`.** The archive is written by
  `markdownify` with two-space list indentation. `python-markdown` mis-assigns
  nesting depth on that dialect whenever a list item carries a long paragraph:
  measured over the whole corpus, 29.4% of list items landed at the wrong
  depth. `markdown-it-py` parses the same input correctly, so it is the only
  parser used here.
* **Checklists are always flattened by the importer.** Thirteen ENML shapes
  were tested (nested `<li>`, sibling `<ul>`, `padding-left`, `margin-left`,
  nested `<div>`s, `<blockquote>`, `text-indent`, `<ol>`, ...) and every one
  puts the `<en-todo>` paragraph at the top level. There is no markup that
  yields an indented checklist item, so this module does not pretend
  otherwise: it emits every checklist item as a flat top-level `<div>` and
  records the intended depth in `NoteEnml.checklist`. Restoring the real
  indentation is `notes_indent`'s job, driven by exactly that plan.
* **Ordinary (non-task) lists nest natively** to at least five levels, and a
  nested `<ol>` inside a `<ul>` is preserved, so those are emitted as real
  nested lists and left alone.
* `<h1>`/`<h2>`/`<h3>` map to Notes' Title/Heading/Subheading; deeper headings
  have no equivalent and are rendered as `<h3>`.
* `<pre>` becomes a real monospaced code block; `<table>` becomes a real Notes
  table; `<blockquote>` keeps its text but loses the quote bar; `<hr>` is
  dropped by the importer entirely. The last two are counted as warnings.
* Images travel as `<en-media hash=... type=...>` plus a base64 `<resource>`;
  a missing asset degrades to a `[missing image: name]` text warning rather
  than a dangling reference.

Everything here is pure apart from reading image files off disk. Nothing in
this module talks to Notes.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path

from bs4 import (
    BeautifulSoup,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    ProcessingInstruction,
    Tag,
)
from markdown_it import MarkdownIt

# --- Constants ---------------------------------------------------------

#: Schemes a link may keep. Anything else is rendered as plain text so a
#: `javascript:`/`data:` URL from a malformed archive can never reach Notes.
ALLOWED_LINK_SCHEMES = ("https://", "http://", "mailto:", "tel:")

#: GFM task-list markers, matched only at the very start of a list item.
#: Any whitespace separates the marker from the text, newline included: when an
#: item's text begins on the *next* source line (Quip emits a leading `<br>`
#: often enough) the separator is that line break. The trailing alternative
#: covers a marker that is the whole first text node, with nothing after it.
_TASK_MARKER_RE = re.compile(r"^\[([ xX])\](?:\s+|$)")

#: Characters XML 1.0 cannot carry at all. One of them anywhere in the payload
#: makes the whole `.enex` unparsable, so they are dropped at every escape
#: point. Tab, LF and CR are legal and deliberately absent from the class.
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

#: bs4 markup nodes that are not text and must never reach the output.
_IGNORED_STRING_TYPES = (Comment, Declaration, Doctype, ProcessingInstruction)

#: Block elements that cannot survive inside a checklist paragraph; their text
#: is kept, their structure is not.
_FLATTENED_IN_TASK = ("pre", "table", "blockquote")

#: A task item whose text starts on the next source line renders as
#: `<en-todo/><br/>text`. Notes then puts the checkbox on an *empty* line and
#: the text on a following plain paragraph, which is not a list item at all --
#: so it can never be indented, and the note looks like it has a stray empty
#: checkbox. Dropping the leading break puts the text on the checkbox's own
#: line, where it belongs.
_LEADING_BREAKS_RE = re.compile(r"^(?:<br/>|\s)+")

#: Image magic bytes, for assets whose name carries no usable extension.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_MAX_NOTES_HEADING_LEVEL = 3

_ASSETS_DIR_NAME = "_assets"

#: One empty editable line. Notes renders this as a blank paragraph.
_BLANK_LINE = "<div><br/></div>"

_ENEX_DOCTYPE = '<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export3.dtd">'
_ENML_PROLOGUE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
)

#: Attributes Notes has no use for; stripped so the payload stays minimal and
#: ENML-clean (`class`/`id` are not part of the ENML content model).
_DROPPED_ATTRS = ("class", "id", "style", "data-section-style", "target", "rel", "title")


# --- Result types ------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ChecklistItem:
    """One checklist line, with the depth Notes cannot be told about directly.

    `text` is the visible text exactly as it will appear in Notes, which is
    what `notes_indent` matches against when it walks a note back.
    """

    text: str
    checked: bool
    depth: int


@dataclass(slots=True, frozen=True)
class EnexResource:
    """One binary attachment, referenced from the ENML by its MD5 hash."""

    md5: str
    mime: str
    filename: str
    data: bytes


@dataclass(slots=True, frozen=True)
class NoteEnml:
    """One document rendered to ENML, ready to be wrapped in an `.enex`."""

    title: str
    enml: str
    resources: tuple[EnexResource, ...] = ()
    checklist: tuple[ChecklistItem, ...] = ()
    warnings: tuple[str, ...] = ()
    created: str | None = None
    updated: str | None = None

    @property
    def needs_indent_pass(self) -> bool:
        """True when at least one checklist item is meant to be indented."""
        return any(item.depth > 0 for item in self.checklist)


@dataclass(slots=True)
class _RenderState:
    """Mutable accumulators threaded through the recursive render."""

    md_dir: Path
    blocks: list[str] = field(default_factory=list)
    resources: dict[str, EnexResource] = field(default_factory=dict)
    checklist: list[ChecklistItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- Markdown -> ENML ---------------------------------------------------

#: Raw HTML is disabled deliberately. The archive is written by `markdownify`
#: and never intentionally carries HTML, so an angle-bracketed word in prose
#: (`--filter <param>`, `<url>`) is a *placeholder*, not a tag. With `html`
#: enabled the parser turns it into an element that the renderer then unwraps,
#: deleting the text; escaped, it survives verbatim.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def _xml_safe(text: str) -> str:
    """Drop the characters XML 1.0 cannot represent."""
    return _XML_ILLEGAL_RE.sub("", text)


def _escape(text: str) -> str:
    """Escape `text` for the payload, dropping XML-illegal characters first.

    `<` and `>` are escaped **twice**. Notes' importer decodes the content and
    then re-parses the result as HTML, so a correctly escaped `&lt;profile
    name&gt;` becomes a real `<profile name>` element on that second pass and
    is swallowed whole -- the text simply vanishes from the note. Verified live
    against a probe note carrying all three encodings: `&amp;lt;` survives as
    literal `<`, while `&lt;` and the numeric `&#60;` are both lost, in plain
    paragraphs and checklist items alike. 141 occurrences across 14 documents
    in this corpus depend on it.
    """
    escaped = html_escape(_xml_safe(text))
    return escaped.replace("&lt;", "&amp;lt;").replace("&gt;", "&amp;gt;")


def markdown_to_enml(
    *,
    title: str,
    quip_url: str | None,
    markdown_text: str,
    md_dir: Path,
    created: str | None = None,
    updated: str | None = None,
) -> NoteEnml:
    """Render one document's Markdown body to ENML.

    `md_dir` is the directory the source `.md` lives in; relative image
    references (`_assets/<thread_id>/<blob_id>`) resolve against it. Never
    raises on malformed input -- unusable constructs degrade to text plus a
    warning.

    The provenance link deliberately uses the Quip URL as its own label. It is
    what the reader already sees today (only now clickable), and it is what
    makes a note matchable back to its source document after import: Notes'
    `body` getter drops the `href` but keeps the visible text.
    """
    html = _MARKDOWN.render(markdown_text)
    soup = BeautifulSoup(html, "html.parser")

    state = _RenderState(md_dir=md_dir)

    if _XML_ILLEGAL_RE.search(markdown_text) or _XML_ILLEGAL_RE.search(title):
        state.warnings.append("control characters removed: not representable in XML")

    _drop_duplicate_leading_heading(soup, title)

    if quip_url:
        safe_url = _safe_url(quip_url)
        if safe_url:
            label = _escape(quip_url)
            state.blocks.append(f'<div>Source: <a href="{_escape(safe_url)}">{label}</a></div>')
        else:
            state.warnings.append(f"provenance URL has an unsupported scheme: {quip_url!r}")
            state.blocks.append(f"<div>Source: {_escape(quip_url)}</div>")
        # A blank line between the provenance and the document itself, so the
        # note does not open with the link welded to its first heading.
        state.blocks.append(_BLANK_LINE)

    for child in soup.contents:
        _render_block(child, state, depth=0)

    body = "".join(state.blocks) or _BLANK_LINE
    enml = f"{_ENML_PROLOGUE}<en-note>{body}</en-note>"

    return NoteEnml(
        title=title,
        enml=enml,
        resources=tuple(state.resources.values()),
        checklist=tuple(state.checklist),
        warnings=tuple(state.warnings),
        created=created,
        updated=updated,
    )


def _drop_duplicate_leading_heading(soup: BeautifulSoup, title: str) -> None:
    """Remove a leading `<h1>` that merely repeats the note's own title.

    Exported documents usually open with an `<h1>` of their title, and the
    `.enex` `<title>` already becomes the note's first line -- keeping both
    shows the title twice.
    """
    first = next((child for child in soup.contents if isinstance(child, Tag)), None)
    if first is not None and first.name == "h1" and first.get_text(strip=True) == title.strip():
        first.decompose()


# --- Block rendering ----------------------------------------------------


def _render_block(node: object, state: _RenderState, *, depth: int) -> None:
    if isinstance(node, _IGNORED_STRING_TYPES):
        return
    if isinstance(node, NavigableString):
        text = str(node).strip()
        if text:
            state.blocks.append(f"<div>{_escape(text)}</div>")
        return
    if not isinstance(node, Tag):
        return

    match node.name:
        case "ul" | "ol":
            _render_list(node, state, depth=depth)
        case "p":
            state.blocks.append(f"<div>{_render_inline(node, state)}</div>")
        case tag if tag in _HEADING_TAGS:
            level = min(int(tag[1]), _MAX_NOTES_HEADING_LEVEL)
            state.blocks.append(f"<h{level}>{_render_inline(node, state)}</h{level}>")
        case "pre":
            state.blocks.append(f"<pre>{_escape(node.get_text())}</pre>")
        case "blockquote":
            state.warnings.append("blockquote: Notes keeps the text but drops the quote styling")
            inner = _RenderState(md_dir=state.md_dir, resources=state.resources)
            for child in node.contents:
                _render_block(child, inner, depth=depth)
            state.blocks.append(f"<blockquote>{''.join(inner.blocks)}</blockquote>")
            state.checklist.extend(inner.checklist)
            state.warnings.extend(inner.warnings)
        case "hr":
            state.warnings.append("horizontal rule: dropped by the Notes importer")
        case "table":
            state.blocks.append(_render_table(node, state))
        case _:
            rendered = _render_inline(node, state)
            if rendered.strip():
                state.blocks.append(f"<div>{rendered}</div>")


def _render_list(list_tag: Tag, state: _RenderState, *, depth: int) -> None:
    """Render one list, choosing between a native list and a flat checklist.

    A list subtree containing any GFM task item is emitted as a flat run of
    `<en-todo>` paragraphs, because the Notes importer flattens checklists no
    matter how they are marked up; the intended depths are recorded instead.
    Every other list is emitted as a real nested list, which Notes preserves.
    """
    if _contains_task_item(list_tag):
        _render_checklist(list_tag, state, depth=depth)
        return

    items: list[str] = []
    for li in list_tag.find_all("li", recursive=False):
        items.append(_render_native_list_item(li, state, depth=depth))
    state.blocks.append(f"<{list_tag.name}>{''.join(items)}</{list_tag.name}>")


def _render_native_list_item(li: Tag, state: _RenderState, *, depth: int) -> str:
    inline_parts: list[str] = []
    sublists: list[str] = []
    for child in li.contents:
        if isinstance(child, Tag) and child.name in ("ul", "ol"):
            nested = _RenderState(md_dir=state.md_dir, resources=state.resources)
            _render_list(child, nested, depth=depth + 1)
            sublists.extend(nested.blocks)
            state.checklist.extend(nested.checklist)
            state.warnings.extend(nested.warnings)
        elif isinstance(child, Tag) and child.name == "p":
            inline_parts.append(_render_inline(child, state))
        else:
            inline_parts.append(_render_inline_node(child, state))
    return f"<li>{''.join(inline_parts).strip()}{''.join(sublists)}</li>"


def _render_checklist(list_tag: Tag, state: _RenderState, *, depth: int) -> None:
    for li in list_tag.find_all("li", recursive=False):
        checked, text_html, text_plain = _split_task_item(li, state)
        if checked is None:
            # A plain item among checkboxes -- usually two adjacent Quip lists
            # (one bullet, one checklist) that CommonMark merged into a single
            # loose list. Keep it a bullet at its own depth rather than
            # inventing a checkbox state or flattening it to a paragraph.
            state.warnings.append("list mixes checklist and plain items")
            # Each intermediate level is an empty <li>: a <ul> directly inside a
            # <ul> is not valid markup and Notes' importer may reflow it.
            opening = "<ul><li>" * depth + "<ul>"
            closing = "</ul>" + "</li></ul>" * depth
            state.blocks.append(f"{opening}<li>{text_html}</li>{closing}")
        elif not text_html.strip():
            # An empty Quip row. Emitting it would add a stray empty checkbox.
            pass
        else:
            marker = '<en-todo checked="true"/>' if checked else "<en-todo/>"
            state.blocks.append(f"<div>{marker}{text_html}</div>")
            state.checklist.append(ChecklistItem(text=text_plain, checked=checked, depth=depth))
        for child in li.contents:
            if isinstance(child, Tag) and child.name in ("ul", "ol"):
                _render_list(child, state, depth=depth + 1)


def _contains_task_item(list_tag: Tag) -> bool:
    return any(_task_marker(li) is not None for li in list_tag.find_all("li"))


def _task_marker(li: Tag) -> bool | None:
    """`True`/`False` for a checked/unchecked task item, `None` if not a task.

    Mirrors GFM: the marker must be the very start of the item's own text, so
    a literal `[x]` inside prose is never mistaken for a checkbox.
    """
    node = _first_text_node(li)
    if node is None:
        return None
    match = _TASK_MARKER_RE.match(str(node))
    if match is None:
        return None
    return match.group(1).lower() == "x"


def _first_text_node(li: Tag) -> NavigableString | None:
    """The item's own leading text node, or `None` if it has none.

    Per GFM the marker only counts when it is a *direct* text child of the item
    -- of the `<li>` itself or of a paragraph directly inside it. A marker that
    the parser put inside `<code>`/`<strong>` is a literal, not a checkbox.
    """
    for child in li.descendants:
        if isinstance(child, NavigableString):
            if not str(child).strip():
                continue
            parent = child.parent
            if parent is li or (parent is not None and parent.name == "p" and parent.parent is li):
                return child
            return None
        if isinstance(child, Tag) and child.name in ("ul", "ol"):
            return None
    return None


def _split_task_item(li: Tag, state: _RenderState) -> tuple[bool | None, str, str]:
    """Strip the task marker and render the item's own content (no sub-lists).

    Every non-list child is rendered, not just the first paragraph: a *loose*
    task item carries its continuation paragraphs -- and sometimes a code block
    or a table -- as further children, and dropping them loses text. Block
    children that cannot live inside a checklist line are flattened to their
    own text, with a warning.
    """
    checked = _task_marker(li)

    # A bare `Tag` rather than a parsed `<span>`: it is the same detached
    # holder node, without paying for a parser instance per task item.
    holder = Tag(name="span")
    for child in li.contents:
        if isinstance(child, Tag) and child.name in ("ul", "ol"):
            continue
        if isinstance(child, Tag | NavigableString):
            holder.append(_clone(child))

    if checked is not None:
        node = _first_text_node(holder)
        if node is not None:
            node.replace_with(NavigableString(_TASK_MARKER_RE.sub("", str(node), count=1)))

    segments: list[str] = []
    inline: list[str] = []

    def flush_inline() -> None:
        joined = "".join(inline).strip()
        inline.clear()
        if joined:
            segments.append(joined)

    for child in holder.contents:
        if isinstance(child, Tag) and child.name in _FLATTENED_IN_TASK:
            flush_inline()
            state.warnings.append("task item: nested block flattened")
            lines = [line.strip() for line in child.get_text("\n").splitlines() if line.strip()]
            if lines:
                segments.append("<br/>".join(_escape(line) for line in lines))
        elif isinstance(child, Tag) and child.name == "p":
            flush_inline()
            rendered = _render_inline(child, state).strip()
            if rendered:
                segments.append(rendered)
        else:
            inline.append(_render_inline_node(child, state))
    flush_inline()

    item_html = _LEADING_BREAKS_RE.sub("", "<br/>".join(segments))
    return checked, item_html, _first_visible_line(item_html)


def _first_visible_line(item_html: str) -> str:
    """The item's first visible line, which is the line Notes puts the box on.

    Notes renders every `<br/>` inside a checklist item as its own paragraph,
    so an item carrying several blocks occupies several lines in the note and
    only the first one holds the checkbox -- and only that one can be indented.
    `notes_indent` matches on this, so it has to be that first line and not the
    whole item, or a multi-block item is never found and the note is refused.
    """
    for part in item_html.split("<br/>"):
        text = " ".join(_xml_safe(_text_of(part)).split())
        if text:
            return text
    return ""


#: Strips the tags out of a fragment this module generated itself. A parser is
#: not needed for that, and bs4 warns when a fragment happens to look like a URL.
_TAG_RE = re.compile(r"<[^>]+>")


def _text_of(fragment: str) -> str:
    """The visible text of a fragment, as Notes will end up showing it.

    Unescaped **twice**, to match `_escape`: `<` is written `&amp;lt;` because
    Notes decodes the content and then re-parses the result. Undoing only one
    of those leaves `&lt;` where the note really shows `<`, and the checklist
    line then never matches the note -- which silently cost 8 of the corpus's
    94 nested-checklist notes their indentation.
    """
    return html_unescape(html_unescape(_TAG_RE.sub(" ", fragment)))


def _render_table(table: Tag, state: _RenderState) -> str:
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells: list[str] = []
        for cell in tr.find_all(["th", "td"], recursive=False):
            cells.append(f"<{cell.name}>{_render_inline(cell, state)}</{cell.name}>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table>{''.join(rows)}</table>"


# --- Inline rendering ---------------------------------------------------

_INLINE_PASSTHROUGH = frozenset({"b", "strong", "i", "em", "s", "del", "strike", "u", "code", "br"})


def _render_inline(node: Tag, state: _RenderState) -> str:
    return "".join(_render_inline_node(child, state) for child in node.contents)


def _render_inline_node(node: object, state: _RenderState) -> str:
    if isinstance(node, _IGNORED_STRING_TYPES):
        return ""
    if isinstance(node, NavigableString):
        return _escape(str(node))
    if not isinstance(node, Tag):
        return ""

    if node.name == "a":
        return _render_link(node, state)
    if node.name == "img":
        return _render_image(node, state)
    if node.name == "br":
        return "<br/>"
    if node.name in _INLINE_PASSTHROUGH:
        return f"<{node.name}>{_render_inline(node, state)}</{node.name}>"
    if node.name in ("ul", "ol", "li", "p", "div"):
        return _render_inline(node, state)
    return _render_inline(node, state)


def _render_link(anchor: Tag, state: _RenderState) -> str:
    href = _attr(anchor, "href")
    label = _render_inline(anchor, state) or _escape(href)
    safe = _safe_url(href)
    if not safe:
        if href:
            state.warnings.append(f"link dropped, unsupported scheme: {href!r}")
        return label
    return f'<a href="{_escape(safe)}">{label}</a>'


def _safe_url(url: str) -> str | None:
    candidate = url.strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered.startswith(ALLOWED_LINK_SCHEMES):
        return candidate
    return None


def _render_image(img: Tag, state: _RenderState) -> str:
    src = _attr(img, "src")
    if not src:
        state.warnings.append("image tag with an empty src")
        return "[missing image: (empty src)]"
    if src.lower().startswith(("http://", "https://")):
        # A remote image cannot travel inside the archive; keep it as a link
        # so the reference is not silently lost.
        return f'<a href="{_escape(src)}">{_escape(_attr(img, "alt") or src)}</a>'

    resolved = (state.md_dir / src).resolve()
    if not resolved.is_file():
        state.warnings.append(f"missing image: {src}")
        return f"[missing image: {_escape(Path(src).name)}]"

    data = resolved.read_bytes()
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    mime = mimetypes.guess_type(resolved.name)[0] or _sniff_image_mime(data)
    if mime is None:
        # Quip blob names often carry no extension at all; without a usable
        # type Notes shows an opaque attachment instead of the picture.
        state.warnings.append(f"unrecognized image type, sent as a binary blob: {resolved.name}")
        mime = "application/octet-stream"
    state.resources.setdefault(
        md5, EnexResource(md5=md5, mime=mime, filename=resolved.name, data=data)
    )
    return f'<en-media hash="{md5}" type="{_escape(mime)}"/>'


def _sniff_image_mime(data: bytes) -> str | None:
    """Identify an image by its magic bytes, or `None` if it is not one."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _attr(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _clone(node: Tag | NavigableString) -> Tag | NavigableString:
    """Deep-copy a node so the source tree is never mutated while rendering."""
    if isinstance(node, Tag):
        copy = Tag(name=node.name, attrs=dict(node.attrs))
        for child in node.contents:
            if isinstance(child, Tag | NavigableString):
                copy.append(_clone(child))
        return copy
    return NavigableString(str(node))


# --- .enex assembly -----------------------------------------------------


def build_enex(notes: list[NoteEnml], *, exported: datetime | None = None) -> str:
    """Wrap rendered notes into one `.enex` document.

    A single file imports in one confirmation click regardless of how many
    notes it holds, and Notes drops the whole batch into a fresh, numbered
    "Imported Notes N" folder -- which is what makes the landing folder an
    unambiguous handle on this run's notes.
    """
    stamp = _enex_timestamp(exported or datetime.now(UTC))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        _ENEX_DOCTYPE,
        f'<en-export export-date="{stamp}" application="quip2md" version="1">',
    ]
    for note in notes:
        parts.append(_render_note(note, stamp))
    parts.append("</en-export>")
    return "".join(parts)


def _render_note(note: NoteEnml, stamp: str) -> str:
    created = _normalize_timestamp(note.created) or stamp
    updated = _normalize_timestamp(note.updated) or created
    chunks = [
        "<note>",
        f"<title>{_escape(note.title)}</title>",
        f"<content>{_cdata(note.enml)}</content>",
        f"<created>{created}</created>",
        f"<updated>{updated}</updated>",
    ]
    for resource in note.resources:
        chunks.append(
            "<resource>"
            f'<data encoding="base64">{base64.b64encode(resource.data).decode("ascii")}</data>'
            f"<mime>{_escape(resource.mime)}</mime>"
            "<resource-attributes>"
            f"<file-name>{_escape(resource.filename)}</file-name>"
            "</resource-attributes>"
            "</resource>"
        )
    chunks.append("</note>")
    return "".join(chunks)


def _cdata(payload: str) -> str:
    """Wrap `payload` in CDATA, splitting any embedded `]]>` terminator."""
    return "<![CDATA[" + payload.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _enex_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y%m%dT%H%M%SZ")


def _normalize_timestamp(value: str | None) -> str | None:
    """Convert an ISO-8601 frontmatter timestamp to ENEX's compact form."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return _enex_timestamp(parsed)
