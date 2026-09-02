"""Restore true checklist indentation in Notes after an `.enex` import.

Notes' Evernote importer always produces **top-level** checklist items: every
ENML shape that could express nesting was tested and all thirteen flatten (see
`enex.py`). The Notes *editor*, however, indents a checklist item natively --
keeping the checkbox and its checked state -- and the result is visible through
the scripting `body` getter as nested `<ul>`s. That combination is what makes
this pass both possible and verifiable.

Indentation is applied through **Format > Indentation > Increase**, not by
pressing Tab. Tab was tried first and silently did nothing: with a multi-line
selection Notes does not treat it as an indent request, and the run aborted on
its own verification with the note untouched. The menu item acts on exactly the
current selection and cannot do anything but change indentation, which also
removes the risk that a drifted caret types a literal tab into prose.

The algorithm, per note:

* Walk the plan (`enex.ChecklistItem`s, already in document order) top-down and
  apply Increase once per level. Top-down matters: Notes clamps an item to at
  most one level deeper than the item above it, so depths only apply correctly
  in order.
* Consecutive items needing the same depth are indented as one selection, since
  the menu command applies to every selected line at once.
* **Every step is verified before the next one runs.** After each step the note
  is read back and compared against the depths expected so far; a mismatch
  applies Decrease once per Increase, checks that the note really was restored,
  and aborts the whole run.

That per-step check is what makes the pass safe to point at real notes.
Navigation is inherently approximate -- the caret moves by paragraph, but a
long checklist item can wrap, and nothing in the scripting interface reports
where the caret actually is -- so the design assumes navigation *can* drift and
catches it immediately instead of trusting it. Each step's target line is
re-located from a fresh read of the note, because indenting a run inside a list
makes Notes re-serialise it and shift every line below. Verification compares
more than depth: every plan line must still be findable, in order, by text.

This needs macOS Accessibility permission for whatever binary runs it (System
Events UI scripting). Without it the pass refuses to start rather than
half-applying; the non-breaking-space fallback in `enex.py` covers that case.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from bs4 import BeautifulSoup, NavigableString, Tag

from quip2md.enex import ChecklistItem
from quip2md.notes_import import NotesError

logger = logging.getLogger("quip2md.notes_indent")

_OSASCRIPT_TIMEOUT_SECONDS = 120.0

#: Safety net on the convergence loop, not a budget: a step now costs a short
#: hop from the previous selection rather than a walk from the top of the
#: document, so a long note can afford hundreds. What actually stops the loop
#: is running out of work, or a line Notes will not move. The corpus's worst
#: note needs a little over three hundred.
MAX_STEPS_PER_NOTE = 600

#: Menu items under Format > Indentation. English-only, like the rest of the
#: AppleScript here.
_INCREASE = "Increase"
_DECREASE = "Decrease"

#: Raised by System Events when the running binary lacks Accessibility access.
#: Both mean the same thing to a user: this binary may not drive the UI.
#: `-1719` is raised when assistive access is off outright, `1002` when the
#: process specifically may not send keystrokes.
_TCC_ACCESSIBILITY_ERRORS = ("-1719", "(1002)", "not allowed to send keystrokes")

ACCESSIBILITY_HELP = (
    "This pass needs macOS Accessibility permission. Grant it in "
    "System Settings > Privacy & Security > Accessibility for the application "
    "running quip2md (your terminal), then run the command again. Without it, "
    "checklists import correctly but stay flat."
)


@dataclass(slots=True)
class IndentReport:
    """Outcome of one indentation pass."""

    notes_considered: int = 0
    notes_indented: int = 0
    notes_already_flat: int = 0
    skipped_unrecognized: int = 0
    levels_applied: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "notes_considered": self.notes_considered,
            "notes_indented": self.notes_indented,
            "notes_already_flat": self.notes_already_flat,
            "skipped_unrecognized": self.skipped_unrecognized,
            "levels_applied": self.levels_applied,
            "failures": [{"note": note, "reason": reason} for note, reason in self.failures],
        }


class IndentRunnerProtocol(Protocol):
    """The UI automation this pass needs, behind a seam for tests."""

    def check_accessibility(self) -> None: ...

    def show_note(self, note_id: str) -> None: ...

    def apply_step(self, step: IndentStep, *, origin: int | None = None) -> None: ...

    def undo(self, *, outdent: bool) -> None: ...

    def read_body(self, note_id: str) -> str: ...

    def read_lines(self, note_id: str) -> list[str]: ...


#: Body elements Notes renders as one editable paragraph each.
_PARAGRAPH_TAGS = ("div", "li", "h1", "h2", "h3")

#: `<ul>` ancestors of an *unindented* checklist line: the checklist's own one.
#: `ChecklistItem.depth` counts from there, `paragraph_lines` from the document.
_CHECKLIST_BASE_DEPTH = 1


@dataclass(slots=True, frozen=True)
class IndentStep:
    """Indent `count` consecutive checklist lines by `levels` each.

    `start` is the zero-based index of the first checklist line in the note's
    checklist sequence; `paragraph` is where that line actually sits among the
    note's paragraphs, which is what the caret has to be moved to.
    """

    start: int
    count: int
    levels: int
    paragraph: int


def _list_depth(tag: Tag) -> int:
    """How many `<ul>`/`<ol>` ancestors a tag has."""
    depth = 0
    node = tag.parent
    while node is not None:
        if node.name in ("ul", "ol"):
            depth += 1
        node = node.parent
    return depth


def _own_text(li: Tag) -> str:
    """An `<li>`'s own text, excluding the nested lists Notes puts inside it.

    Notes serialises a nested checklist by placing the child `<ul>` inside (or
    beside) its parent `<li>`; without this the parent line would read as its
    whole subtree.
    """
    parts: list[str] = []
    for descendant in li.descendants:
        if not isinstance(descendant, NavigableString):
            continue
        node = descendant.parent
        nested = False
        while node is not None and node is not li:
            if node.name in ("ul", "ol"):
                nested = True
                break
            node = node.parent
        if nested:
            continue
        stripped = descendant.strip()
        if stripped:
            parts.append(stripped)
    return " ".join(parts)


#: Notes' `body` getter writes character references **without the closing
#: semicolon**: an item reading `a&b` comes back as `a&ampb`, and
#: a URL's `&param=` as `&ampparam=`. No HTML parser can decode that, so
#: the text read back differs from the text that is really in the note --
#: verified against the same note's `plaintext`, which reads `a&b`
#: correctly. Left alone this silently defeats line matching: 18 of the corpus's
#: 94 nested-checklist notes were refused by the indentation pass because of it.
#: Substitution is left-to-right and non-overlapping, so genuine text that
#: contains `&amp` (which Notes emits as `&ampamp`) repairs to `&amp;amp` and
#: still decodes back to the literal `&amp`.
_UNTERMINATED_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|apos|nbsp)(?!;)")


def _repair_entities(body: str) -> str:
    """Put back the semicolons Notes' `body` getter leaves off."""
    return _UNTERMINATED_ENTITY_RE.sub(r"&\1;", body)


def paragraph_lines(body: str) -> list[tuple[int, str]]:
    """`(depth, text)` for every editable paragraph, in document order.

    A checklist line is a `<li>`, but the note also holds `<div>` paragraphs
    (the provenance line, prose, blank lines) and headings, so a checklist
    item's position among *paragraphs* is not its position among checklist
    items -- and the caret moves by paragraph, so this is the model the
    keystroke arithmetic is built on.

    `depth` is the number of `<ul>`/`<ol>` ancestors, so an unindented
    checklist line is at depth 1 and each Increase adds one. A `<div>` or heading
    is at depth 0 and only counts when it is innermost: Notes writes
    `<div><h1>Doc</h1></div>` for a single title line, and an empty
    `<div><br></div>` is one (empty) editable line that the caret must still
    step over.
    """
    soup = BeautifulSoup(_repair_entities(body), "html.parser")
    lines: list[tuple[int, str]] = []
    for tag in soup.find_all(_PARAGRAPH_TAGS):
        if tag.name == "li":
            lines.append((_list_depth(tag), _own_text(tag)))
        elif tag.find(_PARAGRAPH_TAGS) is None:
            lines.append((0, tag.get_text(" ", strip=True)))
    return lines


def locate_lines(lines: Sequence[str], items: Sequence[ChecklistItem]) -> list[int] | None:
    """Index of each checklist item among the editor's own lines.

    The caret moves over *editor* lines, and those are not the paragraphs the
    `body` getter reports: measured live, one note's HTML carried a blank line
    the editor does not have, and another differed by thirty around its
    attachments. Positioning from the HTML therefore aimed every step one or
    more lines off -- invisibly, because the verification read the same skewed
    model back. Caret arithmetic uses this; depth still comes from the HTML.
    """
    indices: list[int] = []
    cursor = 0
    for item in items:
        while cursor < len(lines) and not _same_line(lines[cursor], item.text):
            cursor += 1
        if cursor >= len(lines):
            return None
        indices.append(cursor)
        cursor += 1
    return indices


def locate_checklist_paragraphs(body: str, items: Sequence[ChecklistItem]) -> list[int] | None:
    """Paragraph index of each checklist item, or `None` if they cannot be found.

    Matching is on text alone, at any depth: a note that was imported flat and
    one that is half-indented must both locate to the same paragraphs, since
    these indices are computed once and reused for every verification. Matching
    is in order and forward-only, so repeated item texts (common in these
    documents) still line up with the right occurrences. Returning `None` makes
    the caller skip the note rather than type into it blind.
    """
    paragraphs = paragraph_lines(body)
    indices: list[int] = []
    cursor = 0
    for item in items:
        while cursor < len(paragraphs) and not _same_line(paragraphs[cursor][1], item.text):
            cursor += 1
        if cursor >= len(paragraphs):
            return None
        indices.append(cursor)
        cursor += 1
    return indices


def plan_indent_steps(
    items: Sequence[ChecklistItem],
    paragraphs: Sequence[int],
    current: Sequence[int] | None = None,
) -> list[IndentStep]:
    """Group a checklist plan into the fewest indent operations that realise it.

    `current[i]` is the depth item `i` sits at *now* (0 when unindented, which
    is what a freshly imported note looks like and hence the default). Steps
    carry the **difference**, not the target: a positive `levels` means that
    many Increases, a negative one that many Decreases, and an item already at
    its target contributes no step at all. Planning absolute targets instead
    made the pass destructive to re-run -- it indented an already-correct note
    a second time, pushing every line one level too deep.

    A run is only merged when the items are consecutive *paragraphs* as well as
    consecutive checklist entries -- otherwise one selection would sweep up an
    unrelated line in between.
    """
    depths = [0] * len(items) if current is None else list(current)
    deltas = [item.depth - depth for item, depth in zip(items, depths, strict=True)]

    steps: list[IndentStep] = []
    index = 0
    while index < len(items):
        delta = deltas[index]
        if delta == 0:
            index += 1
            continue
        run_end = index
        while (
            run_end + 1 < len(items)
            and deltas[run_end + 1] == delta
            and paragraphs[run_end + 1] == paragraphs[run_end] + 1
        ):
            run_end += 1
        steps.append(
            IndentStep(
                start=index,
                count=run_end - index + 1,
                levels=delta,
                paragraph=paragraphs[index],
            )
        )
        index = run_end + 1
    return steps


def observed_depths(body: str, paragraphs: Sequence[int]) -> list[int]:
    """Depth of each located checklist line, counted from the checklist root."""
    lines = paragraph_lines(body)
    return [
        lines[index][0] - _CHECKLIST_BASE_DEPTH if index < len(lines) else 0 for index in paragraphs
    ]


def _wrong_count(items: Sequence[ChecklistItem], observed: Sequence[int]) -> int:
    """How many lines are not yet at the depth the plan asks for."""
    return sum(1 for item, depth in zip(items, observed, strict=True) if item.depth != depth)


def _depth_distance(items: Sequence[ChecklistItem], observed: Sequence[int]) -> int:
    """Total depth error over the note: the pass's measure of progress.

    Counting *wrong lines* instead hides real movement -- a line driven from
    depth 0 to 1 when it wants 3 is closer but still wrong -- and made the pass
    abandon notes it was in the middle of fixing.
    """
    return sum(abs(item.depth - depth) for item, depth in zip(items, observed, strict=True))


def _ignoring(
    items: Sequence[ChecklistItem], observed: Sequence[int], stuck: set[int]
) -> list[int]:
    """`observed`, but with stuck lines reported as already correct.

    Planning then generates no step for them, so one immovable line does not
    block every correction below it.
    """
    return [items[index].depth if index in stuck else depth for index, depth in enumerate(observed)]


def verify_text(body: str, items: Sequence[ChecklistItem], paragraphs: Sequence[int]) -> str | None:
    """Check that every located line still reads what the plan says.

    Depth is deliberately *not* checked here. Indenting a line drags its
    children with it, so a step legitimately changes the depth of lines it did
    not target; the convergence loop fixes those on the next pass. Text, by
    contrast, must never change -- no indent operation alters it, so a
    difference means the caret was somewhere unexpected.
    """
    lines = paragraph_lines(body)
    for position, (item, index) in enumerate(zip(items, paragraphs, strict=True)):
        if index >= len(lines):
            return f"note structure changed during the pass: paragraph {index} is gone"
        text = lines[index][1]
        if not _same_line(text, item.text):
            return f"line {position + 1} reads {text!r}, expected {item.text!r}"
    return None


def verify_indentation(
    body: str,
    items: Sequence[ChecklistItem],
    paragraphs: Sequence[int],
    *,
    applied_through: int | None = None,
    baseline: Sequence[int] | None = None,
) -> str | None:
    """Check a note's read-back body against the plan; `None` means it matches.

    Verification is *positional*: only the paragraphs in `paragraphs` are
    compared, so the bullet lists, numbered lists and prose that share the note
    are none of this function's business. A note with a second list used to
    fail here after the very first step.

    `paragraphs` must be located from the *same* body being verified. Notes
    re-serialises a list when a run inside it is indented -- two extra blank
    paragraphs, observed live -- so positions taken before the step do not
    survive it. An earlier version compared the total paragraph count instead
    and rejected that legitimate reshuffle as corruption.

    `applied_through` limits the depth check to the first N items -- the ones a
    partially-completed pass has already indented. The rest must still be at
    `baseline`, the depths the note had before the pass started; that defaults
    to flat, which is how a freshly imported note arrives, but a note the pass
    has already worked on is not flat and must not be judged as if it were.
    Text is checked at every located paragraph, since no step changes it.
    """
    lines = paragraph_lines(body)
    limit = len(items) if applied_through is None else applied_through
    was = [0] * len(items) if baseline is None else list(baseline)

    for position, (item, index) in enumerate(zip(items, paragraphs, strict=True)):
        if index >= len(lines):
            return f"note structure changed during the pass: paragraph {index} is gone"
        depth, text = lines[index]
        if not _same_line(text, item.text):
            return f"line {position + 1} reads {text!r}, expected {item.text!r}"
        wanted = item.depth if position < limit else was[position]
        expected_depth = wanted + _CHECKLIST_BASE_DEPTH
        if depth != expected_depth:
            return f"line {position + 1} ({text!r}) is at depth {depth}, expected {expected_depth}"
    return None


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _loose(text: str) -> str:
    """`text` with every space removed, for a last-resort comparison.

    Notes' `body` getter sometimes splits a long run -- a URL, most often --
    across nodes, which inserts a space that is not in the note at all. The
    line reads correctly on screen and in `plaintext`, so refusing to match it
    would abandon a note over a defect in the getter rather than in the note.
    """
    return "".join(text.split())


def _same_line(left: str, right: str) -> bool:
    return _normalize(left) == _normalize(right) or _loose(left) == _loose(right)


def _undo_step(
    runner: IndentRunnerProtocol,
    note_id: str,
    items: Sequence[ChecklistItem],
    paragraphs: Sequence[int],
    step: IndentStep,
    baseline: Sequence[int],
) -> bool:
    """Undo an applied step; `True` when the note is back to its earlier state.

    A step moves its selection `abs(levels)` places, so undoing it means the
    same number of moves the other way. The result is read back and checked
    against the note as it stood *before* this step: a half-undone note is
    worse than an unindented one, so the caller has to be able to say so.
    """
    try:
        for _ in range(abs(step.levels)):
            runner.undo(outdent=step.levels > 0)
        restored = runner.read_body(note_id)
    except Exception:  # broad by design: the caller reports and stops either way
        logger.debug("could not undo the failed step on note %s", note_id, exc_info=True)
        return False
    # Re-locate rather than reuse `paragraphs`: the step being undone may have
    # shifted the lines, which is what put us here.
    settled = locate_checklist_paragraphs(restored, items) or paragraphs
    return (
        verify_indentation(restored, items, settled, applied_through=step.start, baseline=baseline)
        is None
    )


def _abandon(
    report: IndentReport,
    runner: IndentRunnerProtocol,
    note_id: str,
    label: str,
    items: Sequence[ChecklistItem],
    paragraphs: Sequence[int],
    step: IndentStep,
    baseline: Sequence[int],
    reason: str,
) -> None:
    """Record a note's failure and put the step that caused it back."""
    report.failures.append((label, f"verification failed: {reason}"))
    if not _undo_step(runner, note_id, items, paragraphs, step, baseline):
        report.failures.append((label, f"undo did not restore the note; check it by hand: {label}"))


def indent_notes(
    runner: IndentRunnerProtocol,
    targets: Sequence[tuple[str, str, tuple[ChecklistItem, ...]]],
) -> IndentReport:
    """Apply and verify indentation for `(note_id, label, plan)` triples.

    A note whose lines fail verification is undone, named in the report, and
    abandoned; the pass moves on. Notes are independent and each step is
    checked against its own note, so one stubborn note is no reason to leave
    the rest flat. An automation failure -- the note cannot be read, shown, or
    driven at all -- stops the whole run instead, because that is systemic.
    """
    report = IndentReport()
    runner.check_accessibility()

    for position, (note_id, label, items) in enumerate(targets, start=1):
        report.notes_considered += 1
        if not any(item.depth for item in items):
            report.notes_already_flat += 1
            continue

        try:
            body = runner.read_body(note_id)
        except Exception as exc:  # broad by design: reported, then the run stops
            report.failures.append((label, f"could not read the note: {exc}"))
            return report

        paragraphs = locate_checklist_paragraphs(body, items)
        if paragraphs is None:
            # The note does not look like what was imported. Skip it rather
            # than type into it on a guess.
            report.skipped_unrecognized += 1
            report.failures.append((label, "skipped: checklist lines did not match the plan"))
            continue

        # The plan is recomputed from the note after *every* step, not once
        # up front. Indenting a line drags its already-indented children one
        # level deeper with it, so a plan made against the starting state goes
        # stale the moment the first step lands -- which is how a note ended up
        # one level too deep in places. Re-reading and re-planning turns the
        # pass into a convergence loop that corrects its own side effects.
        baseline = observed_depths(body, paragraphs)
        if not plan_indent_steps(items, paragraphs, baseline):
            report.notes_already_flat += 1
            continue

        try:
            runner.show_note(note_id)
        except Exception as exc:  # broad by design: reported, then the run stops
            report.failures.append((label, f"could not open the note: {exc}"))
            return report

        # Only now: the editor's line list needs an open window to read.
        try:
            carets = locate_lines(runner.read_lines(note_id), items)
        except Exception as exc:  # broad by design: reported, then the run stops
            report.failures.append((label, f"could not read the note's lines: {exc}"))
            return report
        if carets is None:
            report.skipped_unrecognized += 1
            report.failures.append((label, "skipped: the editor's lines did not match the plan"))
            continue

        logger.info(
            "indenting %d/%d: %r (%d line(s), %d to move)",
            position,
            len(targets),
            label,
            len(items),
            sum(1 for item, was in zip(items, baseline, strict=True) if item.depth != was),
        )

        observed = baseline
        # Where the caret sits, as a checklist-item index. Notes keeps the
        # selection after an indent, so the next step can start from there
        # instead of walking from the top of the document -- the difference
        # between O(steps x paragraphs) keystrokes and O(paragraphs).
        cursor: int | None = None
        failed_note = False
        # Lines Notes will not move. Indent is refused where there is no valid
        # parent above a line, and one such line must not cost the note every
        # other correction, so they are set aside and the rest carries on.
        stuck: set[int] = set()
        distance = _depth_distance(items, observed)

        for _iteration in range(MAX_STEPS_PER_NOTE):
            steps = plan_indent_steps(items, carets, _ignoring(items, observed, stuck))
            if not steps:
                break
            step = steps[0]
            aimed = replace(step, paragraph=carets[step.start])
            origin = None if cursor is None or cursor >= step.start else carets[cursor]
            try:
                runner.apply_step(aimed, origin=origin)
            except Exception as exc:  # broad by design: reported, then the run stops
                report.failures.append((label, f"indent failed: {exc}"))
                return report

            body = runner.read_body(note_id)
            moved = locate_checklist_paragraphs(body, items)
            shifted = locate_lines(runner.read_lines(note_id), items)
            if moved is None or shifted is None:
                # The plan's lines are no longer all findable, in order, by
                # text: something changed that indenting alone cannot explain.
                # That is systemic to this note, so it is abandoned outright.
                _abandon(
                    report,
                    runner,
                    note_id,
                    label,
                    items,
                    paragraphs,
                    aimed,
                    observed,
                    "the checklist no longer matches the plan",
                )
                failed_note = True
                break

            reason = verify_text(body, items, moved)
            if reason is not None:
                _abandon(report, runner, note_id, label, items, paragraphs, aimed, observed, reason)
                failed_note = True
                break

            after = observed_depths(body, moved)
            moved_distance = _depth_distance(items, after)
            if moved_distance >= distance:
                # Progress is measured as total depth error, not the number of
                # wrong lines: driving a line from depth 0 to 1 of a wanted 3 is
                # real progress that leaves the line still wrong. Counting lines
                # made the pass abandon notes it was successfully fixing.
                if after != observed:
                    # It moved something, just not usefully. Put it back.
                    for _ in range(abs(aimed.levels)):
                        runner.undo(outdent=aimed.levels > 0)
                    body = runner.read_body(note_id)
                    reverted = locate_checklist_paragraphs(body, items)
                    moved = reverted if reverted is not None else moved
                    after = observed_depths(body, moved)
                # A step that changed nothing at all needs no undo -- and must
                # not get one, because Decrease is not a no-op even where
                # Increase was, so "undoing" it would outdent a correct line.
                stuck.update(range(step.start, step.start + step.count))
                cursor = None
                observed, paragraphs, carets = after, moved, shifted
                distance = _depth_distance(items, observed)
                continue

            distance = moved_distance
            observed, paragraphs, carets = after, moved, shifted
            # A line Notes refused earlier often becomes movable once the lines
            # around it settle -- it needs a parent above it, and that parent
            # may only just have arrived. Any real progress therefore earns
            # every stuck line another try, which is what lets one run finish
            # the job instead of converging over four.
            stuck.clear()
            cursor = step.start + step.count - 1
            report.levels_applied += abs(step.levels) * step.count
        else:
            report.failures.append(
                (
                    label,
                    f"gave up after {MAX_STEPS_PER_NOTE} steps with "
                    f"{_wrong_count(items, observed)} line(s) still wrong",
                )
            )
            failed_note = True

        if stuck and not failed_note:
            report.failures.append(
                (
                    label,
                    f"{len(stuck)} line(s) would not move; Notes refuses to indent a "
                    "line that has no parent above it. The rest of the note was set.",
                )
            )

        if failed_note:
            continue
        report.notes_indented += 1

    return report


# --- Real UI automation -------------------------------------------------

# `UI elements enabled` reports whether *this* process is trusted for
# accessibility, which is the grant that sending keystrokes needs. Reading
# process names is not: that works under plain Automation access, so a probe
# built on it passes and the first Tab then fails with error 1002.
_AS_CHECK_ACCESSIBILITY = """
on run argv
    tell application "System Events"
        return UI elements enabled
    end tell
end run
"""

# Showing a note is not the same as putting the caret in it. `show note` leaves
# keyboard focus on the note *list* (an AXOutline), where Cmd-Up, Option-Down
# and the Format menu all silently do nothing -- the pass then "applies" every
# step, verifies, finds nothing changed, and abandons a note it never touched.
# Observed live: 86 of 94 notes failed this way. So the editor's text area is
# focused explicitly, and a note whose editor cannot be found is an error rather
# than something to type at blindly.
_AS_SHOW_NOTE = """
on run argv
    tell application "Notes"
        activate
        show note id (item 1 of argv)
    end tell
    repeat 6 times
        delay 0.3
        try
            tell application "System Events"
                tell process "Notes"
                    repeat with w in windows
                        repeat with sg in splitter groups of w
                            repeat with sa in scroll areas of sg
                                if (count of text areas of sa) > 0 then
                                    set focused of (text area 1 of sa) to true
                                    return "focused"
                                end if
                            end repeat
                        end repeat
                    end repeat
                end tell
            end tell
        end try
    end repeat
    return "no-editor"
end run
"""

#: The text area's value, one line per editable line -- the caret's own model
#: of the note, which the `body` getter's paragraphs do not reliably match.
_AS_READ_LINES = """
on run argv
    repeat 6 times
        try
            tell application "System Events"
                tell process "Notes"
                    repeat with w in windows
                        repeat with sg in splitter groups of w
                            repeat with sa in scroll areas of sg
                                if (count of text areas of sa) > 0 then
                                    return value of attribute "AXValue" of (text area 1 of sa)
                                end if
                            end repeat
                        end repeat
                    end repeat
                end tell
            end tell
        end try
        delay 0.3
    end repeat
    return ""
end run
"""

_AS_READ_BODY = """
on run argv
    tell application "Notes"
        return body of note id (item 1 of argv)
    end tell
end run
"""

# Navigation is deliberately paragraph-wise (option+arrow), not line-wise: a
# long checklist item wraps onto several visual lines, and plain arrow keys
# move by visual line, which would silently mis-count. Nothing in the scripting
# interface reports the caret position, so the caller verifies the result of
# every step instead of trusting this.
#
# The arithmetic below follows macOS text-navigation semantics:
#
#   Cmd-Up            start of the document
#   Option-Down       end of the *current* paragraph; repeated, the end of each
#                     following paragraph in turn
#   Shift-Option-Down extends the selection the same way
#   Right             from the end of a paragraph, the start of the next one
#
# So to put the caret at the start of zero-based paragraph P: Cmd-Up, then
# Option-Down P times (which lands at the end of paragraph P-1, or leaves the
# caret at the document start when P is 0), then one plain Right to cross into
# paragraph P -- skipped when P is 0, where the caret is already there and a
# Right would step one character *into* the first line. Shift-Option-Down then
# runs `count` times to cover paragraphs P through P+count-1 (one press per
# paragraph, including the first: the caret starts at that paragraph's start,
# not its end).
#
# WARNING: this sequence is derived from the documented key behaviour and is
# NOT verified against a live Notes window -- the environment it was written in
# has no Accessibility permission. The per-step read-back verification in
# `indent_notes` is what guards it: a wrong count mis-indents one step, which
# is detected and undone before anything else is typed.
#
# key codes: 126 up, 125 down, 123 left, 124 right, 48 tab.
_AS_APPLY_STEP = """
on run argv
    set downCount to (item 1 of argv) as integer
    set lineCount to (item 2 of argv) as integer
    set moveCount to (item 3 of argv) as integer
    set direction to (item 4 of argv)
    set isRelative to (item 5 of argv)
    tell application "System Events"
        tell process "Notes"
            set frontmost to true
            if isRelative is "1" then
                key code 124
            else
                key code 126 using {command down}
                repeat downCount times
                    key code 125 using {option down}
                end repeat
                if downCount > 0 then
                    key code 124
                end if
            end if
            if isRelative is "1" then
                repeat downCount times
                    key code 125 using {option down}
                end repeat
            end if
            repeat lineCount times
                key code 125 using {shift down, option down}
            end repeat
            delay 0.1
            repeat moveCount times
                click menu item direction of menu 1 of menu item ¬
                    "Indentation" of menu 1 of menu bar item "Format" of menu bar 1
                delay 0.15
            end repeat
        end tell
    end tell
end run
"""

# The exact inverse of one step move, applied to the selection the step left
# in place. Cmd-Z was the obvious alternative and is worse: how many edits
# Notes coalesces into one undo entry is not observable, so "press it once per
# move" was a guess. Reversing the menu command is not a guess.
_AS_UNDO = """
on run argv
    set direction to (item 1 of argv)
    tell application "System Events"
        tell process "Notes"
            set frontmost to true
            click menu item direction of menu 1 of menu item ¬
                "Indentation" of menu 1 of menu bar item "Format" of menu bar 1
        end tell
    end tell
end run
"""


class IndentRunner:
    """Drives the Notes editor through System Events."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise NotesError(f"the indentation pass requires macOS (got {sys.platform!r})")

    def _run(self, script: str, argv: Sequence[str]) -> str:
        proc = subprocess.run(
            ["osascript", "-e", script, "--", *argv],
            capture_output=True,
            text=True,
            timeout=_OSASCRIPT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if "Invalid index" in stderr:
                raise NotesError("the Notes window was not ready; nothing was typed", stderr=stderr)
            if any(marker in stderr for marker in _TCC_ACCESSIBILITY_ERRORS):
                raise NotesError(ACCESSIBILITY_HELP, stderr=stderr)
            raise NotesError(f"osascript exited with status {proc.returncode}", stderr=stderr)
        return proc.stdout

    def check_accessibility(self) -> None:
        """Fail closed, with the fix, rather than half-applying the pass.

        The probe asks whether this process may drive the UI at all. An
        earlier version only read a process name, which plain Automation
        access already allows -- so it passed, and the first Tab was refused
        with error 1002 after the pass had already opened a note.
        """
        answer = self._run(_AS_CHECK_ACCESSIBILITY, []).strip().lower()
        if answer != "true":
            raise NotesError(ACCESSIBILITY_HELP)

    def show_note(self, note_id: str) -> None:
        """Open the note *and* put the caret in it, or refuse to go on."""
        if self._run(_AS_SHOW_NOTE, [note_id]).strip() != "focused":
            raise NotesError(
                "could not focus the note editor; refusing to send keystrokes at "
                "whatever is focused instead"
            )

    def apply_step(self, step: IndentStep, *, origin: int | None = None) -> None:
        """Select the step's lines and change their indentation.

        `origin` is the paragraph the caret is already sitting at, from the
        previous step's surviving selection. Given it, the caret walks forward
        by the gap instead of from the top of the document, which is the whole
        difference between a pass that takes minutes and one that takes an
        hour on a long note.
        """
        direction = _INCREASE if step.levels > 0 else _DECREASE
        if origin is None or origin >= step.paragraph:
            relative, downs = "0", step.paragraph
        else:
            relative, downs = "1", step.paragraph - origin - 1
        self._run(
            _AS_APPLY_STEP,
            [str(downs), str(step.count), str(abs(step.levels)), direction, relative],
        )

    def undo(self, *, outdent: bool) -> None:
        self._run(_AS_UNDO, [_DECREASE if outdent else _INCREASE])

    def read_body(self, note_id: str) -> str:
        return self._run(_AS_READ_BODY, [note_id])

    def read_lines(self, note_id: str) -> list[str]:
        """The editor's own lines, which is what the caret steps over.

        An empty answer means the window never became readable; that is worth
        an error rather than an empty list, which would look like a note with
        no lines at all and get it skipped for the wrong reason.
        """
        value = self._run(_AS_READ_LINES, [note_id])
        if not value.strip():
            raise NotesError("the Notes window never became readable")
        return value.rstrip("\n").split("\n")


__all__ = [
    "ACCESSIBILITY_HELP",
    "IndentReport",
    "IndentRunner",
    "IndentRunnerProtocol",
    "IndentStep",
    "indent_notes",
    "locate_checklist_paragraphs",
    "locate_lines",
    "paragraph_lines",
    "plan_indent_steps",
    "verify_indentation",
]
