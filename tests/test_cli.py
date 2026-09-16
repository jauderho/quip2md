"""Tests for quip2md.cli.

Exit-code coverage: config errors (2), a corrupted manifest hit through the
real `run_export()`/`walk()` path (2), a run with per-thread failures (1),
and a clean run (0). `QuipClient` is monkeypatched to a small in-memory fake
throughout -- no real network -- and `run_export` itself is monkeypatched
only where the test cares about the export outcome rather than the walk.

`import-notes` coverage follows the same shape: `NotesRunner` is
monkeypatched to `_FakeNotesRunner` (a hand-written fake satisfying
`notes_import.NotesRunnerProtocol`) throughout -- no test here, or anywhere
in this module, ever shells out to `osascript`. Real `.md` source files
(with real frontmatter, via `quip2md.convert.build_frontmatter`) are written
under a `tmp_path`-scoped `export/` directory so `run_import()`'s own
`scan_source()`/`markdown_to_notes_html()` machinery runs for real; only the
Notes-automation boundary (`NotesRunner`) is faked.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quip2md import cli, notes_enex
from quip2md.client import QuipFolder, QuipUser, ThreadContent
from quip2md.config import Config
from quip2md.convert import build_frontmatter
from quip2md.export import ExportReport
from quip2md.notes_import import ImportReport, NotesError


@pytest.fixture(autouse=True)
def _no_polling_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the `.enex` landing-folder poll interval: the fakes answer at once."""
    monkeypatch.setattr(notes_enex.time, "sleep", lambda _seconds: None)


class _EmptyFakeQuipClient:
    """Stands in for `QuipClient`: no network, an account with no folders."""

    def __init__(self, config: Config) -> None:
        del config

    def close(self) -> None:
        pass

    def current_user(self) -> QuipUser:
        return QuipUser(
            id="u1",
            name="Test User",
            private_folder_id=None,
            desktop_folder_id=None,
            archive_folder_id=None,
            starred_folder_id=None,
            shared_folder_ids=(),
            group_folder_ids=(),
        )

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]:
        del ids
        return {}

    def threads_batch(self, ids: Sequence[str]) -> dict[str, ThreadContent]:
        del ids
        return {}

    def blob(self, thread_id: str, blob_id: str) -> tuple[bytes, str | None]:
        del thread_id, blob_id
        return b"", None

    def export_xlsx(self, thread_id: str) -> bytes:
        del thread_id
        return b""


# --- exit code 2: configuration error --------------------------------------


def test_missing_token_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QUIP_TOKEN", raising=False)
    monkeypatch.delenv("QUIP_BASE_URL", raising=False)

    exit_code = cli.main(["export"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "configuration error" in captured.err
    assert "QUIP_TOKEN" in captured.err


# --- exit code 2: manifest error --------------------------------------------


def test_corrupted_manifest_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIP_TOKEN", "test-token")
    monkeypatch.setattr(cli, "QuipClient", _EmptyFakeQuipClient)

    state_path = tmp_path / ".quip2md" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not valid json", encoding="utf-8")

    exit_code = cli.main(["export"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "manifest error" in captured.err
    # `Manifest` reports the (relative, cwd-scoped) `state_path` it was
    # configured with -- since cwd is `tmp_path`, that's the relative form.
    assert str(state_path.relative_to(tmp_path)) in captured.err


# --- exit code 1: a thread failed -------------------------------------------


def test_run_export_with_failures_returns_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIP_TOKEN", "test-token")
    monkeypatch.setattr(cli, "QuipClient", _EmptyFakeQuipClient)

    def fake_run_export(
        client: object, config: Config, *, only: Sequence[str] | None = None
    ) -> ExportReport:
        del client, config, only
        report = ExportReport(exported=2)
        report.failed.append(("t1", "simulated failure"))
        return report

    monkeypatch.setattr(cli, "run_export", fake_run_export)

    exit_code = cli.main(["export"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "failed:              1" in captured.out
    assert "t1: simulated failure" in captured.out


# --- exit code 0: success ----------------------------------------------------


def test_successful_export_returns_exit_code_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIP_TOKEN", "test-token")
    monkeypatch.setattr(cli, "QuipClient", _EmptyFakeQuipClient)

    def fake_run_export(
        client: object, config: Config, *, only: Sequence[str] | None = None
    ) -> ExportReport:
        del client, config, only
        return ExportReport(exported=3)

    monkeypatch.setattr(cli, "run_export", fake_run_export)

    exit_code = cli.main(["export"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "exported:            3" in captured.out


# --- import-notes: shared fakes and helpers ----------------------------------


class _FakeNotesRunner:
    """Duck-typed fake satisfying `notes_import.NotesRunnerProtocol`.

    Records every call so tests can assert on what `run_import()` asked of
    it, without ever shelling out to `osascript`.
    """

    def __init__(self) -> None:
        self.resolve_account_calls: list[bool] = []
        self.create_notes_calls: list[list[str]] = []
        self._next_note_id = 1

    def resolve_account(self, *, local: bool) -> str:
        self.resolve_account_calls.append(local)
        return "On My Mac" if local else "iCloud"

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        del account
        return "/".join(path)

    def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
        del folder_id
        self.create_notes_calls.append(list(bodies))
        ids = [f"note-{self._next_note_id + i}" for i in range(len(bodies))]
        self._next_note_id += len(bodies)
        return ids

    def note_ids_in_folder(self, folder_id: str) -> frozenset[str]:
        del folder_id
        return frozenset()

    def set_note_body(self, note_id: str, body_html: str) -> None:
        del note_id, body_html


class _FailingCreateNotesRunner(_FakeNotesRunner):
    """Like `_FakeNotesRunner`, but every `create_notes()` call fails."""

    def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
        del folder_id, bodies
        raise RuntimeError("simulated create failure")


class _RaisingInitNotesRunner:
    """Stands in for `NotesRunner` on a simulated non-macOS platform."""

    def __init__(self) -> None:
        raise NotesError(
            "Apple Notes import requires macOS (osascript is not available on "
            "this platform: 'linux')"
        )


def _write_source_doc(
    source_dir: Path, relative: str, *, quip_id: str, title: str, body: str = "Hello.\n"
) -> Path:
    """Write one `.md` file with real frontmatter under `source_dir`."""
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


# --- import-notes: exit code 0 -----------------------------------------------


def test_import_notes_clean_run_returns_exit_code_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    monkeypatch.setattr(cli, "NotesRunner", _FakeNotesRunner)

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Import complete." in captured.out
    assert "created:             1" in captured.out
    assert (tmp_path / ".quip2md" / "last_notes_run.json").is_file()


# --- import-notes: exit code 1 -----------------------------------------------


def test_import_notes_with_failure_returns_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    monkeypatch.setattr(cli, "NotesRunner", _FailingCreateNotesRunner)

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "failed:              1" in captured.out
    assert "AAA111" in captured.out


# --- import-notes: exit code 2 -----------------------------------------------


def test_import_notes_non_macos_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "NotesRunner", _RaisingInitNotesRunner)

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Notes error" in captured.err
    assert "macOS" in captured.err


def test_import_notes_dryrun_skips_platform_guard_on_non_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--dryrun` must not construct `NotesRunner` (whose `__init__` raises on
    # non-macOS); it should scan+convert offline and exit 0, the same contract
    # the enex writer honours on a non-macOS box. `_RaisingInitNotesRunner`
    # simulates the non-darwin platform guard -- if the dry run ever builds
    # the runner, this test fails with exit 2 instead of 0.
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    monkeypatch.setattr(cli, "NotesRunner", _RaisingInitNotesRunner)

    exit_code = cli.main(["import-notes", "--writer", "applescript", "--dryrun"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Dry run: notes import" in captured.out
    assert "no Notes automation calls were made" in captured.out
    assert "would create:        1" in captured.out
    assert captured.err == ""
    assert not (tmp_path / ".quip2md" / "notes_state.json").exists()
    assert not (tmp_path / ".quip2md" / "last_notes_run.json").exists()


def test_import_notes_corrupted_state_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    monkeypatch.setattr(cli, "NotesRunner", _FakeNotesRunner)

    state_path = tmp_path / ".quip2md" / "notes_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not valid json", encoding="utf-8")

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "notes state error" in captured.err
    assert str(state_path.relative_to(tmp_path)) in captured.err


# --- import-notes: exit code 130 ---------------------------------------------


def test_import_notes_keyboard_interrupt_returns_exit_code_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "NotesRunner", _FakeNotesRunner)

    def fake_run_import(
        runner: object,
        config: Config,
        *,
        source_dir: Path,
        local: bool,
        only: Sequence[str] | None = None,
    ) -> ImportReport:
        del runner, config, source_dir, local, only
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_import", fake_run_import)

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 130
    captured = capsys.readouterr()
    assert "import interrupted" in captured.err
    assert "notes_state.json" in captured.err


# --- import-notes: --dryrun ---------------------------------------------------


def test_import_notes_dryrun_prints_folder_counts_and_writes_no_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source_dir = tmp_path / "export"
    _write_source_doc(source_dir, "Doc One.md", quip_id="AAA111", title="Doc One")
    _write_source_doc(source_dir, "Sub/Doc Two.md", quip_id="BBB222", title="Doc Two")
    monkeypatch.setattr(cli, "NotesRunner", _FakeNotesRunner)

    exit_code = cli.main(["import-notes", "--writer", "applescript", "--dryrun"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Dry run: notes import" in captured.out
    assert "no Notes automation calls were made" in captured.out
    assert "Quip/" in captured.out
    assert "Sub/" in captured.out
    assert "would create:        2" in captured.out
    assert not (tmp_path / ".quip2md" / "notes_state.json").exists()
    assert not (tmp_path / ".quip2md" / "last_notes_run.json").exists()


# --- import-notes: --local ----------------------------------------------------


def test_import_notes_local_flag_passes_through_to_run_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    runner = _FakeNotesRunner()
    monkeypatch.setattr(cli, "NotesRunner", lambda: runner)

    exit_code = cli.main(["import-notes", "--writer", "applescript", "--local"])

    assert exit_code == 0
    assert runner.resolve_account_calls == [True]


def test_import_notes_without_local_flag_targets_default_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    runner = _FakeNotesRunner()
    monkeypatch.setattr(cli, "NotesRunner", lambda: runner)

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 0
    assert runner.resolve_account_calls == [False]


# --- import-notes: --only -----------------------------------------------------


def test_import_notes_only_filters_to_the_given_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source_dir = tmp_path / "export"
    _write_source_doc(source_dir, "Doc One.md", quip_id="AAA111", title="Doc One")
    _write_source_doc(source_dir, "Doc Two.md", quip_id="BBB222", title="Doc Two")
    runner = _FakeNotesRunner()
    monkeypatch.setattr(cli, "NotesRunner", lambda: runner)

    exit_code = cli.main(["import-notes", "--writer", "applescript", "--only", "AAA111"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "created:             1" in captured.out
    assert len(runner.create_notes_calls) == 1
    (bodies,) = runner.create_notes_calls
    assert len(bodies) == 1
    assert "Doc One" in bodies[0]
    assert "Doc Two" not in bodies[0]


# --- import-notes: works without QUIP_TOKEN ----------------------------------


def test_import_notes_works_with_no_quip_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QUIP_TOKEN", raising=False)
    monkeypatch.delenv("QUIP_BASE_URL", raising=False)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    monkeypatch.setattr(cli, "NotesRunner", _FakeNotesRunner)

    exit_code = cli.main(["import-notes", "--writer", "applescript"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "configuration error" not in captured.err
    assert "QUIP_TOKEN" not in captured.err
    assert "created:             1" in captured.out


# --- import-notes: the .enex writer (default) --------------------------------


class _FakeEnexRunner:
    """Duck-typed fake for `notes_enex.EnexNotesRunnerProtocol`."""

    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.moved: list[tuple[str, str]] = []
        self._imported = False

    def resolve_account(self, *, local: bool) -> str:
        del local
        return "iCloud"

    def folder_names(self, account: str) -> frozenset[str]:
        del account
        return frozenset({"Imported Notes"}) if self._imported else frozenset()

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        del account
        return "/".join(path)

    def folder_id_by_name(self, account: str, name: str) -> str:
        del account
        return name

    def notes_in_folder(self, folder_id: str):
        del folder_id
        from quip2md.notes_enex import ImportedNote

        return [
            ImportedNote("id-1", "Doc One", "<div>Source: <u>https://quip.com/AAA111</u></div>")
        ]

    def move_note(self, note_id: str, folder_id: str) -> None:
        self.moved.append((note_id, folder_id))

    def open_enex(self, path: Path) -> None:
        self.opened.append(path)
        self._imported = True


def test_import_notes_enex_writer_is_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(tmp_path / "export", "Doc One.md", quip_id="AAA111", title="Doc One")
    monkeypatch.setattr(cli, "EnexNotesRunner", _FakeEnexRunner)

    exit_code = cli.main(["import-notes"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Import complete." in captured.out
    assert "filed into folders:  1" in captured.out
    assert (tmp_path / ".quip2md" / "quip2md.enex").is_file()


def test_import_notes_enex_dryrun_writes_the_archive_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(
        tmp_path / "export",
        "Doc One.md",
        quip_id="AAA111",
        title="Doc One",
        body="- [x] done\n  - [ ] child\n",
    )

    exit_code = cli.main(["import-notes", "--dryrun"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Dry run (no Notes changes)." in captured.out
    assert "checklist items:     2" in captured.out
    assert (tmp_path / ".quip2md" / "quip2md.enex").is_file()
    assert not (tmp_path / ".quip2md" / "notes_state.json").exists()


def test_import_notes_enex_reports_nested_checklists_needing_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_source_doc(
        tmp_path / "export",
        "Doc One.md",
        quip_id="AAA111",
        title="Doc One",
        body="- [x] parent\n  - [ ] child\n",
    )
    monkeypatch.setattr(cli, "EnexNotesRunner", _FakeEnexRunner)

    exit_code = cli.main(["import-notes"])

    assert exit_code == 0
    assert "--indent-checklists" in capsys.readouterr().out
