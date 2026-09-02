"""End-to-end data-integrity checks over the real fixture corpus.

Two properties, both about *not losing user data* on the way from Quip HTML to
the Apple Notes archive:

1. **Census round-trip** -- for every fixture in `tests/fixtures/`, the number
   of checklist items (checked and unchecked counted separately), numbered-list
   items, hyperlinks and images counted directly off the source HTML must equal
   the number that survive HTML -> Markdown -> ENML. The census is taken from
   Quip's own markup (`div[data-section-style]` wrappers, `<a href>`, `<img>`),
   not from the converter, so it is an independent measurement rather than a
   restatement of the code under test.

2. **Fuzz invariant** -- for randomly mutated Markdown derived from those same
   fixtures, `markdown_to_enml` must never raise, must emit well-formed XML,
   and must not drop visible text. See
   `test_fuzz_enml_is_well_formed_and_keeps_its_text` for the exact statement.
"""

from __future__ import annotations

import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, Tag

from quip2md.convert import ZERO_WIDTH_SPACE, html_to_markdown
from quip2md.enex import build_enex, markdown_to_enml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*.html"))

_SECTION_STYLE_ATTR = "data-section-style"
_STYLE_NUMBERED = "6"
_STYLE_CHECKLIST = "7"

_UNESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_TAG_RE = re.compile(r"<[^>]+>")


def _resolver(thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
    ext = f".{suggested_ext}" if suggested_ext else ""
    return f"_assets/{thread_id}/{blob_id}{ext}"


# --- Census -----------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Census:
    """What a document contains, counted straight off the source markup."""

    checked: int
    unchecked: int
    numbered: int
    links: int
    images: int


def _class_tokens(tag: Tag) -> list[str]:
    value = tag.get("class")
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return value.split()
    return []


def _is_empty_item(li: Tag) -> bool:
    """True for a Quip spacer row: no text of its own once sub-lists are ignored.

    Both converters deliberately drop these (an empty checkbox would be worse
    than nothing), so the census must ignore them too or it would be comparing
    against a number no correct implementation can produce.
    """
    for child in li.children:
        if isinstance(child, Tag):
            if child.name in ("ul", "ol"):
                continue
            if child.name == "img" or child.find("img") is not None:
                return False
            if child.get_text(strip=True).replace(ZERO_WIDTH_SPACE, ""):
                return False
        elif str(child).strip().replace(ZERO_WIDTH_SPACE, ""):
            return False
    return True


def census_html(html: str) -> Census:
    """Count the constructs a faithful conversion has to preserve."""
    soup = BeautifulSoup(html, "lxml")
    checked = unchecked = numbered = 0

    for list_tag in soup.find_all(["ul", "ol"]):
        wrapper = list_tag.find_parent(attrs={_SECTION_STYLE_ATTR: True})
        if wrapper is None:
            continue
        style = str(wrapper.get(_SECTION_STYLE_ATTR) or "").strip()
        items = [li for li in list_tag.find_all("li", recursive=False) if not _is_empty_item(li)]
        if style == _STYLE_CHECKLIST:
            for li in items:
                if any(token.lower() == "checked" for token in _class_tokens(li)):
                    checked += 1
                else:
                    unchecked += 1
        elif style == _STYLE_NUMBERED:
            numbered += len(items)

    return Census(
        checked=checked,
        unchecked=unchecked,
        numbered=numbered,
        links=len(soup.find_all("a", href=True)),
        images=len(soup.find_all("img")),
    )


def _numbered_items_in_enml(enml: str) -> int:
    """Every `<li>` whose own list is an `<ol>`, at any nesting depth."""
    body = enml.split("<en-note>", 1)[1]
    soup = BeautifulSoup(body, "html.parser")
    owners = (li.find_parent(["ul", "ol"]) for li in soup.find_all("li"))
    return sum(1 for owner in owners if owner is not None and owner.name == "ol")


def _image_references_in_enml(enml: str) -> int:
    return enml.count("<en-media") + enml.count("[missing image:")


@pytest.mark.parametrize("fixture", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_census_survives_html_to_markdown_to_enml(fixture: Path) -> None:
    """No checklist item, numbered item, link or image is lost on the way to ENML."""
    html = fixture.read_text(encoding="utf-8")
    expected = census_html(html)

    markdown = html_to_markdown(html, _resolver).markdown
    note = markdown_to_enml(
        title=fixture.stem,
        quip_url=None,
        markdown_text=markdown,
        md_dir=fixture.parent,
    )

    actual = Census(
        checked=sum(1 for item in note.checklist if item.checked),
        unchecked=sum(1 for item in note.checklist if not item.checked),
        numbered=_numbered_items_in_enml(note.enml),
        links=note.enml.count("<a href="),
        images=_image_references_in_enml(note.enml),
    )
    assert actual == expected


def test_the_corpus_actually_exercises_every_counted_construct() -> None:
    """Guards the census tests against becoming vacuously true (all zeros)."""
    totals = [census_html(path.read_text(encoding="utf-8")) for path in FIXTURE_PATHS]
    assert sum(c.unchecked for c in totals) > 0
    assert sum(c.numbered for c in totals) > 0
    assert sum(c.links for c in totals) > 0
    assert sum(c.images for c in totals) > 0


def test_the_synthetic_list_fixture_keeps_state_and_depth_end_to_end() -> None:
    """The one fixture built to exercise every list kind, asserted exactly.

    Checked/unchecked state, nesting depth, the numbered list (which Quip
    emits as a `<ul>`), and the empty spacer row that must collapse away.
    """
    fixture = FIXTURES_DIR / "doc_lists_THREAD0013.html"
    markdown = html_to_markdown(fixture.read_text(encoding="utf-8"), _resolver).markdown
    note = markdown_to_enml(
        title="Project Checklist",
        quip_url=None,
        markdown_text=markdown,
        md_dir=fixture.parent,
    )

    assert [(item.text, item.checked, item.depth) for item in note.checklist] == [
        ("Book the room", True, 0),
        ("Send the agenda", False, 0),
        ("Draft it", True, 1),
        ("Circulate it", False, 1),
    ]
    assert note.needs_indent_pass is True
    assert "<ol><li>First step</li><li>Second step<ol><li>A sub step</li></ol></li>" in note.enml
    assert "<ul><li>A plain bullet</li></ul>" in note.enml


def test_the_whole_corpus_builds_one_parseable_archive() -> None:
    notes = []
    for fixture in FIXTURE_PATHS:
        markdown = html_to_markdown(fixture.read_text(encoding="utf-8"), _resolver).markdown
        notes.append(
            markdown_to_enml(
                title=fixture.stem,
                quip_url=f"https://example.quip.com/{fixture.stem}",
                markdown_text=markdown,
                md_dir=fixture.parent,
            )
        )

    root = ET.fromstring(build_enex(notes))
    assert len(root.findall("note")) == len(FIXTURE_PATHS)
    for note in notes:
        ET.fromstring(note.enml)


# --- Fuzz -------------------------------------------------------------------

MUTATION_COUNT = 150
SEED = 20260901

_MARKDOWN_SYNTAX = "*_`#>-+|[]()!~\\"


_HREF_RE = re.compile(r'href="([^"]*)"')


def _visible_text(markup: str) -> str:
    """Text a reader would see, plus link targets.

    A link's URL is visible in the Markdown source but lives in an `href`
    attribute in the ENML, so it has to be counted as surviving text or every
    `[label](url)` would look like a loss.
    """
    return _normalize(_TAG_RE.sub(" ", markup) + " " + " ".join(_HREF_RE.findall(markup)))


def _normalize(text: str) -> str:
    text = text.replace(ZERO_WIDTH_SPACE, "")
    text = _UNESCAPE_RE.sub(r"\1", text)
    return " ".join(text.split())


_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _comparable_words(markdown: str) -> list[str]:
    """Words the ENML must still contain, with pure syntax punctuation removed.

    Image references are dropped first: one legitimately becomes an
    `<en-media>` element or a "[missing image: name]" placeholder, and neither
    carries the asset path the Markdown spelled out.
    """
    text = _IMAGE_REF_RE.sub(" ", markdown)
    text = _normalize(text.translate({ord(char): " " for char in _MARKDOWN_SYNTAX}))
    return [word for word in text.split() if len(word) > 3]


def _mutate(markdown: str, rng: random.Random) -> str:
    """One of: truncate, duplicate a line, delete a line, or scramble indentation."""
    lines = markdown.splitlines()
    if not lines:
        return markdown
    choice = rng.randrange(4)
    index = rng.randrange(len(lines))
    if choice == 0:
        return "\n".join(lines[:index])
    if choice == 1:
        lines.insert(index, lines[index])
    elif choice == 2:
        del lines[index]
    else:
        lines[index] = " " * rng.randrange(0, 7) + lines[index].lstrip()
    return "\n".join(lines)


def test_fuzz_enml_is_well_formed_and_keeps_its_text(tmp_path: Path) -> None:
    """Invariant, for any mutation of Markdown the converter itself produced:

    `markdown_to_enml` never raises; its `enml` parses as XML; and every word
    of the mutated Markdown that is not pure syntax punctuation still appears
    in the rendered ENML's visible text. Words rather than whole nodes, because
    a mutation can split a construct across blocks and legitimately re-flow the
    surrounding whitespace -- but it can never justify a word disappearing.
    """
    rng = random.Random(SEED)
    sources = [
        html_to_markdown(path.read_text(encoding="utf-8"), _resolver).markdown
        for path in FIXTURE_PATHS
    ]
    assert sources, "no fixtures found -- the fuzz test would be vacuous"

    for iteration in range(MUTATION_COUNT):
        mutated = _mutate(rng.choice(sources), rng)

        try:
            note = markdown_to_enml(
                title="Doc",
                quip_url="https://example.quip.com/THREAD0013",
                markdown_text=mutated,
                md_dir=tmp_path,
            )
        except Exception as exc:  # noqa: BLE001 -- the assertion IS the failure mode
            raise AssertionError(f"iteration {iteration}: markdown_to_enml raised {exc!r}") from exc

        try:
            ET.fromstring(note.enml)
        except ET.ParseError as exc:
            raise AssertionError(
                f"iteration {iteration}: ENML is not well-formed XML: {exc}"
            ) from exc

        rendered = _visible_text(note.enml)
        missing = [word for word in _comparable_words(mutated) if word not in rendered]
        assert not missing, f"iteration {iteration}: words dropped from the ENML: {missing[:5]!r}"
