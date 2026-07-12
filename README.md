# quip2md

Bulk-export a personal Quip account to Markdown, mirroring the account's
folder structure on disk. Built ahead of Quip's March 2027 retirement so a
personal account's documents survive as plain Markdown files instead of
being locked in Quip. An optional second step imports that Markdown backup
into Apple Notes (macOS only), so the migration path is Quip → Markdown
backup (`export`) → Apple Notes (`import-notes`), with `export/` remaining
the durable, full-fidelity archive either way.

## Requirements

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/)

## Setup

```bash
git clone <this repo>
cd quip2md
uv sync
```

Generate a personal API token at <https://quip.com/dev/token> and put it in
a `.env` file in the project root:

```
QUIP_TOKEN=your-token-here
```

The token is read from `QUIP_TOKEN` in the process environment or `.env`
(environment wins if both are set) and is never logged or printed. Personal
Quip tokens expire; if a run fails with a 400/401 "invalid or expired" error,
mint a new token at the link above and update `.env`.

## Usage

```bash
uv run quip2md export
```

Run `--dryrun` first on a new account to sanity-check the folder tree before
fetching anything:

```bash
uv run quip2md export --dryrun --verbose
```

### Flags

All flags are under the `export` subcommand.

| Flag | Effect |
|---|---|
| `--output DIR` | Directory to write the exported Markdown tree into. Defaults to `./export`. |
| `--dryrun` | Walk the account's folder tree and print the would-be output tree with per-folder thread counts. Makes no thread-HTML or blob requests and writes nothing to disk or to the manifest. |
| `--verbose`, `-v` | Enable step-level DEBUG logging. Silent (warnings and above only) by default. |
| `--force` | Re-export every thread even if the manifest says it's unchanged since the last run. |
| `--include-chats` | Also export CHAT-type threads. Skipped by default — see `AGENTS.md`/`PLAN.md`. |
| `--only THREAD_ID` | Restrict the export to the given thread id. Repeatable to export several specific threads (useful for a scoped test run before exporting the whole account). |

## What you get

- `export/<Quip folder path>/<sanitized title>.md` — one Markdown file per
  document/spreadsheet, in a tree that mirrors the Quip account's folders.
  Filename collisions within a folder get ` (2)`, ` (3)` suffixes.
- Each `.md` file has YAML frontmatter: `quip_id`, `quip_url`, `title`,
  `created`, `updated`, `exported`.
- `_assets/<thread_id>/` next to each document holds its downloaded images.
- Spreadsheets are converted to a Markdown table; when a table is too wide
  or complex to convert cleanly, a fidelity-preserving `.xlsx` backup is
  written alongside the `.md` file.
- `.quip2md/state.json` — the resume manifest (thread id -> output path,
  update timestamp, export time). Do not delete this unless you want the
  next run to re-export everything.
- `.quip2md/last_run.json` — a report of the most recent run (counts of
  exported/skipped/failed threads, blobs downloaded, elapsed time).

## Resume / incremental behavior

Every run records each exported thread's Quip `updated_usec` in
`.quip2md/state.json`. On the next run, a thread is skipped if its
`updated_usec` hasn't changed since the manifest was written — even though
its metadata is still fetched to check for changes. Use `--force` to
re-export everything regardless of the manifest.

If the export is interrupted with Ctrl-C, the process exits with status
130. The manifest has already been flushed to disk at that point, so
re-running the exact same command resumes from where it left off rather
than starting over.

## Time expectations

Measured on a real personal account (492 threads):

- Full export: ~57 seconds, ~35 batched API requests, 29 images downloaded,
  zero failures.
- Immediate re-run (nothing changed): ~35 seconds — every thread is
  skipped-unchanged, but metadata is still fetched for all of them to
  detect changes.

The client self-throttles to 80% of the observed per-minute rate limit (50
requests/minute per personal token) and adapts to the `X-RateLimit-*`
response headers, so you never need to handle 429s yourself. For rough
sizing on a larger account: at ~40 effective requests/minute and roughly 15
threads fetched per batched request, expect on the order of a few minutes
per thousand threads.

## Import into Apple Notes

Once you have an `export/` tree, import it into Apple Notes.app:

```bash
uv run quip2md import-notes
```

Run `--dryrun` first to sanity-check the scan before touching Notes:

```bash
uv run quip2md import-notes --dryrun --verbose
```

`import-notes` is a purely offline operation over `--source` (default
`./export`) — it never reads `QUIP_TOKEN` or `.env`.

### Flags

All flags are under the `import-notes` subcommand.

| Flag | Effect |
|---|---|
| `--source DIR` | Directory containing the Markdown tree an earlier `quip2md export` run wrote. Defaults to `./export`. |
| `--local` | Target the "On My Mac" Notes account instead of the default account (usually iCloud). |
| `--dryrun` | Scan and convert every source under `--source`, printing per-folder note counts; makes zero Notes automation calls and writes no `.quip2md/notes_state.json`. |
| `--verbose`, `-v` | Enable step-level DEBUG logging. Silent (warnings and above only) by default. |
| `--force` | Re-import every note even if the state file says it's unchanged since the last run. |
| `--only KEY` | Restrict the import to one note: a Quip thread id (from a source file's `quip_id` frontmatter), or, for a file without that frontmatter, its `path:<relative/posix/path>` key. Repeatable. |

### macOS requirement

`import-notes` automates Notes.app via AppleScript/`osascript` and only runs
on macOS. The *first* run in a while may show a one-time system prompt
("... wants to control Notes") — approve it once per terminal app for the
import to proceed; later runs don't re-prompt.

### What lands in Notes

- A single top-level **Quip** folder in the target account, with the rest
  of the tree mirrored underneath it exactly as it appears under `export/`.
- One note per source Markdown file, converted from its frontmatter + body.
- Images embed as real inline attachments (not links).

### Re-run / update behavior

State lives in `.quip2md/notes_state.json` (thread id/path key -> note id +
source hash). Re-running the same command:

- Skips notes whose source hasn't changed since the last run — no
  duplicates are created.
- Updates changed source files in place (same note, same id).
- `--force` re-pushes every note regardless of the state file.

If interrupted with Ctrl-C, the process exits with status 130. State is
flushed to disk per folder, so re-running the same command resumes safely
without duplicating notes already imported.

### Fidelity caveats

Apple Notes normalizes content on its own terms, independent of this tool:

- Hyperlinks are stripped — links render as plain `text (url)`.
- Nested numbered lists flatten to a single level.
- Code blocks become plain monospace text (no code box).
- Headings become bold, sized text rather than semantic headings.
- Tables and images survive intact.

`export/` remains the full-fidelity Markdown backup by design — Notes is
the working copy, `export/` is the archive.

### Time expectations

Measured on the same real personal account (492 notes):

- Full import: 92.6 seconds, zero failures.
- Immediate re-run (nothing changed): ~16 seconds, all 492 notes
  skipped-unchanged, zero duplicates.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `400`/`401` "invalid or expired" | Your token has expired (observed live as HTTP 400). Mint a new one at <https://quip.com/dev/token> and update `.env`. Only applies to `export` — `import-notes` needs no token. |
| `429` / `503` in verbose logs | Normal rate-limit backoff during `export` — the client handles retries automatically. Just let it run. |
| Corrupted `.quip2md/state.json` | `export` prints a clear manifest error and exits with status 2 rather than silently continuing. Delete `.quip2md/state.json` to start a fresh export (you'll lose incremental skip state, not any exported files). |
| Corrupted `.quip2md/notes_state.json` | `import-notes` prints a clear state error and exits with status 2. Deleting the file lets the next run proceed, but since it wipes the id/hash mapping, that next run will recreate every note as a duplicate. If you actually want to start over, delete the **Quip** folder in Notes.app first, then delete `.quip2md/notes_state.json` and re-run. |
| `import-notes` on a non-macOS platform, or missing "On My Mac" account with `--local` | Exits with status 2 and a clear message — this subcommand only works on macOS with Notes.app configured accordingly. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, nothing failed. |
| `1` | One or more threads failed to export, or one or more notes failed to import. See the printed report and `.quip2md/last_run.json` / `.quip2md/last_notes_run.json`. |
| `2` | Configuration, manifest, or Notes-state error: missing `QUIP_TOKEN` (export), a corrupted `state.json`/`notes_state.json`, an API error during the initial folder walk, or, for `import-notes`, a non-macOS platform or a missing "On My Mac" account. A clear message is printed to stderr, no traceback. |
| `130` | Interrupted (Ctrl-C). The manifest/state file was already flushed; re-run the same command to resume. |
