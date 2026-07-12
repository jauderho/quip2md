"""Apple Notes importer: source scanning, Markdown->Notes-HTML conversion,
the `osascript` bridge, resume state, and `run_import()` orchestration.

Reads the Markdown tree an earlier `quip2md export` run wrote to disk
(frontmatter + body), converts each document's body to the narrow HTML
dialect Apple Notes actually accepts (per `docs/NOTES_API_NOTES.md`, T9's
live recon on this machine), and creates/updates notes under a top-level
"Quip" folder in Notes.app via AppleScript, mirroring the export folder
tree. The CLI subcommand (`import-notes`) is T11's job -- this module only
exposes `run_import()` for it to call.

Design notes, all keyed to observed facts in `docs/NOTES_API_NOTES.md`:

* Notes strips `<a href>` entirely on input (only the underlined text
  survives), so links are rendered as plain `text (url)` at conversion
  time -- never as `<a>` tags. When the link text is the URL itself (the
  common case for autolinks in our own exported Markdown), the redundant
  parenthetical is skipped.
* `<br>` collapses to a single space in Notes' own body normalization, so
  hard line breaks inside a `<p>` are rewritten as sibling `<div>` blocks
  before the HTML is ever sent. This is only done for `<br>` that are
  direct children of a `<p>` (the common case for Markdown hard breaks,
  e.g. two-trailing-space breaks, which is all `python-markdown` ever
  emits without the `nl2br` extension); a stray `<br>` nested deeper
  (list item, table cell) is left in place -- an accepted, minor
  degradation, not attempted here.
* Code blocks are rewritten from `<pre><code>` to one
  `<div><font face="Courier">line</font></div>` per line, matching how
  Notes itself normalizes `<pre>`/`<code>` on input (recon section 3).
  Blank lines inside a code block are rendered as `<div>` wrapping a
  literal U+00A0 (NO-BREAK SPACE) rather than the literal text `&nbsp;`
  -- semantically identical once parsed, but worth a T12 live check since
  recon did not specifically probe empty-line-in-code-block behavior.
* `<img src="file://...">` in the body embeds for real (recon section 4);
  paths are resolved to an absolute, `Path.as_uri()`-escaped `file://`
  URL relative to the source `.md` file's own directory (matching how
  `export.py` writes `_assets/<thread_id>/<blob_id>` image references).
  A missing asset file becomes a `[missing image: <name>]` text warning
  instead of a broken tag.
* Nested `<ul>` survives Notes' own normalization; nested `<ol>` does not
  (recon section 3 round-trip: Notes flattens sub-`<ol>`s). This is
  accepted, not worked around -- each nested `<ol>` found is counted as
  one warning so the flattening is visible in the run report.
* `h1`/`h2`/`h3`, `blockquote`, `hr` all degrade (to bold+font-size text,
  or disappear) per recon section 3; accepted, no special-casing needed
  beyond making sure the note's very first element is an `<h1>` with the
  document title, since Notes uses the first line of the body as the
  note's title.
* `markdown` (stdlib-adjacent, `tables` + `fenced_code` extensions, both
  bundled) converts our own exported Markdown, which is itself produced
  by `markdownify` with 2-space list indentation -- not the 4-space
  indentation `python-markdown` assumes by default. `tab_length=2` is
  passed to close that gap; very deeply nested lists (5+ levels, seen in
  one real export fixture) can still lose a level of nesting under either
  setting. This is a `markdown`-package limitation on our input dialect,
  not something this module works around -- flagged here for T12's live
  eyeball pass.
* Folder creation is always get-or-create: Notes rejects a second
  folder with the same name at the same level with error -10000 (recon
  section 2), so a plain `make new folder` is never safe to call blindly.
* A tracked note's id can still resolve via `note id ...` even after the
  note itself has been deleted (recon section 6, "Recently Deleted" is
  where deleted notes go, and lookups there still succeed) -- so
  `run_import()` never trusts a stored id in isolation. It cross-checks
  every tracked id against `notes_of_folder()`'s live listing before
  deciding whether to update in place or recreate.
* Batching multiple note creates into one `osascript` invocation is
  reliable and roughly 10x faster than one-invocation-per-note (recon
  section 7); `create_notes()` chunks at `DEFAULT_BATCH_SIZE` notes and
  `MAX_BATCH_PAYLOAD_BYTES` of combined body size per invocation,
  whichever limit is hit first.
* The first `osascript` invocation in a process may block on a one-time
  TCC automation-permission dialog (recon section 8), so it gets a longer
  timeout (`FIRST_INVOCATION_TIMEOUT_SECONDS`) than every subsequent call
  (`SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS`).

`markdown_to_notes_html()` and `scan_source()`/`parse_frontmatter()` are
pure/filesystem-read-only (aside from checking whether a referenced image
file exists), never calling `osascript`. `NotesRunner` isolates every
`osascript` invocation behind `NotesRunnerProtocol`, a structural
`Protocol` implemented by a hand-written fake in tests -- no test in this
module or its test file ever shells out to `osascript`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape as html_escape
from pathlib import Path
from typing import Protocol

import markdown as _markdown
from bs4 import BeautifulSoup, NavigableString, Tag

from quip2md.config import DEFAULT_OUTPUT_DIR, Config
from quip2md.walker import sanitize_component

logger = logging.getLogger("quip2md.notes_import")

# --- Constants ---------------------------------------------------------

NOTES_ROOT_FOLDER = "Quip"
IMPORT_KEY_PATH_PREFIX = "path:"
_ASSETS_DIR_NAME = "_assets"

MARKDOWN_EXTENSIONS = ("tables", "fenced_code")
MARKDOWN_TAB_LENGTH = 2

DEFAULT_BATCH_SIZE = 10
MAX_BATCH_PAYLOAD_BYTES = 200_000

FIRST_INVOCATION_TIMEOUT_SECONDS = 120.0
SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS = 30.0

ON_MY_MAC_ACCOUNT_NAME = "On My Mac"

NOTES_STATE_FILENAME = "notes_state.json"


# --- Errors --------------------------------------------------------------


class NotesError(RuntimeError):
    """A non-retryable Notes automation error.

    Carries the raw `osascript` stderr (if any) for diagnostics; never
    carries note/folder body content beyond what the caller already
    supplied to the failing call.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        self.stderr = stderr
        super().__init__(f"{message}: {stderr}" if stderr else message)


# --- Frontmatter parsing ---------------------------------------------------


@dataclass(slots=True, frozen=True)
class NoteFrontmatter:
    """Leniently-parsed subset of the YAML frontmatter `convert.build_frontmatter`
    writes. All fields are `None` when absent or when the frontmatter block
    itself is missing/malformed -- callers fall back to sensible defaults
    rather than treating that as an error, since not every `.md` under
    `--source` is guaranteed to be one of our own exports."""

    quip_id: str | None
    quip_url: str | None
    title: str | None
    created: str | None
    updated: str | None


_FRONTMATTER_DELIMITER = "---"
_NO_FRONTMATTER = NoteFrontmatter(
    quip_id=None, quip_url=None, title=None, created=None, updated=None
)


def parse_frontmatter(text: str) -> tuple[NoteFrontmatter, str]:
    """Split `text` into (frontmatter, body).

    Hand-rolled, deliberately not a general YAML parser: it only needs to
    invert `convert.build_frontmatter`'s own output (double-quoted
    scalars, `\\`, `\\"`, `\\n`, `\\t` escapes). Any frontmatter block that
    doesn't parse cleanly (missing opening/closing `---`, or simply
    absent) yields `_NO_FRONTMATTER` and the original text as the body --
    never raises.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return _NO_FRONTMATTER, text

    fields: dict[str, str] = {}
    index = 1
    closed = False
    while index < len(lines):
        line = lines[index]
        if line.strip() == _FRONTMATTER_DELIMITER:
            closed = True
            index += 1
            break
        key, sep, raw_value = line.partition(": ")
        if sep:
            fields[key.strip()] = _unquote_yaml_scalar(raw_value)
        index += 1

    if not closed:
        return _NO_FRONTMATTER, text

    body = "\n".join(lines[index:])
    return (
        NoteFrontmatter(
            quip_id=fields.get("quip_id"),
            quip_url=fields.get("quip_url"),
            title=fields.get("title"),
            created=fields.get("created"),
            updated=fields.get("updated"),
        ),
        body,
    )


def _unquote_yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return _unescape_dquoted(value[1:-1])
    return value


def _unescape_dquoted(value: str) -> str:
    mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in mapping:
            result.append(mapping[value[index + 1]])
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


# --- Source scanning ---------------------------------------------------


@dataclass(slots=True, frozen=True)
class NoteSource:
    """One `.md` file discovered under `--source`, ready for conversion."""

    key: str
    md_path: Path
    relative_path: str
    folder_path: tuple[str, ...]
    title: str
    quip_url: str | None
    body_markdown: str
    keyed_by_path: bool


def scan_source(source_dir: Path) -> list[NoteSource]:
    """Walk `source_dir` for `*.md` files (skipping any `_assets/` directory).

    Each file's Notes folder path is `("Quip", *sanitized relative dirs)`.
    A file with a `quip_id` in its frontmatter is keyed by that id; one
    without (not one of our own exports, or a corrupted/hand-edited file)
    is keyed by `f"path:{relative_posix_path}"` instead -- a distinct
    namespace (the `path:` prefix) that can never collide with a real Quip
    thread id, so `NoteSource.keyed_by_path` tells callers which is which.
    """
    sources: list[NoteSource] = []
    for md_path in sorted(source_dir.rglob("*.md")):
        relative = md_path.relative_to(source_dir)
        if _ASSETS_DIR_NAME in relative.parts:
            continue
        text = md_path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        relative_posix = relative.as_posix()
        folder_parts = tuple(sanitize_component(part) for part in relative.parent.parts)
        folder_path = (NOTES_ROOT_FOLDER, *folder_parts)
        title = frontmatter.title or md_path.stem

        if frontmatter.quip_id:
            key = frontmatter.quip_id
            keyed_by_path = False
        else:
            key = f"{IMPORT_KEY_PATH_PREFIX}{relative_posix}"
            keyed_by_path = True

        sources.append(
            NoteSource(
                key=key,
                md_path=md_path,
                relative_path=relative_posix,
                folder_path=folder_path,
                title=title,
                quip_url=frontmatter.quip_url,
                body_markdown=body,
                keyed_by_path=keyed_by_path,
            )
        )
    return sources


# --- Markdown -> Notes HTML conversion --------------------------------------


@dataclass(slots=True, frozen=True)
class NoteHtml:
    """Result of converting one document's Markdown body to Notes-safe HTML."""

    html: str
    warnings: tuple[str, ...]


def markdown_to_notes_html(
    *, title: str, quip_url: str | None, markdown_text: str, md_dir: Path
) -> NoteHtml:
    """Convert one document's Markdown body to the Notes-safe HTML dialect.

    `md_dir` is the directory the source `.md` file lives in -- relative
    image references (`_assets/<thread_id>/<blob_id>`) are resolved
    against it. Never raises on malformed input or a missing image; both
    degrade to a warning instead (see module docstring for the exact
    Notes-safe transformations applied).
    """
    warnings: list[str] = []
    fragment_html = _markdown.markdown(
        markdown_text, extensions=list(MARKDOWN_EXTENSIONS), tab_length=MARKDOWN_TAB_LENGTH
    )
    soup = BeautifulSoup(fragment_html, "html.parser")

    _strip_links(soup)
    _resolve_images(soup, md_dir, warnings)
    _convert_code_blocks(soup)
    _count_nested_ordered_lists(soup, warnings)
    _split_hard_breaks(soup)

    # Live finding (T12): exported docs usually begin with an h1 of their
    # own title, and Notes renders the prepended title h1 *and* that one —
    # showing the title twice. Drop the body's leading h1 only when it
    # duplicates the frontmatter title.
    first_tag = next((child for child in soup.contents if isinstance(child, Tag)), None)
    if (
        first_tag is not None
        and first_tag.name == "h1"
        and first_tag.get_text(strip=True) == title.strip()
    ):
        first_tag.decompose()

    body_html = "".join(str(child) for child in soup.contents)
    title_html = f"<h1>{html_escape(title)}</h1>"
    source_html = f"<div>{html_escape(quip_url)}</div>" if quip_url else ""
    return NoteHtml(html=title_html + source_html + body_html, warnings=tuple(warnings))


def _attr_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strip_links(soup: BeautifulSoup) -> None:
    """Replace every `<a href>` with plain `text (url)` text.

    Notes strips `<a href>` entirely on input (recon section 3: only the
    underlined text survives), so there is never a reason to send an
    `<a>` tag. When the visible text already is the URL (autolinks --
    what `python-markdown` emits for bare `<https://...>` links, which is
    what `convert.py` produces for Quip links it can't resolve to a
    title), the redundant `(url)` suffix is skipped.
    """
    for anchor in soup.find_all("a"):
        href = _attr_text(anchor.get("href"))
        text = anchor.get_text()
        if not href:
            anchor.unwrap()
            continue
        if text.strip() == href.strip():
            anchor.replace_with(text)
        else:
            anchor.replace_with(f"{text} ({href})")


def _resolve_images(soup: BeautifulSoup, md_dir: Path, warnings: list[str]) -> None:
    """Rewrite `<img src>` to an absolute, escaped `file://` URL.

    Relative sources (the only kind `convert.py` ever writes) are
    resolved against `md_dir`. A source that is already an absolute
    `http(s)://`/`file://` URL is left untouched. A missing image file
    becomes a `[missing image: <name>]` text warning rather than a
    dangling `<img>` tag Notes would silently drop.
    """
    for img in soup.find_all("img"):
        src = _attr_text(img.get("src"))
        if not src:
            warnings.append("image tag with an empty src")
            img.replace_with("[missing image: (empty src)]")
            continue
        if src.startswith(("http://", "https://", "file://")):
            continue
        resolved = (md_dir / src).resolve()
        if not resolved.is_file():
            warnings.append(f"missing image: {src}")
            img.replace_with(f"[missing image: {Path(src).name}]")
            continue
        img["src"] = resolved.as_uri()


def _convert_code_blocks(soup: BeautifulSoup) -> None:
    """Rewrite each `<pre><code>` block to one Courier `<div>` per line.

    Matches how Notes itself normalizes code on input (recon section 3:
    `<font face="Courier">`-wrapped `<div>`s, one per line). A blank line
    is rendered as a `<div>` wrapping a literal U+00A0 (see module
    docstring) rather than an empty `<div>`, since an empty `<div>` risks
    being collapsed the same way `<br>` is.
    """
    for pre in soup.find_all("pre"):
        code = pre.find("code")
        text = code.get_text() if code is not None else pre.get_text()
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]

        replacement_divs: list[Tag] = []
        for line in lines:
            div = soup.new_tag("div")
            if line.strip() == "":
                div.append("\N{NO-BREAK SPACE}")
            else:
                font = soup.new_tag("font")
                font["face"] = "Courier"
                font.string = line
                div.append(font)
            replacement_divs.append(div)

        for div in replacement_divs:
            pre.insert_before(div)
        pre.decompose()


def _count_nested_ordered_lists(soup: BeautifulSoup, warnings: list[str]) -> None:
    """Count (but do not alter) every `<ol>` nested inside another list.

    Notes flattens nested `<ol>` on input (recon section 3); this is
    accepted, not worked around here -- only surfaced as a warning so a
    run's report shows how many documents were affected.
    """
    for ordered_list in soup.find_all("ol"):
        if ordered_list.find_parent(["ol", "ul"]) is not None:
            warnings.append("nested <ol> will be flattened by Notes")


def _split_hard_breaks(soup: BeautifulSoup) -> None:
    """Split each `<p>` containing `<br>` into sibling `<div>`s.

    Notes collapses `<br>` to a single space (recon section 3), so a hard
    line break can only survive as a block boundary. Only `<br>` that are
    direct children of a `<p>` are handled -- the shape `python-markdown`
    always produces for a Markdown hard break -- a `<br>` nested deeper
    (list item, table cell) is left as-is (accepted minor degradation).
    """
    for paragraph in soup.find_all("p"):
        if paragraph.find("br") is None:
            continue
        replacement_divs: list[Tag] = []
        current = soup.new_tag("div")
        for child in list(paragraph.children):
            if isinstance(child, Tag) and child.name == "br":
                replacement_divs.append(current)
                current = soup.new_tag("div")
            else:
                extracted = child.extract()
                if not current.contents and isinstance(extracted, NavigableString):
                    # `python-markdown` always emits the source newline right
                    # after a hard-break `<br />` (e.g. "<br />\nline two");
                    # that leading newline is source-wrapping, not content.
                    extracted = NavigableString(str(extracted).lstrip("\n"))
                current.append(extracted)
        replacement_divs.append(current)
        for div in replacement_divs:
            paragraph.insert_before(div)
        paragraph.decompose()


# --- osascript bridge --------------------------------------------------


class NotesRunnerProtocol(Protocol):
    """The subset of Notes automation `run_import()` needs.

    A structural `Protocol` (matching `walker.FolderSource`'s and
    `export.ExportClient`'s pattern) so tests exercise `run_import()`
    against a hand-written fake -- no test ever shells out to `osascript`.
    """

    def resolve_account(self, *, local: bool) -> str: ...

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str: ...

    def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]: ...

    def note_ids_in_folder(self, folder_id: str) -> frozenset[str]: ...

    def set_note_body(self, note_id: str, body_html: str) -> None: ...


def _chunk_by_size[T](
    items: Sequence[T], *, size_of: Callable[[T], int], max_count: int, max_bytes: int
) -> Iterator[list[T]]:
    """Chunk `items` so each chunk fits one `osascript` batch call.

    A chunk never exceeds `max_count` items nor `max_bytes` of combined
    `size_of` weight, whichever limit is hit first. A single item heavier
    than `max_bytes` on its own still gets its own one-item chunk (one
    `osascript` invocation always creates whole notes and can't be split
    further).
    """
    chunk: list[T] = []
    chunk_bytes = 0
    for item in items:
        item_bytes = size_of(item)
        if chunk and (len(chunk) >= max_count or chunk_bytes + item_bytes > max_bytes):
            yield chunk
            chunk = []
            chunk_bytes = 0
        chunk.append(item)
        chunk_bytes += item_bytes
    if chunk:
        yield chunk


def chunk_note_bodies(
    bodies: Sequence[str], *, max_count: int, max_bytes: int
) -> Iterator[list[str]]:
    """Chunk note-body strings for one `osascript` batch-create call each."""
    yield from _chunk_by_size(
        bodies, size_of=lambda b: len(b.encode("utf-8")), max_count=max_count, max_bytes=max_bytes
    )


# AppleScript snippets. Every handler is `on run argv ... end run`; content
# (account/folder names, note bodies, ids) always crosses the process
# boundary via `argv`, never interpolated into the script text -- matching
# `scripts/notes_recon.py`'s own convention (see its module docstring).

_AS_LIST_ACCOUNTS = """
on run argv
    tell application "Notes"
        set accNames to {}
        repeat with a in accounts
            set end of accNames to name of a
        end repeat
        return (name of default account) & "||" & (my joinList(accNames, ","))
    end tell
end run

on joinList(lst, delim)
    if (count of lst) is 0 then return ""
    set AppleScript's text item delimiters to delim
    set s to lst as string
    set AppleScript's text item delimiters to ""
    return s
end joinList
"""

# Live-verified in T12: `account "<name>"` by-name lookup works (the
# default account's name resolves to its account id). NB: `container` is a
# reserved Notes-dictionary term — inside `tell application "Notes"` a
# `set container to ...` is a property assignment, not a local variable
# (error -10006) — hence `targetContainer` below.
_AS_GET_OR_CREATE_FOLDER = """
on run argv
    set acc to item 1 of argv
    set isNested to item 2 of argv
    set parentRef to item 3 of argv
    set folderName to item 4 of argv
    tell application "Notes"
        if acc is "" then
            set theAccount to default account
        else
            set theAccount to account acc
        end if
        if isNested is "1" then
            set targetContainer to folder id parentRef
        else
            set targetContainer to theAccount
        end if
        repeat with f in folders of targetContainer
            if name of f is folderName then
                return id of f
            end if
        end repeat
        try
            set newFolder to make new folder at targetContainer with properties {name:folderName}
            return id of newFolder
        on error errMsg number errNum
            if errNum is -10000 then
                repeat with f in folders of targetContainer
                    if name of f is folderName then
                        return id of f
                    end if
                end repeat
            end if
            error errMsg number errNum
        end try
    end tell
end run
"""

_AS_CREATE_NOTES_BATCH = """
on run argv
    set folderId to item 1 of argv
    tell application "Notes"
        set targetFolder to folder id folderId
        set noteIds to {}
        repeat with i from 2 to (count of argv)
            set b to item i of argv
            set n to make new note at targetFolder with properties {body:b}
            set end of noteIds to (id of n as string)
        end repeat
        return my joinList(noteIds, ",")
    end tell
end run

on joinList(lst, delim)
    if (count of lst) is 0 then return ""
    set AppleScript's text item delimiters to delim
    set s to lst as string
    set AppleScript's text item delimiters to ""
    return s
end joinList
"""

_AS_LIST_NOTES_IN_FOLDER = """
on run argv
    set folderId to item 1 of argv
    tell application "Notes"
        set targetFolder to folder id folderId
        set noteIds to {}
        repeat with n in notes of targetFolder
            set end of noteIds to id of n
        end repeat
        return my joinList(noteIds, ",")
    end tell
end run

on joinList(lst, delim)
    if (count of lst) is 0 then return ""
    set AppleScript's text item delimiters to delim
    set s to lst as string
    set AppleScript's text item delimiters to ""
    return s
end joinList
"""

_AS_SET_NOTE_BODY = """
on run argv
    set noteId to item 1 of argv
    set newBody to item 2 of argv
    tell application "Notes"
        set body of note id noteId to newBody
    end tell
end run
"""


class NotesRunner:
    """Real Notes automation bridge: every method shells out to `osascript`.

    Raises `NotesError` immediately in `__init__` on any non-macOS
    platform, before any subprocess is ever spawned. The very first
    `osascript` call in the process's lifetime uses
    `FIRST_INVOCATION_TIMEOUT_SECONDS` (a TCC automation-permission
    dialog can block on it, per recon section 8); every call after that
    uses `SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS`.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise NotesError(
                "Apple Notes import requires macOS (osascript is not available on "
                f"this platform: {sys.platform!r})"
            )
        self._first_call_done = False
        self._folder_id_cache: dict[tuple[str, tuple[str, ...]], str] = {}

    def _run(self, script: str, argv: Sequence[str]) -> str:
        timeout = (
            SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS
            if self._first_call_done
            else FIRST_INVOCATION_TIMEOUT_SECONDS
        )
        cmd = ["osascript", "-e", script, "--", *argv]
        try:
            proc: subprocess.CompletedProcess[str] = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            self._first_call_done = True
            raise NotesError(f"osascript timed out after {timeout}s") from exc
        self._first_call_done = True
        if proc.returncode != 0:
            raise NotesError(
                f"osascript exited with status {proc.returncode}", stderr=proc.stderr.strip()
            )
        return proc.stdout

    def resolve_account(self, *, local: bool) -> str:
        stdout = self._run(_AS_LIST_ACCOUNTS, [])
        default_name, _, rest = stdout.strip().partition("||")
        names = [name for name in rest.split(",") if name]
        if local:
            if ON_MY_MAC_ACCOUNT_NAME not in names:
                raise NotesError(
                    f'"{ON_MY_MAC_ACCOUNT_NAME}" account not found on this machine '
                    f"(available accounts: {names})"
                )
            return ON_MY_MAC_ACCOUNT_NAME
        return default_name

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        parent_id = ""
        for depth, name in enumerate(path):
            cache_key = (account, tuple(path[: depth + 1]))
            cached = self._folder_id_cache.get(cache_key)
            if cached is not None:
                parent_id = cached
                continue
            is_nested = "1" if depth > 0 else "0"
            folder_id = self._run(
                _AS_GET_OR_CREATE_FOLDER, [account, is_nested, parent_id, name]
            ).strip()
            self._folder_id_cache[cache_key] = folder_id
            parent_id = folder_id
        return parent_id

    def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
        # One osascript invocation for the given bodies -- the caller
        # (`_create_notes`) pre-chunks to `DEFAULT_BATCH_SIZE` /
        # `MAX_BATCH_PAYLOAD_BYTES` so that state can be recorded per
        # invocation (a failure here loses at most this one batch, never
        # earlier batches' already-created ids).
        if not bodies:
            return []
        stdout = self._run(_AS_CREATE_NOTES_BATCH, [folder_id, *bodies])
        return [part for part in stdout.strip().split(",") if part]

    def note_ids_in_folder(self, folder_id: str) -> frozenset[str]:
        stdout = self._run(_AS_LIST_NOTES_IN_FOLDER, [folder_id])
        return frozenset(part for part in stdout.strip().split(",") if part)

    def set_note_body(self, note_id: str, body_html: str) -> None:
        self._run(_AS_SET_NOTE_BODY, [note_id, body_html])


# --- Resume state --------------------------------------------------------


class NotesStateError(RuntimeError):
    """Raised when `notes_state.json` exists but cannot be parsed.

    Mirrors `walker.ManifestError`: the state file is never silently
    reset on corruption, since that would risk duplicate notes on the
    next run.
    """


@dataclass(slots=True, frozen=True)
class NoteStateEntry:
    note_id: str
    folder: str
    content_hash: str
    imported_at: str


class NotesState:
    """Resume/skip-unchanged state for `run_import()`, persisted atomically.

    Uses the same tmp-file-plus-`os.replace` pattern as `walker.Manifest`
    (see that class's `flush()` for the rationale); implemented separately
    here rather than shared, since the on-disk schema (`note_id`/`folder`/
    `content_hash`/`imported_at`, keyed by `NoteSource.key` rather than a
    Quip thread id) is unrelated to `Manifest`'s.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, NoteStateEntry] = {}

    def load(self) -> None:
        """Load state from disk. Tolerant of a missing file (empty state then)."""
        if not self._path.is_file():
            self._entries = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise NotesStateError(f"Could not read notes state at {self._path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise NotesStateError(f"Notes state at {self._path} is corrupted: {exc}") from exc
        if not isinstance(raw, dict):
            raise NotesStateError(
                f"Notes state at {self._path} has an unexpected shape (expected a JSON object)"
            )
        entries: dict[str, NoteStateEntry] = {}
        for key, value in raw.items():
            entries[key] = _parse_state_entry(self._path, key, value)
        self._entries = entries

    def get(self, key: str) -> NoteStateEntry | None:
        return self._entries.get(key)

    def record(self, key: str, entry: NoteStateEntry) -> None:
        self._entries[key] = entry

    def flush(self) -> None:
        """Persist current state atomically: write a temp file, then `os.replace`."""
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "note_id": entry.note_id,
                "folder": entry.folder,
                "content_hash": entry.content_hash,
                "imported_at": entry.imported_at,
            }
            for key, entry in self._entries.items()
        }
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{self._path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
            raise


def _parse_state_entry(path: Path, key: str, value: object) -> NoteStateEntry:
    if not isinstance(value, dict):
        raise NotesStateError(f"Notes state at {path} has a malformed entry for {key!r}")
    note_id = value.get("note_id")
    folder = value.get("folder")
    content_hash = value.get("content_hash")
    imported_at = value.get("imported_at")
    if not (
        isinstance(note_id, str)
        and isinstance(folder, str)
        and isinstance(content_hash, str)
        and isinstance(imported_at, str)
    ):
        raise NotesStateError(f"Notes state at {path} has a malformed entry for {key!r}")
    return NoteStateEntry(
        note_id=note_id, folder=folder, content_hash=content_hash, imported_at=imported_at
    )


def _content_hash(title: str, html: str) -> str:
    return hashlib.sha256(f"{title}\n{html}".encode()).hexdigest()


def _now_iso8601() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


# --- run_import ------------------------------------------------------------


@dataclass(slots=True)
class ImportReport:
    """Counts and outcomes for one `run_import()` call."""

    created: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    recreated_missing: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: int = 0
    keyed_by_path: int = 0
    folder_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped_unchanged": self.skipped_unchanged,
            "recreated_missing": self.recreated_missing,
            "failed": [{"key": key, "reason": reason} for key, reason in self.failed],
            "warnings": self.warnings,
            "keyed_by_path": self.keyed_by_path,
            "folder_counts": self.folder_counts,
            "elapsed_seconds": self.elapsed_seconds,
        }


def run_import(
    runner: NotesRunnerProtocol,
    config: Config,
    *,
    source_dir: Path = DEFAULT_OUTPUT_DIR,
    local: bool = False,
    only: Sequence[str] | None = None,
) -> ImportReport:
    """Scan, convert, and import `source_dir`'s Markdown tree into Apple Notes.

    Reads `config.dry_run` and `config.force` (both already general
    `Config` knobs -- `--source`/`--local` are Notes-import-specific and
    so are plain function parameters instead, per T10's contract not to
    modify `config.py`). `only`, if given, restricts the import to
    sources whose `NoteSource.key` is in the set (a Quip thread id, or a
    `path:`-prefixed key for sources without one -- see `scan_source()`).

    In `config.dry_run` mode: scans and converts every source (surfacing
    conversion warnings and per-folder counts) but calls `runner` zero
    times and never touches `.quip2md/notes_state.json` beyond a
    read-only `load()`. The dry-run created/updated/skipped counts are
    therefore best-effort: the "a tracked note went missing from its
    Notes folder" case can only be detected by asking Notes directly
    (`note_ids_in_folder()`), so it never surfaces as `recreated_missing`
    here -- only on a real run.

    `runner` is accepted even in dry-run mode for interface symmetry with
    a real run (mirroring `walker.walk()`'s treatment of `config`) --
    callers may pass any `NotesRunnerProtocol`-satisfying value, including
    a real `NotesRunner`, since it is guaranteed never to be called.
    """
    start_time = time.monotonic()
    only_keys = frozenset(only) if only is not None else None

    sources = scan_source(source_dir)
    if only_keys is not None:
        sources = [source for source in sources if source.key in only_keys]

    report = ImportReport()
    for source in sources:
        folder_key = "/".join(source.folder_path)
        report.folder_counts[folder_key] = report.folder_counts.get(folder_key, 0) + 1
        if source.keyed_by_path:
            report.keyed_by_path += 1

    state_path = config.state_path.parent / NOTES_STATE_FILENAME
    state = NotesState(state_path)
    state.load()

    if config.dry_run:
        _dry_run_diff(sources, state, report)
        report.elapsed_seconds = time.monotonic() - start_time
        return report

    account = runner.resolve_account(local=local)

    sources_by_folder: dict[tuple[str, ...], list[NoteSource]] = {}
    for source in sources:
        sources_by_folder.setdefault(source.folder_path, []).append(source)

    # Flush after every folder and again in `finally`: a Ctrl-C (or any
    # unexpected BaseException) mid-run must not lose the note ids already
    # created, or the re-run would duplicate every note imported so far.
    try:
        for folder_path, folder_sources in sources_by_folder.items():
            _import_folder(
                runner, account, folder_path, folder_sources, state, config.force, report
            )
            state.flush()
    finally:
        state.flush()
        report.elapsed_seconds = time.monotonic() - start_time
    return report


def _dry_run_diff(sources: Sequence[NoteSource], state: NotesState, report: ImportReport) -> None:
    for source in sources:
        conversion = markdown_to_notes_html(
            title=source.title,
            quip_url=source.quip_url,
            markdown_text=source.body_markdown,
            md_dir=source.md_path.parent,
        )
        report.warnings += len(conversion.warnings)
        content_hash = _content_hash(source.title, conversion.html)
        entry = state.get(source.key)
        if entry is None:
            report.created += 1
        elif entry.content_hash != content_hash:
            report.updated += 1
        else:
            report.skipped_unchanged += 1


def _import_folder(
    runner: NotesRunnerProtocol,
    account: str,
    folder_path: tuple[str, ...],
    folder_sources: Sequence[NoteSource],
    state: NotesState,
    force: bool,
    report: ImportReport,
) -> None:
    try:
        folder_id = runner.get_or_create_folder(account, folder_path)
    except Exception as exc:  # broad by design: folder-level failure isolation
        reason = f"folder creation failed: {_error_reason(exc)}"
        for source in folder_sources:
            report.failed.append((source.key, reason))
        return

    tracked_entries = {
        source.key: entry
        for source in folder_sources
        if (entry := state.get(source.key)) is not None
    }
    existing_ids_in_folder: frozenset[str] | None = None
    if tracked_entries:
        try:
            existing_ids_in_folder = runner.note_ids_in_folder(folder_id)
        except Exception as exc:  # broad by design: folder-level failure isolation
            reason = f"listing notes in folder failed: {_error_reason(exc)}"
            for source in folder_sources:
                report.failed.append((source.key, reason))
            return

    to_create: list[tuple[NoteSource, str, str]] = []
    recreate_keys: set[str] = set()
    to_update: list[tuple[NoteSource, str, str, str]] = []

    for source in folder_sources:
        try:
            conversion = markdown_to_notes_html(
                title=source.title,
                quip_url=source.quip_url,
                markdown_text=source.body_markdown,
                md_dir=source.md_path.parent,
            )
        except Exception as exc:  # broad by design: per-source failure isolation
            report.failed.append((source.key, f"conversion failed: {_error_reason(exc)}"))
            continue
        report.warnings += len(conversion.warnings)
        content_hash = _content_hash(source.title, conversion.html)
        entry = tracked_entries.get(source.key)

        if entry is None:
            to_create.append((source, conversion.html, content_hash))
            continue

        note_missing = (
            existing_ids_in_folder is not None and entry.note_id not in existing_ids_in_folder
        )
        if note_missing:
            to_create.append((source, conversion.html, content_hash))
            recreate_keys.add(source.key)
            continue

        if not force and entry.content_hash == content_hash:
            report.skipped_unchanged += 1
            continue

        to_update.append((source, entry.note_id, conversion.html, content_hash))

    if to_create:
        _create_notes(runner, folder_id, folder_path, to_create, recreate_keys, state, report)

    imported_at = _now_iso8601()
    for source, note_id, body_html, content_hash in to_update:
        try:
            runner.set_note_body(note_id, body_html)
        except Exception as exc:  # broad by design: per-note failure isolation
            report.failed.append((source.key, f"update failed: {_error_reason(exc)}"))
            continue
        state.record(
            source.key,
            NoteStateEntry(
                note_id=note_id,
                folder="/".join(folder_path),
                content_hash=content_hash,
                imported_at=imported_at,
            ),
        )
        report.updated += 1


def _create_notes(
    runner: NotesRunnerProtocol,
    folder_id: str,
    folder_path: tuple[str, ...],
    to_create: Sequence[tuple[NoteSource, str, str]],
    recreate_keys: set[str],
    state: NotesState,
    report: ImportReport,
) -> None:
    folder = "/".join(folder_path)
    # Chunk here (not inside the runner) so state is recorded after EACH
    # osascript batch. If a later batch fails, earlier batches' notes are
    # already persisted -- so a re-run neither recreates them (duplicate) nor
    # orphans them.
    for chunk in _chunk_by_size(
        to_create,
        size_of=lambda item: len(item[1].encode("utf-8")),
        max_count=DEFAULT_BATCH_SIZE,
        max_bytes=MAX_BATCH_PAYLOAD_BYTES,
    ):
        bodies = [html for _, html, _ in chunk]
        try:
            new_ids = runner.create_notes(folder_id, bodies)
        except Exception as exc:  # broad by design: per-batch failure isolation
            reason = f"batch create failed: {_error_reason(exc)}"
            for source, _, _ in chunk:
                report.failed.append((source.key, reason))
            continue

        imported_at = _now_iso8601()
        # Record every note that actually came back. If the runner returned
        # fewer ids than bodies (a partial batch), record those and fail the
        # remainder -- never silently drop, never double-count.
        created = min(len(new_ids), len(chunk))
        for (source, _, content_hash), note_id in zip(
            chunk[:created], new_ids[:created], strict=False
        ):
            state.record(
                source.key,
                NoteStateEntry(
                    note_id=note_id,
                    folder=folder,
                    content_hash=content_hash,
                    imported_at=imported_at,
                ),
            )
            if source.key in recreate_keys:
                report.recreated_missing += 1
            else:
                report.created += 1
        for source, _, _ in chunk[created:]:
            report.failed.append((source.key, "note not created (partial batch)"))
