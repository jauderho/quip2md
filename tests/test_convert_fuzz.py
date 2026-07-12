"""Deterministic fuzz test for quip2md.convert.html_to_markdown.

Generates ~200 seeded mutations of the real Quip HTML fixtures in
`tests/fixtures/` -- random tag deletions, attribute stripping, truncation at
random byte offsets, and tag-name scrambling -- and asserts the module's
binding contract (see the `convert.py` module docstring) holds for all of
them: `html_to_markdown` never raises, and no visible text present in the
*mutated* input (the actual string passed to the function -- not the
original, un-mutated fixture) is missing from the output.

A fixed seed makes this deterministic: re-running the test produces the same
sequence of mutations and the same result every time.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from quip2md.convert import ZERO_WIDTH_SPACE, html_to_markdown

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*.html"))

MUTATION_COUNT = 200
SEED = 20260711

# Same normalization approach as tests/test_convert.py's text-preservation
# property test: undo markdown escaping and collapse whitespace before
# substring comparison.
_UNESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def _default_resolver(thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
    ext = f".{suggested_ext}" if suggested_ext else ""
    return f"_assets/{thread_id}/{blob_id}{ext}"


def _normalize_for_comparison(text: str) -> str:
    text = text.replace(ZERO_WIDTH_SPACE, "")
    text = _UNESCAPE_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _pick_tag(soup: BeautifulSoup, rng: random.Random) -> Tag | None:
    tags = soup.find_all(True)
    if not tags:
        return None
    return rng.choice(tags)


def _scramble_name(name: str, rng: random.Random) -> str:
    chars = list(name)
    rng.shuffle(chars)
    scrambled = "".join(chars) or "x"
    if not scrambled[0].isalpha():
        scrambled = "x" + scrambled
    return scrambled


def _mutate_delete_tag(html: str, rng: random.Random) -> str:
    """Randomly unwrap (keep children) or decompose (drop subtree) a tag."""
    soup = BeautifulSoup(html, "html.parser")
    tag = _pick_tag(soup, rng)
    if tag is not None:
        if rng.random() < 0.5:
            tag.unwrap()
        else:
            tag.decompose()
    return str(soup)


def _mutate_strip_attributes(html: str, rng: random.Random) -> str:
    soup = BeautifulSoup(html, "html.parser")
    tag = _pick_tag(soup, rng)
    if tag is not None:
        tag.attrs = {}
    return str(soup)


def _mutate_truncate(html: str, rng: random.Random) -> str:
    """Truncate at a random byte offset, discarding a dangling partial char."""
    data = html.encode("utf-8")
    if not data:
        return html
    offset = rng.randint(0, len(data))
    return data[:offset].decode("utf-8", errors="ignore")


def _mutate_scramble_tag_name(html: str, rng: random.Random) -> str:
    soup = BeautifulSoup(html, "html.parser")
    tag = _pick_tag(soup, rng)
    if tag is not None:
        tag.name = _scramble_name(tag.name, rng)
    return str(soup)


_MUTATORS = (
    _mutate_delete_tag,
    _mutate_strip_attributes,
    _mutate_truncate,
    _mutate_scramble_tag_name,
)


def test_fuzz_mutations_never_raise_and_never_lose_text() -> None:
    rng = random.Random(SEED)
    fixture_texts = {path: path.read_text(encoding="utf-8") for path in FIXTURE_PATHS}
    assert fixture_texts, "no fixtures found -- fuzz test would be vacuous"

    for iteration in range(MUTATION_COUNT):
        fixture_path = rng.choice(FIXTURE_PATHS)
        html = fixture_texts[fixture_path]
        mutator = rng.choice(_MUTATORS)
        mutated_html = mutator(html, rng)

        try:
            result = html_to_markdown(mutated_html, _default_resolver)
        except Exception as exc:  # noqa: BLE001 -- the assertion IS the failure mode
            raise AssertionError(
                f"iteration {iteration}: html_to_markdown raised {exc!r} on a "
                f"{mutator.__name__} mutation of {fixture_path.name}"
            ) from exc

        soup = BeautifulSoup(mutated_html, "html.parser")
        normalized_markdown = _normalize_for_comparison(result.markdown)
        missing = [
            normalized_node
            for node in soup.find_all(string=True)
            if (normalized_node := _normalize_for_comparison(str(node)))
            and normalized_node not in normalized_markdown
        ]
        assert not missing, (
            f"iteration {iteration}: visible text dropped by a "
            f"{mutator.__name__} mutation of {fixture_path.name}: {missing[:5]!r}"
        )
