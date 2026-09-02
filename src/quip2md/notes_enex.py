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
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
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
    scan_source,
)

logger = logging.getLogger("quip2md.notes_enex")

# --- Constants ---------------------------------------------------------

NOTES_STATE_FILENAME = "notes_state.json"
DEFAULT_ENEX_FILENAME = "quip2md.enex"

#: Notes names its landing folders "Imported Notes", "Imported Notes 1", ...
LANDING_FOLDER_PREFIX = "Imported Notes"

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
        proc = subprocess.run(
            ["open", "-a", "Notes", str(path)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise NotesError("could not hand the .enex file to Notes", stderr=proc.stderr.strip())


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
) -> EnexImportReport:
    """Render `source_dir` to a single `.enex` and import it into Notes.

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

    pending = _select_pending(rendered, state, report, force=config.force)
    report.documents = len(pending)
    if not pending:
        logger.info(
            "Nothing to import: all %d document(s) are unchanged since the last run.",
            report.skipped_unchanged,
        )
        report.elapsed_seconds = time.monotonic() - started
        return report

    if adopt_landing is None:
        _write_enex(target, pending, report)

    if config.dry_run:
        report.elapsed_seconds = time.monotonic() - started
        return report

    if runner is None:
        raise NotesError("a Notes runner is required for a real (non-dry-run) import")

    account = runner.resolve_account(local=False)
    deadline = time.monotonic() + timeout_seconds

    if adopt_landing is not None:
        landing_name = adopt_landing
        landing_id = runner.folder_id_by_name(account, adopt_landing)
        if not landing_id:
            raise NotesError(f"no folder named {adopt_landing!r} in the {account} account")
        logger.warning("Adopting the notes already in %r; nothing will be imported.", adopt_landing)
    else:
        before = runner.folder_names(account)
        if confirm:
            logger.warning(
                "Notes will now ask you to confirm the import. Click 'Import' in the "
                "dialog; this run waits up to %.0f minutes.",
                timeout_seconds / 60,
            )
        runner.open_enex(target)
        landing_name, landing_id = _await_landing_folder(runner, account, before, deadline)

    report.landing_folder = landing_name
    if adopt_landing is not None:
        # Adopted notes were already there when the run started, so there is
        # nothing to wait for. Polling would re-read every body twice over.
        imported = runner.notes_in_folder(landing_id)
    else:
        imported = _await_landing_notes(runner, landing_name, landing_id, len(pending), deadline)
    report.imported = len(imported)

    try:
        _file_imported_notes(runner, account, imported, pending, state, report)
    finally:
        state.flush()
        report.elapsed_seconds = time.monotonic() - started

    if report.superseded:
        logger.warning(
            "%d note(s) were re-imported as new notes. Their previous copies are "
            "still in Notes (ids recorded under 'superseded_note_ids' in "
            "notes_state.json) and must be deleted by hand.",
            report.superseded,
        )
    return report


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
    """
    pending: list[tuple[NoteSource, NoteEnml]] = []
    for source, note in rendered:
        entry = state.get(source.key)
        unchanged = entry is not None and entry.content_hash == _content_hash(
            source.title, note.enml
        )
        if unchanged and not force:
            report.skipped_unchanged += 1
            if entry is not None and note.needs_indent_pass:
                report.indent_targets.append((entry.note_id, source.title, note.checklist))
            continue
        pending.append((source, note))
    return pending


def _await_landing_folder(
    runner: EnexNotesRunnerProtocol,
    account: str,
    before: frozenset[str],
    deadline: float,
) -> tuple[str, str]:
    """Block until Notes creates a landing folder that did not exist before."""
    while time.monotonic() < deadline:
        new = {
            name
            for name in runner.folder_names(account) - before
            if name.startswith(LANDING_FOLDER_PREFIX)
        }
        if new:
            # Newest last: "Imported Notes 9" sorts after "Imported Notes 1".
            chosen = sorted(new, key=lambda name: (len(name), name))[-1]
            return chosen, runner.folder_id_by_name(account, chosen)
        time.sleep(IMPORT_POLL_INTERVAL_SECONDS)
    raise NotesError(
        "timed out waiting for Notes to create an import folder -- was the 'Import' button clicked?"
    )


def _await_landing_notes(
    runner: EnexNotesRunnerProtocol,
    landing_name: str,
    landing_id: str,
    expected: int,
    deadline: float,
) -> list[ImportedNote]:
    """Poll the landing folder until it stops filling up.

    The folder appears as soon as the import starts, not when it finishes: on
    a large archive Notes keeps adding notes to it for minutes. Reading it once
    would silently leave the tail of the corpus unfiled, so this waits for the
    count to hold steady across two polls *and* reach the archive's own note
    count before returning.
    """
    notes = runner.notes_in_folder(landing_id)
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
        notes = runner.notes_in_folder(landing_id)
    return notes


def _file_imported_notes(
    runner: EnexNotesRunnerProtocol,
    account: str,
    imported: Sequence[ImportedNote],
    pending: Sequence[tuple[NoteSource, NoteEnml]],
    state: NotesState,
    report: EnexImportReport,
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

        if enml.needs_indent_pass:
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
