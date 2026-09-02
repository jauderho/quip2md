"""Command-line entry point for quip2md.

Usage:
    quip2md export [--output DIR] [--dryrun] [--verbose | -v] [--force]
                    [--include-chats] [--only THREAD_ID [--only THREAD_ID ...]]
    quip2md import-notes [--source DIR] [--writer enex|applescript]
                    [--enex-file FILE] [--indent-checklists] [--local]
                    [--dryrun] [--verbose | -v] [--force] [--workers N]
                    [--only KEY [--only KEY ...]]

Switches (all under the `export` subcommand):
    --output DIR        Directory to write the exported Markdown tree into.
                         Defaults to ./export.
    --dryrun             Walk the account's folder tree and print the
                         would-be output tree with per-folder thread counts;
                         makes no thread-HTML/blob requests and writes
                         nothing to disk or to the manifest.
    --verbose, -v         Enable DEBUG-level logging for the `quip2md`
                         logger hierarchy, with a concise one-line format.
                         Silent (WARNING and above only) by default.
    --force               Re-export every thread even if the manifest says
                         it is unchanged since the last run.
    --include-chats       Also export CHAT-type threads (skipped by
                         default -- see AGENTS.md/PLAN.md rationale).
    --only THREAD_ID       Restrict the export to the given thread id. May
                         be repeated to export several specific threads.

The Quip API token is read from `QUIP_TOKEN` in the process environment or a
`.env` file (see `quip2md.config.load_config`); it is never printed or
logged.

Switches (all under the `import-notes` subcommand):
    --source DIR         Directory containing the Markdown tree an earlier
                         `quip2md export` run wrote (frontmatter + body).
                         Defaults to ./export.
    --writer WRITER       `enex` (default) renders one Evernote archive and
                         hands it to Notes' own importer, which preserves
                         hyperlinks and native checklists; it needs one
                         click on Notes' confirmation sheet. `applescript`
                         is the legacy body writer, which cannot produce
                         either (see docs/NOTES_API_NOTES.md).
    --enex-file FILE      Where the `enex` writer puts its archive.
                         Defaults to .quip2md/quip2md.enex.
    --indent-checklists   After a successful `enex` import, drive the Notes
                         editor to restore nested checklist indentation
                         (Notes' importer always creates checklist items at
                         the top level). Requires macOS Accessibility
                         permission and types into your notes; off by
                         default, and every step is verified against a
                         read-back before the next one runs. Rejected with
                         `--writer applescript`, which makes no checklists.
    --local               Target the "On My Mac" Notes account instead of
                         the default account (usually iCloud). Only
                         `--writer applescript` can honour it -- Notes always
                         imports an archive into the default account -- so
                         `--local --writer enex` is rejected.
    --dryrun              Scan and convert every source under --source
                         (surfacing per-folder note counts and conversion
                         warnings), but make zero Notes automation calls and
                         write no `.quip2md/notes_state.json`. With the `enex`
                         writer an existing state file is still *read*, so the
                         dry run skips exactly the documents a real run would.
    --workers N           How many worker processes render the Markdown to
                         ENML (`enex` writer only). Defaults to the smaller
                         of the CPU count and 6; `1` renders in this process.
    --verbose, -v         Enable DEBUG-level logging for the `quip2md`
                         logger hierarchy, with a concise one-line format.
                         Silent (WARNING and above only) by default.
    --force               Re-import every note even if the state file says
                         it is unchanged since the last run. With the `enex`
                         writer this creates a *second* note for each one:
                         Notes' importer cannot replace a note, so the
                         previous copy is recorded as superseded and left for
                         you to delete.
    --only KEY             Restrict the import to the given key: a Quip
                         thread id (from a source `.md` file's `quip_id`
                         frontmatter), or, for a file without that
                         frontmatter, its `path:<relative/posix/path>` key
                         (see `quip2md.notes_import.scan_source`). May be
                         repeated.

`import-notes` is a purely offline operation over `--source` and never reads
`QUIP_TOKEN` or `.env`. On macOS, the *first* `import-notes` run in a while
may show a one-time system prompt asking for permission to let this process
automate Notes.app (via AppleScript/`osascript`) -- approve it for the
import to proceed; later runs do not re-prompt.

Exit codes:
    0    success, nothing failed.
    1    one or more threads failed to export, or one or more notes failed
         to import or could not be matched back to their source (the `enex`
         writer leaves those in its landing folder, whose name the report
         prints) -- see the printed report and `.quip2md/last_run.json` /
         `.quip2md/last_notes_run.json` for details.
    2    a rejected flag combination (`--local` with `--writer enex`,
         `--indent-checklists` with `--writer applescript`), or a
         configuration, manifest, or Notes-state error (e.g. `QUIP_TOKEN`
         missing for `export`; a corrupted `.quip2md/state.json` or
         `.quip2md/notes_state.json`; an API error during the initial
         folder walk; or, for `import-notes`, a non-macOS platform or a
         missing "On My Mac" account) -- a clear message is printed to
         stderr, no traceback.
    130  interrupted (Ctrl-C). The manifest/state file has already been
         flushed; re-running the same command resumes from where it left
         off.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from quip2md.config import DEFAULT_OUTPUT_DIR, DEFAULT_STATE_PATH, Config, ConfigError, load_config

if TYPE_CHECKING:  # imported for type hints only; see `__getattr__` below
    from quip2md.export import ExportReport
    from quip2md.notes_enex import EnexImportReport, EnexNotesRunnerProtocol
    from quip2md.notes_import import ImportReport
    from quip2md.notes_indent import IndentReport

_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

#: Every subcommand dependency, imported on first use rather than at start-up.
#: `export` pulls in httpx; `import-notes` pulls in BeautifulSoup, markdown-it
#: and python-markdown. Loading both for whichever one was asked for (and for
#: `--help`, which needs neither) doubled the CLI's start-up time.
_DEFERRED_SYMBOLS = {
    "ManifestError": "quip2md.walker",
    "QuipApiError": "quip2md.client",
    "QuipClient": "quip2md.client",
    "run_export": "quip2md.export",
    "EnexNotesRunner": "quip2md.notes_enex",
    "run_enex_import": "quip2md.notes_enex",
    "NotesError": "quip2md.notes_import",
    "NotesRunner": "quip2md.notes_import",
    "NotesStateError": "quip2md.notes_import",
    "run_import": "quip2md.notes_import",
    "IndentRunner": "quip2md.notes_indent",
    "PruneRunner": "quip2md.notes_prune",
    "prune_notes": "quip2md.notes_prune",
    "indent_notes": "quip2md.notes_indent",
}


def __getattr__(name: str) -> object:
    """Resolve a deferred symbol the first time anything asks for it.

    Keeping the names reachable as module attributes -- rather than importing
    them inside each handler -- is what preserves them as a seam: a caller (the
    test suite) can still replace `cli.QuipClient`, and because the handlers
    read them back off this module (`_cli.QuipClient`) the replacement is the
    one they use. The resolved object is cached in the module globals, so this
    runs at most once per name.
    """
    module_name = _DEFERRED_SYMBOLS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


#: This module, so the handlers below can read the deferred symbols back off it
#: at call time instead of binding them at import time.
_cli = sys.modules[__name__]


def _worker_count(raw: str) -> int:
    """Parse `--workers`, rejecting anything below one process."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected at least 1 worker, got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quip2md", description="Bulk-export a Quip account to Markdown."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export the Quip account to Markdown.")
    export_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory (default: ./export).",
    )
    export_parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Walk and print the folder tree; no thread/blob fetches or writes.",
    )
    export_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable step-level DEBUG logging.",
    )
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-export every thread even if unchanged since the last run.",
    )
    export_parser.add_argument(
        "--include-chats",
        action="store_true",
        help="Also export CHAT-type threads (skipped by default).",
    )
    export_parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="THREAD_ID",
        help="Restrict the export to this thread id; may be repeated.",
    )

    notes_parser = subparsers.add_parser(
        "import-notes", help="Import an exported Markdown tree into Apple Notes."
    )
    notes_parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory containing the exported Markdown tree (default: ./export).",
    )
    notes_parser.add_argument(
        "--local",
        action="store_true",
        help=(
            'Target the "On My Mac" account instead of the default account. '
            "Only valid with --writer applescript."
        ),
    )
    notes_parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Scan and convert every source; no Notes automation calls or state writes.",
    )
    notes_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable step-level DEBUG logging.",
    )
    notes_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-import every note even if unchanged since the last run. With "
            "--writer enex this adds a second note per document; the previous "
            "copy is recorded as superseded and left in Notes."
        ),
    )
    notes_parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="KEY",
        help=(
            "Restrict the import to this key (a Quip thread id, or a "
            "path-based key for files without quip_id frontmatter); may be "
            "repeated."
        ),
    )
    notes_parser.add_argument(
        "--writer",
        choices=("enex", "applescript"),
        default="enex",
        help=(
            "enex (default): import via an Evernote archive, preserving links "
            "and native checklists. applescript: the legacy body writer, which "
            "cannot produce either."
        ),
    )
    notes_parser.add_argument(
        "--enex-file",
        type=Path,
        default=None,
        help="Where to write the .enex archive (default: .quip2md/quip2md.enex).",
    )
    notes_parser.add_argument(
        "--workers",
        type=_worker_count,
        default=None,
        metavar="N",
        help=(
            "Worker processes used to render the archive (--writer enex only; "
            "default: the smaller of the CPU count and 6). 1 renders in this "
            "process."
        ),
    )
    notes_parser.add_argument(
        "--adopt-landing",
        metavar="FOLDER",
        default=None,
        help=(
            "Resume an import whose notes reached Notes but were never filed "
            "(--writer enex only). Imports nothing; files the notes already in "
            "the named 'Imported Notes N' folder. Use this instead of re-running "
            "after a failure, which would import a second copy of everything."
        ),
    )
    notes_parser.add_argument(
        "--indent-checklists",
        action="store_true",
        help=(
            "After importing, drive the Notes editor to restore nested "
            "checklist indentation. Needs Accessibility permission and types "
            "into your notes; off by default. Only valid with --writer enex."
        ),
    )

    prune_parser = subparsers.add_parser(
        "prune-notes",
        help="Delete stale copies and empty landing folders left by an import.",
    )
    prune_parser.add_argument(
        "--folder",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Delete this top-level folder and everything in it. Repeatable. "
            "Refuses anything that is not a top-level folder of the account, "
            "and refuses the Quip folder outright."
        ),
    )
    prune_parser.add_argument(
        "--empty-landing",
        action="store_true",
        help="Delete every empty 'Imported Notes N' folder.",
    )
    prune_parser.add_argument(
        "--superseded",
        action="store_true",
        help=(
            "Delete the previous copy of every re-imported note, by the exact "
            "id recorded in notes_state.json, and clear those records."
        ),
    )
    prune_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it nothing is touched and the plan is printed.",
    )
    prune_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable step-level DEBUG logging."
    )

    return parser


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger = logging.getLogger("quip2md")
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False


def _print_report(report: ExportReport) -> None:
    print("Export complete.")
    print(f"  exported:            {report.exported}")
    print(f"  skipped (unchanged): {report.skipped_unchanged}")
    print(f"  skipped (chats):     {report.skipped_chats}")
    print(f"  skipped (other):     {report.skipped_other}")
    print(f"  xlsx backups:        {report.xlsx_backups}")
    print(f"  blobs downloaded:    {report.blobs_downloaded}")
    print(f"  elapsed:             {report.elapsed_seconds:.1f}s")
    if report.failed:
        print(f"  failed:              {len(report.failed)}")
        for thread_id, reason in report.failed:
            print(f"    - {thread_id}: {reason}")


def _print_import_report(report: ImportReport) -> None:
    print("Import complete.")
    print(f"  created:             {report.created}")
    print(f"  updated:             {report.updated}")
    print(f"  skipped (unchanged): {report.skipped_unchanged}")
    print(f"  recreated (missing): {report.recreated_missing}")
    print(f"  keyed by path:       {report.keyed_by_path}")
    print(f"  warnings:            {report.warnings}")
    print(f"  elapsed:             {report.elapsed_seconds:.1f}s")
    if report.failed:
        print(f"  failed:              {len(report.failed)}")
        for key, reason in report.failed:
            print(f"    - {key}: {reason}")


def _print_dry_run_notes_report(report: ImportReport) -> None:
    print("Dry run: notes import (no Notes automation calls were made)")
    for folder_key in sorted(report.folder_counts):
        parts = folder_key.split("/")
        depth = max(len(parts) - 1, 0)
        label = parts[-1] if parts else "(root)"
        print(f"{'  ' * depth}{label}/  [{report.folder_counts[folder_key]} note(s)]")
    print()
    print(f"  would create:        {report.created}")
    print(f"  would update:        {report.updated}")
    print(f"  skipped (unchanged): {report.skipped_unchanged}")
    print(f"  keyed by path:       {report.keyed_by_path}")
    if report.warnings:
        print(f"  conversion warnings: {report.warnings}")


class _SerializableReport(Protocol):
    """Any run report that can render itself for `last_notes_run.json`."""

    def as_dict(self) -> dict[str, object]: ...


def _write_notes_report_json(config: Config, report: _SerializableReport) -> None:
    # Replicates `export._write_report_json`'s two lines rather than
    # importing that (private) helper -- `notes_import.run_import()` does
    # not write `last_notes_run.json` itself (unlike `run_export()`, which
    # writes `last_run.json` internally), so the CLI layer owns this here.
    report_path = config.state_path.parent / "last_notes_run.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _main_export(args: argparse.Namespace) -> int:
    try:
        config = load_config(
            output_dir=args.output,
            dry_run=args.dryrun,
            verbose=args.verbose,
            include_chats=args.include_chats,
            force=args.force,
        )
    except ConfigError as exc:
        print(f"quip2md: configuration error: {exc}", file=sys.stderr)
        return 2

    client = _cli.QuipClient(config)
    try:
        report: ExportReport = _cli.run_export(client, config, only=args.only)
    except KeyboardInterrupt:
        print(
            "\nquip2md: export interrupted. The manifest has been flushed; "
            "re-run the same command to resume.",
            file=sys.stderr,
        )
        return 130
    except _cli.ManifestError as exc:
        print(f"quip2md: manifest error: {exc}", file=sys.stderr)
        return 2
    except _cli.QuipApiError as exc:
        print(f"quip2md: API error: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    if config.dry_run:
        return 0

    _print_report(report)
    return 1 if report.failed else 0


def _main_import_notes(args: argparse.Namespace) -> int:
    # Design choice: `import-notes` is a purely offline operation over
    # `--source` and never needs `QUIP_TOKEN`, so it bypasses
    # `load_config()` entirely (rather than calling it and catching
    # `ConfigError`) and builds `Config` directly -- simpler than routing
    # through a token-requiring loader just to discard the token.
    config = Config(
        token="",
        output_dir=DEFAULT_OUTPUT_DIR,
        state_path=DEFAULT_STATE_PATH,
        dry_run=args.dryrun,
        verbose=args.verbose,
        include_chats=False,
        force=args.force,
    )

    if args.writer == "enex":
        return _main_import_notes_enex(args, config)

    try:
        runner = _cli.NotesRunner()
    except _cli.NotesError as exc:
        print(f"quip2md: Notes error: {exc}", file=sys.stderr)
        return 2

    try:
        report: ImportReport = _cli.run_import(
            runner, config, source_dir=args.source, local=args.local, only=args.only
        )
    except KeyboardInterrupt:
        print(
            "\nquip2md: import interrupted. .quip2md/notes_state.json has "
            "been flushed; re-run the same command to resume.",
            file=sys.stderr,
        )
        return 130
    except _cli.NotesStateError as exc:
        print(f"quip2md: notes state error: {exc}", file=sys.stderr)
        return 2
    except _cli.NotesError as exc:
        print(f"quip2md: Notes error: {exc}", file=sys.stderr)
        return 2

    if config.dry_run:
        _print_dry_run_notes_report(report)
        return 0

    _write_notes_report_json(config, report)
    _print_import_report(report)
    return 1 if report.failed else 0


def _main_import_notes_enex(args: argparse.Namespace, config: Config) -> int:
    """The fidelity-preserving import: render a `.enex`, then let Notes open it."""
    runner: EnexNotesRunnerProtocol | None = None
    if not config.dry_run:
        try:
            runner = _cli.EnexNotesRunner()
        except _cli.NotesError as exc:
            print(f"quip2md: Notes error: {exc}", file=sys.stderr)
            return 2

    try:
        report: EnexImportReport = _cli.run_enex_import(
            runner,
            config,
            source_dir=args.source,
            enex_path=args.enex_file,
            only=args.only,
            workers=args.workers,
            adopt_landing=args.adopt_landing,
        )
    except KeyboardInterrupt:
        print(
            "\nquip2md: import interrupted. Any notes already filed are "
            "recorded in .quip2md/notes_state.json.",
            file=sys.stderr,
        )
        return 130
    except _cli.NotesStateError as exc:
        print(f"quip2md: notes state error: {exc}", file=sys.stderr)
        return 2
    except _cli.NotesError as exc:
        print(f"quip2md: Notes error: {exc}", file=sys.stderr)
        return 2

    _print_enex_report(report, dry_run=config.dry_run)
    if config.dry_run:
        return 0

    indent_report: IndentReport | None = None
    if args.indent_checklists and report.indent_targets:
        try:
            indent_report = _cli.indent_notes(_cli.IndentRunner(), report.indent_targets)
        except _cli.NotesError as exc:
            print(f"quip2md: checklist indentation skipped: {exc}", file=sys.stderr)
            return 1
        _print_indent_report(indent_report)
    elif report.indent_targets:
        print(
            f"\n{len(report.indent_targets)} note(s) have nested checklists that "
            "Notes flattened on import. Re-run with --indent-checklists to "
            "restore their indentation."
        )

    _write_notes_report_json(config, report)
    # Unmatched notes are a failure of the run, not a detail of it: they sit in
    # the landing folder, filed nowhere and recorded in no state file, and only
    # a human can decide what they are.
    failed = (
        bool(report.failed)
        or bool(report.unmatched)
        or bool(indent_report and indent_report.failures)
    )
    return 1 if failed else 0


def _print_enex_report(report: EnexImportReport, *, dry_run: bool) -> None:
    heading = "Dry run (no Notes changes)." if dry_run else "Import complete."
    print(heading)
    print(f"  documents:           {report.documents}")
    if report.enex_path:
        print(f"  archive:             {report.enex_path} ({report.enex_bytes / 1_000_000:.1f} MB)")
    else:
        print("  archive:             none written (nothing to import)")
    print(
        f"  checklist items:     {report.checklist_items} "
        f"({report.checklist_checked} checked, "
        f"{report.checklist_items - report.checklist_checked} unchecked)"
    )
    print(f"  hyperlinks:          {report.links}")
    print(f"  images:              {report.images}")
    print(
        f"  nested checklists:   {report.docs_needing_indent} doc(s), "
        f"{report.indent_levels} level(s)"
    )
    if not dry_run:
        print(f"  imported:            {report.imported}")
        print(f"  filed into folders:  {report.moved}")
    # Reported in both modes: a dry run consults the same state file, so this
    # is what a real run would skip, not a guess at it.
    print(f"  skipped (unchanged): {report.skipped_unchanged}")
    if not dry_run:
        if report.superseded:
            print(
                f"  superseded:          {report.superseded} "
                "(previous copies are still in Notes -- delete them by hand)"
            )
        if report.unmatched:
            folder = report.landing_folder or "the import folder"
            print(f"  unmatched (left in {folder}): {len(report.unmatched)}")
            for name in report.unmatched[:10]:
                print(f"    - {name}")
    print(f"  conversion warnings: {report.warnings}")
    if report.failed:
        print(f"  failed:              {len(report.failed)}")
        for key, reason in report.failed[:10]:
            print(f"    - {key}: {reason}")


def _print_indent_report(report: IndentReport) -> None:
    print("Checklist indentation:")
    print(f"  notes considered:  {report.notes_considered}")
    print(f"  notes indented:    {report.notes_indented}")
    print(f"  already flat:      {report.notes_already_flat}")
    print(f"  skipped (no match):{report.skipped_unrecognized}")
    print(f"  levels applied:    {report.levels_applied}")
    for note, reason in report.failures:
        print(f"    - {note}: {reason}")


def _reject_incompatible_writer_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Fail loudly on a flag the chosen writer cannot honour.

    Both combinations used to be accepted and then ignored, which reads as a
    silent success: `--local` cannot steer Notes' own importer, and the
    indentation pass only exists to repair what that importer flattens.
    """
    if args.writer == "enex" and args.local:
        parser.error(
            "--local cannot be used with --writer enex: Notes always imports an "
            "archive into the default account. Use --writer applescript to "
            'target "On My Mac".'
        )
    if args.writer == "applescript" and args.indent_checklists:
        parser.error(
            "--indent-checklists cannot be used with --writer applescript: that "
            "writer produces no checklists to indent. Use --writer enex."
        )
    if args.writer == "applescript" and args.adopt_landing:
        parser.error(
            "--adopt-landing cannot be used with --writer applescript: only the "
            "enex writer imports through a landing folder."
        )


def _main_prune_notes(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Delete leftovers. Prints the plan and changes nothing without `--apply`."""
    if not (args.folder or args.empty_landing or args.superseded):
        parser.error("nothing to prune: pass --folder, --empty-landing and/or --superseded")

    config = Config(
        token="",
        output_dir=DEFAULT_OUTPUT_DIR,
        state_path=DEFAULT_STATE_PATH,
        dry_run=not args.apply,
        verbose=args.verbose,
        include_chats=False,
        force=False,
    )
    try:
        report = _cli.prune_notes(
            _cli.PruneRunner(),
            config,
            folders=args.folder,
            empty_landing=args.empty_landing,
            superseded=args.superseded,
            apply=args.apply,
        )
    except _cli.NotesStateError as exc:
        print(f"quip2md: notes state error: {exc}", file=sys.stderr)
        return 2
    except _cli.NotesError as exc:
        print(f"quip2md: Notes error: {exc}", file=sys.stderr)
        return 2

    print("Prune complete." if args.apply else "Prune plan (nothing deleted; pass --apply).")
    print(f"  notes deleted:     {report.notes_deleted}")
    print(f"  folders deleted:   {len(report.folders_deleted)}")
    for name in report.folders_deleted:
        print(f"    - {name}")
    for target, reason in report.skipped:
        print(f"  skipped {target}: {reason}")
    for target, reason in report.failed:
        print(f"  FAILED {target}: {reason}")
    return 1 if report.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "import-notes":
        _reject_incompatible_writer_flags(parser, args)
        return _main_import_notes(args)
    if args.command == "prune-notes":
        return _main_prune_notes(parser, args)
    return _main_export(args)
