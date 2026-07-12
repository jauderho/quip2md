"""Command-line entry point for quip2md.

Usage:
    quip2md export [--output DIR] [--dryrun] [--verbose | -v] [--force]
                    [--include-chats] [--only THREAD_ID [--only THREAD_ID ...]]
    quip2md import-notes [--source DIR] [--local] [--dryrun] [--verbose | -v]
                    [--force] [--only KEY [--only KEY ...]]

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
    --local               Target the "On My Mac" Notes account instead of
                         the default account (usually iCloud).
    --dryrun              Scan and convert every source under --source
                         (surfacing per-folder note counts and conversion
                         warnings), but make zero Notes automation calls and
                         write no `.quip2md/notes_state.json`.
    --verbose, -v         Enable DEBUG-level logging for the `quip2md`
                         logger hierarchy, with a concise one-line format.
                         Silent (WARNING and above only) by default.
    --force               Re-import every note even if the state file says
                         it is unchanged since the last run.
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
         to import -- see the printed report and `.quip2md/last_run.json` /
         `.quip2md/last_notes_run.json` for details.
    2    configuration, manifest, or Notes-state error (e.g. `QUIP_TOKEN`
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
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from quip2md.client import QuipApiError, QuipClient
from quip2md.config import DEFAULT_OUTPUT_DIR, DEFAULT_STATE_PATH, Config, ConfigError, load_config
from quip2md.export import ExportReport, run_export
from quip2md.notes_import import ImportReport, NotesError, NotesRunner, NotesStateError, run_import
from quip2md.walker import ManifestError

_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


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
        help='Target the "On My Mac" account instead of the default account.',
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
        help="Re-import every note even if unchanged since the last run.",
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


def _write_notes_report_json(config: Config, report: ImportReport) -> None:
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

    client = QuipClient(config)
    try:
        report = run_export(client, config, only=args.only)
    except KeyboardInterrupt:
        print(
            "\nquip2md: export interrupted. The manifest has been flushed; "
            "re-run the same command to resume.",
            file=sys.stderr,
        )
        return 130
    except ManifestError as exc:
        print(f"quip2md: manifest error: {exc}", file=sys.stderr)
        return 2
    except QuipApiError as exc:
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

    try:
        runner = NotesRunner()
    except NotesError as exc:
        print(f"quip2md: Notes error: {exc}", file=sys.stderr)
        return 2

    try:
        report = run_import(
            runner, config, source_dir=args.source, local=args.local, only=args.only
        )
    except KeyboardInterrupt:
        print(
            "\nquip2md: import interrupted. .quip2md/notes_state.json has "
            "been flushed; re-run the same command to resume.",
            file=sys.stderr,
        )
        return 130
    except NotesStateError as exc:
        print(f"quip2md: notes state error: {exc}", file=sys.stderr)
        return 2
    except NotesError as exc:
        print(f"quip2md: Notes error: {exc}", file=sys.stderr)
        return 2

    if config.dry_run:
        _print_dry_run_notes_report(report)
        return 0

    _write_notes_report_json(config, report)
    _print_import_report(report)
    return 1 if report.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "import-notes":
        return _main_import_notes(args)
    return _main_export(args)
