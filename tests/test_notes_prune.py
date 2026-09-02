"""Coverage of the only destructive path in the tool.

`prune_notes` deletes notes and folders, so the tests that matter most are the
ones proving what it *refuses* to touch: a folder that is not top-level, the
Quip folder itself, and any id that is still some document's current note.
Earlier in this project a URL-matched cleanup deleted two of the user's own
notes; deleting only ids the state file vouches for is the fix, and these
tests pin it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from quip2md.config import Config
from quip2md.notes_import import NotesError
from quip2md.notes_prune import FolderInfo, PruneRunner, prune_notes


def _config(tmp_path: Path, *, dry_run: bool = False) -> Config:
    return Config(
        token="",
        output_dir=tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=dry_run,
        verbose=False,
        include_chats=False,
        force=False,
    )


def _write_state(tmp_path: Path, entries: dict[str, dict[str, object]]) -> Path:
    path = tmp_path / ".quip2md" / "notes_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _entry(note_id: str, *, superseded: Sequence[str] = ()) -> dict[str, object]:
    return {
        "note_id": note_id,
        "folder": "Quip",
        "content_hash": "h",
        "imported_at": "2026-01-01T00:00:00Z",
        "superseded_note_ids": list(superseded),
    }


@dataclass
class FakePruneRunner:
    """Records deletions; never touches Notes."""

    folders: list[FolderInfo] = field(default_factory=list)
    deleted_folders: list[str] = field(default_factory=list)
    deleted_notes: list[str] = field(default_factory=list)
    fail_on: str = ""

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def top_level_folders(self, account: str) -> list[FolderInfo]:
        return list(self.folders)

    def delete_folder(self, folder_id: str) -> None:
        if folder_id == self.fail_on:
            raise RuntimeError("Notes said no")
        self.deleted_folders.append(folder_id)
        # Notes really removes it, so the post-delete confirmation sees it gone.
        self.folders = [f for f in self.folders if f.folder_id != folder_id]

    def delete_note(self, note_id: str) -> None:
        if note_id == self.fail_on:
            raise RuntimeError("Notes said no")
        self.deleted_notes.append(note_id)


# --- Refusals ---------------------------------------------------------------


def test_nothing_is_deleted_without_apply(tmp_path: Path) -> None:
    """The default is a plan, not an action."""
    runner = FakePruneRunner(folders=[FolderInfo("Quip-Old", "f1", 0, 2)])
    _write_state(tmp_path, {"T1": _entry("live-1", superseded=["old-1"])})

    report = prune_notes(
        runner, _config(tmp_path), folders=["Quip-Old"], superseded=True, apply=False
    )

    assert runner.deleted_folders == []
    assert runner.deleted_notes == []
    assert report.applied is False
    assert report.folders_deleted == ["Quip-Old"]
    assert report.notes_deleted == 1
    # The state file must be untouched by a plan.
    state = json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text())
    assert state["T1"]["superseded_note_ids"] == ["old-1"]


def test_the_quip_folder_itself_is_never_deleted(tmp_path: Path) -> None:
    runner = FakePruneRunner(folders=[FolderInfo("Quip", "f1", 0, 2)])
    report = prune_notes(runner, _config(tmp_path), folders=["Quip"], apply=True)

    assert runner.deleted_folders == []
    assert report.folders_deleted == []
    assert any("protected" in reason for _, reason in report.skipped)


def test_a_folder_that_is_not_top_level_is_refused(tmp_path: Path) -> None:
    """`folders of account` is flat, so a name alone is not a safe handle."""
    runner = FakePruneRunner(folders=[FolderInfo("Quip", "f1", 0, 2)])
    report = prune_notes(runner, _config(tmp_path), folders=["Archive"], apply=True)

    assert runner.deleted_folders == []
    assert report.skipped == [("Archive", "no top-level folder by that name")]


def test_a_superseded_id_that_is_still_live_is_refused(tmp_path: Path) -> None:
    """A stale record must never cost a document its current note."""
    runner = FakePruneRunner()
    _write_state(
        tmp_path,
        {
            "T1": _entry("shared", superseded=["shared"]),
            "T2": _entry("live-2"),
        },
    )

    report = prune_notes(runner, _config(tmp_path), superseded=True, apply=True)

    assert runner.deleted_notes == []
    assert report.notes_deleted == 0
    assert any("still the current note" in reason for _, reason in report.skipped)


def test_pruning_nothing_is_not_an_error(tmp_path: Path) -> None:
    runner = FakePruneRunner(folders=[FolderInfo("Quip", "f1", 0, 2)])
    report = prune_notes(runner, _config(tmp_path), apply=True)
    assert report.folders_deleted == []
    assert report.notes_deleted == 0


# --- Deletions --------------------------------------------------------------


def test_a_named_top_level_folder_is_deleted_by_id(tmp_path: Path) -> None:
    runner = FakePruneRunner(
        folders=[FolderInfo("Quip-Old", "folder-old", 0, 2), FolderInfo("Quip", "f2", 0, 2)]
    )
    report = prune_notes(runner, _config(tmp_path), folders=["Quip-Old"], apply=True)

    assert runner.deleted_folders == ["folder-old"]
    assert report.folders_deleted == ["Quip-Old"]


def test_only_empty_landing_folders_are_swept(tmp_path: Path) -> None:
    runner = FakePruneRunner(
        folders=[
            FolderInfo("Imported Notes", "a", 0, 0),
            FolderInfo("Imported Notes 2", "b", 1, 0),
            FolderInfo("Imported Notes 3", "c", 0, 0),
            FolderInfo("Personal", "d", 0, 0),
        ]
    )
    report = prune_notes(runner, _config(tmp_path), empty_landing=True, apply=True)

    assert sorted(report.folders_deleted) == ["Imported Notes", "Imported Notes 3"]
    assert "b" not in runner.deleted_folders, "a landing folder holding a note must survive"
    assert "d" not in runner.deleted_folders


def test_superseded_notes_are_deleted_and_forgotten(tmp_path: Path) -> None:
    runner = FakePruneRunner()
    _write_state(tmp_path, {"T1": _entry("live-1", superseded=["old-a", "old-b"])})

    report = prune_notes(runner, _config(tmp_path), superseded=True, apply=True)

    assert runner.deleted_notes == ["old-a", "old-b"]
    assert report.notes_deleted == 2
    state = json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text())
    assert state["T1"]["note_id"] == "live-1", "the live note must survive untouched"
    assert state["T1"].get("superseded_note_ids", []) == []


def test_a_note_that_will_not_delete_stays_recorded(tmp_path: Path) -> None:
    """A failed delete must not be forgotten, or it can never be retried."""
    runner = FakePruneRunner(fail_on="old-b")
    _write_state(tmp_path, {"T1": _entry("live-1", superseded=["old-a", "old-b"])})

    report = prune_notes(runner, _config(tmp_path), superseded=True, apply=True)

    assert runner.deleted_notes == ["old-a"]
    assert [target for target, _ in report.failed] == ["old-b"]
    state = json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text())
    assert state["T1"]["superseded_note_ids"] == ["old-b"]


def test_a_folder_that_comes_back_is_reported_as_a_failure(tmp_path: Path) -> None:
    """Notes does not persist deleting a folder that still holds notes.

    Observed live on iCloud: a folder of 492 notes reported deleted and was
    back within a minute, while the empty ones stayed gone. Reporting success
    for that would be a lie the caller acts on.
    """

    @dataclass
    class ResurrectingRunner(FakePruneRunner):
        def delete_folder(self, folder_id: str) -> None:
            self.deleted_folders.append(folder_id)  # accepted, then ignored by Notes

    runner = ResurrectingRunner(folders=[FolderInfo("Quip-Old", "f1", 492, 2)])
    report = prune_notes(runner, _config(tmp_path), folders=["Quip-Old"], apply=True)

    assert report.folders_deleted == []
    assert [target for target, _ in report.failed] == ["Quip-Old"]
    assert "empty it first" in report.failed[0][1]


def test_a_folder_that_really_goes_away_is_not_reported_as_failed(tmp_path: Path) -> None:
    runner = FakePruneRunner(folders=[FolderInfo("Quip-Old", "f1", 0, 0)])
    report = prune_notes(runner, _config(tmp_path), folders=["Quip-Old"], apply=True)

    assert report.folders_deleted == ["Quip-Old"]
    assert report.failed == []


def test_a_folder_that_will_not_delete_is_reported(tmp_path: Path) -> None:
    runner = FakePruneRunner(folders=[FolderInfo("Quip-Old", "bad", 0, 2)], fail_on="bad")
    report = prune_notes(runner, _config(tmp_path), folders=["Quip-Old"], apply=True)

    assert report.folders_deleted == []
    assert [target for target, _ in report.failed] == ["Quip-Old"]


def test_a_folder_named_twice_is_deleted_once(tmp_path: Path) -> None:
    runner = FakePruneRunner(folders=[FolderInfo("Imported Notes", "a", 0, 0)])
    report = prune_notes(
        runner,
        _config(tmp_path),
        folders=["Imported Notes"],
        empty_landing=True,
        apply=True,
    )
    assert report.folders_deleted == ["Imported Notes"]
    assert runner.deleted_folders == ["a"]


@pytest.mark.parametrize("missing", [{}, {"T1": _entry("live-1")}])
def test_a_state_file_with_nothing_superseded_deletes_nothing(
    tmp_path: Path, missing: dict[str, dict[str, object]]
) -> None:
    runner = FakePruneRunner()
    _write_state(tmp_path, missing)
    report = prune_notes(runner, _config(tmp_path), superseded=True, apply=True)
    assert runner.deleted_notes == []
    assert report.notes_deleted == 0


# --- The real runner: the only code here that can destroy anything ----------


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _stub_osascript(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> list[list[str]]:
    """Replace `subprocess.run` with a stub that cannot execute anything.

    Stronger than `conftest.py`'s guard for these tests: there is no path to a
    real process, and the command really is the `osascript` call under test.
    """
    from quip2md import notes_enex

    seen: list[list[str]] = []

    def fake_run(command: Sequence[str], *args: object, **kwargs: object) -> FakeCompletedProcess:
        assert list(command)[:2] == ["osascript", "-e"], command
        seen.append(list(command))
        return FakeCompletedProcess(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(notes_enex.subprocess, "run", fake_run)
    monkeypatch.setattr(notes_enex.sys, "platform", "darwin")
    return seen


def _runner() -> PruneRunner:
    return PruneRunner()


def test_top_level_folders_parses_the_record_and_field_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_osascript(
        monkeypatch,
        stdout="Quip\x1ffolder-1\x1f0\x1f2\x1e   \x1eQuip-Old\x1ffolder-2\x1f492\x1f3\x1e",
    )
    folders = _runner().top_level_folders("iCloud")

    assert folders == [
        FolderInfo("Quip", "folder-1", 0, 2),
        FolderInfo("Quip-Old", "folder-2", 492, 3),
    ]


def test_a_folder_line_that_is_not_numbers_is_an_error_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mis-reading a count could make a folder full of notes look empty."""
    _stub_osascript(monkeypatch, stdout="Quip\x1ffolder-1\x1fmany\x1f2\x1e")
    with pytest.raises(NotesError, match="could not read the folder list"):
        _runner().top_level_folders("iCloud")


def test_a_short_folder_record_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_osascript(monkeypatch, stdout="truncated\x1frecord\x1e")
    assert _runner().top_level_folders("iCloud") == []


def test_delete_folder_and_note_pass_the_id_through_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ids go over the process boundary as arguments, never interpolated."""
    seen = _stub_osascript(monkeypatch)
    runner = _runner()
    runner.delete_folder("x-coredata://S/ICFolder/p1")
    runner.delete_note("x-coredata://S/ICNote/p2")

    assert seen[0][-1] == "x-coredata://S/ICFolder/p1"
    assert seen[1][-1] == "x-coredata://S/ICNote/p2"
    assert all("x-coredata" not in call[2] for call in seen), "id must not reach the script text"


def test_the_delete_scripts_address_folders_and_notes_by_id() -> None:
    from quip2md import notes_prune

    assert "delete folder id (item 1 of argv)" in notes_prune._AS_DELETE_FOLDER
    assert "delete note id (item 1 of argv)" in notes_prune._AS_DELETE_NOTE
    # Top-level only: `folders of account` is flat, so the container must match.
    assert "id of container of f) is accId" in notes_prune._AS_TOP_LEVEL_FOLDERS


# --- The run lock -----------------------------------------------------------


def test_a_second_run_cannot_prune_while_one_is_working(tmp_path: Path) -> None:
    """Two concurrent runs would each act on a state file the other is changing."""
    from quip2md.notes_import import notes_run_lock

    _write_state(tmp_path, {"T1": _entry("live-1", superseded=["old-1"])})
    runner = FakePruneRunner()

    with notes_run_lock(tmp_path / ".quip2md"):
        with pytest.raises(NotesError, match="another quip2md run"):
            prune_notes(runner, _config(tmp_path), superseded=True, apply=True)

    assert runner.deleted_notes == [], "nothing may be deleted while locked out"


def test_a_plan_needs_no_lock(tmp_path: Path) -> None:
    """Reading is always safe, so a dry run must never be blocked."""
    from quip2md.notes_import import notes_run_lock

    _write_state(tmp_path, {"T1": _entry("live-1", superseded=["old-1"])})
    with notes_run_lock(tmp_path / ".quip2md"):
        report = prune_notes(FakePruneRunner(), _config(tmp_path), superseded=True, apply=False)
    assert report.notes_deleted == 1


def test_the_lock_is_released_when_the_run_ends(tmp_path: Path) -> None:
    from quip2md.notes_import import notes_run_lock

    with notes_run_lock(tmp_path / ".quip2md"):
        pass
    with notes_run_lock(tmp_path / ".quip2md"):
        pass  # a second acquisition must succeed


def test_the_lock_is_released_even_when_the_run_raises(tmp_path: Path) -> None:
    from quip2md.notes_import import notes_run_lock

    with pytest.raises(RuntimeError), notes_run_lock(tmp_path / ".quip2md"):
        raise RuntimeError("boom")
    with notes_run_lock(tmp_path / ".quip2md"):
        pass
