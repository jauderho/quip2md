"""Coverage of the checklist indentation pass's planning and failure paths.

This pass types into live notes, so its value is entirely in what it refuses
to do: skip a note it cannot recognise, undo and abort on the first
verification mismatch, and stop the whole run when the automation itself
fails. Those refusals are what this module exercises.

`IndentRunner`'s own methods are driven through a stub of its single
`osascript` call site (`_run`), so no AppleScript is assembled or executed and
`tests/conftest.py`'s guard stays armed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from quip2md import notes_indent
from quip2md.enex import ChecklistItem
from quip2md.notes_import import NotesError
from quip2md.notes_indent import (
    ACCESSIBILITY_HELP,
    IndentReport,
    IndentRunner,
    IndentStep,
    indent_notes,
    locate_checklist_paragraphs,
    paragraph_lines,
    plan_indent_steps,
    verify_indentation,
)


def _items(*spec: tuple[str, int]) -> tuple[ChecklistItem, ...]:
    return tuple(ChecklistItem(text=text, checked=False, depth=depth) for text, depth in spec)


# --- Reading a note body back -----------------------------------------------


def test_paragraph_lines_lists_every_editable_paragraph_in_order() -> None:
    body = (
        "<div>Source: https://example.quip.com/THREAD0013</div>"
        "<h1>Title</h1><h2>Section</h2><h3>Sub</h3>"
        "<ul><li>one</li><li>two</li></ul>"
        "<span>not a paragraph</span>"
    )
    assert paragraph_lines(body) == [
        (0, "Source: https://example.quip.com/THREAD0013"),
        (0, "Title"),
        (0, "Section"),
        (0, "Sub"),
        (1, "one"),
        (1, "two"),
    ]


def test_paragraph_lines_joins_child_nodes_with_a_single_space() -> None:
    """Notes splits a styled line into several nodes; they must read as one line."""
    assert paragraph_lines("<div><b>bold</b><i>ital</i> tail</div>") == [(0, "bold ital tail")]


def test_a_wrapping_div_is_not_a_paragraph_of_its_own() -> None:
    """Notes writes a title as `<div><h1>`; that is one editable line, not two."""
    assert paragraph_lines("<div><h1>Doc</h1></div>") == [(0, "Doc")]


def test_a_div_wrapping_a_list_yields_only_the_list_line() -> None:
    assert paragraph_lines("<div><ul><li>a</li></ul></div>") == [(1, "a")]


def test_an_empty_div_is_still_a_paragraph_the_caret_steps_over() -> None:
    assert paragraph_lines("<div>before</div><div><br></div><div>after</div>") == [
        (0, "before"),
        (0, ""),
        (0, "after"),
    ]


def test_locating_tolerates_the_ragged_whitespace_notes_reports() -> None:
    """`paragraph_lines` keeps a node's inner runs; matching normalizes them away."""
    body = "<ul><li>a   long\nitem</li></ul>"
    assert paragraph_lines(body) == [(1, "a   long\nitem")]
    assert locate_checklist_paragraphs(body, _items(("a long item", 0))) == [0]


def test_paragraph_lines_reads_depth_out_of_the_nested_uls() -> None:
    body = "<ul><li>a</li><ul><li>b</li><ul><li>c</li></ul></ul><li>d</li></ul>"
    assert paragraph_lines(body) == [(1, "a"), (2, "b"), (3, "c"), (1, "d")]


def test_a_sibling_ul_nests_the_lines_it_holds() -> None:
    """Notes puts the child list beside its parent `<li>`, not inside it."""
    assert paragraph_lines("<ul><li>p</li><ul><li>c</li></ul></ul>") == [(1, "p"), (2, "c")]


def test_a_parent_li_does_not_absorb_the_text_of_a_nested_list() -> None:
    assert paragraph_lines("<ul><li>p<ul><li>c</li></ul></li></ul>") == [(1, "p"), (2, "c")]


def test_paragraph_lines_counts_an_ol_wrapper_as_a_level() -> None:
    assert paragraph_lines("<ul><li>a</li><ol><li>b</li></ol></ul>") == [(1, "a"), (2, "b")]


def test_a_body_with_no_list_at_all_has_only_depth_zero_paragraphs() -> None:
    assert paragraph_lines("<div>plain paragraph</div>") == [(0, "plain paragraph")]


def test_a_li_notes_reports_outside_any_list_is_at_depth_zero() -> None:
    """Pinned so a future body shape that loses its `<ul>` is caught, not guessed."""
    assert paragraph_lines("<li>orphan</li>") == [(0, "orphan")]


def test_character_references_notes_leaves_unterminated_are_repaired() -> None:
    """Notes' `body` getter writes `&amp` with no semicolon.

    Verified live against the same note's `plaintext`, which reads `a&b`
    correctly. No HTML parser can decode `&ampb`, so without this repair
    the line read back never equals the line in the plan -- which is why the
    indentation pass refused 18 of the corpus's 94 nested-checklist notes.
    """
    body = "<ul><li>keep a&ampb</li><li>pf=&ltprofile&gt</li></ul>"
    assert paragraph_lines(body) == [
        (1, "keep a&b"),
        (1, "pf=<profile>"),
    ]


def test_a_properly_terminated_reference_is_left_alone() -> None:
    assert paragraph_lines("<ul><li>a&amp;b</li></ul>") == [(1, "a&b")]


def test_text_that_really_contains_an_entity_name_round_trips() -> None:
    """Notes escapes a literal `&amp` as `&ampamp`; that must decode back."""
    assert paragraph_lines("<ul><li>u&ampampv</li></ul>") == [(1, "u&ampv")]


def test_observed_depths_reads_each_located_line_from_the_checklist_root() -> None:
    body = "<div>x</div><ul><li>a</li><ul><li>b</li></ul></ul>"
    assert notes_indent.observed_depths(body, [1, 2]) == [0, 1]


def test_observed_depths_treats_a_vanished_paragraph_as_unindented() -> None:
    assert notes_indent.observed_depths("<ul><li>a</li></ul>", [0, 9]) == [0, 0]


# --- Locating checklist lines -----------------------------------------------


def test_repeated_item_texts_match_their_own_occurrences_in_order() -> None:
    body = "<ul><li>Review</li><li>Review</li><li>Review</li></ul>"
    items = _items(("Review", 0), ("Review", 1), ("Review", 1))
    assert locate_checklist_paragraphs(body, items) == [0, 1, 2]


def test_matching_is_forward_only_so_a_reordered_note_is_rejected() -> None:
    body = "<ul><li>second</li><li>first</li></ul>"
    assert locate_checklist_paragraphs(body, _items(("first", 0), ("second", 0))) is None


def test_extra_paragraphs_between_items_are_skipped() -> None:
    body = "<ul><li>a</li></ul><div>an interruption</div><ul><li>b</li></ul>"
    assert locate_checklist_paragraphs(body, _items(("a", 0), ("b", 1))) == [0, 2]


def test_a_line_the_getter_split_into_pieces_still_matches() -> None:
    """Notes' `body` getter splits a long URL across nodes, inventing spaces.

    The line reads correctly on screen and in `plaintext`; only the getter is
    wrong. Refusing to match it abandoned a whole note over a defect in how it
    was read, so the comparison falls back to ignoring spaces entirely.
    """
    body = (
        "<ul><li><u>https://example.com/media/40025/</u>"
        "<u>a-long-document-name</u><u>.pdf</u></li></ul>"
    )
    items = _items(("https://example.com/media/40025/a-long-document-name.pdf", 0))
    assert locate_checklist_paragraphs(body, items) == [0]


def test_the_loose_fallback_does_not_match_genuinely_different_lines() -> None:
    body = "<ul><li>buy milk</li></ul>"
    assert locate_checklist_paragraphs(body, _items(("buy bread", 0))) is None


def test_caret_positions_come_from_the_editor_not_the_html() -> None:
    """The two models disagree, and only the editor's one steers the caret.

    Measured live: one note's `body` carried a blank paragraph the editor does
    not have, so every caret target after it was off by one and the pass
    scrambled the note's indentation. The verification could not see it,
    because it read the same skewed model back.
    """
    body = "<div>Title</div><div><br></div><ul><li>a</li><li>b</li></ul>"
    editor = ["Title", "a", "b"]  # no blank line: the editor does not have one
    items = _items(("a", 0), ("b", 1))

    assert locate_checklist_paragraphs(body, items) == [2, 3]
    assert notes_indent.locate_lines(editor, items) == [1, 2]


def test_a_note_whose_lines_the_editor_does_not_show_is_skipped() -> None:
    items = _items(("a", 0), ("b", 1))
    assert notes_indent.locate_lines(["Title", "a"], items) is None


def test_an_empty_plan_locates_trivially() -> None:
    assert locate_checklist_paragraphs("<div>x</div>", ()) == []


# --- Planning ---------------------------------------------------------------


def test_duplicate_item_texts_still_produce_one_step_per_run() -> None:
    items = _items(("same", 1), ("same", 1), ("same", 2))
    assert plan_indent_steps(items, [3, 4, 5]) == [
        IndentStep(start=0, count=2, levels=1, paragraph=3),
        IndentStep(start=2, count=1, levels=2, paragraph=5),
    ]


def test_a_depth_jump_from_zero_to_two_is_planned_as_two_tab_presses() -> None:
    items = _items(("parent", 0), ("grandchild", 2))
    assert plan_indent_steps(items, [1, 2]) == [IndentStep(start=1, count=1, levels=2, paragraph=2)]


def test_differing_depths_never_merge_into_one_selection() -> None:
    items = _items(("a", 1), ("b", 2), ("c", 1))
    steps = plan_indent_steps(items, [1, 2, 3])
    assert [(step.start, step.count, step.levels) for step in steps] == [
        (0, 1, 1),
        (1, 1, 2),
        (2, 1, 1),
    ]


def test_a_zero_depth_item_inside_a_run_splits_it() -> None:
    items = _items(("a", 1), ("b", 0), ("c", 1))
    steps = plan_indent_steps(items, [1, 2, 3])
    assert [step.start for step in steps] == [0, 2]


def test_an_empty_plan_needs_no_steps() -> None:
    assert plan_indent_steps((), []) == []


def test_a_note_already_at_the_target_depth_needs_no_steps() -> None:
    """The pass must be safe to re-run.

    Planning absolute targets made a second run indent an already-correct note
    again, pushing every line one level too deep -- observed live on a note the
    previous run had just fixed.
    """
    items = _items(("a", 0), ("b", 1), ("c", 1))
    assert plan_indent_steps(items, [0, 1, 2], [0, 1, 1]) == []


def test_a_partially_indented_note_only_moves_what_is_still_wrong() -> None:
    items = _items(("a", 1), ("b", 1))
    assert plan_indent_steps(items, [0, 1], [1, 0]) == [
        IndentStep(start=1, count=1, levels=1, paragraph=1)
    ]


def test_a_line_indented_too_deep_is_planned_as_an_outdent() -> None:
    items = _items(
        ("a", 1),
    )
    assert plan_indent_steps(items, [0], [3]) == [
        IndentStep(start=0, count=1, levels=-2, paragraph=0)
    ]


# --- Verification -----------------------------------------------------------


def test_verify_ignores_depth_past_the_applied_point_but_still_checks_text() -> None:
    body = "<ul><li>a</li><ul><li>b</li></ul><li>WRONG</li></ul>"
    items = _items(("a", 0), ("b", 1), ("c", 1))
    reason = verify_indentation(body, items, [0, 1, 2], applied_through=2)
    assert reason is not None
    assert "reads 'WRONG'" in reason


def test_verify_reports_the_one_based_position_of_the_bad_line() -> None:
    body = "<ul><li>a</li><li>b</li><li>c</li></ul>"
    items = _items(("a", 0), ("b", 0), ("c", 1))
    reason = verify_indentation(body, items, [0, 1, 2])
    assert reason is not None
    assert reason.startswith("line 3 ")


def test_verify_normalizes_whitespace_before_comparing_text() -> None:
    body = "<ul><li>a   long\nitem</li></ul>"
    assert verify_indentation(body, _items(("a long item", 0)), [0]) is None


def test_verify_ignores_lists_that_are_not_part_of_the_checklist() -> None:
    """The defect this positional check exists for: a note with a second list."""
    body = (
        "<div>Source</div>"
        "<ul><li>bullet one</li><li>bullet two</li></ul>"
        "<ul><li>task</li><ul><li>subtask</li></ul></ul>"
        "<ol><li>numbered</li></ol>"
    )
    items = _items(("task", 0), ("subtask", 1))
    paragraphs = locate_checklist_paragraphs(body, items)
    assert paragraphs is not None
    assert paragraphs == [3, 4]
    assert verify_indentation(body, items, paragraphs) is None


def test_verify_tolerates_the_reshuffle_indenting_a_list_causes() -> None:
    """Notes re-serialises a list when a run inside it is indented.

    Observed live: indenting a run in the middle of a 282-paragraph note left
    284 paragraphs, text and items all intact. Comparing the total count
    rejected that as corruption; re-locating the lines accepts it.
    """
    before = "<ul><li>a</li><li>b</li><li>c</li></ul>"
    after = "<ul><li>a</li><div><br></div><ul><li>b</li></ul><div><br></div><li>c</li></ul>"
    items = _items(("a", 0), ("b", 1), ("c", 0))

    assert len(paragraph_lines(after)) != len(paragraph_lines(before))
    moved = locate_checklist_paragraphs(after, items)
    assert moved is not None
    assert verify_indentation(after, items, moved) is None


def test_verify_reports_a_paragraph_index_that_no_longer_exists() -> None:
    body = "<ul><li>a</li></ul>"
    reason = verify_indentation(body, _items(("a", 0), ("b", 1)), [0, 9])
    assert reason is not None
    assert "note structure changed during the pass" in reason


# --- The pass ---------------------------------------------------------------


@dataclass
class ScriptedIndentRunner:
    """Returns a prepared body for each read, in order; records every call."""

    bodies: list[str]
    applied: list[IndentStep] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)
    undone: int = 0
    undo_directions: list[bool] = field(default_factory=list)
    reads: int = 0
    read_error: Exception | None = None
    show_error: Exception | None = None
    apply_error: Exception | None = None
    undo_error: Exception | None = None

    def check_accessibility(self) -> None:
        return None

    def show_note(self, note_id: str) -> None:
        if self.show_error is not None:
            raise self.show_error
        self.shown.append(note_id)

    def apply_step(self, step: IndentStep, *, origin: int | None = None) -> None:
        if self.apply_error is not None:
            raise self.apply_error
        self.applied.append(step)

    def undo(self, *, outdent: bool) -> None:
        if self.undo_error is not None:
            raise self.undo_error
        self.undone += 1
        self.undo_directions.append(outdent)

    def read_lines(self, note_id: str) -> list[str]:
        """The editor's lines, kept in step with the body without consuming it."""
        current = self.bodies[min(max(self.reads - 1, 0), len(self.bodies) - 1)]
        return [text for _, text in paragraph_lines(current)]

    def read_body(self, note_id: str) -> str:
        if self.read_error is not None:
            raise self.read_error
        self.reads += 1
        return self.bodies[min(self.reads - 1, len(self.bodies) - 1)]


_FLAT = "<ul><li>a</li><li>b</li></ul>"
_INDENTED = "<ul><li>a</li><ul><li>b</li></ul></ul>"
_PLAN = _items(("a", 0), ("b", 1))


@dataclass
class SimulatingIndentRunner:
    """A fake Notes that actually indents, so the convergence loop can run.

    `drag` reproduces the behaviour that forced the loop to exist: indenting a
    line also pushes every deeper line below it one level down, so a plan made
    before the first step is stale immediately.
    """

    texts: list[str]
    depths: list[int]
    drag: bool = False
    applied: list[IndentStep] = field(default_factory=list)
    undone: int = 0
    shown: list[str] = field(default_factory=list)

    def check_accessibility(self) -> None:
        return None

    def show_note(self, note_id: str) -> None:
        self.shown.append(note_id)

    def apply_step(self, step: IndentStep, *, origin: int | None = None) -> None:
        self.applied.append(step)
        last = step.start + step.count - 1
        for index in range(step.start, step.start + step.count):
            self.depths[index] += step.levels
        if self.drag:
            for index in range(last + 1, len(self.depths)):
                if self.depths[index] <= self.depths[last]:
                    break
                self.depths[index] += step.levels

    def undo(self, *, outdent: bool) -> None:
        self.undone += 1
        step = self.applied[-1]
        for index in range(step.start, step.start + step.count):
            self.depths[index] -= 1 if outdent else -1

    def read_lines(self, note_id: str) -> list[str]:
        return list(self.texts)

    def read_body(self, note_id: str) -> str:
        out = []
        for text, depth in zip(self.texts, self.depths, strict=True):
            out.append("<ul>" * (depth + 1) + f"<li>{text}</li>" + "</ul>" * (depth + 1))
        return "".join(out)


def test_the_loop_converges_even_though_indenting_drags_children() -> None:
    """Indenting a parent pushes its already-indented children deeper too.

    A plan computed once, before any keystroke, is wrong the moment that
    happens -- which is how a real note ended up a level too deep in places.
    Re-planning from the note after every step absorbs the side effect.
    """
    items = _items(("a", 1), ("b", 2), ("c", 1), ("d", 0))
    runner = SimulatingIndentRunner(texts=["a", "b", "c", "d"], depths=[0, 0, 0, 0], drag=True)
    report = indent_notes(runner, [("id-1", "Doc", items)])

    assert report.notes_indented == 1
    assert report.failures == []
    assert runner.depths == [1, 2, 1, 0]


def test_a_line_notes_refuses_to_move_is_set_aside_not_undone() -> None:
    """Notes refuses to indent a line with no parent above it.

    The Increase is simply ignored -- so there is nothing to undo, and issuing
    a Decrease "to undo it" would outdent a line that was already correct.
    """

    @dataclass
    class InertRunner(SimulatingIndentRunner):
        """Accepts every step and changes nothing, like a caret in the wrong pane."""

        def apply_step(self, step: IndentStep, *, origin: int | None = None) -> None:
            self.applied.append(step)

    items = _items(("a", 0), ("b", 1))
    runner = InertRunner(texts=["a", "b"], depths=[0, 0])
    report = indent_notes(runner, [("id-1", "Stuck", items)])

    assert runner.undone == 0, "a step that changed nothing must not be 'undone'"
    assert any("would not move" in reason for _, reason in report.failures)


def test_the_pass_gives_up_after_the_step_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A note needing more steps than the budget is reported, not ground through.

    Every step costs a read-back and a keystroke walk, so an unbounded loop on
    a pathological note is minutes of the caret marching up and down it.
    """
    monkeypatch.setattr(notes_indent, "MAX_STEPS_PER_NOTE", 12)
    count = notes_indent.MAX_STEPS_PER_NOTE + 5
    # Alternating depths: no two lines ever merge into one selection.
    items = _items(*[(f"line {i}", i % 2) for i in range(2 * count)])
    runner = SimulatingIndentRunner(
        texts=[f"line {i}" for i in range(2 * count)], depths=[0] * (2 * count)
    )
    report = indent_notes(runner, [("id-1", "Huge", items)])

    assert report.notes_indented == 0
    assert len(runner.applied) == notes_indent.MAX_STEPS_PER_NOTE
    assert any("gave up after" in reason for _, reason in report.failures)


def test_a_read_failure_stops_the_whole_run() -> None:
    runner = ScriptedIndentRunner(bodies=[_FLAT], read_error=RuntimeError("read boom"))
    report = indent_notes(runner, [("id-1", "First", _PLAN), ("id-2", "Second", _PLAN)])

    assert report.notes_considered == 1
    assert report.failures == [("First", "could not read the note: read boom")]
    assert runner.shown == []


def test_a_show_failure_stops_the_whole_run() -> None:
    runner = ScriptedIndentRunner(bodies=[_FLAT], show_error=RuntimeError("show boom"))
    report = indent_notes(runner, [("id-1", "First", _PLAN), ("id-2", "Second", _PLAN)])

    assert report.notes_considered == 1
    assert report.failures == [("First", "could not open the note: show boom")]
    assert runner.applied == []


def test_an_apply_failure_stops_the_whole_run() -> None:
    runner = ScriptedIndentRunner(bodies=[_FLAT], apply_error=RuntimeError("tab boom"))
    report = indent_notes(runner, [("id-1", "First", _PLAN), ("id-2", "Second", _PLAN)])

    assert report.notes_considered == 1
    assert report.failures == [("First", "indent failed: tab boom")]
    assert report.notes_indented == 0


def test_a_note_whose_plan_is_already_satisfied_is_counted_flat_not_retyped() -> None:
    """Every item at depth 0 means no keystrokes -- even though the plan is not empty."""
    runner = ScriptedIndentRunner(bodies=[_FLAT])
    report = indent_notes(runner, [("id-1", "Doc", _items(("a", 0), ("b", 0)))])

    assert report.notes_already_flat == 1
    assert runner.applied == []


def test_a_failing_undo_is_reported_alongside_the_verification_failure() -> None:
    """Text changing under the pass abandons the note; a failed undo is named too."""
    scrambled = "<ul><li>a</li><li>NOT b</li></ul>"
    runner = ScriptedIndentRunner(bodies=[_FLAT, scrambled], undo_error=RuntimeError("undo boom"))
    report = indent_notes(runner, [("id-1", "Doc", _PLAN)])

    reasons = [reason for _, reason in report.failures]
    assert len(reasons) == 2
    assert "verification failed" in reasons[0]
    assert reasons[1] == "undo did not restore the note; check it by hand: Doc"


def test_a_step_that_moves_the_wrong_way_is_walked_back_once_per_level() -> None:
    """Two levels means two Increases, so two Decreases to walk back.

    The step here moves a line *away* from its target, so the pass reverts it
    rather than keeping the damage.
    """
    wrong_way = "<ul><ul><ul><li>a</li></ul></ul><li>b</li></ul>"
    runner = ScriptedIndentRunner(bodies=[_FLAT, wrong_way, _FLAT])
    report = indent_notes(runner, [("id-1", "Doc", _items(("a", 0), ("b", 2)))])

    assert runner.undone == 2
    assert any("would not move" in reason for _, reason in report.failures)


def test_an_undo_that_leaves_the_note_wrong_is_called_out_for_a_human() -> None:
    """A note left half-restored is worse than one left flat: say so by name."""
    scrambled = "<ul><li>a</li><li>NOT b</li></ul>"
    still_wrong = "<ul><li>a</li><ul><li>NOT b</li></ul></ul>"
    # read 1: locate; read 2: the text mismatch; read 3: after the undo.
    runner = ScriptedIndentRunner(bodies=[_FLAT, scrambled, still_wrong])
    report = indent_notes(runner, [("id-1", "Doc", _items(("a", 0), ("b", 2)))])

    reasons = [reason for _, reason in report.failures]
    assert len(reasons) == 2
    assert "verification failed" in reasons[0]
    assert reasons[1] == "undo did not restore the note; check it by hand: Doc"


def test_a_successful_pass_over_two_notes_counts_both() -> None:
    runner = ScriptedIndentRunner(bodies=[_FLAT, _INDENTED, _FLAT, _INDENTED])
    report = indent_notes(runner, [("id-1", "First", _PLAN), ("id-2", "Second", _PLAN)])

    assert report.notes_indented == 2
    assert report.levels_applied == 2
    assert report.failures == []
    assert runner.shown == ["id-1", "id-2"]


def test_the_report_serializes_its_failures() -> None:
    report = IndentReport(notes_considered=1, failures=[("Doc", "nope")])
    assert report.as_dict()["failures"] == [{"note": "Doc", "reason": "nope"}]


# --- IndentRunner -----------------------------------------------------------


def test_the_runner_refuses_to_construct_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notes_indent.sys, "platform", "linux")
    with pytest.raises(NotesError, match="requires macOS"):
        IndentRunner()


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str = "", stderr: str = ""
) -> list[list[str]]:
    """Replace `subprocess.run` with a stub that cannot execute anything.

    Stronger than `conftest.py`'s guard for the duration of these tests: the
    stub has no path to a real process at all, and asserts the command really
    is the `osascript` invocation under test.
    """
    seen: list[list[str]] = []

    def fake_run(command: Sequence[str], *args: Any, **kwargs: Any) -> FakeCompletedProcess:
        assert list(command)[:2] == ["osascript", "-e"], command
        seen.append(list(command))
        return FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(notes_indent.subprocess, "run", fake_run)
    return seen


def test_read_body_returns_osascripts_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_subprocess(monkeypatch, returncode=0, stdout="<div>body</div>")
    assert IndentRunner().read_body("note-1") == "<div>body</div>"
    assert seen[0][-1] == "note-1"


def test_apply_step_sends_paragraph_line_count_and_tab_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Shift-Option-Down per selected paragraph, including the first."""
    seen = _stub_subprocess(monkeypatch, returncode=0)
    IndentRunner().apply_step(IndentStep(start=0, count=3, levels=2, paragraph=7))
    assert seen[0][-5:] == ["7", "3", "2", "Increase", "0"]


def test_a_negative_step_outdents_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line already too deep is brought back up, not indented further."""
    seen = _stub_subprocess(monkeypatch, returncode=0)
    IndentRunner().apply_step(IndentStep(start=0, count=1, levels=-2, paragraph=4))
    assert seen[0][-5:] == ["4", "1", "2", "Decrease", "0"]


def test_a_step_that_knows_where_the_caret_is_walks_only_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notes keeps the selection after an indent, so the next step starts there.

    Walking from the top every time makes a pass cost O(steps x paragraphs):
    on a 1,400-line note the caret marched down a thousand paragraphs per step,
    which is what made a full run take an hour. The gap from paragraph 40 to
    paragraph 44 is three Option-Downs, not forty-four.
    """
    seen = _stub_subprocess(monkeypatch, returncode=0)
    IndentRunner().apply_step(IndentStep(start=9, count=2, levels=1, paragraph=44), origin=40)
    assert seen[0][-5:] == ["3", "2", "1", "Increase", "1"]


def test_a_step_that_would_move_backwards_re_anchors_at_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative movement is forward-only; anything else starts over."""
    seen = _stub_subprocess(monkeypatch, returncode=0)
    IndentRunner().apply_step(IndentStep(start=0, count=1, levels=1, paragraph=4), origin=9)
    assert seen[0][-5:] == ["4", "1", "1", "Increase", "0"]


def test_undo_reverses_whichever_direction_the_step_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_subprocess(monkeypatch, returncode=0)
    runner = IndentRunner()
    runner.undo(outdent=True)
    runner.undo(outdent=False)
    assert [call[-1] for call in seen] == ["Decrease", "Increase"]


def test_the_navigation_script_crosses_into_the_target_paragraph_only_when_needed() -> None:
    """Pinned because it cannot be exercised live: Cmd-Up already sits at line 0."""
    script = notes_indent._AS_APPLY_STEP
    assert "key code 125 using {option down}" in script
    assert "if downCount > 0 then\n                    key code 124" in script
    assert "key code 123" not in script  # Option-Left went to the previous word
    # Relative mode collapses the surviving selection forward first.
    assert 'if isRelative is "1" then\n                key code 124' in script


def test_show_note_and_undo_and_check_accessibility_each_shell_out_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_subprocess(monkeypatch, returncode=0, stdout="true\n")
    runner = IndentRunner()
    runner.check_accessibility()
    runner.undo(outdent=True)
    assert len(seen) == 2


def test_showing_a_note_that_leaves_the_editor_unfocused_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`show note` focuses the note *list*, where every keystroke is a no-op.

    Observed live: 86 of 94 notes were "processed" without a single character
    reaching them, because the Format menu and arrow keys were being sent to an
    AXOutline. Typing at an unknown focus is worse than stopping.
    """
    _stub_subprocess(monkeypatch, returncode=0, stdout="no-editor\n")
    with pytest.raises(NotesError, match="could not focus the note editor"):
        IndentRunner().show_note("note-1")


def test_showing_a_note_that_focuses_the_editor_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_subprocess(monkeypatch, returncode=0, stdout="focused\n")
    IndentRunner().show_note("note-1")
    assert len(seen) == 1


def test_the_show_script_focuses_the_editor_text_area() -> None:
    """Pinned: it cannot be exercised without a live Notes window."""
    script = notes_indent._AS_SHOW_NOTE
    assert "set focused of (text area 1 of sa) to true" in script
    # Searched across every window: `window 1` is not reliably the editor's.
    assert "repeat with w in windows" in script
    assert "no-editor" in script


def test_accessibility_is_refused_when_ui_scripting_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero exit is not consent.

    `UI elements enabled` answers "false" rather than failing, and reading a
    process name -- what this probe used to do -- succeeds under plain
    Automation access. Both let the pass open a note and only then be refused
    the first Tab with error 1002.
    """
    _stub_subprocess(monkeypatch, returncode=0, stdout="false\n")
    with pytest.raises(NotesError) as excinfo:
        IndentRunner().check_accessibility()
    assert str(excinfo.value).startswith(ACCESSIBILITY_HELP)


def test_a_keystroke_refusal_is_surfaced_as_the_accessibility_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed live: sending keystrokes is refused with 1002, not -1719."""
    _stub_subprocess(
        monkeypatch,
        returncode=1,
        stderr=(
            "execution error: System Events got an error: osascript is not "
            "allowed to send keystrokes. (1002)"
        ),
    )
    with pytest.raises(NotesError) as excinfo:
        IndentRunner().apply_step(IndentStep(start=0, count=1, levels=1, paragraph=3))
    assert str(excinfo.value).startswith(ACCESSIBILITY_HELP)


def test_a_minus_1719_error_is_surfaced_as_the_accessibility_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(
        monkeypatch,
        returncode=1,
        stderr="execution error: System Events got an error: ... (-1719)",
    )
    with pytest.raises(NotesError) as excinfo:
        IndentRunner().check_accessibility()
    assert str(excinfo.value).startswith(ACCESSIBILITY_HELP)
    assert "-1719" in (excinfo.value.stderr or "")


def test_any_other_osascript_failure_keeps_its_own_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(monkeypatch, returncode=2, stderr="something else went wrong")
    with pytest.raises(NotesError, match="status 2"):
        IndentRunner().undo(outdent=True)
