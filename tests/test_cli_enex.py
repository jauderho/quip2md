"""CLI coverage for the `.enex` writer and the remaining exit-code paths.

`tests/test_cli.py` covers the `applescript` writer end to end. This module
covers the default `enex` writer, its flags (`--writer`, `--enex-file`,
`--indent-checklists`), every exit code it can return (0/1/2/130), and the two
report printers, plus the `export` paths (`--dryrun`, API error, Ctrl-C) that
`test_cli.py` does not reach.

Nothing here touches Notes: `cli.EnexNotesRunner` and `cli.IndentRunner` are
monkeypatched to hand-written fakes, and `cli.QuipClient` to an in-memory one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from quip2md import cli, notes_enex
from quip2md.client import QuipApiError
from quip2md.convert import build_frontmatter
from quip2md.enex import ChecklistItem
from quip2md.notes_enex import EnexImportReport, ImportedNote
from quip2md.notes_import import NotesError, NotesStateError
from quip2md.notes_indent import IndentReport, IndentStep
from tests.test_cli import _EmptyFakeQuipClient

# --- Helpers ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_polling_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the landing-folder poll interval: every fake here answers at once."""
    monkeypatch.setattr(notes_enex.time, "sleep", lambda _seconds: None)


def _write_source_doc(
    source_dir: Path,
    relative: str,
    *,
    quip_id: str,
    title: str,
    body: str = "Hello.\n",
) -> Path:
    frontmatter = build_frontmatter(
        quip_id=quip_id,
        quip_url=f"https://quip.com/{quip_id}",
        title=title,
        created_usec=0,
        updated_usec=0,
        exported=datetime(2024, 1, 1, tzinfo=UTC),
    )
    path = source_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + "\n" + body, encoding="utf-8")
    return path


class _FakeEnexRunner:
    """Duck-typed fake satisfying `notes_enex.EnexNotesRunnerProtocol`."""

    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.moved: list[tuple[str, str]] = []
        self.landing_notes: list[ImportedNote] = []
        self._opened_yet = False

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def folder_names(self, account: str) -> frozenset[str]:
        base = frozenset({"Notes"})
        return base | {"Imported Notes 1"} if self._opened_yet else base

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        return "folder:" + "/".join(path)

    def folder_id_by_name(self, account: str, name: str) -> str:
        return f"folder:{name}"

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        return list(self.landing_notes)

    def move_note(self, note_id: str, folder_id: str) -> None:
        self.moved.append((note_id, folder_id))

    def open_enex(self, path: Path) -> None:
        self.opened.append(path)
        self._opened_yet = True


def _matching_runner(*pairs: tuple[str, str]) -> _FakeEnexRunner:
    runner = _FakeEnexRunner()
    runner.landing_notes = [
        ImportedNote(note_id, title, f"<div>Source: <u>https://quip.com/{quip_id}</u></div>")
        for note_id, (quip_id, title) in ((f"id-{i}", pair) for i, pair in enumerate(pairs, 1))
    ]
    return runner


class _RaisingInitEnexRunner:
    def __init__(self) -> None:
        raise NotesError("Apple Notes import requires macOS (…: 'linux')")


# --- import-notes --writer enex ---------------------------------------------


def test_enex_writer_is_the_default_and_returns_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="THREAD0013", title="Doc One")
    runner = _matching_runner(("THREAD0013", "Doc One"))
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: runner)

    assert cli.main(["import-notes"]) == 0

    captured = capsys.readouterr()
    assert "Import complete." in captured.out
    assert "filed into folders:  1" in captured.out
    assert runner.moved == [("id-1", "folder:Quip")]
    assert (tmp_path / ".quip2md" / "last_notes_run.json").is_file()
    assert (tmp_path / ".quip2md" / "quip2md.enex").is_file()


def test_enex_file_flag_chooses_where_the_archive_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", _FakeEnexRunner)
    target = tmp_path / "archives" / "custom.enex"

    assert cli.main(["import-notes", "--dryrun", "--enex-file", str(target)]) == 0
    assert target.is_file()
    assert str(target) in capsys.readouterr().out


def test_enex_dry_run_prints_the_dry_run_heading_and_writes_no_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(
        tmp_path / "export",
        "Doc.md",
        quip_id="THREAD0013",
        title="Doc",
        body="- [x] done\n- [ ] todo\n",
    )

    assert cli.main(["import-notes", "--dryrun"]) == 0

    captured = capsys.readouterr()
    assert "Dry run (no Notes changes)." in captured.out
    assert "checklist items:     2 (1 checked, 1 unchecked)" in captured.out
    assert "imported:" not in captured.out
    assert not (tmp_path / ".quip2md" / "notes_state.json").exists()
    assert not (tmp_path / ".quip2md" / "last_notes_run.json").exists()


def test_a_non_macos_platform_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", _RaisingInitEnexRunner)

    assert cli.main(["import-notes"]) == 2
    assert "quip2md: Notes error:" in capsys.readouterr().err


def test_a_notes_error_during_the_run_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", _FakeEnexRunner)

    def boom(*args: Any, **kwargs: Any) -> EnexImportReport:
        raise NotesError("timed out waiting for Notes to create an import folder")

    monkeypatch.setattr(cli, "run_enex_import", boom)

    assert cli.main(["import-notes"]) == 2
    assert "timed out waiting" in capsys.readouterr().err


def test_a_corrupted_state_file_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", _FakeEnexRunner)
    state = tmp_path / ".quip2md" / "notes_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("[]", encoding="utf-8")

    assert cli.main(["import-notes"]) == 2
    assert "quip2md: notes state error:" in capsys.readouterr().err


def test_ctrl_c_during_an_enex_import_returns_exit_code_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", _FakeEnexRunner)

    def interrupted(*args: Any, **kwargs: Any) -> EnexImportReport:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_enex_import", interrupted)

    assert cli.main(["import-notes"]) == 130
    assert "import interrupted" in capsys.readouterr().err


def test_a_failed_note_returns_exit_code_1_and_is_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")

    class FailingMove(_FakeEnexRunner):
        def move_note(self, note_id: str, folder_id: str) -> None:
            raise RuntimeError("simulated move failure")

    runner = FailingMove()
    runner.landing_notes = [
        ImportedNote("id-1", "Doc", "<div>Source: <u>https://quip.com/THREAD0013</u></div>")
    ]
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: runner)

    assert cli.main(["import-notes"]) == 1
    captured = capsys.readouterr()
    assert "failed:              1" in captured.out
    assert "THREAD0013: move failed" in captured.out


def test_unmatched_notes_return_exit_code_1_and_name_their_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    runner = _FakeEnexRunner()
    runner.landing_notes = [ImportedNote("id-9", "Stray Note", "<div>no provenance</div>")]
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: runner)

    assert cli.main(["import-notes"]) == 1
    captured = capsys.readouterr()
    assert "unmatched (left in Imported Notes 1): 1" in captured.out
    assert "- Stray Note" in captured.out


def test_a_second_run_with_nothing_changed_writes_no_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))
    assert cli.main(["import-notes"]) == 0
    capsys.readouterr()

    second = _matching_runner(("THREAD0013", "Doc"))
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: second)

    assert cli.main(["import-notes"]) == 0
    out = capsys.readouterr().out
    assert "documents:           0" in out
    assert "archive:             none written (nothing to import)" in out
    assert "skipped (unchanged): 1" in out
    assert second.opened == []


def test_force_reimports_and_reports_the_superseded_copies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))
    assert cli.main(["import-notes"]) == 0
    capsys.readouterr()

    assert cli.main(["import-notes", "--force"]) == 0
    out = capsys.readouterr().out
    assert "documents:           1" in out
    assert "superseded:          1" in out


def test_only_filters_the_documents_the_enex_writer_renders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "export"
    _write_source_doc(source, "A.md", quip_id="THREAD0013", title="A")
    _write_source_doc(source, "B.md", quip_id="THREAD0014", title="B")

    assert cli.main(["import-notes", "--dryrun", "--only", "THREAD0013"]) == 0
    assert "documents:           1" in capsys.readouterr().out


def test_source_flag_points_the_scan_somewhere_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    _write_source_doc(elsewhere, "A.md", quip_id="THREAD0013", title="A")

    assert cli.main(["import-notes", "--dryrun", "--source", str(elsewhere)]) == 0
    assert "documents:           1" in capsys.readouterr().out


# --- --indent-checklists ----------------------------------------------------


def _nested_source(source_dir: Path) -> None:
    _write_source_doc(
        source_dir,
        "Doc.md",
        quip_id="THREAD0013",
        title="Doc",
        body="- [ ] parent\n  - [x] child\n",
    )


def test_nested_checklists_without_the_flag_only_print_a_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _nested_source(tmp_path / "export")
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))

    assert cli.main(["import-notes"]) == 0
    assert "Re-run with --indent-checklists" in capsys.readouterr().out


def test_indent_checklists_runs_the_pass_and_prints_its_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _nested_source(tmp_path / "export")
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))

    seen: list[tuple[str, str, tuple[ChecklistItem, ...]]] = []

    def fake_indent(runner: object, targets: Sequence[Any]) -> IndentReport:
        seen.extend(targets)
        return IndentReport(notes_considered=1, notes_indented=1, levels_applied=1)

    monkeypatch.setattr(cli, "IndentRunner", lambda: object())
    monkeypatch.setattr(cli, "indent_notes", fake_indent)

    assert cli.main(["import-notes", "--indent-checklists"]) == 0

    captured = capsys.readouterr()
    assert "Checklist indentation:" in captured.out
    assert "notes indented:    1" in captured.out
    assert [label for _note_id, label, _plan in seen] == ["Doc"]


def test_a_failed_indentation_pass_returns_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _nested_source(tmp_path / "export")
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))
    monkeypatch.setattr(cli, "IndentRunner", lambda: object())
    monkeypatch.setattr(
        cli,
        "indent_notes",
        lambda runner, targets: IndentReport(
            notes_considered=1, failures=[("Doc", "verification failed: line 2")]
        ),
    )

    assert cli.main(["import-notes", "--indent-checklists"]) == 1
    assert "- Doc: verification failed: line 2" in capsys.readouterr().out


def test_a_missing_accessibility_grant_returns_exit_code_1_with_the_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _nested_source(tmp_path / "export")
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))

    def raising() -> object:
        raise NotesError("This pass needs macOS Accessibility permission.")

    monkeypatch.setattr(cli, "IndentRunner", raising)

    assert cli.main(["import-notes", "--indent-checklists"]) == 1
    assert "checklist indentation skipped" in capsys.readouterr().err


def test_indent_checklists_is_a_no_op_when_no_note_needs_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(
        tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc", body="- [x] flat\n"
    )
    monkeypatch.setattr(cli, "EnexNotesRunner", lambda: _matching_runner(("THREAD0013", "Doc")))

    def never(runner: object, targets: Sequence[Any]) -> IndentReport:
        raise AssertionError("the indentation pass must not run for a flat checklist")

    monkeypatch.setattr(cli, "indent_notes", never)

    assert cli.main(["import-notes", "--indent-checklists"]) == 0
    assert "Checklist indentation:" not in capsys.readouterr().out


# --- Report printers --------------------------------------------------------


def test_print_enex_report_truncates_long_lists(capsys: pytest.CaptureFixture[str]) -> None:
    report = EnexImportReport(
        documents=20,
        unmatched=[f"Note {index}" for index in range(15)],
        failed=[(f"THREAD{index:04d}", "move failed") for index in range(15)],
    )
    cli._print_enex_report(report, dry_run=False)

    out = capsys.readouterr().out
    assert "unmatched (left in the import folder): 15" in out
    assert out.count("    - Note ") == 10
    assert "failed:              15" in out
    assert out.count(": move failed") == 10


def test_print_indent_report_lists_every_failure(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_indent_report(
        IndentReport(
            notes_considered=3,
            notes_indented=1,
            notes_already_flat=1,
            skipped_unrecognized=1,
            levels_applied=4,
            failures=[("Doc", "skipped: checklist lines did not match the plan")],
        )
    )
    out = capsys.readouterr().out
    assert "notes considered:  3" in out
    assert "skipped (no match):1" in out
    assert "- Doc: skipped:" in out


def test_dry_run_applescript_report_shows_conversion_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(
        tmp_path / "export",
        "Doc.md",
        quip_id="THREAD0013",
        title="Doc",
        body="1. one\n   1. nested\n",
    )

    assert cli.main(["import-notes", "--writer", "applescript", "--dryrun"]) == 0
    assert "conversion warnings:" in capsys.readouterr().out


# --- export -----------------------------------------------------------------


def test_export_dry_run_returns_0_without_printing_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIP_TOKEN", "test-token")
    monkeypatch.setattr(cli, "QuipClient", _EmptyFakeQuipClient)

    assert cli.main(["export", "--dryrun"]) == 0
    assert "Export complete." not in capsys.readouterr().out


def test_an_api_error_during_export_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIP_TOKEN", "test-token")
    monkeypatch.setattr(cli, "QuipClient", _EmptyFakeQuipClient)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise QuipApiError(status_code=503, message="service unavailable", path="/1/folders")

    monkeypatch.setattr(cli, "run_export", boom)

    assert cli.main(["export"]) == 2
    assert "quip2md: API error:" in capsys.readouterr().err


def test_ctrl_c_during_export_returns_exit_code_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIP_TOKEN", "test-token")
    monkeypatch.setattr(cli, "QuipClient", _EmptyFakeQuipClient)

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_export", interrupted)

    assert cli.main(["export"]) == 130
    assert "export interrupted" in capsys.readouterr().err


# --- Argument parsing -------------------------------------------------------


def test_an_unknown_writer_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["import-notes", "--writer", "carrier-pigeon"])
    assert excinfo.value.code == 2


def test_local_with_the_enex_writer_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """Notes always imports an archive into the default account."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["import-notes", "--local"])
    assert excinfo.value.code == 2
    assert "--local cannot be used with --writer enex" in capsys.readouterr().err


def test_indent_checklists_with_the_applescript_writer_is_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """That writer makes no checklists, so there is nothing to indent."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["import-notes", "--writer", "applescript", "--indent-checklists"])
    assert excinfo.value.code == 2
    assert "--indent-checklists cannot be used with --writer applescript" in capsys.readouterr().err


def test_a_missing_subcommand_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_verbose_switches_the_quip2md_logger_to_debug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import logging

    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")

    cli.main(["import-notes", "--dryrun", "--verbose"])
    assert logging.getLogger("quip2md").level == logging.DEBUG

    cli.main(["import-notes", "--dryrun"])
    assert logging.getLogger("quip2md").level == logging.WARNING


def test_state_error_and_step_types_are_importable_from_the_cli_surface() -> None:
    """Guards the CLI's import list against a rename in the modules it wires up."""
    assert issubclass(NotesStateError, RuntimeError)
    assert IndentStep(start=0, count=1, levels=1, paragraph=0).count == 1


def test_a_notes_error_during_an_applescript_import_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc.md", quip_id="THREAD0013", title="Doc")
    monkeypatch.setattr(cli, "NotesRunner", lambda: object())

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise NotesError('no "On My Mac" account is configured')

    monkeypatch.setattr(cli, "run_import", boom)

    assert cli.main(["import-notes", "--writer", "applescript", "--local"]) == 2
    assert "On My Mac" in capsys.readouterr().err


# --- prune-notes: the CLI layer of the only destructive command -------------


@dataclass
class RecordingPruneRunner:
    """Stands in for Notes; records what the CLI asked it to delete."""

    folders: list[Any] = field(default_factory=list)
    deleted_folders: list[str] = field(default_factory=list)
    deleted_notes: list[str] = field(default_factory=list)

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def top_level_folders(self, account: str) -> list[Any]:
        return list(self.folders)

    def delete_folder(self, folder_id: str) -> None:
        self.deleted_folders.append(folder_id)
        self.folders = [f for f in self.folders if f.folder_id != folder_id]

    def delete_note(self, note_id: str) -> None:
        self.deleted_notes.append(note_id)


def _install_prune_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: RecordingPruneRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "PruneRunner", lambda: runner)


def test_prune_without_a_target_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deleting nothing in particular is a mistake, not a default."""
    _install_prune_runner(monkeypatch, tmp_path, RecordingPruneRunner())
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["prune-notes"])
    assert exit_info.value.code == 2


def test_prune_prints_a_plan_and_deletes_nothing_without_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from quip2md.notes_prune import FolderInfo

    runner = RecordingPruneRunner(folders=[FolderInfo("Imported Notes", "f1", 0, 0)])
    _install_prune_runner(monkeypatch, tmp_path, runner)

    assert cli.main(["prune-notes", "--empty-landing"]) == 0

    out = capsys.readouterr().out
    assert "nothing deleted" in out
    assert "Imported Notes" in out
    assert runner.deleted_folders == []


def test_prune_with_apply_deletes_and_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from quip2md.notes_prune import FolderInfo

    runner = RecordingPruneRunner(folders=[FolderInfo("Imported Notes", "f1", 0, 0)])
    _install_prune_runner(monkeypatch, tmp_path, runner)

    assert cli.main(["prune-notes", "--empty-landing", "--apply"]) == 0

    assert runner.deleted_folders == ["f1"]
    assert "Prune complete." in capsys.readouterr().out


def test_prune_reports_a_refusal_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from quip2md.notes_prune import FolderInfo

    runner = RecordingPruneRunner(folders=[FolderInfo("Quip", "f1", 0, 2)])
    _install_prune_runner(monkeypatch, tmp_path, runner)

    assert cli.main(["prune-notes", "--folder", "Quip", "--apply"]) == 0

    assert runner.deleted_folders == [], "the Quip folder is never deletable"
    assert "protected" in capsys.readouterr().out


def test_prune_exits_1_when_a_deletion_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from quip2md.notes_prune import FolderInfo

    @dataclass
    class FailingRunner(RecordingPruneRunner):
        def delete_folder(self, folder_id: str) -> None:
            raise RuntimeError("Notes said no")

    runner = FailingRunner(folders=[FolderInfo("Imported Notes", "f1", 0, 0)])
    _install_prune_runner(monkeypatch, tmp_path, runner)

    assert cli.main(["prune-notes", "--empty-landing", "--apply"]) == 1


def test_prune_surfaces_a_notes_error_as_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @dataclass
    class RefusingRunner(RecordingPruneRunner):
        def resolve_account(self, *, local: bool) -> str:
            raise NotesError("Notes is not available")

    _install_prune_runner(monkeypatch, tmp_path, RefusingRunner())
    assert cli.main(["prune-notes", "--superseded", "--apply"]) == 2


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_a_nonsensical_worker_count_is_rejected(bad: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["import-notes", "--workers", bad, "--dryrun"])
    assert exit_info.value.code == 2
