"""Delete what an import leaves behind: stale copies and empty landing folders.

Every other module here is additive on purpose -- `notes_enex` never deletes,
it records a replaced note's id in `NoteStateEntry.superseded_note_ids` and
says so. That is the right default, but it means a migration accumulates: one
superseded copy per re-import, plus an empty `Imported Notes N` folder per run.
This module is the one place allowed to remove them, and it is deliberately
awkward to fire: nothing happens without `apply=True`.

Two rules exist because breaking either one has already cost real notes:

* **Only top-level folders, matched by id.** Notes resolves `folder "X"` by
  name across the whole account, so a nested folder sharing a name with the
  one you meant is a live hazard. Every folder here is checked against the
  account's own top-level list first and then addressed by id.
* **Only ids the state file vouches for.** Superseded notes are deleted by the
  exact id recorded when they were replaced -- never by title, URL or any
  other guess. Matching notes by URL is what deleted two of the user's own
  notes earlier in this project's history.

Deleted notes and folders go to Notes' *Recently Deleted*, so this is
recoverable for thirty days; it is still the only destructive path in the tool.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from quip2md.config import Config
from quip2md.notes_enex import NOTES_STATE_FILENAME
from quip2md.notes_import import NotesError, NotesState, NoteStateEntry, notes_run_lock

logger = logging.getLogger("quip2md.notes_prune")

#: Never removable, whatever the arguments say: this is the migration itself.
PROTECTED_FOLDERS = frozenset({"Quip", "Notes", "Recently Deleted"})


@dataclass(slots=True, frozen=True)
class FolderInfo:
    """One top-level folder, with enough context to refuse to delete it."""

    name: str
    folder_id: str
    notes: int
    subfolders: int

    @property
    def is_empty(self) -> bool:
        return self.notes == 0 and self.subfolders == 0


@dataclass(slots=True)
class PruneReport:
    """What a prune did, or would have done."""

    applied: bool = False
    notes_deleted: int = 0
    folders_deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "notes_deleted": self.notes_deleted,
            "folders_deleted": self.folders_deleted,
            "skipped": [{"target": t, "reason": r} for t, r in self.skipped],
            "failed": [{"target": t, "reason": r} for t, r in self.failed],
        }


class PruneRunnerProtocol(Protocol):
    """The Notes automation a prune needs, behind a seam for tests."""

    def resolve_account(self, *, local: bool) -> str: ...

    def top_level_folders(self, account: str) -> list[FolderInfo]: ...

    def delete_folder(self, folder_id: str) -> None: ...

    def delete_note(self, note_id: str) -> None: ...


def prune_notes(
    runner: PruneRunnerProtocol,
    config: Config,
    *,
    folders: Sequence[str] = (),
    empty_landing: bool = False,
    superseded: bool = False,
    apply: bool = False,
) -> PruneReport:
    """Remove named folders, empty landing folders and/or superseded notes.

    Without `apply` nothing is touched and the report describes what would go,
    which is what makes this safe to run first and read.
    """
    report = PruneReport(applied=apply)
    if not apply:
        return _plan_or_prune(runner, config, report, folders, empty_landing, superseded, False)
    # Deleting while an import is filing notes could remove one it is about to
    # record, so a real prune takes the same lock a real import does.
    with notes_run_lock(Path(config.state_path).parent):
        return _plan_or_prune(runner, config, report, folders, empty_landing, superseded, True)


def _plan_or_prune(
    runner: PruneRunnerProtocol,
    config: Config,
    report: PruneReport,
    folders: Sequence[str],
    empty_landing: bool,
    superseded: bool,
    apply: bool,
) -> PruneReport:
    account = runner.resolve_account(local=False)
    top = {info.name: info for info in runner.top_level_folders(account)}

    if superseded:
        _prune_superseded(runner, config, report, apply=apply)

    wanted = list(folders)
    if empty_landing:
        wanted += [
            info.name
            for info in top.values()
            if info.name.startswith("Imported Notes") and info.is_empty
        ]

    for name in dict.fromkeys(wanted):
        info = top.get(name)
        if info is None:
            report.skipped.append((name, "no top-level folder by that name"))
            continue
        if name in PROTECTED_FOLDERS:
            report.skipped.append((name, "protected: this folder is the migration itself"))
            continue
        if not apply:
            report.folders_deleted.append(name)
            continue
        try:
            runner.delete_folder(info.folder_id)
        except Exception as exc:  # broad by design: per-folder failure isolation
            report.failed.append((name, str(exc)))
            continue
        report.folders_deleted.append(name)

    if apply and report.folders_deleted:
        _confirm_folders_are_gone(runner, account, report)
    return report


def _confirm_folders_are_gone(
    runner: PruneRunnerProtocol, account: str, report: PruneReport
) -> None:
    """Re-read the account and report any folder that came back.

    Deleting a folder that still holds notes reports success and then does not
    stick -- observed live on an iCloud account, where a folder of 492 notes
    reappeared within a minute while the empty ones stayed gone. Delete the
    notes first (`--superseded`), then the folder. Without this check the
    command would claim a deletion that silently undid itself.
    """
    survivors = {info.name for info in runner.top_level_folders(account)}
    still_there = [name for name in report.folders_deleted if name in survivors]
    for name in still_there:
        report.folders_deleted.remove(name)
        report.failed.append(
            (
                name,
                "still present after deletion -- Notes does not persist deleting a "
                "folder that still holds notes; empty it first",
            )
        )


def _prune_superseded(
    runner: PruneRunnerProtocol,
    config: Config,
    report: PruneReport,
    *,
    apply: bool,
) -> None:
    """Delete every note id the state file records as replaced."""
    state = NotesState(Path(config.state_path).parent / NOTES_STATE_FILENAME)
    state.load()
    live = {entry.note_id for entry in state.entries.values()}

    # Persist before moving to the next id, so an interrupting BaseException
    # (Ctrl-C / SystemExit -- not caught by the per-note `except Exception`
    # inside the loop) cannot leave the on-disk state claiming as superseded
    # an id Notes has already moved to Recently Deleted. Mirrors
    # `_run_import_locked`'s per-batch `record` + `finally: flush()`: the
    # per-id `record` is the load-bearing part, since a bare `try/finally`
    # around the loop would persist an unchanged entry otherwise.
    try:
        for key, entry in list(state.entries.items()):
            if not entry.superseded_note_ids:
                continue
            kept: list[str] = list(entry.superseded_note_ids)
            for note_id in entry.superseded_note_ids:
                if note_id in live:
                    # Paranoia, not decoration: deleting a note that is also some
                    # document's current copy would destroy live data.
                    report.skipped.append((note_id, "still the current note for a document"))
                    continue
                if not apply:
                    report.notes_deleted += 1
                    continue
                try:
                    runner.delete_note(note_id)
                except Exception as exc:  # broad by design: per-note failure isolation
                    report.failed.append((note_id, str(exc)))
                    continue
                report.notes_deleted += 1
                kept.remove(note_id)
                state.record(key, _without_superseded(entry, kept))
                state.flush()
    finally:
        if apply:
            state.flush()


def _without_superseded(entry: NoteStateEntry, kept: Sequence[str]) -> NoteStateEntry:
    return NoteStateEntry(
        note_id=entry.note_id,
        folder=entry.folder,
        content_hash=entry.content_hash,
        imported_at=entry.imported_at,
        superseded_note_ids=tuple(kept),
    )


# --- Real Notes automation ----------------------------------------------

# Top-level only, and the container check is what enforces it: `folders of
# account` is flat, so a nested folder with the same name would otherwise be
# indistinguishable from the one that was asked for.
_AS_TOP_LEVEL_FOLDERS = """
on run argv
    set acc to item 1 of argv
    tell application "Notes"
        if acc is "" then
            set theAccount to default account
        else
            set theAccount to account acc
        end if
        set accId to id of theAccount
        set out to ""
        repeat with f in folders of theAccount
            set isTop to false
            try
                if (id of container of f) is accId then set isTop to true
            end try
            if isTop then
                set out to out & (name of f) & (ASCII character 31) & (id of f) & ¬
                    (ASCII character 31) & (count of notes of f) & (ASCII character 31) & ¬
                    (count of folders of f) & (ASCII character 30)
            end if
        end repeat
        return out
    end tell
end run
"""

_AS_DELETE_FOLDER = """
on run argv
    tell application "Notes"
        delete folder id (item 1 of argv)
    end tell
end run
"""

_AS_DELETE_NOTE = """
on run argv
    tell application "Notes"
        delete note id (item 1 of argv)
    end tell
end run
"""


class PruneRunner:
    """Real Notes automation for pruning. Shares `EnexNotesRunner`'s plumbing."""

    def __init__(self) -> None:
        from quip2md.notes_enex import EnexNotesRunner

        self._inner = EnexNotesRunner()

    def resolve_account(self, *, local: bool) -> str:
        return self._inner.resolve_account(local=local)

    def top_level_folders(self, account: str) -> list[FolderInfo]:
        from quip2md.notes_enex import _FIELD_SEPARATOR, _RECORD_SEPARATOR

        stdout = self._inner._run(_AS_TOP_LEVEL_FOLDERS, [account])
        folders: list[FolderInfo] = []
        for record in stdout.split(_RECORD_SEPARATOR):
            if not record.strip():
                continue
            parts = record.split(_FIELD_SEPARATOR)
            if len(parts) < 4:
                continue
            try:
                folders.append(
                    FolderInfo(
                        name=parts[0].strip(),
                        folder_id=parts[1].strip(),
                        notes=int(parts[2].strip()),
                        subfolders=int(parts[3].strip()),
                    )
                )
            except ValueError as exc:
                raise NotesError(f"could not read the folder list: {record!r}") from exc
        return folders

    def delete_folder(self, folder_id: str) -> None:
        self._inner._run(_AS_DELETE_FOLDER, [folder_id])

    def delete_note(self, note_id: str) -> None:
        self._inner._run(_AS_DELETE_NOTE, [note_id])


__all__ = [
    "PROTECTED_FOLDERS",
    "FolderInfo",
    "PruneReport",
    "PruneRunner",
    "PruneRunnerProtocol",
    "prune_notes",
]
