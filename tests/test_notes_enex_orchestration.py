"""Orchestration, state and runner-parsing coverage for the `.enex` route.

`tests/test_notes_enex.py` covers the happy paths. This module covers what
happens when the run goes sideways -- a render that raises, a landing folder
that never appears, notes that match nothing, two documents claiming the same
Quip URL -- plus the parts of `EnexNotesRunner` that turn AppleScript's
delimiter-separated stdout back into objects.

No test here reaches the real Notes app: the orchestration tests drive a
hand-written fake runner, and the `EnexNotesRunner` tests replace its single
`osascript` chokepoint (`_run`) with a canned-stdout stub, so nothing is ever
executed. `tests/conftest.py`'s guard stays armed throughout.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quip2md import notes_enex
from quip2md.config import Config
from quip2md.enex import NoteEnml
from quip2md.notes_enex import (
    EnexImportReport,
    EnexNotesRunner,
    ImportedNote,
    _extract_quip_url,
    render_sources,
    run_enex_import,
)
from quip2md.notes_import import NotesError, NotesStateError, scan_source

# --- Fixtures ---------------------------------------------------------------


def _config(tmp_path: Path, *, dry_run: bool = False, force: bool = False) -> Config:
    return Config(
        token="",
        output_dir=tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=dry_run,
        verbose=False,
        include_chats=False,
        force=force,
    )


def _write_doc(
    root: Path, relative: str, *, quip_id: str, url: str, title: str, body: str = "body\n"
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f'quip_id: "{quip_id}"\n'
        f'quip_url: "{url}"\n'
        f'title: "{title}"\n'
        'created: "2020-01-01T00:00:00Z"\n'
        'updated: "2020-01-02T00:00:00Z"\n'
        "---\n\n" + body,
        encoding="utf-8",
    )
    return path


@dataclass
class FakeEnexRunner:
    """Records every call and fabricates a landing folder once the file opens."""

    landing_notes: list[ImportedNote] = field(default_factory=list)
    folders_before: frozenset[str] = frozenset({"Notes"})
    landing_name: str = "Imported Notes 1"
    #: How many `folder_names()` polls to answer with "nothing new yet".
    polls_before_landing: int = 0
    opened: list[Path] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    created_folders: list[tuple[str, ...]] = field(default_factory=list)
    folder_name_calls: int = 0
    folder_reads: int = 0
    _opened_yet: bool = False

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def folder_names(self, account: str) -> frozenset[str]:
        self.folder_name_calls += 1
        if self._opened_yet and self.folder_name_calls > self.polls_before_landing + 1:
            return self.folders_before | {self.landing_name}
        return self.folders_before

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        self.created_folders.append(tuple(path))
        return "folder:" + "/".join(path)

    def folder_id_by_name(self, account: str, name: str) -> str:
        return f"folder:{name}"

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        self.folder_reads += 1
        return list(self.landing_notes)

    def move_note(self, note_id: str, folder_id: str) -> None:
        self.moved.append((note_id, folder_id))

    def open_enex(self, path: Path) -> None:
        self.opened.append(path)
        self._opened_yet = True


@pytest.fixture(autouse=True)
def _instant_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every wait in `notes_enex` instantaneous but still finite.

    `time.sleep` advances a fake `time.monotonic` instead of blocking, so the
    poll loops take their real number of iterations (which some tests assert
    on) and still reach their deadlines -- no test sleeps, and none can spin
    forever waiting for a fake that never delivers.
    """
    now = 0.0

    def fake_sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(notes_enex.time, "monotonic", lambda: now)
    monkeypatch.setattr(notes_enex.time, "sleep", fake_sleep)


def _provenance(url: str) -> str:
    return f"<div>Source: <u>{url}</u></div><div>body</div>"


def _state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text(encoding="utf-8"))


# --- URL extraction ---------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<div>Source: https://quip.com/THREAD0013</div>", "https://quip.com/THREAD0013"),
        (
            "<div>Source: https://example.quip.com/THREAD0013</div>",
            "https://example.quip.com/THREAD0013",
        ),
        ("<div>Source: HTTPS://QUIP.COM/THREAD0013</div>", "HTTPS://QUIP.COM/THREAD0013"),
        ("<div>Source: http://quip.com/abc-DEF_9</div>", "http://quip.com/abc-DEF_9"),
        # Trailing punctuation must not be swallowed into the id.
        ("<div>Source: https://quip.com/THREAD0013.</div>", "https://quip.com/THREAD0013"),
        # A second URL later in the body never wins over the provenance line.
        (
            "<div>Source: <u>https://quip.com/THREAD0013</u></div>"
            "<div>https://quip.com/THREAD0099</div>",
            "https://quip.com/THREAD0013",
        ),
        # A document that links to another export, with no provenance line of
        # its own, must match nothing rather than steal that document's source.
        ("<div>see also <u>https://quip.com/THREAD0099</u></div>", None),
        (
            "<div><h1>Title</h1></div><div>Related: https://quip.com/THREAD0099</div>",
            None,
        ),
        ("<div>Source: https://example.com/THREAD0013</div>", None),
        ("<div>Source: no link at all</div><div>https://quip.com/THREAD0099</div>", None),
        ("", None),
    ],
)
def test_extract_quip_url_handles_awkward_bodies(body: str, expected: str | None) -> None:
    assert _extract_quip_url(body) == expected


def test_extract_quip_url_reads_the_provenance_line_wherever_it_sits() -> None:
    body = (
        "<div><h1>A</h1></div>"
        "<div>Source: <u>https://quip.com/THREAD0013</u><br></div>"
        "<div>and a link to <u>https://quip.com/THREAD0099</u></div>"
    )
    assert _extract_quip_url(body) == "https://quip.com/THREAD0013"


# --- Rendering failures -----------------------------------------------------


def test_a_render_failure_is_isolated_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    _write_doc(source, "B.md", quip_id="THREAD0014", url="https://quip.com/THREAD0014", title="B")

    real = notes_enex.markdown_to_enml

    def flaky(**kwargs: Any) -> NoteEnml:
        if kwargs["title"] == "A":
            raise ValueError("simulated render failure")
        return real(**kwargs)

    monkeypatch.setattr(notes_enex, "markdown_to_enml", flaky)

    report = EnexImportReport()
    notes = render_sources(scan_source(source), report)

    assert [note.title for _source, note in notes] == ["B"]
    assert [source.key for source, _note in notes] == ["THREAD0014"]
    assert report.failed == [
        ("THREAD0013", "conversion failed: ValueError: simulated render failure")
    ]


def test_render_sources_accumulates_every_counter(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(
        source,
        "A.md",
        quip_id="THREAD0013",
        url="https://quip.com/THREAD0013",
        title="A",
        body="- [x] done\n- [ ] todo\n  - [ ] child\n\n[link](https://example.com/p)\n\n---\n",
    )
    report = EnexImportReport()
    render_sources(scan_source(source), report)

    assert report.checklist_items == 3
    assert report.checklist_checked == 1
    assert report.links == 2  # the inline link plus the provenance line
    assert report.docs_needing_indent == 1
    assert report.indent_levels == 1
    assert report.warnings == 1  # the horizontal rule


# --- Landing folder ---------------------------------------------------------


def test_the_run_waits_for_a_landing_folder_that_appears_late(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    runner = FakeEnexRunner(
        polls_before_landing=2,
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))],
    )
    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )

    assert report.moved == 1
    assert runner.folder_name_calls > 2


def test_a_landing_folder_that_never_appears_fails_with_a_clear_message(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    class NeverLands(FakeEnexRunner):
        def folder_names(self, account: str) -> frozenset[str]:
            return self.folders_before

    with pytest.raises(NotesError, match="timed out waiting"):
        run_enex_import(
            NeverLands(),
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=False,
            timeout_seconds=0.05,
        )


def test_the_newest_landing_folder_wins_when_several_appear(tmp_path: Path) -> None:
    """ "Imported Notes 10" is newer than "Imported Notes 9" despite sorting first."""
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    class ManyFolders(FakeEnexRunner):
        chosen: str = ""

        def folder_names(self, account: str) -> frozenset[str]:
            if self._opened_yet:
                return self.folders_before | {
                    "Imported Notes",
                    "Imported Notes 9",
                    "Imported Notes 10",
                    "Some Other New Folder",
                }
            return self.folders_before

        def folder_id_by_name(self, account: str, name: str) -> str:
            self.chosen = name
            return f"folder:{name}"

    runner = ManyFolders()
    run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )
    assert runner.chosen == "Imported Notes 10"


def test_confirm_true_logs_the_click_instruction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    with caplog.at_level("WARNING", logger="quip2md.notes_enex"):
        run_enex_import(
            FakeEnexRunner(),
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=True,
        )
    assert "Click 'Import'" in caplog.text


# --- Matching ---------------------------------------------------------------


def test_zero_matches_leaves_every_note_in_the_landing_folder(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    runner = FakeEnexRunner(
        landing_notes=[
            ImportedNote("id-1", "Stray One", "<div>nothing useful</div>"),
            ImportedNote("id-2", "Stray Two", _provenance("https://quip.com/THREAD0099")),
        ]
    )
    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )

    assert report.imported == 2
    assert report.moved == 0
    assert report.unmatched == ["Stray One", "Stray Two"]
    assert runner.moved == []
    assert _state(tmp_path) == {}


def test_a_partial_match_files_what_it_can_and_reports_the_rest(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    _write_doc(source, "B.md", quip_id="THREAD0014", url="https://quip.com/THREAD0014", title="B")

    runner = FakeEnexRunner(
        landing_notes=[
            ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013")),
            ImportedNote("id-2", "Stray", "<div>no provenance</div>"),
        ]
    )
    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )

    assert report.moved == 1
    assert report.unmatched == ["Stray"]
    assert set(_state(tmp_path)) == {"THREAD0013"}


def test_two_documents_sharing_a_quip_url_collapse_to_the_last_one_scanned(
    tmp_path: Path,
) -> None:
    """A duplicated `quip_url` is ambiguous; the URL index keeps one source.

    Pinned deliberately: it decides which folder a duplicate lands in, and
    scan order is alphabetical by path, so the *later* file wins.
    """
    source = tmp_path / "export"
    _write_doc(
        source, "Alpha/A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A"
    )
    _write_doc(
        source, "Beta/B.md", quip_id="THREAD0014", url="https://quip.com/THREAD0013", title="B"
    )

    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
    )
    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )

    assert report.documents == 2
    assert report.moved == 1
    assert runner.moved == [("id-1", "folder:Quip/Beta")]
    assert set(_state(tmp_path)) == {"THREAD0014"}


def test_only_accepts_a_path_key_for_a_file_without_frontmatter(tmp_path: Path) -> None:
    source = tmp_path / "export"
    (source / "Loose").mkdir(parents=True)
    (source / "Loose" / "Hand Written.md").write_text("just a body\n", encoding="utf-8")
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    report = run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path, dry_run=True),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        only=["path:Loose/Hand Written.md"],
    )

    assert report.documents == 1
    assert "<title>Hand Written</title>" in (tmp_path / "o.enex").read_text(encoding="utf-8")


def test_only_matching_nothing_writes_no_archive_at_all(tmp_path: Path) -> None:
    """With nothing to import, a dry run writes nothing -- as a real run does.

    This used to write an archive holding zero notes. A dry run now takes the
    same "nothing pending, so no file" exit as a real run, which is the whole
    point of the mode: it should report what a real run would do.
    """
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    report = run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path, dry_run=True),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        only=["THREAD0099"],
    )

    assert report.documents == 0
    assert report.enex_path == ""
    assert not (tmp_path / "o.enex").exists()


# --- Guards and state -------------------------------------------------------


def test_a_real_run_without_a_runner_refuses_rather_than_half_importing(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    with pytest.raises(NotesError, match="runner is required"):
        run_enex_import(
            None, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
        )


def test_a_dry_run_needs_no_runner_at_all(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    report = run_enex_import(
        None,
        _config(tmp_path, dry_run=True),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
    )
    assert report.documents == 1
    assert report.enex_bytes > 0


def test_the_archive_defaults_to_the_state_directory(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    report = run_enex_import(None, _config(tmp_path, dry_run=True), source_dir=source)
    assert report.enex_path == str(tmp_path / ".quip2md" / "quip2md.enex")
    assert (tmp_path / ".quip2md" / "quip2md.enex").is_file()


def test_a_corrupted_state_file_stops_the_run(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    state_path = tmp_path / ".quip2md" / "notes_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(NotesStateError):
        run_enex_import(
            FakeEnexRunner(),
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=False,
        )


def test_state_written_before_a_crash_survives_it(tmp_path: Path) -> None:
    """The `finally: state.flush()` is what makes an interrupted run resumable."""
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    _write_doc(source, "B.md", quip_id="THREAD0014", url="https://quip.com/THREAD0014", title="B")

    class CrashOnSecond(FakeEnexRunner):
        def move_note(self, note_id: str, folder_id: str) -> None:
            if note_id == "id-2":
                raise KeyboardInterrupt
            super().move_note(note_id, folder_id)

    runner = CrashOnSecond(
        landing_notes=[
            ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013")),
            ImportedNote("id-2", "B", _provenance("https://quip.com/THREAD0014")),
        ]
    )

    with pytest.raises(KeyboardInterrupt):
        run_enex_import(
            runner,
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=False,
        )

    assert set(_state(tmp_path)) == {"THREAD0013"}


def test_a_second_run_records_the_same_content_hash(tmp_path: Path) -> None:
    """State is a pure function of the source, so re-running never churns it."""
    source = tmp_path / "export"
    _write_doc(
        source,
        "A.md",
        quip_id="THREAD0013",
        url="https://quip.com/THREAD0013",
        title="A",
        body="- [x] one\n",
    )

    def one_run() -> dict[str, Any]:
        runner = FakeEnexRunner(
            landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
        )
        run_enex_import(
            runner,
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=False,
        )
        return _state(tmp_path)

    first = one_run()
    second = one_run()
    assert first["THREAD0013"]["content_hash"] == second["THREAD0013"]["content_hash"]
    assert first["THREAD0013"]["folder"] == second["THREAD0013"]["folder"]


def test_an_earlier_runs_state_is_preserved_when_nothing_new_imports(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    run_enex_import(
        FakeEnexRunner(
            landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
        ),
        _config(tmp_path),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        confirm=False,
    )
    # A second run whose landing folder comes back empty must not erase what
    # the first run recorded.
    run_enex_import(
        FakeEnexRunner(landing_notes=[]),
        _config(tmp_path),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        confirm=False,
    )

    assert _state(tmp_path)["THREAD0013"]["note_id"] == "id-1"


def _run_once(
    tmp_path: Path,
    source: Path,
    *,
    note_id: str = "id-1",
    url: str = "https://quip.com/THREAD0013",
    force: bool = False,
) -> tuple[EnexImportReport, FakeEnexRunner]:
    runner = FakeEnexRunner(landing_notes=[ImportedNote(note_id, "A", _provenance(url))])
    report = run_enex_import(
        runner,
        _config(tmp_path, force=force),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        confirm=False,
    )
    return report, runner


def test_a_second_run_skips_a_document_that_has_not_changed(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    _run_once(tmp_path, source)
    second, runner = _run_once(tmp_path, source, note_id="id-2")

    assert second.documents == 0
    assert second.skipped_unchanged == 1
    assert second.imported == 0
    assert second.moved == 0
    # Nothing to import means nothing to open, and no archive to open with.
    assert runner.opened == []
    assert second.enex_path == ""
    # The first run's note keeps its place in the state file.
    assert _state(tmp_path)["THREAD0013"]["note_id"] == "id-1"


def test_a_second_run_writes_no_archive_when_everything_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    archive = tmp_path / "o.enex"

    _run_once(tmp_path, source)
    first_bytes = archive.read_bytes()
    archive.unlink()

    _run_once(tmp_path, source, note_id="id-2")

    assert b"<note>" in first_bytes
    assert not archive.exists()


def test_a_changed_document_is_reimported_and_supersedes_the_old_note(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    _run_once(tmp_path, source)
    _write_doc(
        source,
        "A.md",
        quip_id="THREAD0013",
        url="https://quip.com/THREAD0013",
        title="A",
        body="a different body\n",
    )
    second, runner = _run_once(tmp_path, source, note_id="id-2")

    assert second.documents == 1
    assert second.skipped_unchanged == 0
    assert second.moved == 1
    assert second.superseded == 1
    assert runner.opened == [tmp_path / "o.enex"]

    entry = _state(tmp_path)["THREAD0013"]
    assert entry["note_id"] == "id-2"
    assert entry["superseded_note_ids"] == ["id-1"]


def test_superseding_warns_that_the_old_copies_are_still_in_notes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    _run_once(tmp_path, source)
    with caplog.at_level("WARNING", logger="quip2md.notes_enex"):
        _run_once(tmp_path, source, note_id="id-2", force=True)

    assert "must be deleted by hand" in caplog.text


def test_force_reimports_an_unchanged_document(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    _run_once(tmp_path, source)
    second, runner = _run_once(tmp_path, source, note_id="id-2", force=True)

    assert second.documents == 1
    assert second.skipped_unchanged == 0
    assert second.moved == 1
    assert second.superseded == 1
    assert runner.opened == [tmp_path / "o.enex"]

    entry = _state(tmp_path)["THREAD0013"]
    assert entry["note_id"] == "id-2"
    assert entry["superseded_note_ids"] == ["id-1"]


def test_superseded_ids_accumulate_across_runs(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    _run_once(tmp_path, source)
    _run_once(tmp_path, source, note_id="id-2", force=True)
    _run_once(tmp_path, source, note_id="id-3", force=True)

    entry = _state(tmp_path)["THREAD0013"]
    assert entry["note_id"] == "id-3"
    assert entry["superseded_note_ids"] == ["id-1", "id-2"]


def test_state_written_without_the_superseded_key_still_loads(tmp_path: Path) -> None:
    """Entries the AppleScript writer produced predate the new field."""
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    state_path = tmp_path / ".quip2md" / "notes_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "THREAD0013": {
                    "note_id": "old-1",
                    "folder": "Quip",
                    "content_hash": "stale",
                    "imported_at": "2024-01-01T00:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    report, _runner = _run_once(tmp_path, source, note_id="id-2")

    assert report.moved == 1
    assert report.superseded == 1
    assert _state(tmp_path)["THREAD0013"]["superseded_note_ids"] == ["old-1"]


def test_the_landing_folder_is_polled_until_its_note_count_settles(tmp_path: Path) -> None:
    """Notes fills the folder over time; reading it once loses the tail."""
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    _write_doc(source, "B.md", quip_id="THREAD0014", url="https://quip.com/THREAD0014", title="B")

    arriving = [
        [],
        [ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))],
        [
            ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013")),
            ImportedNote("id-2", "B", _provenance("https://quip.com/THREAD0014")),
        ],
    ]

    class FillsUpSlowly(FakeEnexRunner):
        reads: int = 0

        def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
            self.reads += 1
            return list(arriving[min(self.reads - 1, len(arriving) - 1)])

    runner = FillsUpSlowly()
    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )

    assert report.imported == 2
    assert report.moved == 2
    # Three reads to see both notes, plus one confirming the count held steady.
    assert runner.reads == 4


def test_an_already_imported_note_is_still_offered_to_the_indent_pass(
    tmp_path: Path,
) -> None:
    """Indentation must stay reachable after the import that created the note.

    Notes flattens nested checklists on import and the repair is a separate,
    permission-gated pass. If only freshly filed notes were offered to it, the
    only way to indent an existing note would be to re-import it -- which the
    skip logic exists to prevent.
    """
    source = tmp_path / "export"
    _write_doc(
        source,
        "A.md",
        quip_id="THREAD0013",
        url="https://quip.com/THREAD0013",
        title="A",
        body="- [ ] parent\n  - [ ] child\n",
    )
    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
    )
    first = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )
    assert [target[0] for target in first.indent_targets] == ["id-1"]

    second = run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path),
        source_dir=source,
        enex_path=tmp_path / "again.enex",
        confirm=False,
    )

    assert second.skipped_unchanged == 1
    assert second.documents == 0
    assert [target[0] for target in second.indent_targets] == ["id-1"]
    assert [item.depth for item in second.indent_targets[0][2]] == [0, 1]


def test_a_flat_skipped_note_is_not_offered_to_the_indent_pass(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(
        source,
        "A.md",
        quip_id="THREAD0013",
        url="https://quip.com/THREAD0013",
        title="A",
        body="- [ ] one\n- [ ] two\n",
    )
    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
    )
    run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "o.enex", confirm=False
    )
    again = run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path),
        source_dir=source,
        enex_path=tmp_path / "again.enex",
        confirm=False,
    )

    assert again.skipped_unchanged == 1
    assert again.indent_targets == []


def test_adopting_a_landing_folder_files_its_notes_without_importing(
    tmp_path: Path,
) -> None:
    """Resuming a half-finished import must not import a second copy.

    This is the recovery path for a run that died between the confirmation
    click and the read-back: the notes are already in Notes, so re-running the
    import would duplicate every one of them.
    """
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
    )
    report = run_enex_import(
        runner,
        _config(tmp_path),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        confirm=False,
        adopt_landing="Imported Notes 2",
    )

    assert runner.opened == [], "adopting must not hand a file to Notes"
    assert runner.folder_reads == 1, "adopted notes are settled; polling re-reads every body"
    assert not (tmp_path / "o.enex").exists(), "adopting must not write an archive"
    assert report.landing_folder == "Imported Notes 2"
    assert report.moved == 1
    assert runner.moved == [("id-1", "folder:Quip")]

    # The note is now recorded, so an ordinary re-run has nothing left to do.
    again = run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path),
        source_dir=source,
        enex_path=tmp_path / "again.enex",
        confirm=False,
    )
    assert again.documents == 0
    assert again.skipped_unchanged == 1


def test_adopting_a_folder_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")

    class NoSuchFolder(FakeEnexRunner):
        def folder_id_by_name(self, account: str, name: str) -> str:
            return ""

    with pytest.raises(NotesError, match="no folder named 'Imported Notes 9'"):
        run_enex_import(
            NoSuchFolder(),
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=False,
            adopt_landing="Imported Notes 9",
        )


def test_a_landing_folder_that_stays_short_proceeds_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    _write_doc(source, "B.md", quip_id="THREAD0014", url="https://quip.com/THREAD0014", title="B")

    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
    )
    with caplog.at_level("WARNING", logger="quip2md.notes_enex"):
        report = run_enex_import(
            runner,
            _config(tmp_path),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
            confirm=False,
            timeout_seconds=30.0,
        )

    assert report.imported == 1
    assert report.moved == 1
    assert "holds 1 of the 2 note(s)" in caplog.text
    assert "Imported Notes 1" in caplog.text


# --- EnexNotesRunner --------------------------------------------------------


def test_the_runner_refuses_to_construct_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notes_enex.sys, "platform", "linux")
    with pytest.raises(NotesError, match="requires macOS"):
        EnexNotesRunner()


@pytest.fixture
def scripted_runner(monkeypatch: pytest.MonkeyPatch) -> tuple[EnexNotesRunner, list[Any]]:
    """An `EnexNotesRunner` whose only `osascript` call site returns canned text.

    `_run` is the single process boundary in the class; stubbing it means no
    AppleScript is ever assembled, let alone executed.
    """
    runner = EnexNotesRunner()
    calls: list[Any] = []
    replies: list[str] = []

    def fake_run(
        self: EnexNotesRunner,
        script: str,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str:
        calls.append(list(argv))
        return replies.pop(0) if replies else ""

    monkeypatch.setattr(EnexNotesRunner, "_run", fake_run)
    return runner, replies


def test_resolve_account_rejects_a_local_account_request() -> None:
    monkeyed = EnexNotesRunner()
    with pytest.raises(NotesError, match="cannot target a specific account"):
        monkeyed.resolve_account(local=True)


def test_folder_names_splits_on_the_record_separator(
    scripted_runner: tuple[EnexNotesRunner, list[Any]],
) -> None:
    runner, replies = scripted_runner
    replies.append("Notes\x1eImported Notes 1\x1e   \x1e")
    assert runner.folder_names("iCloud") == frozenset({"Notes", "Imported Notes 1"})


def test_folder_id_by_name_trims_the_applescript_newline(
    scripted_runner: tuple[EnexNotesRunner, list[Any]],
) -> None:
    runner, replies = scripted_runner
    replies.append("x-coredata://ABC/ICFolder/p1\n")
    assert runner.folder_id_by_name("iCloud", "Quip") == "x-coredata://ABC/ICFolder/p1"


def test_notes_in_folder_parses_records_and_keeps_separators_inside_bodies(
    scripted_runner: tuple[EnexNotesRunner, list[Any]],
) -> None:
    runner, replies = scripted_runner
    # First reply is the id sweep, second the batched head read.
    replies.append("id-1\x1eid-2\x1e")
    replies.append(
        "id-1\x1fA\x1f<div>body\x1fwith a separator</div>\x1e"
        "  \x1e"
        "malformed-record-without-fields\x1e"
        "id-2\x1fB\x1f<div>plain</div>\x1e"
    )
    assert runner.notes_in_folder("folder:x") == [
        ImportedNote("id-1", "A", "<div>body\x1fwith a separator</div>"),
        ImportedNote("id-2", "B", "<div>plain</div>"),
    ]


def test_notes_in_folder_reads_heads_in_batches(
    scripted_runner: tuple[EnexNotesRunner, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole corpus is never read in one osascript call.

    A single call carrying 490 full bodies overran the osascript timeout on a
    real import and left every note unfiled, so the ids are swept first and the
    heads are fetched in bounded batches.
    """
    runner, replies = scripted_runner
    seen: list[list[str]] = []

    def recording_run(
        self: EnexNotesRunner,
        script: str,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str:
        seen.append(list(argv))
        if len(seen) == 1:
            return "".join(f"id-{n}\x1e" for n in range(60))
        return "".join(f"{i}\x1fN\x1f<div>b</div>\x1e" for i in argv[1:])

    monkeypatch.setattr(EnexNotesRunner, "_run", recording_run)
    notes = runner.notes_in_folder("folder:x")

    assert len(notes) == 60
    head_calls = seen[1:]
    assert len(head_calls) == 3, "60 notes must span more than one batch"
    assert all(len(call) - 1 <= 25 for call in head_calls)
    # Every id is fetched exactly once, in order.
    fetched = [note_id for call in head_calls for note_id in call[1:]]
    assert fetched == [f"id-{n}" for n in range(60)]


def test_get_or_create_folder_caches_each_ancestor(
    scripted_runner: tuple[EnexNotesRunner, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _replies = scripted_runner
    seen: list[list[str]] = []

    def fake_run(self: EnexNotesRunner, script: str, argv: Sequence[str]) -> str:
        seen.append(list(argv))
        return f"folder:{argv[3]}\n"

    monkeypatch.setattr(EnexNotesRunner, "_run", fake_run)

    assert runner.get_or_create_folder("iCloud", ["Quip", "Team"]) == "folder:Team"
    # "Quip" is now cached; only "Projects" needs a new call.
    assert runner.get_or_create_folder("iCloud", ["Quip", "Projects"]) == "folder:Projects"

    assert [argv[3] for argv in seen] == ["Quip", "Team", "Projects"]
    # The nested call is told it has a parent; the root call is not.
    assert [argv[1] for argv in seen] == ["0", "1", "1"]
    assert seen[1][2] == "folder:Quip"


def test_move_note_passes_both_ids_through_argv(
    scripted_runner: tuple[EnexNotesRunner, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _replies = scripted_runner
    seen: list[list[str]] = []
    monkeypatch.setattr(
        EnexNotesRunner,
        "_run",
        lambda self, script, argv: seen.append(list(argv)) or "",
    )
    runner.move_note("note-1", "folder-2")
    assert seen == [["note-1", "folder-2"]]


# --- The `osascript` boundary ------------------------------------------------


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raises: BaseException | None = None,
) -> list[list[str]]:
    """Replace `subprocess.run` with a stub that cannot execute anything.

    Stronger than `conftest.py`'s guard for the duration of these tests: the
    stub has no path to a real process at all. It is what lets the argv this
    module builds be asserted without ever handing it to the operating system.
    """
    seen: list[list[str]] = []

    def fake_run(command: Sequence[str], *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        seen.append(list(command))
        if raises is not None:
            raise raises
        return FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(notes_enex.subprocess, "run", fake_run)
    return seen


def test_resolve_account_returns_the_default_account_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(monkeypatch, stdout="iCloud\n")
    assert EnexNotesRunner().resolve_account(local=False) == "iCloud"


def test_content_is_passed_through_argv_never_interpolated_into_the_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_subprocess(monkeypatch, stdout="")
    EnexNotesRunner().folder_id_by_name("iCloud", 'Quip" & (do shell script "id")')

    command = seen[0]
    assert command[0] == "osascript"
    assert command[3] == "--"
    assert command[4:] == ["iCloud", 'Quip" & (do shell script "id")']


def test_a_nonzero_exit_becomes_a_notes_error_carrying_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(monkeypatch, returncode=1, stderr="  execution error: -1728  ")
    with pytest.raises(NotesError) as excinfo:
        EnexNotesRunner().folder_names("iCloud")
    assert "status 1" in str(excinfo.value)
    assert excinfo.value.stderr == "execution error: -1728"


def test_a_timeout_becomes_a_notes_error_naming_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(
        monkeypatch, raises=subprocess.TimeoutExpired(cmd=["osascript"], timeout=120.0)
    )
    with pytest.raises(NotesError, match="timed out after 120.0s"):
        EnexNotesRunner().folder_names("iCloud")


def test_the_first_call_gets_a_longer_budget_than_the_ones_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first `osascript` call may sit behind a one-time permission prompt."""
    timeouts: list[float] = []

    def fake_run(command: Sequence[str], *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        timeouts.append(kwargs["timeout"])
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(notes_enex.subprocess, "run", fake_run)

    runner = EnexNotesRunner()
    runner.folder_names("iCloud")
    runner.folder_names("iCloud")

    assert timeouts == [120.0, 60.0]


def test_a_timed_out_first_call_still_shortens_the_next_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def fake_run(command: Sequence[str], *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=kwargs["timeout"])
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(notes_enex.subprocess, "run", fake_run)

    runner = EnexNotesRunner()
    with pytest.raises(NotesError):
        runner.folder_names("iCloud")
    runner.folder_names("iCloud")

    assert calls == [120.0, 60.0]


def test_open_enex_hands_the_file_to_notes_own_importer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _stub_subprocess(monkeypatch, returncode=0)
    archive = tmp_path / "quip2md.enex"
    EnexNotesRunner().open_enex(archive)
    assert seen == [["open", "-a", "Notes", str(archive)]]


def test_a_failed_open_is_reported_rather_than_leaving_the_run_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_subprocess(monkeypatch, returncode=1, stderr="Unable to find application")
    with pytest.raises(NotesError, match="could not hand the .enex file to Notes"):
        EnexNotesRunner().open_enex(tmp_path / "quip2md.enex")


def test_a_source_directory_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    """A typo in --source must not read as a clean run over zero documents.

    That is the one outcome a migration must never fake: the report would say
    success, the archive would be empty, and nothing would say why.
    """
    with pytest.raises(NotesError, match="no such source directory"):
        run_enex_import(
            None,
            _config(tmp_path, dry_run=True),
            source_dir=tmp_path / "not-here",
            enex_path=tmp_path / "out.enex",
        )


def test_an_existing_but_empty_source_directory_is_allowed(tmp_path: Path) -> None:
    """An export tree with nothing in it is odd but not a mistake to refuse."""
    empty = tmp_path / "export"
    empty.mkdir()
    report = run_enex_import(
        None, _config(tmp_path, dry_run=True), source_dir=empty, enex_path=tmp_path / "out.enex"
    )
    assert report.documents == 0


def test_a_second_import_cannot_run_while_one_is_working(tmp_path: Path) -> None:
    """The worst mistake this tool can make is importing the corpus twice.

    Two concurrent runs each read the state file, each conclude the same
    documents are missing, and each import them.
    """
    from quip2md.notes_import import notes_run_lock

    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", _provenance("https://quip.com/THREAD0013"))]
    )

    with notes_run_lock(tmp_path / ".quip2md"):
        with pytest.raises(NotesError, match="another quip2md run"):
            run_enex_import(
                runner,
                _config(tmp_path),
                source_dir=source,
                enex_path=tmp_path / "o.enex",
                confirm=False,
            )

    assert runner.opened == [], "nothing may be handed to Notes while locked out"
    assert runner.moved == []


def test_a_dry_run_is_never_blocked_by_the_lock(tmp_path: Path) -> None:
    from quip2md.notes_import import notes_run_lock

    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="THREAD0013", url="https://quip.com/THREAD0013", title="A")
    with notes_run_lock(tmp_path / ".quip2md"):
        report = run_enex_import(
            None,
            _config(tmp_path, dry_run=True),
            source_dir=source,
            enex_path=tmp_path / "o.enex",
        )
    assert report.documents == 1
