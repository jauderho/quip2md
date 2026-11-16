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
import signal
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
from quip2md.notes_indent import _AS_CHECK_ACCESSIBILITY, _TCC_ACCESSIBILITY_ERRORS
from quip2md.walker import sanitize_component

logger = logging.getLogger("quip2md.notes_enex")

# --- Constants ---------------------------------------------------------

NOTES_STATE_FILENAME = "notes_state.json"
DEFAULT_ENEX_FILENAME = "quip2md.enex"

#: The archive a stalled Markdown import's undelivered documents are re-routed
#: through. Separate from `DEFAULT_ENEX_FILENAME`, which the run may still hold.
FALLBACK_ENEX_FILENAME = "quip2md-fallback.enex"

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

#: How long the landing folder may go without a new note before the import
#: counts as stalled. Notes' Markdown importer has hung mid-import at a frozen
#: count; without this the run would sit out the whole timeout. Applied only
#: where the run then restarts Notes (see `_import_batch`).
IMPORT_STALL_SECONDS = 180.0

#: Matches the provenance URL in a note body read back from Notes.
_QUIP_URL_RE = re.compile(r"https?://(?:[\w.-]*\.)?quip\.com/[\w-]+", re.IGNORECASE)

#: Splits a note body into paragraph-ish blocks, so the provenance line can be
#: told apart from any other link in the document.
_BLOCK_BOUNDARY_RE = re.compile(r"</?(?:div|p|li|br|h[1-6])[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_PROVENANCE_PREFIX = "Source:"

#: `--auto-confirm`: how long to wait for Notes' confirmation sheet after a
#: file is opened. An archive never shows one on current Notes, so the wait
#: ending without a sheet is normal.
AUTO_CONFIRM_WAIT_SECONDS = 30.0
_CONFIRM_POLL_INTERVAL_SECONDS = 0.5
_CONFIRM_PROBE_TIMEOUT_SECONDS = 10.0

#: What System Events says when the running binary lacks Accessibility access.
_UI_SCRIPTING_DENIED = (*_TCC_ACCESSIBILITY_ERRORS, "-25211", "assistive access")

UI_SCRIPTING_HELP = (
    "--auto-confirm needs macOS Accessibility permission. Add the application "
    "running quip2md (your terminal) under System Settings > Privacy & Security "
    "> Accessibility, restart the terminal, then run the command again. "
    "Nothing was imported."
)

_FIRST_INVOCATION_TIMEOUT_SECONDS = 120.0
_SUBSEQUENT_INVOCATION_TIMEOUT_SECONDS = 60.0

_NOTE_READ_TIMEOUT_SECONDS = 300.0

#: `restart_notes()` budgets. Notes refuses a scripted quit while it is
#: importing, so each step escalates only after the one before it had its time.
_QUIT_REQUEST_TIMEOUT_SECONDS = 10.0
_QUIT_WAIT_SECONDS = 10.0
_SIGTERM_WAIT_SECONDS = 20.0
_SIGKILL_WAIT_SECONDS = 5.0
_PROCESS_POLL_INTERVAL_SECONDS = 0.5
_RELAUNCH_WAIT_SECONDS = 60.0
_RELAUNCH_POLL_INTERVAL_SECONDS = 2.0
_RELAUNCH_PROBE_TIMEOUT_SECONDS = 15.0

#: How much of each body to fetch. The provenance line is the first block;
#: whole bodies would only bloat stdout for no gain.
_BODY_HEAD_CHARS = 1200

_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"

#: Notes filed per osascript call: one launch per batch, yet small enough that an
#: interrupted batch leaves little to reconcile and stays far under the call timeout.
_MOVE_BATCH = 25

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
    #: Documents a stalled Markdown import re-routed through the fallback archive.
    rerouted: int = 0
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
        self.rerouted += other.rerouted
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
            "rerouted": self.rerouted,
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

    def note_ids_in_folder(self, folder_id: str) -> list[str]: ...

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]: ...

    def child_folder_id(self, parent_id: str, name: str) -> str: ...

    def move_notes(self, landing_id: str, moves: Sequence[tuple[str, str]]) -> dict[str, str]: ...

    def open_enex(self, path: Path) -> None: ...

    def restart_notes(self) -> None: ...

    def check_ui_scripting(self) -> None: ...

    def confirm_import(self, wait_seconds: float) -> bool: ...


_AS_LIST_ACCOUNTS = """
on run argv
    tell application "Notes"
        return name of default account
    end tell
end run
"""

_AS_QUIT_NOTES = 'tell application "Notes" to quit'

# Clicks "Import" on Notes' confirmation sheet, and on nothing else: the sheet
# must carry the exact prompt *and* a button with that exact title. The
# comparisons run here, under `considering case`, rather than in a `whose`
# clause, which System Events matches without regard to case. English UI only.
_AS_CONFIRM_IMPORT = """
on run argv
    tell application "System Events"
        tell process "Notes"
            repeat with w in windows
                repeat with s in sheets of w
                    set promptTexts to value of static texts of s
                    considering case
                        if promptTexts contains "Import to Notes?" then
                            repeat with b in buttons of s
                                if (title of b) is "Import" then
                                    click (contents of b)
                                    return "confirmed"
                                end if
                            end repeat
                        end if
                    end considering
                end repeat
            end repeat
        end tell
    end tell
    return "none"
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

# One bulk read of the ids: far cheaper than walking the notes one by one.
_AS_NOTE_IDS_IN_FOLDER = """
on run argv
    tell application "Notes"
        set ids to id of every note of folder id (item 1 of argv)
    end tell
    set AppleScript's text item delimiters to (ASCII character 30)
    set out to ids as text
    set AppleScript's text item delimiters to ""
    return out
end run
"""

# Bulk property reads cost ~0.05 ms per note; a `note id` lookup costs ~150 ms.
# The three lists are separate Apple events, so the ids are re-read afterwards
# and compared: if a note arrived in between, the lists could misalign and a
# note would get another note's body. Bodies are cut to `headLen` here so
# stdout stays small; matching only needs the provenance line at the top.
_AS_NOTE_HEADS = """
on run argv
    set folderId to item 1 of argv
    set headLen to (item 2 of argv) as integer
    repeat 3 times
        tell application "Notes"
            set f to folder id folderId
            set ids to id of every note of f
            set nms to name of every note of f
            set bds to body of every note of f
            set idsAfter to id of every note of f
        end tell
        if ids is idsAfter then exit repeat
    end repeat
    if ids is not idsAfter then error "the folder changed while it was being read" number 1001
    set recs to {}
    repeat with k from 1 to count of ids
        set b to item k of bds
        if (count of b) > headLen then set b to text 1 thru headLen of b
        set end of recs to (item k of ids) & (ASCII character 31) & ¬
            (item k of nms) & (ASCII character 31) & b
    end repeat
    set AppleScript's text item delimiters to (ASCII character 30)
    set out to recs as text
    set AppleScript's text item delimiters to ""
    return out & (ASCII character 30)
end run
"""

_AS_MOVE_NOTES = """
on run argv
    set srcId to item 1 of argv
    set failures to {}
    tell application "Notes"
        set src to folder id srcId
        repeat with k from 2 to (count of argv) by 2
            set noteId to item k of argv
            try
                move (first note of src whose id is noteId) to folder id (item (k + 1) of argv)
            on error errMsg
                set end of failures to noteId & (ASCII character 31) & errMsg
            end try
        end repeat
    end tell
    set AppleScript's text item delimiters to (ASCII character 30)
    set out to failures as text
    set AppleScript's text item delimiters to ""
    return out
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

    def note_ids_in_folder(self, folder_id: str) -> list[str]:
        """Ids of every note in the folder, in one bulk read."""
        return [
            part.strip()
            for part in self._run(_AS_NOTE_IDS_IN_FOLDER, [folder_id]).split(_RECORD_SEPARATOR)
            if part.strip()
        ]

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        """Every note in the folder: id, name and the head of the body.

        One call reads the whole folder with bulk property reads, which cost
        far less per note than a `note id` lookup each (~150 ms apiece).
        """
        stdout = self._run(
            _AS_NOTE_HEADS,
            [folder_id, str(_BODY_HEAD_CHARS)],
            timeout=_NOTE_READ_TIMEOUT_SECONDS,
        )
        notes: list[ImportedNote] = []
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

    def move_notes(self, landing_id: str, moves: Sequence[tuple[str, str]]) -> dict[str, str]:
        """Move notes out of the landing folder in one call; returns `{note_id: error}`."""
        if not moves:
            return {}
        argv = [landing_id]
        for note_id, folder_id in moves:
            argv += [note_id, folder_id]
        failures: dict[str, str] = {}
        for record in self._run(_AS_MOVE_NOTES, argv).split(_RECORD_SEPARATOR):
            if not record.strip():
                continue
            note_id, _, reason = record.partition(_FIELD_SEPARATOR)
            failures[note_id.strip()] = reason
        return failures

    def open_enex(self, path: Path) -> None:
        """Hand a `.enex` file, or a folder of Markdown files, to Notes' importer."""
        proc = subprocess.run(
            ["open", "-a", "Notes", str(path)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise NotesError(f"could not hand {path.name} to Notes", stderr=proc.stderr.strip())

    def check_ui_scripting(self) -> None:
        """Raise unless this process may drive the UI (Accessibility permission)."""
        try:
            answer = self._run(_AS_CHECK_ACCESSIBILITY, []).strip().lower()
        except NotesError as exc:
            if _ui_scripting_denied(exc):
                raise NotesError(UI_SCRIPTING_HELP, stderr=exc.stderr) from exc
            raise
        if answer != "true":
            raise NotesError(UI_SCRIPTING_HELP)

    def confirm_import(self, wait_seconds: float) -> bool:
        """Click "Import" on Notes' confirmation sheet; False if none appears in time.

        A failed probe counts as "not yet" (Notes may still be launching, or
        the sheet still animating in), except a refused Accessibility
        permission, which no amount of waiting fixes.
        """
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                answer = self._run(_AS_CONFIRM_IMPORT, [], timeout=_CONFIRM_PROBE_TIMEOUT_SECONDS)
            except NotesError as exc:
                if _ui_scripting_denied(exc):
                    raise NotesError(UI_SCRIPTING_HELP, stderr=exc.stderr) from exc
                logger.debug("confirmation probe failed: %s", _error_reason(exc))
            else:
                if answer.strip() == "confirmed":
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_CONFIRM_POLL_INTERVAL_SECONDS)

    def restart_notes(self) -> None:
        """Quit Notes -- by force if it must be -- and relaunch it.

        The way out of a hung import. Notes refuses a scripted quit while it
        is importing (error -128), so the request is only the first step:
        SIGTERM follows, then SIGKILL. A terminated Notes loses nothing that
        had already arrived, and does not resume the import when relaunched.
        """
        try:
            subprocess.run(
                ["osascript", "-e", _AS_QUIT_NOTES],
                capture_output=True,
                text=True,
                timeout=_QUIT_REQUEST_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.debug("Notes did not answer the quit request")
        if not self._await_notes_exit(_QUIT_WAIT_SECONDS):
            logger.warning("Notes did not quit when asked; terminating it.")
            self._signal_notes(signal.SIGTERM)
            if not self._await_notes_exit(_SIGTERM_WAIT_SECONDS):
                logger.warning("Notes ignored the termination request; killing it.")
                self._signal_notes(signal.SIGKILL)
                if not self._await_notes_exit(_SIGKILL_WAIT_SECONDS):
                    raise NotesError("Notes could not be quit, even with SIGKILL")

        proc = subprocess.run(
            ["open", "-g", "-a", "Notes"], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise NotesError("could not relaunch Notes", stderr=proc.stderr.strip())
        deadline = time.monotonic() + _RELAUNCH_WAIT_SECONDS
        while True:
            try:
                self._run(_AS_LIST_ACCOUNTS, [], timeout=_RELAUNCH_PROBE_TIMEOUT_SECONDS)
            except NotesError:
                if time.monotonic() >= deadline:
                    raise NotesError(
                        f"Notes did not answer within {_RELAUNCH_WAIT_SECONDS:.0f}s of "
                        "being relaunched"
                    ) from None
                time.sleep(_RELAUNCH_POLL_INTERVAL_SECONDS)
            else:
                return

    @staticmethod
    def _notes_pids() -> list[int]:
        """Pids of this user's running Notes processes; empty when it is not running."""
        proc = subprocess.run(
            ["pgrep", "-x", "-U", str(os.getuid()), "Notes"],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(part) for part in proc.stdout.split() if part.isdigit()]

    def _await_notes_exit(self, seconds: float) -> bool:
        """Poll until Notes is gone; False if it is still running after `seconds`."""
        deadline = time.monotonic() + seconds
        while self._notes_pids():
            if time.monotonic() >= deadline:
                return False
            time.sleep(_PROCESS_POLL_INTERVAL_SECONDS)
        return True

    def _signal_notes(self, signum: signal.Signals) -> None:
        for pid in self._notes_pids():
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                continue  # it exited between the listing and the signal


def _ui_scripting_denied(exc: NotesError) -> bool:
    """Whether an osascript failure is a refused Accessibility permission.

    "Invalid index" shares error -1719 with the refusal but only means the
    window or sheet was not there (yet).
    """
    if "Invalid index" in exc.stderr:
        return False
    return any(marker in exc.stderr for marker in _UI_SCRIPTING_DENIED)


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
    stall_seconds: float = IMPORT_STALL_SECONDS,
    fallback: bool = True,
    auto_confirm: bool = False,
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
    non-interactive context; without `auto_confirm` the sheet still has to be
    clicked by a human, which is why the wait is generous and its timeout
    explicit. Notes shows the sheet for a Markdown folder; current Notes
    imports an archive without asking.

    With `auto_confirm`, the run clicks the sheet's "Import" button itself
    (`runner.confirm_import()`) after each file it opens. That is UI
    scripting, so `runner.check_ui_scripting()` must pass before anything is
    opened; it raises `NotesError` when the Accessibility permission is
    missing. It does nothing in a dry run or with `adopt_landing`, which open
    nothing.

    With `fallback`, a Markdown import whose landing folder gains no note for
    `stall_seconds` has stalled, and the run recovers on its own instead of
    sitting out the whole timeout: it quits and relaunches Notes
    (`runner.restart_notes()`), files whatever arrived late, and imports the
    documents still missing through a second archive
    (`FALLBACK_ENEX_FILENAME`, with its own confirmation and a fresh timeout).
    They are counted in `report.rerouted`, and their nested checklists arrive
    flat, as in any archive import.

    The stall cutoff applies to nothing else. It is only safe because the
    restart stops the importer: a slow import abandoned early would keep
    delivering notes that are never filed or recorded, and the next run would
    import them a second time. So an archive import (the fallback archive
    included), and a Markdown import without `fallback`, wait for the full
    timeout and never restart Notes. A document that is still missing in the
    end is listed in `report.failed`.

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
    auto_confirm = auto_confirm and adopt_landing is None
    if auto_confirm:
        runner.check_ui_scripting()

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
            stall_seconds=stall_seconds,
            fallback_path=(config.state_path.parent / FALLBACK_ENEX_FILENAME if fallback else None),
            auto_confirm=auto_confirm,
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
    stall_seconds: float = IMPORT_STALL_SECONDS,
    fallback_path: Path | None = None,
    auto_confirm: bool = False,
) -> EnexImportReport:
    """Hand each batch to Notes and file what comes back. Holds the run lock.

    A batch is `(path, child, pending)`: the archive or Markdown folder to
    open, the folder Notes nests the notes in inside its landing folder
    (`None` for an archive, whose notes land directly in it), and the sources
    the batch carries. Batches run one after the other, each with its own
    confirmation, and share one deadline.

    `fallback_path` is where a stalled Markdown batch writes the archive it
    re-routes its undelivered documents through; `None` turns that off.
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
                    timeout_seconds=timeout_seconds,
                    stall_seconds=stall_seconds,
                    fallback_path=fallback_path,
                    auto_confirm=auto_confirm,
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
    timeout_seconds: float = IMPORT_TIMEOUT_SECONDS,
    stall_seconds: float = IMPORT_STALL_SECONDS,
    fallback_path: Path | None = None,
    auto_confirm: bool = False,
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
        if auto_confirm:
            logger.info(
                "If Notes asks to confirm the import of %s, it is confirmed automatically.",
                target.name,
            )
        elif confirm:
            logger.warning(
                "Notes will now ask you to confirm the import of %s. Click 'Import' "
                "in the dialog; this run waits up to %.0f minutes.",
                target.name,
                max(deadline - time.monotonic(), 0) / 60,
            )
        runner.open_enex(target)
        if auto_confirm:
            if runner.confirm_import(AUTO_CONFIRM_WAIT_SECONDS):
                logger.info("Confirmed the import of %s.", target.name)
            else:
                logger.info("Notes showed no confirmation sheet for %s.", target.name)
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
            # Giving up early is only safe when the run then restarts Notes:
            # an importer left running keeps delivering notes nobody files,
            # and the next run would import those documents a second time.
            stall_seconds=(
                stall_seconds if child is not None and fallback_path is not None else None
            ),
        )
    report.imported += len(imported)
    _file_imported_notes(
        runner, account, landing_id, imported, pending, state, report, markdown=markdown
    )
    if adopt_landing is None:
        # A source Notes never delivered (the wait expired) is not in Notes at
        # all, so the run must not report success over it.
        arrived = {_extract_quip_url(note.body) for note in imported}
        missing = [item for item in pending if item[0].quip_url not in arrived]
        if missing and child is not None and fallback_path is not None:
            # Only the Markdown importer is known to hang, and the archive
            # route is what gets its documents in; an archive never falls back.
            return _reroute_through_archive(
                runner,
                account,
                report,
                state,
                landing_name,
                landing_id,
                frozenset(note.note_id for note in imported),
                missing,
                fallback_path,
                confirm=confirm,
                timeout_seconds=timeout_seconds,
                stall_seconds=stall_seconds,
                auto_confirm=auto_confirm,
            )
        _fail_undelivered(report, landing_name, missing)
    return landing_name


def _fail_undelivered(
    report: EnexImportReport, landing_name: str, missing: Sequence[tuple[NoteSource, NoteEnml]]
) -> None:
    for source, _note in missing:
        report.failed.append(
            (
                source.key,
                f"never arrived in {landing_name!r} before the wait expired; re-run to import it",
            )
        )


def _reroute_through_archive(
    runner: EnexNotesRunnerProtocol,
    account: str,
    report: EnexImportReport,
    state: NotesState,
    landing_name: str,
    landing_id: str,
    seen: frozenset[str],
    missing: Sequence[tuple[NoteSource, NoteEnml]],
    fallback_path: Path,
    *,
    confirm: bool,
    timeout_seconds: float,
    stall_seconds: float,
    auto_confirm: bool = False,
) -> str:
    """Recover a stalled Markdown import; returns the landing folder name(s).

    `missing` are the sources the Markdown folder never delivered and `seen`
    the ids of the notes it did, which were dealt with already. Notes is
    restarted (a hung importer never finishes, and blocks the next import),
    the folder is read once more for notes that arrived after the wait ended,
    and only what is still missing then goes into the fallback archive -- a
    source that did arrive must never be imported a second time.
    """
    logger.warning(
        "Notes stalled importing %r. Quitting and relaunching Notes, then importing "
        "the %d remaining document(s) through the archive.",
        landing_name,
        len(missing),
    )
    try:
        runner.restart_notes()
        late = [note for note in runner.notes_in_folder(landing_id) if note.note_id not in seen]
    except NotesError as exc:
        # Without a clean restart and a trustworthy re-read, a second import
        # could duplicate notes. Report the documents instead.
        logger.error("Could not restart Notes to recover the import: %s", _error_reason(exc))
        _fail_undelivered(report, landing_name, missing)
        return landing_name
    report.imported += len(late)
    _file_imported_notes(runner, account, landing_id, late, missing, state, report, markdown=True)
    arrived_late = {_extract_quip_url(note.body) for note in late}
    remaining = [item for item in missing if item[0].quip_url not in arrived_late]
    if not remaining:
        return landing_name

    fallback_path.parent.mkdir(parents=True, exist_ok=True)
    fallback_path.write_text(build_enex([note for _source, note in remaining]), encoding="utf-8")
    report.rerouted += len(remaining)
    # `markdown=False`: an archive flattens nested checklists, so these notes
    # are indent targets like any other archive note. No `fallback_path`: an
    # archive waits out its deadline, then reports what is missing as failed.
    fallback_landing = _import_batch(
        runner,
        account,
        report,
        state,
        fallback_path,
        None,
        remaining,
        markdown=False,
        deadline=time.monotonic() + timeout_seconds,
        confirm=confirm,
        adopt_landing=None,
        timeout_seconds=timeout_seconds,
        stall_seconds=stall_seconds,
        auto_confirm=auto_confirm,
    )
    return f"{landing_name}, {fallback_landing}"


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
        name: frozenset(runner.note_ids_in_folder(runner.folder_id_by_name(account, name)))
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
            if any(note_id not in ids for note_id in runner.note_ids_in_folder(folder_id)):
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
    stall_seconds: float | None = None,
) -> list[ImportedNote]:
    """Poll the landing folder until it stops filling up, then read it once.

    Polling reads ids only; the (slower) heads are read a single time at the end.

    `already_there` are ids the folder held before this import; they belong to
    someone else and are neither waited for nor returned.

    The folder appears as soon as the import starts, not when it finishes: on
    a large archive Notes keeps adding notes to it for minutes. Reading it once
    would silently leave the tail of the corpus unfiled, so this waits for the
    count to hold steady across two polls *and* reach the archive's own note
    count before returning.

    A folder short of that count that gains no note for `stall_seconds` has
    stalled: Notes' importer can hang mid-import, and never recovers. The wait
    ends there, as it does at the deadline, with whatever arrived. `None`
    turns the cutoff off, leaving the deadline alone -- which is what a caller
    that will not restart Notes afterwards must pass.
    """

    def arrived() -> int:
        return sum(1 for i in runner.note_ids_in_folder(landing_id) if i not in already_there)

    count = arrived()
    previous = -1
    most = -1
    last_growth = 0.0
    while count != previous or count < expected:
        now = time.monotonic()
        if count > most:
            most, last_growth = count, now
        if now >= deadline:
            logger.warning(
                "Notes stopped short: folder %r holds %d of the %d note(s) in the "
                "archive after the wait expired. Proceeding with those; re-run to "
                "import the remaining %d.",
                landing_name,
                count,
                expected,
                max(expected - count, 0),
            )
            break
        if stall_seconds is not None and count < expected and now - last_growth >= stall_seconds:
            logger.warning(
                "Notes stopped delivering: folder %r holds %d of the %d note(s) and "
                "gained none in %.0f seconds. Proceeding with those.",
                landing_name,
                count,
                expected,
                now - last_growth,
            )
            break
        previous = count
        time.sleep(IMPORT_POLL_INTERVAL_SECONDS)
        count = arrived()
    return [n for n in runner.notes_in_folder(landing_id) if n.note_id not in already_there]


def _file_imported_notes(
    runner: EnexNotesRunnerProtocol,
    account: str,
    landing_id: str,
    imported: Sequence[ImportedNote],
    pending: Sequence[tuple[NoteSource, NoteEnml]],
    state: NotesState,
    report: EnexImportReport,
    *,
    markdown: bool = False,
) -> None:
    by_url = {source.quip_url: (source, note) for source, note in pending if source.quip_url}
    imported_at = _now_iso8601()

    planned: list[tuple[ImportedNote, NoteSource, NoteEnml, str]] = []
    for note in imported:
        url = _extract_quip_url(note.body)
        match = by_url.get(url) if url else None
        if match is None:
            report.unmatched.append(note.name)
            continue
        source, enml = match
        try:
            folder_id = runner.get_or_create_folder(account, source.folder_path)
        except Exception as exc:  # broad by design: per-note failure isolation
            report.failed.append((source.key, f"move failed: {_error_reason(exc)}"))
            continue
        planned.append((note, source, enml, folder_id))

    for start in range(0, len(planned), _MOVE_BATCH):
        chunk = planned[start : start + _MOVE_BATCH]
        try:
            failures = runner.move_notes(
                landing_id, [(note.note_id, folder_id) for note, _, _, folder_id in chunk]
            )
        except BaseException as exc:
            # Unknown how far the call got. A note that left the landing folder
            # was filed; recording it stops the next run importing it again.
            try:
                remaining = set(runner.note_ids_in_folder(landing_id))
            except Exception:  # broad by design: never mask the original error
                remaining = {note.note_id for note, _, _, _ in chunk}
            for note, source, enml, folder_id in chunk:
                if note.note_id not in remaining:
                    _record_moved(
                        note, source, enml, folder_id, state, report, imported_at, markdown
                    )
            if not isinstance(exc, Exception):
                raise
            for note, source, _, _ in chunk:
                if note.note_id in remaining:
                    report.failed.append((source.key, f"move failed: {_error_reason(exc)}"))
            continue
        for note, source, enml, folder_id in chunk:
            if note.note_id in failures:
                report.failed.append((source.key, f"move failed: {failures[note.note_id]}"))
            else:
                _record_moved(note, source, enml, folder_id, state, report, imported_at, markdown)


def _record_moved(
    note: ImportedNote,
    source: NoteSource,
    enml: NoteEnml,
    folder_id: str,
    state: NotesState,
    report: EnexImportReport,
    imported_at: str,
    markdown: bool,
) -> None:
    """Record a note that now sits in its target folder."""
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
            folder="/".join(source.folder_path),
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
