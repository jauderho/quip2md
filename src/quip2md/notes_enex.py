"""Apple Notes import via the Evernote-archive (`.enex`) route.

This is the fidelity-preserving import path. `notes_import.py`'s AppleScript
`body` writer is retained for environments that cannot use this one, but it
cannot produce hyperlinks or checklists at all (see that module and
`docs/NOTES_API_NOTES.md`).

How a run works, and why:

1. Every source document is rendered to ENML; those whose rendered content
   still matches `notes_state.json` are dropped (unless `config.force`), and
   the rest are collected into **one** `.enex` file. Notes shows a single
   confirmation sheet per file regardless of how many notes it holds, so one
   file means one click for the whole corpus. When nothing is left to import
   no archive is written and Notes is never opened.
2. Opening that file makes Notes create a **fresh, numbered landing folder**
   ("Imported Notes", then "Imported Notes 1", ...). It never merges into an
   existing one, which is what makes the new folder an unambiguous handle on
   exactly the notes this run created. The run snapshots folder names before
   the import and waits for one to appear that was not there before, then
   keeps polling it until its note count stops growing: Notes fills the
   folder over many seconds, and reading it once would miss the tail.
3. Each imported note is matched back to its source by the Quip URL in its
   provenance line. Titles cannot be used: the corpus has 13 colliding titles,
   and import order is not guaranteed. Notes' `body` getter drops the `href`
   but keeps the visible text, and `enex.py` deliberately labels the
   provenance link with the URL itself so the thread id stays readable.
4. Matched notes are moved into their mirrored folder under "Quip" and
   recorded in `notes_state.json`. Anything unmatched is **left in the landing
   folder and reported** -- never guessed at, never deleted.

Deliberately absent: an update-in-place path. Rewriting a note's body is the
only scripted write Notes offers and it would destroy the links and checklists
this module exists to create, so a changed document is re-imported as a new
note and the old one is only removed on an explicit, consented request. The
previous note's id is kept in `NoteStateEntry.superseded_note_ids` and the run
ends with a warning naming how many stale copies were left behind; nothing is
ever deleted from Notes by this module.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from quip2md.config import DEFAULT_OUTPUT_DIR, Config
from quip2md.enex import ChecklistItem, NoteEnml, build_enex, markdown_to_enml
from quip2md.notes_import import (
    NOTES_ROOT_FOLDER,
    NotesError,
    NoteSource,
    NotesState,
    NoteStateEntry,
    _content_hash,
    _error_reason,
    _now_iso8601,
    notes_run_lock,
    scan_source,
)
from quip2md.walker import sanitize_component

logger = logging.getLogger("quip2md.notes_enex")

# --- Constants ---------------------------------------------------------

NOTES_STATE_FILENAME = "notes_state.json"
DEFAULT_ENEX_FILENAME = "quip2md.enex"

#: Notes names its landing folders "Imported Notes", "Imported Notes 1", ...
LANDING_FOLDER_PREFIX = "Imported Notes"

#: Prefix of the staging folder the Markdown route writes. A folder import
#: lands as `Imported Notes...` > the folder's name > the notes. Unlike an
#: archive, it does *not* get a fresh landing folder -- Notes nests it in an
#: existing "Imported Notes" when there is one -- so each run's folder carries a
#: timestamp, and that unique name is how the run finds its notes.
MARKDOWN_STAGING_DIRNAME = "quip2md-markdown"

#: Notes 4.13 (macOS 27) added Markdown import: native nested checklists, and
#: creation/modification dates taken from the file's birth time and mtime.
_MARKDOWN_IMPORT_MIN_VERSION = (4, 13)
_NOTES_INFO_PLIST = Path("/System/Applications/Notes.app/Contents/Info.plist")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(")
_MARKDOWN_TABLE_URL_RE = re.compile(r"(?m)^\|.*https?://.*\|\s*$")
_MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>#|!~])")
_MARKDOWN_ESCAPE_RE = re.compile(r"\\(.)")
#: A checklist marker with nothing after it, its text on the next, deeper line.
_EMPTY_TASK_MARKER_RE = re.compile(
    r"(?m)^([ \t]*[-*+] \[[ xX]\])[ \t]*\n[ \t]+(?![-*+] |\d+\. )(?=\S)"
)

#: How long to wait for the user to click "Import" and for Notes to finish.
IMPORT_POLL_INTERVAL_SECONDS = 3.0
IMPORT_TIMEOUT_SECONDS = 900.0

#: Matches the provenance URL in a note body read back from Notes.
_QUIP_URL_RE = re.compile(r"https?://(?:[\w.-]*\.)?quip\.com/[\w-]+", re.IGNORECASE)

#: Splits a note body into paragraph-ish blocks, so the provenance line can be
#: told apart from any other link in the document.
_BLOCK_BOUNDARY_RE = re.compile(r"</?(?:div|p|li|br|h[1-6])[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_PROVENANCE_PREFIX = "Source:"

_FIRST_INVOCATION_TIMEOUT_SECONDS = 120.0
_SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS = 60.0

#: Notes are read back in batches of this many, each with its own timeout.
#: One call for a whole corpus overran the timeout on a real 490-note run.
_NOTE_READ_BATCH = 25
_NOTE_READ_TIMEOUT_SECONDS = 300.0

#: How much of each body to fetch. The provenance line is the first block;
#: fetching whole bodies moves megabytes through osascript for no gain.
_BODY_HEAD_CHARS = 1200

_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"

#: Rendering is CPU-bound and per-document independent, so it is spread over a
#: process pool. Below this many documents the pool costs more than it saves
#: (spawning an interpreter per worker dominates), so the sequential path runs.
_PARALLEL_MIN_SOURCES = 50

#: Chunks per worker. Documents vary in size by two orders of magnitude, so one
#: chunk per worker leaves the pool waiting on whichever chunk drew the largest
#: files; four gives the scheduler enough slack to even that out without paying
#: a round-trip per document.
_CHUNKS_PER_WORKER = 4

#: Measured ceiling on this project's corpus: past six workers the run is bound
#: by the parent's pickling of the rendered notes, not by rendering, and an
#: asymmetric CPU (the 4P+4E Apple M2 this was measured on) starts scheduling
#: chunks onto efficiency cores. It is a cap, not a target -- a machine with
#: fewer cores uses fewer, and `--workers` overrides it on a box (a homogeneous
#: x86-64 one, say) that can usefully run more.
_MAX_DEFAULT_WORKERS = 6


# --- Data ---------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ImportedNote:
    """One note read back out of the landing folder."""

    note_id: str
    name: str
    body: str


@dataclass(slots=True)
class EnexImportReport:
    """Counts and outcomes for one `run_enex_import()` call."""

    documents: int = 0
    enex_path: str = ""
    enex_bytes: int = 0
    markdown_path: str = ""
    markdown_notes: int = 0
    checklist_items: int = 0
    checklist_checked: int = 0
    links: int = 0
    images: int = 0
    docs_needing_indent: int = 0
    indent_levels: int = 0
    imported: int = 0
    moved: int = 0
    skipped_unchanged: int = 0
    superseded: int = 0
    landing_folder: str = ""
    unmatched: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: int = 0
    elapsed_seconds: float = 0.0

    #: `(note_id, label, plan)` for every moved note that needs indenting.
    #: Deliberately absent from `as_dict()`: it is a hand-off to the
    #: indentation pass, not part of the run report.
    indent_targets: list[tuple[str, str, tuple[ChecklistItem, ...]]] = field(default_factory=list)

    def merge(self, other: EnexImportReport) -> None:
        """Fold a chunk's report into this one, in chunk order.

        Every counter is additive and every list is extended, so merging the
        chunks of a parallel render back in order yields exactly the report a
        single sequential pass over the same sources would have produced.

        `enex_path`, `landing_folder` and `elapsed_seconds` are deliberately
        untouched: they describe the run as a whole, not a share of it, and a
        rendering chunk never sets them.
        """
        self.documents += other.documents
        self.enex_bytes += other.enex_bytes
        self.checklist_items += other.checklist_items
        self.checklist_checked += other.checklist_checked
        self.links += other.links
        self.images += other.images
        self.docs_needing_indent += other.docs_needing_indent
        self.indent_levels += other.indent_levels
        self.imported += other.imported
        self.moved += other.moved
        self.skipped_unchanged += other.skipped_unchanged
        self.superseded += other.superseded
        self.warnings += other.warnings
        self.unmatched.extend(other.unmatched)
        self.failed.extend(other.failed)
        self.indent_targets.extend(other.indent_targets)

    def as_dict(self) -> dict[str, object]:
        return {
            "documents": self.documents,
            "enex_path": self.enex_path,
            "enex_bytes": self.enex_bytes,
            "markdown_path": self.markdown_path,
            "markdown_notes": self.markdown_notes,
            "checklist_items": self.checklist_items,
            "checklist_checked": self.checklist_checked,
            "links": self.links,
            "images": self.images,
            "docs_needing_indent": self.docs_needing_indent,
            "indent_levels": self.indent_levels,
            "imported": self.imported,
            "moved": self.moved,
            "skipped_unchanged": self.skipped_unchanged,
            "superseded": self.superseded,
            "landing_folder": self.landing_folder,
            "unmatched": self.unmatched,
            "failed": [{"key": key, "reason": reason} for key, reason in self.failed],
            "warnings": self.warnings,
            "elapsed_seconds": self.elapsed_seconds,
        }


# --- Notes automation ---------------------------------------------------


class EnexNotesRunnerProtocol(Protocol):
    """The Notes automation `run_enex_import()` needs, behind a seam for tests."""

    def resolve_account(self, *, local: bool) -> str: ...

    def folder_names(self, account: str) -> frozenset[str]: ...

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str: ...

    def folder_id_by_name(self, account: str, name: str) -> str: ...

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]: ...

    def child_folder_id(self, parent_id: str, name: str) -> str: ...

    def move_note(self, note_id: str, folder_id: str) -> None: ...

    def open_enex(self, path: Path) -> None: ...


_AS_LIST_ACCOUNTS = """
on run argv
    tell application "Notes"
        return name of default account
    end tell
end run
"""

_AS_FOLDER_NAMES = """
on run argv
    set acc to item 1 of argv
    tell application "Notes"
        if acc is "" then
            set theAccount to default account
        else
            set theAccount to account acc
        end if
        set out to ""
        repeat with f in folders of theAccount
            set out to out & (name of f) & (ASCII character 30)
        end repeat
        return out
    end tell
end run
"""

# `folder "X" of account` was observed to resolve by name across *every*
# folder in the account, not just its top level, so a nested "Quip" could be
# returned in place of the real one. Every by-name lookup therefore checks the
# candidate's container. The check is wrapped because a folder whose container
# is not scriptable raises rather than answering "no".
_AS_IS_TOP_LEVEL = """
on isTopLevel(theFolder, theAccount)
    tell application "Notes"
        try
            return (id of container of theFolder) is (id of theAccount)
        on error
            return false
        end try
    end tell
end isTopLevel
"""

_AS_FOLDER_ID_BY_NAME = (
    """
on run argv
    set acc to item 1 of argv
    set wanted to item 2 of argv
    tell application "Notes"
        if acc is "" then
            set theAccount to default account
        else
            set theAccount to account acc
        end if
        repeat with f in folders of theAccount
            if name of f is wanted and my isTopLevel(f, theAccount) then return id of f
        end repeat
        return ""
    end tell
end run
"""
    + _AS_IS_TOP_LEVEL
)

_AS_CHILD_FOLDER_ID = """
on run argv
    set parentId to item 1 of argv
    set wanted to item 2 of argv
    tell application "Notes"
        repeat with f in folders of folder id parentId
            if name of f is wanted then return id of f
        end repeat
        return ""
    end tell
end run
"""

# Ids are snapshotted before any read so the collection is never mutated while
# it is being walked (Notes errors out if it is).
_AS_NOTE_IDS_IN_FOLDER = """
on run argv
    set folderId to item 1 of argv
    tell application "Notes"
        set out to ""
        repeat with n in notes of folder id folderId
            set out to out & (id of n as string) & (ASCII character 30)
        end repeat
        return out
    end tell
end run
"""

# Only the head of each body is fetched. Matching needs the provenance line,
# which `enex.py` puts in the first block, and a full-corpus read of 490 bodies
# is megabytes of osascript stdout -- which is what made a real 492-note run
# time out at the read-back step.
_AS_NOTE_HEADS = """
on run argv
    set headLen to (item 1 of argv) as integer
    tell application "Notes"
        set out to ""
        repeat with idx from 2 to (count of argv)
            set i to item idx of argv
            set n to note id i
            set b to body of n
            if (count of b) > headLen then set b to text 1 thru headLen of b
            set out to out & i & (ASCII character 31) & (name of n) & ¬
                (ASCII character 31) & b & (ASCII character 30)
        end repeat
        return out
    end tell
end run
"""

_AS_MOVE_NOTE = """
on run argv
    set noteId to item 1 of argv
    set folderId to item 2 of argv
    tell application "Notes"
        move note id noteId to folder id folderId
    end tell
end run
"""

_AS_GET_OR_CREATE_FOLDER = (
    """
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
                if isNested is "1" or my isTopLevel(f, theAccount) then
                    return id of f
                end if
            end if
        end repeat
        set newFolder to make new folder at targetContainer with properties {name:folderName}
        return id of newFolder
    end tell
end run
"""
    + _AS_IS_TOP_LEVEL
)


class EnexNotesRunner:
    """Real Notes automation for the `.enex` route.

    Every call crosses the process boundary through `argv`, never by
    interpolating content into AppleScript source.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise NotesError(
                "Apple Notes import requires macOS (osascript is not available on "
                f"this platform: {sys.platform!r})"
            )
        self._first_call_done = False
        self._folder_id_cache: dict[tuple[str, tuple[str, ...]], str] = {}

    def _run(self, script: str, argv: Sequence[str], *, timeout: float | None = None) -> str:
        if timeout is None:
            timeout = (
                _SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS
                if self._first_call_done
                else _FIRST_INVOCATION_TIMEOUT_SECONDS
            )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script, "--", *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
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
        if local:
            raise NotesError(
                "the .enex import route cannot target a specific account: Notes "
                "always imports into the default account's landing folder"
            )
        return self._run(_AS_LIST_ACCOUNTS, []).strip()

    def folder_names(self, account: str) -> frozenset[str]:
        stdout = self._run(_AS_FOLDER_NAMES, [account])
        return frozenset(part for part in stdout.split(_RECORD_SEPARATOR) if part.strip())

    def folder_id_by_name(self, account: str, name: str) -> str:
        return self._run(_AS_FOLDER_ID_BY_NAME, [account, name]).strip()

    def child_folder_id(self, parent_id: str, name: str) -> str:
        return self._run(_AS_CHILD_FOLDER_ID, [parent_id, name]).strip()

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

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        """Every note in the folder, read back in batches.

        Ids first, then their heads in `_NOTE_READ_BATCH`-sized calls: a single
        call covering a whole corpus overran the osascript timeout on a real
        490-note import, which left every note unfiled.
        """
        ids = [
            part.strip()
            for part in self._run(_AS_NOTE_IDS_IN_FOLDER, [folder_id]).split(_RECORD_SEPARATOR)
            if part.strip()
        ]
        notes: list[ImportedNote] = []
        for start in range(0, len(ids), _NOTE_READ_BATCH):
            batch = ids[start : start + _NOTE_READ_BATCH]
            stdout = self._run(
                _AS_NOTE_HEADS,
                [str(_BODY_HEAD_CHARS), *batch],
                timeout=_NOTE_READ_TIMEOUT_SECONDS,
            )
            for record in stdout.split(_RECORD_SEPARATOR):
                if not record.strip():
                    continue
                parts = record.split(_FIELD_SEPARATOR)
                if len(parts) < 3:
                    continue
                notes.append(
                    ImportedNote(
                        note_id=parts[0].strip(),
                        name=parts[1],
                        body=_FIELD_SEPARATOR.join(parts[2:]),
                    )
                )
        return notes

    def move_note(self, note_id: str, folder_id: str) -> None:
        self._run(_AS_MOVE_NOTE, [note_id, folder_id])

    def open_enex(self, path: Path) -> None:
        """Hand a `.enex` file, or a folder of Markdown files, to Notes' importer."""
        proc = subprocess.run(
            ["open", "-a", "Notes", str(path)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise NotesError(f"could not hand {path.name} to Notes", stderr=proc.stderr.strip())


# --- Markdown route ------------------------------------------------------


def markdown_import_available() -> bool:
    """True when this Mac's Notes can import Markdown (Notes 4.13 and later)."""
    try:
        with _NOTES_INFO_PLIST.open("rb") as handle:
            version = str(plistlib.load(handle).get("CFBundleShortVersionString", ""))
    except OSError, plistlib.InvalidFileException:
        return False
    numbers = tuple(int(part) for part in re.findall(r"\d+", version)[:2])
    return numbers >= _MARKDOWN_IMPORT_MIN_VERSION


def routes_through_markdown(source: NoteSource) -> bool:
    """Whether the Markdown route can carry this document.

    Notes' Markdown importer never embeds an image: relative and absolute
    paths, `file://` and `data:` URIs all arrive as plain links (tested on
    Notes 4.13). A URL inside a table row is worse: the cell arrives empty and
    every cell after it is lost (seen in a real document). Either keeps the
    document on the archive route.
    """
    body = source.body_markdown
    return not (_MARKDOWN_IMAGE_RE.search(body) or _MARKDOWN_TABLE_URL_RE.search(body))


def markdown_note_text(source: NoteSource) -> str:
    """The Markdown file for one note: title, provenance line, then the body.

    The layout matches the archive route's note, so both routes produce the
    same first lines and `_extract_quip_url` matches either. Notes takes the
    note's title from the leading heading, and links the bare URL itself.
    """
    title = source.title.strip() or "Untitled"
    body = _drop_leading_title_heading(source.body_markdown, title).strip()
    # Notes reads an empty marker line as a bullet showing a literal "[ ]", so
    # the item's first line of text is pulled up onto the marker.
    body = _EMPTY_TASK_MARKER_RE.sub(r"\1 ", body)
    lines = [f"# {_MARKDOWN_SPECIAL_RE.sub(r'\\\1', title)}", ""]
    if source.quip_url:
        lines += [f"{_PROVENANCE_PREFIX} {source.quip_url}", ""]
    return "\n".join(lines) + "\n" + body + "\n"


def _drop_leading_title_heading(markdown: str, title: str) -> str:
    """Remove a first `# heading` that only repeats the title, as `enex.py` does."""
    head, _, rest = markdown.lstrip("\n").partition("\n")
    if head.startswith("# ") and _MARKDOWN_ESCAPE_RE.sub(r"\1", head[2:]).strip() == title:
        return rest
    return markdown


def _write_markdown_folder(folder: Path, sources: Sequence[NoteSource]) -> None:
    """Write one dated Markdown file per source into a fresh `folder`."""
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    used: set[str] = set()
    for source in sources:
        stem = sanitize_component(source.title.strip() or "Untitled")
        name, counter = stem, 2
        while name.casefold() in used:
            name, counter = f"{stem} ({counter})", counter + 1
        used.add(name.casefold())
        path = folder / f"{name}.md"
        path.write_text(markdown_note_text(source), encoding="utf-8")
        _set_file_dates(path, created=source.created, updated=source.updated)


def _set_file_dates(path: Path, *, created: str | None, updated: str | None) -> None:
    """Give `path` the document's dates; Notes reads them on import.

    Notes takes a note's creation date from the file's birth time and its
    modification date from the mtime. There is no call that sets a birth time,
    but lowering the mtime below it lowers the birth time too (APFS), so the
    file is stamped with the creation date first and then the modification
    date. A creation date after the modification date ends up as the latter.
    """
    stamps = [stamp for stamp in (_epoch(created), _epoch(updated)) if stamp is not None]
    if not stamps:
        return
    born = stamps[0]
    modified = stamps[-1]
    os.utime(path, (born, born))
    os.utime(path, (modified, modified))


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --- Rendering ----------------------------------------------------------


def render_sources(
    sources: Sequence[NoteSource], report: EnexImportReport
) -> list[tuple[NoteSource, NoteEnml]]:
    """Render every source to ENML, accumulating report counters.

    Each note is returned paired with the source it came from: titles collide
    in the corpus, so a note can only be tied back to its source by identity,
    never by looking it up by name.
    """
    notes: list[tuple[NoteSource, NoteEnml]] = []
    for source in sources:
        try:
            note = markdown_to_enml(
                title=source.title,
                quip_url=source.quip_url,
                markdown_text=source.body_markdown,
                md_dir=source.md_path.parent,
                created=source.created,
                updated=source.updated,
            )
        except Exception as exc:  # broad by design: per-source failure isolation
            report.failed.append((source.key, f"conversion failed: {_error_reason(exc)}"))
            continue
        notes.append((source, note))
        report.warnings += len(note.warnings)
        report.checklist_items += len(note.checklist)
        report.checklist_checked += sum(1 for item in note.checklist if item.checked)
        report.images += len(note.resources)
        report.links += note.enml.count("<a href=")
        if note.needs_indent_pass:
            report.docs_needing_indent += 1
        report.indent_levels += sum(item.depth for item in note.checklist)
    return notes


def default_render_workers() -> int:
    """How many processes `render_sources_parallel()` uses when not told."""
    return min(os.cpu_count() or 1, _MAX_DEFAULT_WORKERS)


def _render_chunk(
    chunk: Sequence[NoteSource],
) -> tuple[list[tuple[NoteSource, NoteEnml]], EnexImportReport]:
    """Render one contiguous chunk in a worker process.

    Module-level (not a closure) because the `spawn` start method pickles the
    callable by qualified name. It returns its own report rather than mutating
    a shared one: the parent merges the chunk reports back in chunk order.
    """
    report = EnexImportReport()
    return render_sources(chunk, report), report


def render_sources_parallel(
    sources: Sequence[NoteSource], report: EnexImportReport, *, workers: int | None = None
) -> list[tuple[NoteSource, NoteEnml]]:
    """Render every source to ENML across a process pool, order preserved.

    Identical in output to `render_sources()`: the chunks are contiguous, the
    results are concatenated in chunk order and the chunk reports are merged in
    the same order, so both the note list and the report are byte-for-byte what
    a sequential run produces. `workers=1` (or too few sources to be worth a
    pool) takes the sequential path directly.
    """
    count = default_render_workers() if workers is None else workers
    if count <= 1 or len(sources) < _PARALLEL_MIN_SOURCES:
        return render_sources(sources, report)

    chunks = _contiguous_chunks(sources, count * _CHUNKS_PER_WORKER)
    # `spawn`, explicitly: it is macOS' default and the only safe start method
    # here, since `fork` would duplicate an interpreter that already has lxml
    # (and its own threads and global state) loaded into the child.
    context = multiprocessing.get_context("spawn")
    notes: list[tuple[NoteSource, NoteEnml]] = []
    try:
        with ProcessPoolExecutor(max_workers=count, mp_context=context) as executor:
            for chunk_notes, chunk_report in executor.map(_render_chunk, chunks):
                notes.extend(chunk_notes)
                report.merge(chunk_report)
    except Exception as exc:  # a whole chunk died: not isolable, so fail loudly
        raise NotesError(
            f"parallel rendering failed across {count} worker(s): {_error_reason(exc)}. "
            "Re-run with --workers 1 to render in this process."
        ) from exc
    return notes


def _contiguous_chunks(sources: Sequence[NoteSource], count: int) -> list[list[NoteSource]]:
    """Split `sources` into at most `count` contiguous, near-equal chunks."""
    size, remainder = divmod(len(sources), count)
    chunks: list[list[NoteSource]] = []
    start = 0
    for index in range(count):
        end = start + size + (1 if index < remainder else 0)
        if end > start:
            chunks.append(list(sources[start:end]))
        start = end
    return chunks


# --- Orchestration ------------------------------------------------------


def run_enex_import(
    runner: EnexNotesRunnerProtocol | None,
    config: Config,
    *,
    source_dir: Path = DEFAULT_OUTPUT_DIR,
    enex_path: Path | None = None,
    only: Sequence[str] | None = None,
    confirm: bool = True,
    timeout_seconds: float = IMPORT_TIMEOUT_SECONDS,
    workers: int | None = None,
    adopt_landing: str | None = None,
    markdown: bool = False,
) -> EnexImportReport:
    """Render `source_dir` to a single `.enex` and import it into Notes.

    With `markdown`, every document without an image is imported through
    Notes' Markdown importer instead (see `routes_through_markdown`): its
    checklists arrive natively nested and both of its dates come from the
    staged file, so no indentation pass is needed for it. The rest still goes
    through the archive. Each route is one import, so up to two confirmations.

    In `config.dry_run` the `.enex` is still written (it is the artefact worth
    inspecting) but Notes is never contacted and no state is written -- so
    `runner` may be `None` in that mode, which is also what stops a dry run
    from needing a working Notes automation permission at all.

    `confirm` exists so the caller can suppress the "click Import" prompt in a
    non-interactive context; the sheet still has to be clicked by a human
    either way, which is why the wait is generous and its timeout explicit.

    A source whose rendered content still matches its `notes_state.json` entry
    is skipped (counted in `report.skipped_unchanged`) unless `config.force`;
    with every source skipped, no archive is written and Notes is never
    opened. A dry run reads the same state file (never writing it) and skips
    the same documents, so what it reports is what a real run would do rather
    than what a first-ever run would.

    `workers` bounds the rendering process pool; `1` renders in this process.

    `adopt_landing` resumes a run whose notes reached Notes but were never
    filed -- an import that died between the confirmation click and the
    read-back. Nothing is imported; the named landing folder's notes are
    matched and filed exactly as a fresh run would have done, which is what
    keeps the retry from creating a second copy of every document.
    """
    started = time.monotonic()
    report = EnexImportReport()

    sources = scan_source(source_dir)
    if only is not None:
        wanted = frozenset(only)
        sources = [source for source in sources if source.key in wanted]
    report.documents = len(sources)

    rendered = render_sources_parallel(sources, report, workers=workers)
    target = enex_path or (config.state_path.parent / DEFAULT_ENEX_FILENAME)

    state = NotesState(config.state_path.parent / NOTES_STATE_FILENAME)
    state.load()

    pending = _select_pending(rendered, state, report, force=config.force, markdown=markdown)
    report.documents = len(pending)
    if not pending:
        logger.info(
            "Nothing to import: all %d document(s) are unchanged since the last run.",
            report.skipped_unchanged,
        )
        report.elapsed_seconds = time.monotonic() - started
        return report

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    markdown_folder = config.state_path.parent / f"{MARKDOWN_STAGING_DIRNAME}-{stamp}"
    by_markdown = [item for item in pending if markdown and routes_through_markdown(item[0])]
    by_archive = [item for item in pending if not (markdown and routes_through_markdown(item[0]))]
    if adopt_landing is None:
        if by_archive:
            _write_enex(target, by_archive, report)
        if by_markdown:
            for stale in config.state_path.parent.glob(f"{MARKDOWN_STAGING_DIRNAME}*"):
                shutil.rmtree(stale)
            _write_markdown_folder(markdown_folder, [source for source, _note in by_markdown])
            report.markdown_path = str(markdown_folder)
            report.markdown_notes = len(by_markdown)

    if config.dry_run:
        report.elapsed_seconds = time.monotonic() - started
        return report

    if runner is None:
        raise NotesError("a Notes runner is required for a real (non-dry-run) import")

    with notes_run_lock(config.state_path.parent):
        batches: list[tuple[Path, str | None, list[tuple[NoteSource, NoteEnml]]]] = []
        if adopt_landing is not None:
            batches.append((target, None, pending))
        else:
            if by_archive:
                batches.append((target, None, by_archive))
            if by_markdown:
                batches.append((markdown_folder, markdown_folder.name, by_markdown))
        return _import_into_notes(
            runner,
            report,
            state,
            batches,
            markdown=markdown,
            started=started,
            confirm=confirm,
            timeout_seconds=timeout_seconds,
            adopt_landing=adopt_landing,
        )


def _import_into_notes(
    runner: EnexNotesRunnerProtocol,
    report: EnexImportReport,
    state: NotesState,
    batches: Sequence[tuple[Path, str | None, Sequence[tuple[NoteSource, NoteEnml]]]],
    *,
    markdown: bool,
    started: float,
    confirm: bool,
    timeout_seconds: float,
    adopt_landing: str | None,
) -> EnexImportReport:
    """Hand each batch to Notes and file what comes back. Holds the run lock.

    A batch is `(path, child, pending)`: the archive or Markdown folder to
    open, the folder Notes nests the notes in inside its landing folder
    (`None` for an archive, whose notes land directly in it), and the sources
    the batch carries. Batches run one after the other, each with its own
    confirmation, and share one deadline.
    """
    account = runner.resolve_account(local=False)
    deadline = time.monotonic() + timeout_seconds
    landings: list[str] = []
    try:
        for path, child, pending in batches:
            landings.append(
                _import_batch(
                    runner,
                    account,
                    report,
                    state,
                    path,
                    child,
                    pending,
                    markdown=markdown,
                    deadline=deadline,
                    confirm=confirm,
                    adopt_landing=adopt_landing,
                )
            )
    finally:
        state.flush()
        report.landing_folder = ", ".join(landings)
        report.elapsed_seconds = time.monotonic() - started

    if report.superseded:
        logger.warning(
            "%d note(s) were re-imported as new notes. Their previous copies are "
            "still in Notes (ids recorded under 'superseded_note_ids' in "
            "notes_state.json) and must be deleted by hand.",
            report.superseded,
        )
    return report


def _import_batch(
    runner: EnexNotesRunnerProtocol,
    account: str,
    report: EnexImportReport,
    state: NotesState,
    target: Path,
    child: str | None,
    pending: Sequence[tuple[NoteSource, NoteEnml]],
    *,
    markdown: bool,
    deadline: float,
    confirm: bool,
    adopt_landing: str | None,
) -> str:
    """Import one archive or Markdown folder and file its notes; returns the landing name."""
    if adopt_landing is not None:
        # "Imported Notes/quip2md-markdown-..." names a Markdown run's folder.
        landing_name = adopt_landing
        top, *nested = adopt_landing.split("/")
        landing_id = runner.folder_id_by_name(account, top)
        for name in nested:
            landing_id = runner.child_folder_id(landing_id, name) if landing_id else ""
        if not landing_id:
            raise NotesError(f"no folder named {adopt_landing!r} in the {account} account")
        logger.warning("Adopting the notes already in %r; nothing will be imported.", adopt_landing)
    else:
        before = _landing_snapshot(runner, account) if child is None else {}
        if confirm:
            logger.warning(
                "Notes will now ask you to confirm the import of %s. Click 'Import' "
                "in the dialog; this run waits up to %.0f minutes.",
                target.name,
                max(deadline - time.monotonic(), 0) / 60,
            )
        runner.open_enex(target)
        if child is None:
            landing_name, landing_id = _await_landing_folder(runner, account, before, deadline)
        else:
            landing_name, landing_id = _await_child_folder(runner, account, child, deadline)

    if adopt_landing is not None:
        # Adopted notes were already there when the run started, so there is
        # nothing to wait for. Polling would re-read every body twice over.
        imported = runner.notes_in_folder(landing_id)
    else:
        imported = _await_landing_notes(
            runner,
            landing_name,
            landing_id,
            len(pending),
            deadline,
            already_there=before.get(landing_name, frozenset()),
        )
    report.imported += len(imported)
    _file_imported_notes(runner, account, imported, pending, state, report, markdown=markdown)
    return landing_name


def _write_enex(
    target: Path, rendered: Sequence[tuple[NoteSource, NoteEnml]], report: EnexImportReport
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    document = build_enex([note for _source, note in rendered])
    target.write_text(document, encoding="utf-8")
    report.enex_path = str(target)
    report.enex_bytes = len(document.encode("utf-8"))


def _select_pending(
    rendered: Sequence[tuple[NoteSource, NoteEnml]],
    state: NotesState,
    report: EnexImportReport,
    *,
    force: bool,
    markdown: bool = False,
) -> list[tuple[NoteSource, NoteEnml]]:
    """Drop sources already in Notes with the content they would be given now.

    The hash is over the *rendered* note, so the source has to be rendered
    before it can be skipped -- which is cheap, and the only way to notice a
    change that comes from the converter rather than from the file.

    A skipped note still contributes an indent target. Notes flattens nested
    checklists on import and the repair is a separate, permission-gated pass,
    so it has to stay reachable after the import that created the note has
    already been recorded -- otherwise the only way to indent an existing note
    would be to re-import it, which would duplicate it.

    A source with no `quip_url` is refused before it reaches the archive. This
    route matches an imported note back to its source by the URL in the note's
    provenance line, so a url-less source (a hand-written `path:`-keyed file, or
    a corrupted export whose `quip_url` was stripped while its `quip_id`
    survived) can never be matched, filed, or recorded in `notes_state.json`.
    Letting it through would import an orphan note on every operator-confirmed
    run -- recorded in no state file, so the skip-on-unchanged path can never
    engage for it -- so it fails loudly here and never reaches the archive.
    Unlike the unchanged-skip, this guard is structural, not content-based, so
    `force` does not override it.
    """
    pending: list[tuple[NoteSource, NoteEnml]] = []
    for source, note in rendered:
        if not source.quip_url:
            report.failed.append(
                (
                    source.key,
                    "no quip_url: the .enex import route matches notes back by "
                    "their provenance URL and cannot file a source without one; "
                    "import it with --writer applescript",
                )
            )
            continue
        entry = state.get(source.key)
        unchanged = entry is not None and entry.content_hash == _content_hash(
            source.title, note.enml
        )
        if unchanged and not force:
            report.skipped_unchanged += 1
            if entry is not None and _needs_indent_pass(source, note, markdown=markdown):
                report.indent_targets.append((entry.note_id, source.title, note.checklist))
            continue
        pending.append((source, note))
    return pending


def _landing_snapshot(runner: EnexNotesRunnerProtocol, account: str) -> dict[str, frozenset[str]]:
    """Ids of the notes already in each landing folder, before an archive opens."""
    return {
        name: frozenset(
            note.note_id for note in runner.notes_in_folder(runner.folder_id_by_name(account, name))
        )
        for name in runner.folder_names(account)
        if name.startswith(LANDING_FOLDER_PREFIX)
    }


def _await_landing_folder(
    runner: EnexNotesRunnerProtocol,
    account: str,
    before: dict[str, frozenset[str]],
    deadline: float,
) -> tuple[str, str]:
    """Block until an archive's notes start arriving; return their folder.

    Older Notes put each import in a fresh, numbered "Imported Notes N"
    folder. Notes 4.13 adds to an existing "Imported Notes" folder instead, so
    a folder that was already there counts once it holds a note it did not.
    """
    while time.monotonic() < deadline:
        names = runner.folder_names(account)
        new = {name for name in names if name.startswith(LANDING_FOLDER_PREFIX)} - set(before)
        if new:
            # Newest last: "Imported Notes 9" sorts after "Imported Notes 1".
            chosen = sorted(new, key=lambda name: (len(name), name))[-1]
            return chosen, runner.folder_id_by_name(account, chosen)
        for name, ids in before.items():
            if name not in names:
                continue
            folder_id = runner.folder_id_by_name(account, name)
            if any(note.note_id not in ids for note in runner.notes_in_folder(folder_id)):
                return name, folder_id
        time.sleep(IMPORT_POLL_INTERVAL_SECONDS)
    raise NotesError(
        "timed out waiting for Notes to create an import folder -- was the 'Import' button clicked?"
    )


def _await_child_folder(
    runner: EnexNotesRunnerProtocol, account: str, child: str, deadline: float
) -> tuple[str, str]:
    """Block until `child` appears inside any landing folder.

    A folder import nests into an existing "Imported Notes" folder rather than
    creating a new one, so there is no new top-level folder to wait for. The
    child's name is unique to this run, which makes finding it unambiguous.
    """
    while time.monotonic() < deadline:
        for name in sorted(runner.folder_names(account)):
            if not name.startswith(LANDING_FOLDER_PREFIX):
                continue
            child_id = runner.child_folder_id(runner.folder_id_by_name(account, name), child)
            if child_id:
                return f"{name}/{child}", child_id
        time.sleep(IMPORT_POLL_INTERVAL_SECONDS)
    raise NotesError(
        f"timed out waiting for Notes to create the folder {child!r} -- was the "
        "'Import' button clicked?"
    )


def _needs_indent_pass(source: NoteSource, note: NoteEnml, *, markdown: bool) -> bool:
    """Only an archive import flattens checklists; the Markdown route keeps them."""
    return note.needs_indent_pass and not (markdown and routes_through_markdown(source))


def _await_landing_notes(
    runner: EnexNotesRunnerProtocol,
    landing_name: str,
    landing_id: str,
    expected: int,
    deadline: float,
    *,
    already_there: frozenset[str] = frozenset(),
) -> list[ImportedNote]:
    """Poll the landing folder until it stops filling up.

    `already_there` are ids the folder held before this import; they belong to
    someone else and are neither waited for nor returned.

    The folder appears as soon as the import starts, not when it finishes: on
    a large archive Notes keeps adding notes to it for minutes. Reading it once
    would silently leave the tail of the corpus unfiled, so this waits for the
    count to hold steady across two polls *and* reach the archive's own note
    count before returning.
    """

    def arrived() -> list[ImportedNote]:
        return [n for n in runner.notes_in_folder(landing_id) if n.note_id not in already_there]

    notes = arrived()
    previous = -1
    while len(notes) != previous or len(notes) < expected:
        if time.monotonic() >= deadline:
            logger.warning(
                "Notes stopped short: folder %r holds %d of the %d note(s) in the "
                "archive after the wait expired. Proceeding with those; re-run to "
                "import the remaining %d.",
                landing_name,
                len(notes),
                expected,
                max(expected - len(notes), 0),
            )
            break
        previous = len(notes)
        time.sleep(IMPORT_POLL_INTERVAL_SECONDS)
        notes = arrived()
    return notes


def _file_imported_notes(
    runner: EnexNotesRunnerProtocol,
    account: str,
    imported: Sequence[ImportedNote],
    pending: Sequence[tuple[NoteSource, NoteEnml]],
    state: NotesState,
    report: EnexImportReport,
    *,
    markdown: bool = False,
) -> None:
    by_url = {source.quip_url: (source, note) for source, note in pending if source.quip_url}
    imported_at = _now_iso8601()

    for note in imported:
        url = _extract_quip_url(note.body)
        match = by_url.get(url) if url else None
        if match is None:
            report.unmatched.append(note.name)
            continue
        source, enml = match

        folder_path = source.folder_path
        try:
            folder_id = runner.get_or_create_folder(account, folder_path)
            runner.move_note(note.note_id, folder_id)
        except Exception as exc:  # broad by design: per-note failure isolation
            report.failed.append((source.key, f"move failed: {_error_reason(exc)}"))
            continue

        if _needs_indent_pass(source, enml, markdown=markdown):
            report.indent_targets.append((note.note_id, source.title, enml.checklist))
        previous = state.get(source.key)
        superseded: tuple[str, ...] = ()
        if previous is not None:
            # The old note is kept, not deleted: this module never destroys
            # anything in Notes. The run warns about the leftovers at the end.
            superseded = (*previous.superseded_note_ids, previous.note_id)
            report.superseded += 1
        state.record(
            source.key,
            NoteStateEntry(
                note_id=note.note_id,
                folder="/".join(folder_path),
                content_hash=_content_hash(source.title, enml.enml),
                imported_at=imported_at,
                superseded_note_ids=superseded,
            ),
        )
        report.moved += 1


def _extract_quip_url(body: str) -> str | None:
    """Pull the provenance URL out of a note body read back from Notes.

    Only the provenance line counts -- the first paragraph reading
    "Source: <url>". A document may link to another exported document, and
    matching anywhere in the body would file the note under whichever thread
    that link happens to name.

    The `body` getter strips the `href`, so this reads the visible link text --
    which `enex.py` sets to the URL precisely so this works.
    """
    for block in _BLOCK_BOUNDARY_RE.split(body):
        text = _TAG_RE.sub("", block).strip()
        if not text.startswith(_PROVENANCE_PREFIX):
            continue
        match = _QUIP_URL_RE.search(text)
        return match.group(0) if match else None
    return None


__all__ = [
    "NOTES_ROOT_FOLDER",
    "EnexImportReport",
    "EnexNotesRunner",
    "EnexNotesRunnerProtocol",
    "ImportedNote",
    "default_render_workers",
    "render_sources",
    "render_sources_parallel",
    "run_enex_import",
]
