"""Tests for the `.enex` import route and the checklist indentation pass.

Every Notes interaction goes through a hand-written fake, mirroring
`test_notes_import.py`'s convention: no test here shells out to `osascript` or
touches the real Notes app.
"""

from __future__ import annotations

import pickle
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quip2md import notes_enex
from quip2md.config import Config
from quip2md.enex import ChecklistItem, EnexResource, NoteEnml, build_enex
from quip2md.notes_enex import (
    EnexImportReport,
    ImportedNote,
    _extract_quip_url,
    render_sources,
    render_sources_parallel,
    run_enex_import,
)
from quip2md.notes_import import NotesError, NoteSource
from quip2md.notes_indent import (
    IndentStep,
    indent_notes,
    locate_checklist_paragraphs,
    paragraph_lines,
    plan_indent_steps,
    verify_indentation,
)

# --- Fixtures ---------------------------------------------------------------


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


def _write_doc(root: Path, relative: str, *, quip_id: str, url: str, title: str, body: str) -> Path:
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
    """Stands in for Notes: records every call, fabricates the landing folder."""

    landing_notes: list[ImportedNote] = field(default_factory=list)
    folders_before: frozenset[str] = frozenset({"Notes"})
    landing_name: str = "Imported Notes 1"
    opened: list[Path] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    created_folders: list[tuple[str, ...]] = field(default_factory=list)
    _opened_yet: bool = False

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def folder_names(self, account: str) -> frozenset[str]:
        if self._opened_yet:
            return self.folders_before | {self.landing_name}
        return self.folders_before

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        self.created_folders.append(tuple(path))
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


@pytest.fixture(autouse=True)
def _no_polling_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the landing-folder poll interval: every fake here answers at once."""
    monkeypatch.setattr(notes_enex.time, "sleep", lambda _seconds: None)


# --- URL matching -----------------------------------------------------------


def test_extract_quip_url_reads_the_stripped_link_text() -> None:
    body = "<div>Source: <u>https://quip.com/THREAD0001</u><br></div><div>x</div>"
    assert _extract_quip_url(body) == "https://quip.com/THREAD0001"


def test_extract_quip_url_returns_none_when_absent() -> None:
    assert _extract_quip_url("<div>no provenance here</div>") is None


# --- Dry run ----------------------------------------------------------------


def test_dry_run_writes_the_enex_but_never_touches_notes(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(
        source,
        "Folder/Doc.md",
        quip_id="T1",
        url="https://quip.com/T1",
        title="Doc",
        body="- [x] done\n- [ ] todo\n  - [ ] child\n",
    )
    runner = FakeEnexRunner()

    report = run_enex_import(
        runner, _config(tmp_path, dry_run=True), source_dir=source, enex_path=tmp_path / "out.enex"
    )

    assert runner.opened == []
    assert runner.moved == []
    assert (tmp_path / "out.enex").is_file()
    assert report.documents == 1
    assert report.checklist_items == 3
    assert report.checklist_checked == 1
    assert report.docs_needing_indent == 1
    assert report.indent_levels == 1


def test_dry_run_writes_no_state(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="body\n")
    run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path, dry_run=True),
        source_dir=source,
        enex_path=tmp_path / "out.enex",
    )
    assert not (tmp_path / ".quip2md" / "notes_state.json").exists()


def test_dry_run_reads_the_state_file_and_skips_what_a_real_run_would(tmp_path: Path) -> None:
    """A dry run reports the pending work, not the whole tree.

    The state file is consulted read-only: an already-imported, unchanged
    document is counted in `skipped_unchanged` and kept out of the archive,
    exactly as in a real run, and the file itself is left untouched.
    """
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="body\n")
    _write_doc(source, "B.md", quip_id="T2", url="https://quip.com/T2", title="B", body="body\n")

    real = FakeEnexRunner(
        landing_notes=[
            ImportedNote("id-1", "A", "<div>Source: https://quip.com/T1</div>"),
            ImportedNote("id-2", "B", "<div>Source: https://quip.com/T2</div>"),
        ]
    )
    run_enex_import(
        real, _config(tmp_path), source_dir=source, enex_path=tmp_path / "out.enex", confirm=False
    )
    state_path = tmp_path / ".quip2md" / "notes_state.json"
    before = state_path.read_bytes()

    # B changes on disk; A does not.
    _write_doc(source, "B.md", quip_id="T2", url="https://quip.com/T2", title="B", body="new\n")
    report = run_enex_import(
        None,
        _config(tmp_path, dry_run=True),
        source_dir=source,
        enex_path=tmp_path / "dry.enex",
    )

    assert report.skipped_unchanged == 1
    assert report.documents == 1
    archive = (tmp_path / "dry.enex").read_text(encoding="utf-8")
    assert "<title>B</title>" in archive
    assert "<title>A</title>" not in archive
    assert state_path.read_bytes() == before


def test_dry_run_with_nothing_pending_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="body\n")

    real = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", "<div>Source: https://quip.com/T1</div>")]
    )
    run_enex_import(
        real, _config(tmp_path), source_dir=source, enex_path=tmp_path / "out.enex", confirm=False
    )

    report = run_enex_import(
        None, _config(tmp_path, dry_run=True), source_dir=source, enex_path=tmp_path / "dry.enex"
    )

    assert report.documents == 0
    assert report.skipped_unchanged == 1
    assert report.enex_path == ""
    assert not (tmp_path / "dry.enex").exists()


# --- Parallel rendering -----------------------------------------------------


def _synthetic_sources(count: int) -> list[NoteSource]:
    """`count` tiny in-memory sources, varied enough to exercise the counters."""
    return [
        NoteSource(
            key=f"T{index:03d}",
            md_path=Path("export") / f"Doc {index}.md",
            relative_path=f"Doc {index}.md",
            folder_path=("Quip",),
            title=f"Doc {index}",
            quip_url=f"https://quip.com/T{index:03d}",
            body_markdown=(
                f"# Doc {index}\n\ntext [a link](https://example.com/{index})\n\n"
                "- [x] done\n- [ ] todo\n  - [ ] child\n"
            ),
            keyed_by_path=False,
            created="2020-01-01T00:00:00Z",
            updated="2020-01-02T00:00:00Z",
        )
        for index in range(count)
    ]


@pytest.mark.parametrize(
    "value",
    [
        ChecklistItem(text="t", checked=True, depth=2),
        EnexResource(md5="d41d8", mime="image/png", filename="a.png", data=b"\x89PNG"),
        NoteEnml(title="T", enml="<en-note/>", checklist=(ChecklistItem("t", False, 0),)),
        _synthetic_sources(1)[0],
    ],
    ids=["ChecklistItem", "EnexResource", "NoteEnml", "NoteSource"],
)
def test_every_type_crossing_the_pool_boundary_is_picklable(value: object) -> None:
    """`spawn` moves these by pickle; an unpicklable field would hang the pool."""
    assert pickle.loads(pickle.dumps(value)) == value


def test_parallel_rendering_matches_the_sequential_render_exactly() -> None:
    """Same notes, same order, same report -- the pool is an optimisation only."""
    sources = _synthetic_sources(60)

    sequential_report = EnexImportReport()
    sequential = render_sources(sources, sequential_report)

    parallel_report = EnexImportReport()
    parallel = render_sources_parallel(sources, parallel_report, workers=2)

    exported = datetime(2026, 1, 1, tzinfo=UTC)
    assert build_enex([note for _source, note in parallel], exported=exported) == build_enex(
        [note for _source, note in sequential], exported=exported
    )
    assert [source.key for source, _note in parallel] == [
        source.key for source, _note in sequential
    ]
    assert parallel_report.as_dict() == sequential_report.as_dict()


def test_too_few_sources_stay_in_this_process() -> None:
    """Below the threshold the pool costs more than it saves, so it is skipped."""
    sources = _synthetic_sources(3)
    report = EnexImportReport()
    rendered = render_sources_parallel(sources, report, workers=4)
    assert [source.key for source, _note in rendered] == [source.key for source in sources]
    assert report.checklist_items == 9


def test_a_chunk_that_dies_whole_becomes_a_notes_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-source failures are isolated; a failed *chunk* is not, so it is loud."""

    def explode(_chunk: object) -> object:
        raise RuntimeError("worker died")

    monkeypatch.setattr(notes_enex, "_render_chunk", explode)
    with pytest.raises(NotesError, match="parallel rendering failed"):
        render_sources_parallel(_synthetic_sources(60), EnexImportReport(), workers=2)


def test_chunks_are_contiguous_and_cover_every_source() -> None:
    sources = _synthetic_sources(10)
    chunks = notes_enex._contiguous_chunks(sources, 4)
    assert [len(chunk) for chunk in chunks] == [3, 3, 2, 2]
    assert [source for chunk in chunks for source in chunk] == sources


def test_more_chunks_than_sources_yields_no_empty_chunk() -> None:
    chunks = notes_enex._contiguous_chunks(_synthetic_sources(2), 8)
    assert [len(chunk) for chunk in chunks] == [1, 1]


def test_merging_chunk_reports_adds_counters_and_keeps_list_order() -> None:
    total = EnexImportReport(checklist_items=1, warnings=2, failed=[("a", "first")])
    total.merge(EnexImportReport(checklist_items=3, warnings=1, failed=[("b", "second")]))
    assert total.checklist_items == 4
    assert total.warnings == 3
    assert total.failed == [("a", "first"), ("b", "second")]


def test_the_default_worker_count_is_capped_and_at_least_one() -> None:
    assert 1 <= notes_enex.default_render_workers() <= 6


# --- Real run ---------------------------------------------------------------


def test_import_moves_matched_notes_into_their_mirrored_folder(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(
        source, "Team/A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="body\n"
    )
    _write_doc(source, "B.md", quip_id="T2", url="https://quip.com/T2", title="B", body="body\n")

    runner = FakeEnexRunner(
        landing_notes=[
            ImportedNote("id-1", "A", "<div>Source: <u>https://quip.com/T1</u></div>"),
            ImportedNote("id-2", "B", "<div>Source: <u>https://quip.com/T2</u></div>"),
        ]
    )

    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "out.enex", confirm=False
    )

    assert report.imported == 2
    assert report.moved == 2
    assert report.unmatched == []
    assert ("id-1", "folder:Quip/Team") in runner.moved
    assert ("id-2", "folder:Quip") in runner.moved


def test_unmatched_notes_are_reported_and_left_alone(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="body\n")

    runner = FakeEnexRunner(
        landing_notes=[
            ImportedNote("id-1", "A", "<div>Source: <u>https://quip.com/T1</u></div>"),
            ImportedNote("id-9", "Stray", "<div>no provenance</div>"),
        ]
    )

    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "out.enex", confirm=False
    )

    assert report.unmatched == ["Stray"]
    assert [note_id for note_id, _ in runner.moved] == ["id-1"]


def test_state_records_every_moved_note(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="body\n")
    runner = FakeEnexRunner(
        landing_notes=[ImportedNote("id-1", "A", "<div>Source: <u>https://quip.com/T1</u></div>")]
    )

    run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "out.enex", confirm=False
    )

    state_file = tmp_path / ".quip2md" / "notes_state.json"
    assert state_file.is_file()
    assert "id-1" in state_file.read_text()


def test_only_restricts_the_documents_rendered(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="a\n")
    _write_doc(source, "B.md", quip_id="T2", url="https://quip.com/T2", title="B", body="b\n")

    report = run_enex_import(
        FakeEnexRunner(),
        _config(tmp_path, dry_run=True),
        source_dir=source,
        enex_path=tmp_path / "out.enex",
        only=["T1"],
    )

    assert report.documents == 1
    assert "<title>A</title>" in (tmp_path / "out.enex").read_text()
    assert "<title>B</title>" not in (tmp_path / "out.enex").read_text()


def test_a_move_failure_is_isolated_to_its_own_note(tmp_path: Path) -> None:
    source = tmp_path / "export"
    _write_doc(source, "A.md", quip_id="T1", url="https://quip.com/T1", title="A", body="a\n")
    _write_doc(source, "B.md", quip_id="T2", url="https://quip.com/T2", title="B", body="b\n")

    class Failing(FakeEnexRunner):
        def move_note(self, note_id: str, folder_id: str) -> None:
            if note_id == "id-1":
                raise RuntimeError("boom")
            super().move_note(note_id, folder_id)

    runner = Failing(
        landing_notes=[
            ImportedNote("id-1", "A", "<div>Source: <u>https://quip.com/T1</u></div>"),
            ImportedNote("id-2", "B", "<div>Source: <u>https://quip.com/T2</u></div>"),
        ]
    )

    report = run_enex_import(
        runner, _config(tmp_path), source_dir=source, enex_path=tmp_path / "out.enex", confirm=False
    )

    assert report.moved == 1
    assert [key for key, _ in report.failed] == ["T1"]


# --- Indentation planning ---------------------------------------------------

_BODY = (
    "<div><h1>Doc</h1></div>"
    "<div>Source: <u>https://quip.com/T1</u></div>"
    "<ul><li>parent</li><li>child one</li><li>child two</li><li>next parent</li></ul>"
)

_PLAN = (
    ChecklistItem("parent", False, 0),
    ChecklistItem("child one", False, 1),
    ChecklistItem("child two", True, 1),
    ChecklistItem("next parent", False, 0),
)


def test_locate_checklist_paragraphs_skips_non_checklist_lines() -> None:
    # "Doc" heading (h1 inside a div counts once) plus the provenance div sit
    # before the list, so the first checklist line is not paragraph 0.
    indices = locate_checklist_paragraphs(_BODY, _PLAN)
    assert indices is not None
    assert indices == [indices[0] + i for i in range(4)]
    assert indices[0] > 0


def test_locate_returns_none_when_the_note_does_not_match() -> None:
    assert locate_checklist_paragraphs("<div>something else</div>", _PLAN) is None


def test_plan_groups_a_consecutive_run_into_one_step() -> None:
    steps = plan_indent_steps(_PLAN, [2, 3, 4, 5])
    assert steps == [IndentStep(start=1, count=2, levels=1, paragraph=3)]


def test_plan_splits_a_run_broken_by_another_paragraph() -> None:
    steps = plan_indent_steps(_PLAN, [2, 3, 7, 8])
    assert steps == [
        IndentStep(start=1, count=1, levels=1, paragraph=3),
        IndentStep(start=2, count=1, levels=1, paragraph=7),
    ]


def test_plan_is_empty_for_a_flat_checklist() -> None:
    flat = (ChecklistItem("a", False, 0), ChecklistItem("b", True, 0))
    assert plan_indent_steps(flat, [1, 2]) == []


def test_deeper_levels_become_more_tab_presses() -> None:
    plan = (ChecklistItem("a", False, 0), ChecklistItem("b", False, 3))
    assert plan_indent_steps(plan, [1, 2])[0].levels == 3


# --- Indentation verification -----------------------------------------------


def _nested_body() -> str:
    """`_BODY` after a successful pass: the same paragraphs, two of them nested.

    The leading paragraphs have to match `_BODY`'s, because the paragraph
    indices verification uses are located once, before any keystroke.
    """
    return (
        "<div><h1>Doc</h1></div>"
        "<div>Source: <u>https://quip.com/T1</u></div>"
        "<ul><li>parent</li>"
        "<ul><li>child one</li><li>child two</li></ul>"
        "<li>next parent</li></ul>"
    )


#: Where `_PLAN`'s items sit among `_nested_body()`'s paragraphs: the
#: provenance div is paragraph 0. Located once, before any keystroke.
_NESTED_PARAGRAPHS = [2, 3, 4, 5]


def test_verify_accepts_a_correctly_indented_note() -> None:
    assert verify_indentation(_nested_body(), _PLAN, _NESTED_PARAGRAPHS) is None


def test_verify_rejects_a_flat_note() -> None:
    paragraphs = locate_checklist_paragraphs(_BODY, _PLAN)
    assert paragraphs is not None
    reason = verify_indentation(_BODY, _PLAN, paragraphs)
    assert reason is not None
    assert "depth" in reason


def test_verify_only_checks_depth_up_to_the_applied_point() -> None:
    partial = (
        "<div><h1>Doc</h1></div>"
        "<div>Source: <u>https://quip.com/T1</u></div>"
        "<ul><li>parent</li>"
        "<ul><li>child one</li></ul>"
        "<li>child two</li><li>next parent</li></ul>"
    )
    assert verify_indentation(partial, _PLAN, _NESTED_PARAGRAPHS, applied_through=2) is None


def test_verify_rejects_changed_text() -> None:
    mangled = _nested_body().replace("child two", "child TWO EDITED")
    reason = verify_indentation(mangled, _PLAN, _NESTED_PARAGRAPHS)
    assert reason is not None
    assert "reads" in reason


def test_verify_rejects_a_note_that_lost_paragraphs_under_it() -> None:
    reason = verify_indentation("<ul><li>parent</li></ul>", _PLAN, _NESTED_PARAGRAPHS)
    assert reason is not None
    assert "note structure changed during the pass" in reason


# --- Indentation pass -------------------------------------------------------


@dataclass
class FakeIndentRunner:
    bodies: list[str]
    applied: list[IndentStep] = field(default_factory=list)
    undone: int = 0
    shown: list[str] = field(default_factory=list)
    accessibility_error: Exception | None = None

    def check_accessibility(self) -> None:
        if self.accessibility_error is not None:
            raise self.accessibility_error

    def show_note(self, note_id: str) -> None:
        self.shown.append(note_id)

    def apply_step(self, step: IndentStep, *, origin: int | None = None) -> None:
        self.applied.append(step)

    def undo(self, *, outdent: bool) -> None:
        self.undone += 1

    def read_lines(self, note_id: str) -> list[str]:
        """The editor's lines, kept in step with the body without consuming it."""
        current = self.bodies[min(len(self.applied), len(self.bodies) - 1)]
        return [text for _, text in paragraph_lines(current)]

    def read_body(self, note_id: str) -> str:
        return self.bodies[min(len(self.applied), len(self.bodies) - 1)]


def test_pass_applies_and_verifies_each_step() -> None:
    runner = FakeIndentRunner(bodies=[_BODY, _nested_body()])
    report = indent_notes(runner, [("id-1", "Doc", _PLAN)])

    assert report.notes_indented == 1
    assert report.levels_applied == 2
    assert report.failures == []
    assert runner.undone == 0


def test_pass_skips_a_flat_note_without_typing() -> None:
    """A note already at the depths the plan asks for is never opened.

    The plan is read against the note itself rather than assumed from its
    depths, so this also covers the case the shortcut used to hide: a note
    whose plan is all zeros but which sits too deep still gets corrected.
    """
    flat = tuple(ChecklistItem(item.text, item.checked, 0) for item in _PLAN)
    runner = FakeIndentRunner(bodies=[_BODY])
    report = indent_notes(runner, [("id-1", "Doc", flat)])

    assert report.notes_already_flat == 1
    assert runner.applied == []
    assert runner.shown == []


def test_a_note_whose_lines_will_not_move_does_not_strand_the_others() -> None:
    """One immovable note must not cost every other note its indentation.

    The body never changes here, so each note's step moves nothing. Notes
    refuses to indent a line with no parent above it; the pass sets those
    lines aside, names them, and carries on to the next note.
    """
    runner = FakeIndentRunner(bodies=[_BODY])
    report = indent_notes(runner, [("id-1", "First", _PLAN), ("id-2", "Second", _PLAN)])

    assert runner.undone == 0, "a step that changed nothing must not be 'undone'"
    assert [label for label, _ in report.failures] == ["First", "Second"]
    assert all("would not move" in reason for _, reason in report.failures)
    assert runner.shown == ["id-1", "id-2"]


def test_pass_skips_a_note_whose_lines_do_not_match() -> None:
    runner = FakeIndentRunner(bodies=["<div>unrelated note</div>"])
    report = indent_notes(runner, [("id-1", "Doc", _PLAN)])

    assert report.skipped_unrecognized == 1
    assert runner.applied == []
    assert runner.shown == []


def test_pass_refuses_to_start_without_accessibility() -> None:
    runner = FakeIndentRunner(bodies=[_BODY], accessibility_error=RuntimeError("-1719"))
    with pytest.raises(RuntimeError):
        indent_notes(runner, [("id-1", "Doc", _PLAN)])
    assert runner.applied == []


def test_report_serializes() -> None:
    assert EnexImportReport().as_dict()["documents"] == 0
