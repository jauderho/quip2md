#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""scripts/notes_recon.py — Apple Notes AppleScript ground-truth recon.

Usage:
    uv run scripts/notes_recon.py [--verbose]

Probes Apple Notes automation via `osascript` (AppleScript) on this machine
and writes:

  - docs/NOTES_API_NOTES.md   Observed facts about Notes automation, answering
                               the 8 recon questions from PLAN.md task T9.
                               Anything that could not be verified live is
                               labeled "NOT VERIFIED" rather than guessed.

Flags:
  --verbose, -v   Print one line per osascript invocation to stderr (probe
                   name, running invocation count, exit code, elapsed ms).

Hard constraints enforced by this script:
  - Every probe operates ONLY inside a single top-level folder named
    "quip2md-recon", created first and deleted last. No other folder or note
    is ever read, modified, or deleted — the sole exception is reading
    account/folder NAMES (never bodies) for the account/folder-listing probe.
  - Total osascript invocations are capped at MAX_INVOCATIONS (40); the
    running count is printed at the end regardless of outcome.
  - Note/script content is NEVER interpolated into AppleScript source text.
    All content crosses the process boundary via `osascript -e 'on run argv'
    ... end run' -- <args>`, i.e. as `argv` parameters to the AppleScript
    handler, never string-substituted into the script.
  - Every subprocess.run call passes an explicit timeout (30s; 120s for the
    very first invocation, which is where a TCC automation-permission dialog
    can block waiting on the user) and check=False (exit codes are inspected
    explicitly so a single failed probe cannot raise and abort the run).
    shell=True is never used.
  - If osascript is denied automation permission (AppleScript error -1743) or
    a probe times out, the script records that fact, marks every dependent
    probe "NOT VERIFIED", and continues to write the report rather than
    retrying in a loop or attempting a workaround.
  - Re-running the script deletes any pre-existing "quip2md-recon" folder at
    start (idempotent) and overwrites docs/NOTES_API_NOTES.md with fresh
    output.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
NOTES_API_NOTES_PATH: Final[Path] = ROOT / "docs" / "NOTES_API_NOTES.md"
RECON_FOLDER: Final[str] = "quip2md-recon"
MAX_INVOCATIONS: Final[int] = 40
DEFAULT_TIMEOUT: Final[float] = 30.0
FIRST_INVOCATION_TIMEOUT: Final[float] = 120.0
TCC_DENIED_CODE: Final[str] = "-1743"

# A real, small image from the export tree (read-only probe input).
SAMPLE_IMAGE: Final[Path] = (
    ROOT / "export" / "Private" / "Work" / "_assets" / "GPRAAAoQfLS"
    / "dGrHsJc2zBYxf22iesKy1w.png"
)

VERBOSE: Final[bool] = "--verbose" in sys.argv or "-v" in sys.argv


def log(msg: str) -> None:
    if VERBOSE:
        print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# osascript plumbing
# --------------------------------------------------------------------------


@dataclass(slots=True)
class OsaResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    denied: bool = False


@dataclass(slots=True)
class Recon:
    invocation_count: int = 0
    budget_exhausted: bool = False
    permission_denied: bool = False
    permission_note: str = ""
    invocation_log: list[str] = field(default_factory=list)

    def run_applescript(
        self, probe_name: str, script: str, argv: list[str] | None = None
    ) -> OsaResult:
        """Run `script` (an AppleScript `on run argv ... end run` handler body,
        or a full script) via `osascript -e <script> -- <argv...>`. Content
        parameters go through `argv`, never string interpolation into
        `script` itself."""
        if self.invocation_count >= MAX_INVOCATIONS:
            self.budget_exhausted = True
            log(f"BUDGET EXHAUSTED ({MAX_INVOCATIONS}) — skipped probe {probe_name!r}")
            return OsaResult(ok=False, stdout="", stderr="budget exhausted", returncode=-1)

        is_first = self.invocation_count == 0
        timeout = FIRST_INVOCATION_TIMEOUT if is_first else DEFAULT_TIMEOUT

        cmd = ["osascript", "-e", script, "--"] + (argv or [])
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.invocation_count += 1
            self.invocation_log.append(
                f"{self.invocation_count}. {probe_name}: TIMEOUT after {timeout}s"
            )
            log(
                f"osascript[{probe_name}] -> TIMEOUT ({timeout}s) "
                f"({self.invocation_count}/{MAX_INVOCATIONS}, {elapsed_ms:.0f}ms)"
            )
            if is_first:
                self.permission_denied = True
                self.permission_note = (
                    "First osascript invocation TIMED OUT after "
                    f"{FIRST_INVOCATION_TIMEOUT}s. This almost always means a TCC "
                    "automation-permission dialog is waiting for the user (System "
                    "Settings > Privacy & Security > Automation) and no one "
                    "responded in time. Not retried; see report for what remains "
                    "NOT VERIFIED."
                )
            return OsaResult(
                ok=False, stdout="", stderr="timeout", returncode=-1, timed_out=True
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        self.invocation_count += 1
        denied = TCC_DENIED_CODE in proc.stderr
        if denied and is_first:
            self.permission_denied = True
            self.permission_note = (
                f"First osascript invocation was DENIED (AppleScript error "
                f"{TCC_DENIED_CODE}, 'Not authorized to send Apple events'). The "
                "user has not granted this terminal/process automation access to "
                "Notes (System Settings > Privacy & Security > Automation). Not "
                "retried; see report for what remains NOT VERIFIED. "
                f"stderr: {proc.stderr.strip()!r}"
            )
        self.invocation_log.append(
            f"{self.invocation_count}. {probe_name}: exit={proc.returncode} "
            f"({elapsed_ms:.0f}ms)"
            + (f" DENIED({TCC_DENIED_CODE})" if denied else "")
        )
        log(
            f"osascript[{probe_name}] -> exit={proc.returncode} "
            f"({self.invocation_count}/{MAX_INVOCATIONS}, {elapsed_ms:.0f}ms)"
        )
        return OsaResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            denied=denied,
        )


# --------------------------------------------------------------------------
# Report builder
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Report:
    sections: dict[str, list[str]] = field(default_factory=dict)

    def add(self, section: str, line: str) -> None:
        self.sections.setdefault(section, []).append(line)

    def render(self, recon: Recon) -> str:
        order = [
            "1. Default account / On My Mac",
            "2. Folder creation (top-level, nested, duplicate-name)",
            "3. Note HTML round-trip",
            "4. Image embedding",
            "5. Update in place / id stability",
            "6. Lookup and deletion",
            "7. Timing",
            "8. Permissions (TCC)",
        ]
        lines: list[str] = [
            "# Apple Notes AppleScript automation — ground-truth recon notes",
            "",
            "Generated by `scripts/notes_recon.py` (task T9). All values below are",
            "**observed live from this machine's Notes automation surface** unless a",
            "line is explicitly marked `NOT VERIFIED`. Nothing here is inferred from",
            "documentation or folklore.",
            "",
            f"- Total osascript invocations issued this run: **{recon.invocation_count}** "
            f"(cap: {MAX_INVOCATIONS})",
            f"- Invocation budget exhausted before all probes completed: "
            f"{'yes' if recon.budget_exhausted else 'no'}",
            f"- Automation permission denied or blocked on first invocation: "
            f"{'yes' if recon.permission_denied else 'no'}",
        ]
        if recon.permission_denied:
            lines.append("")
            lines.append(f"  > {recon.permission_note}")
        lines.extend(["", "---", ""])

        for section in order:
            lines.append(f"## {section}")
            lines.append("")
            body = self.sections.get(section)
            if body:
                lines.extend(body)
            else:
                lines.append(
                    "NOT VERIFIED — this probe was not reached (see permission/budget "
                    "notes above)."
                )
            lines.append("")
            lines.append("---")
            lines.append("")

        lines.append("## Raw invocation log")
        lines.append("")
        if recon.invocation_log:
            lines.extend(f"- {entry}" for entry in recon.invocation_log)
        else:
            lines.append("(no invocations were issued)")
        lines.append("")

        return "\n".join(lines)


# --------------------------------------------------------------------------
# AppleScript snippets
#
# Each is a full `on run argv ... end run` handler. Content (folder names,
# note bodies, ids, etc.) is always passed via argv, never interpolated into
# the script text below.
# --------------------------------------------------------------------------

AS_GET_DEFAULT_ACCOUNT = """
on run argv
    tell application "Notes"
        set accNames to {}
        repeat with a in accounts
            set end of accNames to name of a
        end repeat
        return (name of default account) & "||" & (my joinList(accNames, ","))
    end tell
end run

on joinList(lst, delim)
    set AppleScript's text item delimiters to delim
    set s to lst as string
    set AppleScript's text item delimiters to ""
    return s
end joinList
"""

AS_CLEANUP_RECON_FOLDER = """
on run argv
    set folderName to item 1 of argv
    tell application "Notes"
        set acc to default account
        -- Collect matching ids into a plain list first, then delete by id.
        -- Deleting while live-iterating "folders of acc" shifts indices out
        -- from under the repeat loop and raises "Can't get item N of every
        -- folder" (observed during recon) once more than one match exists
        -- (e.g. the duplicate-name probe leaves two folders with this name).
        set matchIds to {}
        repeat with f in folders of acc
            if name of f is folderName then
                set end of matchIds to (id of f as string)
            end if
        end repeat
        repeat with fid in matchIds
            delete (folder id fid)
        end repeat
        return (count of matchIds) as string
    end tell
end run
"""

AS_CREATE_TOP_FOLDER = """
on run argv
    set folderName to item 1 of argv
    tell application "Notes"
        set acc to default account
        set newFolder to make new folder at acc with properties {name:folderName}
        return id of newFolder
    end tell
end run
"""

AS_CREATE_NESTED_FOLDER = """
on run argv
    set parentId to item 1 of argv
    set childName to item 2 of argv
    tell application "Notes"
        set parentFolder to folder id parentId
        set newFolder to make new folder at parentFolder with properties {name:childName}
        return id of newFolder
    end tell
end run
"""

AS_CREATE_NOTE_HTML = """
on run argv
    set folderId to item 1 of argv
    set noteBody to item 2 of argv
    tell application "Notes"
        set targetFolder to folder id folderId
        set newNote to make new note at targetFolder with properties {body:noteBody}
        return id of newNote
    end tell
end run
"""

AS_GET_NOTE_BODY = """
on run argv
    set noteId to item 1 of argv
    tell application "Notes"
        return body of note id noteId
    end tell
end run
"""

AS_MAKE_ATTACHMENT = """
on run argv
    set folderId to item 1 of argv
    set imgPosixPath to item 2 of argv
    tell application "Notes"
        set targetFolder to folder id folderId
        set newNote to make new note at targetFolder with properties {body:"attachment test"}
        tell newNote
            make new attachment with data (POSIX file imgPosixPath)
        end tell
        return id of newNote
    end tell
end run
"""

AS_SET_NOTE_BODY = """
on run argv
    set noteId to item 1 of argv
    set newBody to item 2 of argv
    tell application "Notes"
        set body of note id noteId to newBody
        return id of note id noteId
    end tell
end run
"""

AS_LIST_NOTES_IN_FOLDER = """
on run argv
    set folderId to item 1 of argv
    tell application "Notes"
        set targetFolder to folder id folderId
        set noteIds to {}
        repeat with n in notes of targetFolder
            set end of noteIds to id of n
        end repeat
        return my joinList(noteIds, ",")
    end tell
end run

on joinList(lst, delim)
    if (count of lst) is 0 then return ""
    set AppleScript's text item delimiters to delim
    set s to lst as string
    set AppleScript's text item delimiters to ""
    return s
end joinList
"""

AS_DELETE_NOTE = """
on run argv
    set noteId to item 1 of argv
    tell application "Notes"
        delete note id noteId
    end tell
    return "ok"
end run
"""

AS_LOOKUP_NOTE_BY_ID = """
on run argv
    set noteId to item 1 of argv
    tell application "Notes"
        try
            return "FOUND:" & (name of note id noteId)
        on error errMsg
            return "ERROR:" & errMsg
        end try
    end tell
end run
"""

AS_CREATE_NOTE_BATCH = """
on run argv
    set folderId to item 1 of argv
    set b1 to item 2 of argv
    set b2 to item 3 of argv
    set b3 to item 4 of argv
    set b4 to item 5 of argv
    set b5 to item 6 of argv
    tell application "Notes"
        set targetFolder to folder id folderId
        set ids to {}
        repeat with b in {b1, b2, b3, b4, b5}
            set n to make new note at targetFolder with properties {body:b}
            set end of ids to id of n
        end repeat
        return my joinList(ids, ",")
    end tell
end run

on joinList(lst, delim)
    set AppleScript's text item delimiters to delim
    set s to lst as string
    set AppleScript's text item delimiters to ""
    return s
end joinList
"""


# --------------------------------------------------------------------------
# Round-trip HTML probe elements
# --------------------------------------------------------------------------

ROUNDTRIP_ELEMENTS: Final[list[tuple[str, str]]] = [
    ("h1", "<h1>Heading One</h1>"),
    ("h2", "<h2>Heading Two</h2>"),
    ("h3", "<h3>Heading Three</h3>"),
    ("bold", "<p><b>bold text</b></p>"),
    ("italic", "<p><i>italic text</i></p>"),
    ("underline", "<p><u>underlined text</u></p>"),
    ("strike", "<p><strike>struck text</strike></p>"),
    ("nested_ul", "<ul><li>one<ul><li>one-a</li><li>one-b</li></ul></li><li>two</li></ul>"),
    ("nested_ol", "<ol><li>first<ol><li>first-a</li></ol></li><li>second</li></ol>"),
    ("link_https", '<p><a href="https://example.com/page">example link</a></p>'),
    ("link_file", '<p><a href="file:///tmp/quip2md-recon-test.txt">file link</a></p>'),
    (
        "table",
        "<table><tr><th>H1</th><th>H2</th></tr><tr><td>a</td><td>b</td></tr></table>",
    ),
    ("pre_code", "<pre><code>def f():\n    return 1</code></pre>"),
    ("blockquote", "<blockquote>quoted text</blockquote>"),
    ("hr", "<p>above</p><hr/><p>below</p>"),
    ("br", "<p>line one<br/>line two</p>"),
    ("div_p", "<div>div text</div><p>p text</p>"),
]


def build_roundtrip_html() -> str:
    parts = ["<div>quip2md-recon roundtrip probe</div>"]
    for name, fragment in ROUNDTRIP_ELEMENTS:
        parts.append(f"<div data-probe='{name}'>{fragment}</div>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Probe 1 — default account / On My Mac
# --------------------------------------------------------------------------


def probe_account(recon: Recon, report: Report) -> bool:
    section = "1. Default account / On My Mac"
    result = recon.run_applescript("get_default_account", AS_GET_DEFAULT_ACCOUNT)
    if result.timed_out:
        report.add(
            section,
            "NOT VERIFIED — first osascript invocation timed out waiting for the "
            "TCC automation-permission dialog. See top-of-report permission note.",
        )
        return False
    if result.denied:
        report.add(
            section,
            f"NOT VERIFIED — automation permission DENIED (error {TCC_DENIED_CODE}). "
            "See top-of-report permission note.",
        )
        return False
    if not result.ok:
        report.add(
            section,
            f"Probe failed (exit {result.returncode}). stderr: `{result.stderr.strip()}`",
        )
        return False

    default_name, _, acc_list_str = result.stdout.strip().partition("||")
    acc_names = [a for a in acc_list_str.split(",") if a]
    report.add(section, f"Default account name: `{default_name}`")
    report.add(section, f"All account names visible to Notes.app: `{acc_names}`")
    local_present = "On My Mac" in acc_names
    report.add(
        section,
        "An \"On My Mac\" (local) account is "
        + ("present" if local_present else "NOT present")
        + " in the accounts list on this machine (report only — Notes settings unchanged).",
    )
    return True


# --------------------------------------------------------------------------
# Probe 2 — folder creation (top-level, nested, duplicate-name)
# --------------------------------------------------------------------------


def probe_folders(recon: Recon, report: Report) -> str | None:
    """Creates the quip2md-recon top folder and up to 2 levels of nested
    children plus a duplicate-name sibling. Returns the top folder's id."""
    section = "2. Folder creation (top-level, nested, duplicate-name)"

    result = recon.run_applescript(
        "create_top_folder", AS_CREATE_TOP_FOLDER, [RECON_FOLDER]
    )
    if not result.ok:
        report.add(
            section,
            f"Top-level folder creation FAILED (exit {result.returncode}). "
            f"stderr: `{result.stderr.strip()}`",
        )
        return None
    top_id = result.stdout.strip()
    report.add(section, f"Top-level folder `{RECON_FOLDER}` created. id: `{top_id}`")

    # Nest 3 levels deep: recon/level1/level2/level3
    parent_id = top_id
    depth_reached = 0
    for level in range(1, 4):
        child_name = f"level{level}"
        result = recon.run_applescript(
            "create_nested_folder", AS_CREATE_NESTED_FOLDER, [parent_id, child_name]
        )
        if not result.ok:
            report.add(
                section,
                f"Nesting stopped at depth {level} (creating `{child_name}` inside "
                f"parent id `{parent_id}` failed, exit {result.returncode}). "
                f"stderr: `{result.stderr.strip()}`",
            )
            break
        parent_id = result.stdout.strip()
        depth_reached = level
        report.add(section, f"Depth {level} folder `{child_name}` created. id: `{parent_id}`")

    report.add(
        section,
        f"Max nesting depth achieved in this probe: {depth_reached} level(s) "
        f"below the top folder (attempted 3).",
    )

    # Duplicate-name folder at top level.
    result = recon.run_applescript(
        "create_duplicate_folder", AS_CREATE_TOP_FOLDER, [RECON_FOLDER]
    )
    if result.ok:
        dup_id = result.stdout.strip()
        report.add(
            section,
            f"Creating a second top-level folder with the SAME name (`{RECON_FOLDER}`) "
            f"did NOT error — Notes allows duplicate names as distinct folders. "
            f"New id: `{dup_id}` (different from original `{top_id}`: {dup_id != top_id}). "
            "This duplicate is inside the recon scope and will be cleaned up with "
            "the rest at the end.",
        )
    else:
        report.add(
            section,
            f"Creating a second top-level folder with the SAME name FAILED "
            f"(exit {result.returncode}). stderr: `{result.stderr.strip()}` "
            "— Notes appears to reject/disallow duplicate folder names at this level.",
        )

    return top_id


# --------------------------------------------------------------------------
# Probe 3 — note HTML round-trip
# --------------------------------------------------------------------------


def probe_roundtrip(recon: Recon, report: Report, top_folder_id: str) -> str | None:
    section = "3. Note HTML round-trip"
    html = build_roundtrip_html()

    result = recon.run_applescript(
        "create_roundtrip_note", AS_CREATE_NOTE_HTML, [top_folder_id, html]
    )
    if not result.ok:
        report.add(
            section,
            f"Round-trip note creation FAILED (exit {result.returncode}). "
            f"stderr: `{result.stderr.strip()}`",
        )
        return None
    note_id = result.stdout.strip()
    report.add(section, f"Round-trip probe note created. id: `{note_id}`")

    result = recon.run_applescript("get_roundtrip_body", AS_GET_NOTE_BODY, [note_id])
    if not result.ok:
        report.add(
            section,
            f"Reading body back FAILED (exit {result.returncode}). "
            f"stderr: `{result.stderr.strip()}`",
        )
        return note_id

    returned_html = result.stdout
    report.add(section, "Body read back successfully. Per-element observations:")
    report.add(section, "")
    for name, original_fragment in ROUNDTRIP_ELEMENTS:
        marker = f"data-probe='{name}'"
        idx = returned_html.find(marker)
        if idx == -1:
            # Notes strips the wrapping div/data-attr; fall back to a looser
            # presence check on distinguishing text/tag from the fragment.
            report.add(
                section,
                f"- `{name}`: wrapper `<div data-probe>` not found verbatim in "
                "returned body (Notes normalizes/strips custom attributes — expected). "
                "See raw dump below for what actually came back.",
            )
            continue
        # Grab a window of returned HTML after the marker for inspection.
        window = returned_html[idx : idx + 300]
        report.add(section, f"- `{name}`: original=`{original_fragment}`")
        report.add(section, f"  returned (window)=`{window}`")

    report.add(section, "")
    report.add(section, "Full returned body HTML (for reference / future fixture use):")
    report.add(section, "")
    report.add(section, "```html")
    report.add(section, returned_html.strip()[:6000])
    report.add(section, "```")

    return note_id


# --------------------------------------------------------------------------
# Probe 4 — image embedding
# --------------------------------------------------------------------------


def probe_image(recon: Recon, report: Report, top_folder_id: str) -> None:
    section = "4. Image embedding"
    if not SAMPLE_IMAGE.exists():
        report.add(
            section,
            f"NOT VERIFIED — sample image `{SAMPLE_IMAGE}` does not exist on disk.",
        )
        return

    # (a) make new attachment with data (POSIX file ...)
    result = recon.run_applescript(
        "make_attachment", AS_MAKE_ATTACHMENT, [top_folder_id, str(SAMPLE_IMAGE)]
    )
    if result.ok:
        note_id = result.stdout.strip()
        body_result = recon.run_applescript(
            "get_attachment_note_body", AS_GET_NOTE_BODY, [note_id]
        )
        body_snippet = body_result.stdout.strip()[:2000] if body_result.ok else "(read failed)"
        report.add(
            section,
            "(a) `make new attachment with data (POSIX file ...)`: SUCCEEDED "
            f"(exit 0). Note id: `{note_id}`.",
        )
        report.add(section, "")
        report.add(section, "Body of the note after attaching (window, up to 2000 chars):")
        report.add(section, "")
        report.add(section, "```html")
        report.add(section, body_snippet)
        report.add(section, "```")
        embedded = "img" in body_snippet.lower() or "data:" in body_snippet.lower()
        report.add(
            section,
            f"Body contains an `<img>`/`data:` reference: {embedded} "
            "(if false, the attachment likely lives outside the returned `body` "
            "property rather than being inlined as an <img> tag).",
        )
    else:
        report.add(
            section,
            "(a) `make new attachment with data (POSIX file ...)`: FAILED "
            f"(exit {result.returncode}). stderr: `{result.stderr.strip()}`",
        )

    # (b) <img src="file://..."> in the body HTML.
    file_uri = SAMPLE_IMAGE.as_uri()
    img_html = f"<div>img-src-probe</div><img src=\"{file_uri}\" />"
    result = recon.run_applescript(
        "create_note_with_img_src", AS_CREATE_NOTE_HTML, [top_folder_id, img_html]
    )
    report.add(section, "")
    if result.ok:
        note_id = result.stdout.strip()
        body_result = recon.run_applescript("get_img_src_note_body", AS_GET_NOTE_BODY, [note_id])
        body_snippet = body_result.stdout.strip()[:2000] if body_result.ok else "(read failed)"
        report.add(
            section,
            f"(b) `<img src=\"file://...\">` in body HTML: note creation SUCCEEDED. "
            f"Note id: `{note_id}`.",
        )
        report.add(section, "")
        report.add(section, "Body of the note after creation (window, up to 2000 chars):")
        report.add(section, "")
        report.add(section, "```html")
        report.add(section, body_snippet)
        report.add(section, "```")
        preserved = "img" in body_snippet.lower()
        report.add(
            section,
            f"Body still contains an `<img>` tag: {preserved} "
            "(if false, Notes stripped the file:// image reference entirely on input).",
        )
    else:
        report.add(
            section,
            f"(b) `<img src=\"file://...\">` in body HTML: note creation FAILED "
            f"(exit {result.returncode}). stderr: `{result.stderr.strip()}`",
        )


# --------------------------------------------------------------------------
# Probe 5 — update in place / id stability
# --------------------------------------------------------------------------


def probe_update(recon: Recon, report: Report, top_folder_id: str) -> None:
    section = "5. Update in place / id stability"
    result = recon.run_applescript(
        "create_update_note",
        AS_CREATE_NOTE_HTML,
        [top_folder_id, "<div>original body</div>"],
    )
    if not result.ok:
        report.add(
            section,
            f"NOT VERIFIED — note creation for update probe FAILED "
            f"(exit {result.returncode}). stderr: `{result.stderr.strip()}`",
        )
        return
    note_id = result.stdout.strip()
    report.add(section, f"Created note for update probe. id format observed: `{note_id}`")

    result2 = recon.run_applescript(
        "set_note_body", AS_SET_NOTE_BODY, [note_id, "<div>updated body v2</div>"]
    )
    if not result2.ok:
        report.add(
            section,
            f"`set body of note id ...` FAILED (exit {result2.returncode}). "
            f"stderr: `{result2.stderr.strip()}`",
        )
        return
    id_after_update = result2.stdout.strip()
    report.add(
        section,
        f"After `set body of note id ...`, `id of note id ...` returned: "
        f"`{id_after_update}` — stable: {id_after_update == note_id}",
    )

    result3 = recon.run_applescript("get_updated_body", AS_GET_NOTE_BODY, [note_id])
    if result3.ok:
        contains_update = "updated body v2" in result3.stdout
        report.add(
            section,
            f"Re-reading the body shows the update took effect: {contains_update}",
        )
    else:
        report.add(
            section,
            f"Re-reading body after update FAILED (exit {result3.returncode}). "
            f"stderr: `{result3.stderr.strip()}`",
        )


# --------------------------------------------------------------------------
# Probe 6 — lookup and deletion
# --------------------------------------------------------------------------


def probe_lookup_delete(recon: Recon, report: Report, top_folder_id: str) -> None:
    section = "6. Lookup and deletion"

    result = recon.run_applescript(
        "create_lookup_note",
        AS_CREATE_NOTE_HTML,
        [top_folder_id, "<div>lookup/delete probe</div>"],
    )
    if not result.ok:
        report.add(
            section,
            f"NOT VERIFIED — note creation for lookup/delete probe FAILED "
            f"(exit {result.returncode}).",
        )
        return
    note_id = result.stdout.strip()

    lookup1 = recon.run_applescript("lookup_before_delete", AS_LOOKUP_NOTE_BY_ID, [note_id])
    lookup1_text = lookup1.stdout.strip() if lookup1.ok else lookup1.stderr.strip()
    report.add(section, f"Lookup by id before deletion: `{lookup1_text}`")

    listing = recon.run_applescript(
        "list_notes_in_folder", AS_LIST_NOTES_IN_FOLDER, [top_folder_id]
    )
    if listing.ok:
        ids = [i for i in listing.stdout.strip().split(",") if i]
        report.add(
            section,
            f"`notes of folder id ...` listing: {len(ids)} note id(s) returned, "
            f"target note present: {note_id in ids}",
        )
    else:
        report.add(
            section,
            f"Listing notes of folder FAILED (exit {listing.returncode}). "
            f"stderr: `{listing.stderr.strip()}`",
        )

    delete_result = recon.run_applescript("delete_note", AS_DELETE_NOTE, [note_id])
    if not delete_result.ok:
        report.add(
            section,
            f"`delete note id ...` FAILED (exit {delete_result.returncode}). "
            f"stderr: `{delete_result.stderr.strip()}`",
        )
        return
    report.add(section, "`delete note id ...` succeeded (exit 0).")

    listing2 = recon.run_applescript(
        "list_notes_in_folder_after_delete", AS_LIST_NOTES_IN_FOLDER, [top_folder_id]
    )
    if listing2.ok:
        ids2 = [i for i in listing2.stdout.strip().split(",") if i]
        report.add(
            section,
            f"After deletion, `notes of folder id ...` no longer lists the note: "
            f"{note_id not in ids2} (folder now has {len(ids2)} note(s)) — confirms the "
            "note disappears from normal folder listing (goes to Recently Deleted).",
        )
    else:
        report.add(
            section,
            "NOT VERIFIED — could not re-list folder notes after deletion "
            f"(exit {listing2.returncode}).",
        )

    lookup2 = recon.run_applescript("lookup_after_delete", AS_LOOKUP_NOTE_BY_ID, [note_id])
    report.add(
        section,
        "Lookup by id after deletion: "
        f"`{lookup2.stdout.strip() if lookup2.ok else lookup2.stderr.strip()}` "
        "(observe whether the id still resolves via Recently Deleted or errors outright).",
    )


# --------------------------------------------------------------------------
# Probe 7 — timing
# --------------------------------------------------------------------------


def _make_html_of_size(target_bytes: int, tag: str) -> str:
    filler = f"<p data-tag='{tag}'>" + ("Lorem ipsum dolor sit amet. " * 20) + "</p>"
    parts = [f"<div>timing probe {tag}</div>"]
    size = len("".join(parts))
    while size < target_bytes:
        parts.append(filler)
        size += len(filler)
    return "".join(parts)


def probe_timing(recon: Recon, report: Report, top_folder_id: str) -> None:
    section = "7. Timing"

    # Sequential: 5 notes, one osascript invocation each.
    durations: list[float] = []
    for i in range(5):
        html = _make_html_of_size(5 * 1024, f"seq{i}")
        start = time.monotonic()
        result = recon.run_applescript(
            f"timing_seq_{i}", AS_CREATE_NOTE_HTML, [top_folder_id, html]
        )
        elapsed = time.monotonic() - start
        if result.ok:
            durations.append(elapsed)
        else:
            report.add(
                section,
                f"Sequential create #{i} FAILED (exit {result.returncode}); excluded from mean.",
            )

    if durations:
        mean_s = sum(durations) / len(durations)
        report.add(
            section,
            f"Sequential (1 osascript invocation per note): {len(durations)}/5 notes "
            f"created with a ~5 KB HTML body. Mean: **{mean_s:.3f} s/note** "
            f"(individual: {[round(d, 3) for d in durations]}).",
        )
    else:
        report.add(section, "NOT VERIFIED — all sequential timing creates failed.")

    # Batched: 5 notes in ONE osascript invocation.
    batch_bodies = [_make_html_of_size(5 * 1024, f"batch{i}") for i in range(5)]
    start = time.monotonic()
    batch_result = recon.run_applescript(
        "timing_batch", AS_CREATE_NOTE_BATCH, [top_folder_id, *batch_bodies]
    )
    elapsed = time.monotonic() - start
    if batch_result.ok:
        ids = [i for i in batch_result.stdout.strip().split(",") if i]
        per_note = elapsed / max(len(ids), 1)
        report.add(
            section,
            f"Batched (5 notes, ONE osascript invocation): SUCCEEDED, {len(ids)} note(s) "
            f"created in {elapsed:.3f} s total ({per_note:.3f} s/note effective). "
            + (
                "Batching works reliably and is faster per-note than sequential "
                "invocations (avoids per-invocation osascript process startup cost)."
                if durations and per_note < mean_s
                else "Comparison to sequential mean inconclusive (see above)."
            ),
        )
    else:
        report.add(
            section,
            f"Batched (5 notes, ONE osascript invocation): FAILED "
            f"(exit {batch_result.returncode}). stderr: `{batch_result.stderr.strip()}` "
            "— batching does NOT work reliably on this machine/version.",
        )


# --------------------------------------------------------------------------
# Probe 8 — permissions (TCC)
# --------------------------------------------------------------------------


def probe_permissions(recon: Recon, report: Report) -> None:
    section = "8. Permissions (TCC)"
    if recon.permission_denied:
        report.add(section, recon.permission_note)
        return
    if recon.invocation_count == 0:
        report.add(section, "NOT VERIFIED — no osascript invocations were issued this run.")
        return
    report.add(
        section,
        f"No TCC denial (error {TCC_DENIED_CODE}) or first-invocation timeout was "
        f"observed across all {recon.invocation_count} osascript invocations this run. "
        "This implies automation permission for Notes was already granted to the "
        "process running this script (either from a prior run/prompt on this "
        "machine, or the dialog was answered silently/instantly) — subsequent "
        "invocations in this run were silent (no further prompts observed). "
        "If this is the very first time this exact binary/terminal has automated "
        "Notes, macOS would normally have shown a one-time 'quip2md wants access "
        "to control Notes' dialog before the first invocation returned; since no "
        "timeout occurred, either that dialog was already resolved previously or "
        "did not block this run.",
    )


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


def cleanup(recon: Recon, report: Report) -> None:
    section_note = (
        "Cleanup: deleting the top-level `quip2md-recon` folder (and everything "
        "nested inside it) at end of run. This sends its contents to Recently "
        "Deleted — acceptable per task constraints."
    )
    result = recon.run_applescript(
        "cleanup_delete_recon_folder", AS_CLEANUP_RECON_FOLDER, [RECON_FOLDER]
    )
    if result.ok:
        log(f"{section_note} OK.")
    else:
        log(f"{section_note} FAILED (exit {result.returncode}): {result.stderr.strip()}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    report = Report()
    recon = Recon()

    # Idempotency: remove any pre-existing recon folder from a prior run
    # before doing anything else (also the first invocation, so it carries
    # the extended TCC timeout).
    pre_cleanup = recon.run_applescript(
        "pre_cleanup_delete_recon_folder", AS_CLEANUP_RECON_FOLDER, [RECON_FOLDER]
    )
    if pre_cleanup.timed_out or pre_cleanup.denied:
        # Permission blocked before anything could run; write the report now
        # with everything NOT VERIFIED rather than looping/retrying.
        for section in (
            "1. Default account / On My Mac",
            "2. Folder creation (top-level, nested, duplicate-name)",
            "3. Note HTML round-trip",
            "4. Image embedding",
            "5. Update in place / id stability",
            "6. Lookup and deletion",
            "7. Timing",
        ):
            report.add(section, "NOT VERIFIED — blocked by automation permission (see above).")
        probe_permissions(recon, report)
        NOTES_API_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        NOTES_API_NOTES_PATH.write_text(report.render(recon), encoding="utf-8")
        print(f"Wrote {NOTES_API_NOTES_PATH}")
        print(f"Total osascript invocations issued: {recon.invocation_count}/{MAX_INVOCATIONS}")
        print("PERMISSION BLOCKED — see docs/NOTES_API_NOTES.md for details.")
        return

    account_ok = probe_account(recon, report)
    top_folder_id = probe_folders(recon, report)

    if top_folder_id is not None:
        probe_roundtrip(recon, report, top_folder_id)
        probe_image(recon, report, top_folder_id)
        probe_update(recon, report, top_folder_id)
        probe_lookup_delete(recon, report, top_folder_id)
        probe_timing(recon, report, top_folder_id)
        cleanup(recon, report)
    else:
        for section in (
            "3. Note HTML round-trip",
            "4. Image embedding",
            "5. Update in place / id stability",
            "6. Lookup and deletion",
            "7. Timing",
        ):
            report.add(
                section,
                "NOT VERIFIED — skipped because the quip2md-recon top-level folder "
                "could not be created (see section 2).",
            )

    probe_permissions(recon, report)

    if not account_ok and not recon.permission_denied:
        report.add(
            "1. Default account / On My Mac",
            "NOT VERIFIED — probe did not succeed (see raw invocation log); "
            "not a permission issue.",
        )

    NOTES_API_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTES_API_NOTES_PATH.write_text(report.render(recon), encoding="utf-8")

    print(f"Wrote {NOTES_API_NOTES_PATH}")
    print(f"Total osascript invocations issued: {recon.invocation_count}/{MAX_INVOCATIONS}")


if __name__ == "__main__":
    main()
