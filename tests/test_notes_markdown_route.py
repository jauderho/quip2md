"""The Markdown import route: what it stages, how it routes, how it files.

Notes 4.13 imports Markdown with natively nested checklists, and takes a
note's creation and modification dates from the file's birth time and mtime.
Images are the exception -- they arrive as plain links -- so a document with
an image keeps the archive route. No test here reaches the real Notes app.
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from quip2md import cli, notes_enex
from quip2md.config import Config
from quip2md.notes_enex import (
    MARKDOWN_STAGING_DIRNAME,
    ImportedNote,
    markdown_import_available,
    markdown_note_text,
    routes_through_markdown,
    run_enex_import,
)
from quip2md.notes_import import NoteSource, scan_source


def _config(tmp_path: Path) -> Config:
    return Config(
        token="",
        output_dir=tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=False,
        include_chats=False,
        force=False,
    )


def _write_doc(root: Path, relative: str, *, quip_id: str, title: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f'quip_id: "{quip_id}"\n'
        f'quip_url: "https://quip.com/{quip_id}"\n'
        f'title: "{title}"\n'
        'created: "2015-03-14T22:30:00Z"\n'
        'updated: "2021-06-01T16:00:00Z"\n'
        "---\n\n" + body,
        encoding="utf-8",
    )


def _source(title: str, body: str, url: str | None = "https://quip.com/THREAD0001") -> NoteSource:
    return NoteSource(
        key="THREAD0001",
        md_path=Path("doc.md"),
        relative_path="doc.md",
        folder_path=("Private",),
        title=title,
        quip_url=url,
        body_markdown=body,
        keyed_by_path=False,
        created="2015-03-14T22:30:00Z",
        updated="2021-06-01T16:00:00Z",
    )


# --- Staged Markdown ----------------------------------------------------------


def test_the_note_opens_with_title_then_provenance_then_body() -> None:
    text = markdown_note_text(_source("Plan", "# Plan\n\n- [ ] a\n    - [x] b\n"))
    assert text == "# Plan\n\nSource: https://quip.com/THREAD0001\n\n- [ ] a\n    - [x] b\n"


def test_an_empty_checklist_marker_takes_its_text_from_the_next_line() -> None:
    body = "- [ ] a\n  - [x]   \n    **first  \n     second**\n  - [ ]\n    - [ ] child\n"
    assert markdown_note_text(_source("T", body)).endswith(
        "- [ ] a\n  - [x] **first  \n     second**\n  - [ ]\n    - [ ] child\n"
    )


def test_a_leading_heading_is_kept_when_it_is_not_the_title() -> None:
    text = markdown_note_text(_source("Plan", "# Other\n\nbody\n"))
    assert text.endswith("Source: https://quip.com/THREAD0001\n\n# Other\n\nbody\n")


def test_an_escaped_title_heading_is_still_recognised_as_the_title() -> None:
    text = markdown_note_text(_source("a_b", "# a\\_b\n\nbody\n"))
    assert text == "# a\\_b\n\nSource: https://quip.com/THREAD0001\n\nbody\n"


def test_markdown_syntax_in_a_title_is_escaped() -> None:
    assert markdown_note_text(_source("*Q3* #1 <draft>", "x\n")).startswith(
        "# \\*Q3\\* \\#1 \\<draft\\>\n"
    )


def test_a_source_without_a_url_has_no_provenance_line() -> None:
    assert markdown_note_text(_source("T", "body\n", url=None)) == "# T\n\nbody\n"


def test_only_documents_without_images_take_the_markdown_route() -> None:
    assert routes_through_markdown(_source("T", "text [link](https://x.test)\n"))
    assert not routes_through_markdown(_source("T", "![pic](_assets/a/b.png)\n"))


def test_a_url_inside_a_table_keeps_the_archive_route() -> None:
    """Notes 4.13 empties the cell and drops every cell after it."""
    table = "| Item | URL |\n| --- | --- |\n| widget | https://example.com/p |\n"
    assert not routes_through_markdown(_source("T", table))
    assert routes_through_markdown(_source("T", "| a | b |\n| --- | --- |\n| 1 | 2 |\n"))
    assert routes_through_markdown(_source("T", "see https://example.com/p\n\n| a |\n| --- |\n"))


def test_staged_files_are_uniquely_named_and_carry_the_document_dates(tmp_path: Path) -> None:
    folder = tmp_path / "stage"
    folder.mkdir()
    (folder / "stale.md").write_text("from a previous run", encoding="utf-8")

    notes_enex._write_markdown_folder(folder, [_source("Same", "a\n"), _source("Same", "b\n")])

    assert sorted(p.name for p in folder.iterdir()) == ["Same (2).md", "Same.md"]
    stat = (folder / "Same.md").stat()
    assert stat.st_mtime == datetime(2021, 6, 1, 16, tzinfo=UTC).timestamp()
    if sys.platform == "darwin":  # birth time exists, and is what Notes reads
        assert stat.st_birthtime == datetime(2015, 3, 14, 22, 30, tzinfo=UTC).timestamp()


def test_a_file_without_dates_is_left_with_its_own(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text("x", encoding="utf-8")
    before = path.stat().st_mtime
    notes_enex._set_file_dates(path, created=None, updated="not a date")
    assert path.stat().st_mtime == before


@pytest.mark.parametrize(
    ("version", "expected"),
    [("4.13", True), ("4.14.1", True), ("5.0", True), ("4.12", False), ("", False)],
)
def test_markdown_import_needs_notes_4_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, expected: bool
) -> None:
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    monkeypatch.setattr(notes_enex, "_NOTES_INFO_PLIST", plist)
    assert markdown_import_available() is expected


def test_no_notes_app_means_no_markdown_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notes_enex, "_NOTES_INFO_PLIST", tmp_path / "missing.plist")
    assert markdown_import_available() is False


# --- Two-route import -------------------------------------------------------


@dataclass
class TwoRouteRunner:
    """Models Notes 4.13: an archive gets a fresh landing folder, a Markdown
    folder nests inside the existing one, one level down."""

    notes_by_open: list[list[ImportedNote]]
    opened: list[Path] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    child_lookups: list[tuple[str, str]] = field(default_factory=list)

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def folder_names(self, account: str) -> frozenset[str]:
        return frozenset({"Notes", "Imported Notes 1"} if self.opened else {"Notes"})

    def folder_id_by_name(self, account: str, name: str) -> str:
        return f"folder:{name}"

    def child_folder_id(self, parent_id: str, name: str) -> str:
        self.child_lookups.append((parent_id, name))
        landed = any(path.name == name for path in self.opened)
        return f"{parent_id}/{name}" if landed and parent_id.endswith("Imported Notes 1") else ""

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        return "folder:" + "/".join(path)

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        return list(self.notes_by_open[len(self.opened) - 1])

    def move_note(self, note_id: str, folder_id: str) -> None:
        self.moved.append((note_id, folder_id))

    def open_enex(self, path: Path) -> None:
        self.opened.append(path)


@pytest.fixture(autouse=True)
def _instant_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0

    def fake_sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(notes_enex.time, "monotonic", lambda: now)
    monkeypatch.setattr(notes_enex.time, "sleep", fake_sleep)


def _note(note_id: str, quip_id: str) -> ImportedNote:
    return ImportedNote(note_id, quip_id, f"<div>Source: https://quip.com/{quip_id}</div>")


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    nested = "- [ ] a\n    - [x] b\n"
    _write_doc(root, "Private/Plain.md", quip_id="THREAD0001", title="Plain", body=nested)
    _write_doc(
        root, "Private/Pic.md", quip_id="THREAD0002", title="Pic", body=nested + "![p](x.png)\n"
    )
    return root


def test_markdown_documents_and_image_documents_import_separately(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    runner = TwoRouteRunner([[_note("n-pic", "THREAD0002")], [_note("n-plain", "THREAD0001")]])

    report = run_enex_import(runner, _config(tmp_path), source_dir=root, markdown=True)

    (stage,) = (tmp_path / ".quip2md").glob(f"{MARKDOWN_STAGING_DIRNAME}-*")
    assert runner.opened == [tmp_path / ".quip2md" / "quip2md.enex", stage]
    assert [p.name for p in stage.iterdir()] == ["Plain.md"]
    assert "THREAD0001" not in (tmp_path / ".quip2md" / "quip2md.enex").read_text()
    assert runner.child_lookups == [("folder:Imported Notes 1", stage.name)]
    assert sorted(runner.moved) == [
        ("n-pic", "folder:Quip/Private"),
        ("n-plain", "folder:Quip/Private"),
    ]
    assert report.moved == 2 and report.markdown_notes == 1
    assert report.landing_folder == f"Imported Notes 1, Imported Notes 1/{stage.name}"
    # Only the archive flattens checklists, so only its note needs the pass.
    assert [target[0] for target in report.indent_targets] == ["n-pic"]
    state = json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text())
    assert {key: entry["note_id"] for key, entry in state.items()} == {
        "THREAD0001": "n-plain",
        "THREAD0002": "n-pic",
    }


def test_a_rerun_skips_both_routes_and_never_opens_notes(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    first = TwoRouteRunner([[_note("n-pic", "THREAD0002")], [_note("n-plain", "THREAD0001")]])
    run_enex_import(first, _config(tmp_path), source_dir=root, markdown=True)

    again = TwoRouteRunner([])
    report = run_enex_import(again, _config(tmp_path), source_dir=root, markdown=True)

    assert again.opened == []
    assert report.skipped_unchanged == 2
    assert [target[0] for target in report.indent_targets] == ["n-pic"]


def test_without_markdown_everything_goes_through_one_archive(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    runner = TwoRouteRunner([[_note("n-pic", "THREAD0002"), _note("n-plain", "THREAD0001")]])

    report = run_enex_import(runner, _config(tmp_path), source_dir=root)

    assert runner.opened == [tmp_path / ".quip2md" / "quip2md.enex"]
    assert not list((tmp_path / ".quip2md").glob(f"{MARKDOWN_STAGING_DIRNAME}*"))
    assert sorted(target[0] for target in report.indent_targets) == ["n-pic", "n-plain"]


def test_the_markdown_landing_folder_must_appear(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _write_doc(root, "Private/Plain.md", quip_id="THREAD0001", title="Plain", body="x\n")

    class NoChild(TwoRouteRunner):
        def child_folder_id(self, parent_id: str, name: str) -> str:
            return ""

    with pytest.raises(notes_enex.NotesError, match=MARKDOWN_STAGING_DIRNAME):
        run_enex_import(NoChild([[]]), _config(tmp_path), source_dir=root, markdown=True)


def test_a_previous_runs_staging_folder_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _write_doc(root, "Private/Plain.md", quip_id="THREAD0001", title="Plain", body="x\n")
    stale = tmp_path / ".quip2md" / f"{MARKDOWN_STAGING_DIRNAME}-20000101T000000Z"
    stale.mkdir(parents=True)
    (stale / "old.md").write_text("old", encoding="utf-8")

    config = replace(_config(tmp_path), dry_run=True)
    report = run_enex_import(None, config, source_dir=root, markdown=True)

    assert not stale.exists()
    assert Path(report.markdown_path).name.startswith(f"{MARKDOWN_STAGING_DIRNAME}-")


def test_adopting_a_nested_markdown_folder_files_its_notes(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _write_doc(root, "Private/Plain.md", quip_id="THREAD0001", title="Plain", body="x\n")
    child = f"{MARKDOWN_STAGING_DIRNAME}-20261005T000000Z"
    runner = TwoRouteRunner([[_note("n-plain", "THREAD0001")]])
    runner.opened.append(Path(child))  # the earlier run's folder already landed

    report = run_enex_import(
        runner,
        _config(tmp_path),
        source_dir=root,
        markdown=True,
        adopt_landing=f"Imported Notes 1/{child}",
    )

    assert runner.opened == [Path(child)], "adopting imports nothing"
    assert runner.moved == [("n-plain", "folder:Quip/Private")]
    assert report.moved == 1


# --- CLI ----------------------------------------------------------------------


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(runner: object, config: Config, **kwargs: Any) -> notes_enex.EnexImportReport:
        seen.update(kwargs)
        return notes_enex.EnexImportReport()

    monkeypatch.setattr(cli, "run_enex_import", fake_run)
    return seen


def test_the_cli_defaults_to_markdown_where_notes_supports_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "markdown_import_available", lambda: True)
    seen = _capture_run(monkeypatch)
    assert cli.main(["import-notes", "--dryrun"]) == 0
    assert seen["markdown"] is True


def test_the_cli_defaults_to_enex_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    seen = _capture_run(monkeypatch)
    assert cli.main(["import-notes", "--dryrun"]) == 0
    assert seen["markdown"] is False


def test_asking_for_markdown_without_support_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["import-notes", "--writer", "markdown", "--dryrun"])
    assert exc.value.code == 2
    assert "Notes 4.13" in capsys.readouterr().err


def test_local_is_rejected_with_the_markdown_writer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "markdown_import_available", lambda: True)
    with pytest.raises(SystemExit):
        cli.main(["import-notes", "--writer", "markdown", "--local"])
    assert "--writer markdown" in capsys.readouterr().err


def test_scan_source_still_feeds_the_route(tmp_path: Path) -> None:
    """The staged text is built from exactly what `scan_source` reads."""
    root = _corpus(tmp_path)
    plain = next(s for s in scan_source(root) if s.key == "THREAD0001")
    assert markdown_note_text(plain).startswith("# Plain\n\nSource: https://quip.com/THREAD0001")
    assert os.fspath(plain.md_path).endswith("Plain.md")


@dataclass
class MergingRunner(TwoRouteRunner):
    """Notes 4.13: an archive adds to an existing "Imported Notes" folder."""

    already: list[ImportedNote] = field(default_factory=list)

    def folder_names(self, account: str) -> frozenset[str]:
        return frozenset({"Notes", "Imported Notes 1"})

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        arrived = self.notes_by_open[len(self.opened) - 1] if self.opened else []
        return [*self.already, *arrived]


def test_an_archive_that_lands_in_an_existing_folder_is_found(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _write_doc(root, "Private/Pic.md", quip_id="THREAD0002", title="Pic", body="![p](x.png)\n")
    someone_elses = ImportedNote("old-1", "Unrelated", "<div>an older import</div>")
    runner = MergingRunner([[_note("n-pic", "THREAD0002")]], already=[someone_elses])

    report = run_enex_import(runner, _config(tmp_path), source_dir=root, markdown=True)

    assert runner.moved == [("n-pic", "folder:Quip/Private")]
    assert report.unmatched == [], "the note that was already there is not this run's"
    assert report.imported == 1
    assert report.landing_folder == "Imported Notes 1"


# --- Edge cases and the real runner's argv -------------------------------------


def test_a_failed_second_import_keeps_the_first_imports_notes_recorded(tmp_path: Path) -> None:
    """The archive's notes are filed and recorded even if the Markdown batch fails.

    A re-run must then skip them, not import them a second time.
    """
    root = _corpus(tmp_path)

    class MarkdownNeverLands(TwoRouteRunner):
        def child_folder_id(self, parent_id: str, name: str) -> str:
            return ""

    runner = MarkdownNeverLands([[_note("n-pic", "THREAD0002")], []])
    with pytest.raises(notes_enex.NotesError):
        run_enex_import(runner, _config(tmp_path), source_dir=root, markdown=True)

    state = json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text())
    assert list(state) == ["THREAD0002"]
    assert runner.moved == [("n-pic", "folder:Quip/Private")]


def test_a_creation_date_after_the_update_still_gives_a_valid_birth_time(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text("x", encoding="utf-8")
    notes_enex._set_file_dates(path, created="2021-01-02T00:00:00Z", updated="2020-01-01T00:00:00Z")
    stat = path.stat()
    assert stat.st_mtime == datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    if sys.platform == "darwin":
        assert stat.st_birthtime <= stat.st_mtime


def test_a_landing_folder_deleted_during_the_wait_times_out_cleanly(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _write_doc(root, "Private/Pic.md", quip_id="THREAD0002", title="Pic", body="![p](x.png)\n")

    class FolderDeleted(MergingRunner):
        def folder_names(self, account: str) -> frozenset[str]:
            return frozenset({"Notes"} if self.opened else {"Notes", "Imported Notes 1"})

    runner = FolderDeleted([[]])
    with pytest.raises(notes_enex.NotesError, match="import folder"):
        run_enex_import(runner, _config(tmp_path), source_dir=root, markdown=True)
    assert runner.moved == []


def test_the_real_runner_looks_up_a_child_folder_through_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    @dataclass
    class Done:
        returncode: int = 0
        stdout: str = "x-coredata://child\n"
        stderr: str = ""

    def fake_run(command: Sequence[str], *args: Any, **kwargs: Any) -> Done:
        seen.append(list(command))
        return Done()

    monkeypatch.setattr(notes_enex.sys, "platform", "darwin")
    monkeypatch.setattr(notes_enex.subprocess, "run", fake_run)
    runner = notes_enex.EnexNotesRunner()

    assert runner.child_folder_id("x-coredata://parent", 'odd" name') == "x-coredata://child"
    assert seen[0][0] == "osascript"
    assert seen[0][-2:] == ["x-coredata://parent", 'odd" name']


def test_the_cli_report_names_the_markdown_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "markdown_import_available", lambda: True)

    def fake_run(runner: object, config: Config, **kwargs: Any) -> notes_enex.EnexImportReport:
        return notes_enex.EnexImportReport(markdown_path="stage", markdown_notes=3)

    monkeypatch.setattr(cli, "run_enex_import", fake_run)
    assert cli.main(["import-notes", "--dryrun"]) == 0
    out = capsys.readouterr().out
    assert "markdown:            stage (3 notes)" in out
    assert "none written" not in out
