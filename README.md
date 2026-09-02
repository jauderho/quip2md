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
| `--local` | Target the "On My Mac" Notes account instead of the default account (usually iCloud). Only valid with `--writer applescript`; rejected (exit 2) with the default `enex` writer, which always imports into the default account. |
| `--dryrun` | Scan and convert every source under `--source`, printing per-folder note counts; makes zero Notes automation calls and writes no `.quip2md/notes_state.json`. |
| `--verbose`, `-v` | Enable step-level DEBUG logging. Silent (warnings and above only) by default. |
| `--force` | Re-import every note even if the state file says it's unchanged since the last run. With `--writer enex` this creates a second note per document; the previous copy is recorded as superseded and left in Notes for you to delete. |
| `--only KEY` | Restrict the import to one note: a Quip thread id (from a source file's `quip_id` frontmatter), or, for a file without that frontmatter, its `path:<relative/posix/path>` key. Repeatable. |
| `--adopt-landing FOLDER` | Resume an import whose notes reached Notes but were never filed, e.g. one that failed after you clicked Import. Imports nothing; files the notes already sitting in the named `Imported Notes N` folder and records them in the state file. Use this instead of re-running, which would import a second copy of every document. `--writer enex` only. |
| `--workers N` | Number of processes used to render Markdown to ENML with `--writer enex`. Defaults to the CPU count capped at 6 (measured optimum on a 4P+4E Apple Silicon machine; a homogeneous x86-64 box may benefit from more). `1` renders in-process. Output is byte-identical at any setting. |

### macOS requirement and permissions

`import-notes` automates Notes.app via AppleScript/`osascript` and only runs
on macOS. It needs **two different** macOS privacy permissions, and they are
not the same grant — having one does not imply the other. Both are granted to
whichever application runs `quip2md` (your terminal: Terminal, iTerm, Ghostty,
VS Code…), not to `quip2md` itself.

| Permission | Where | Needed for | Symptom if missing |
|---|---|---|---|
| **Automation → Notes** | System Settings → Privacy & Security → Automation → *your terminal* → Notes | Every import. Granted by clicking **OK** on the one-time "… wants to control Notes" prompt. | Import fails immediately; osascript reports "Not authorized to send Apple events". |
| **Accessibility** | System Settings → Privacy & Security → **Accessibility** → add and enable *your terminal* | `--indent-checklists` only. That pass drives the Notes editor's Format menu, which is UI scripting. | The pass refuses to start and prints how to fix it. Import itself is unaffected. |

The Accessibility grant is the one people miss, because the import works fine
without it. There is no prompt for it in a non-interactive run — add the
terminal to the list yourself, then re-run. If the terminal was already listed,
toggle it off and on again after upgrading it, since the grant is tied to the
binary.

Notes' own **Import** sheet still has to be clicked by a human, once per run,
whatever the permissions say. That is Notes' confirmation, not a permission.

### What lands in Notes

- A single top-level **Quip** folder in the target account, with the rest
  of the tree mirrored underneath it exactly as it appears under `export/`.
- One note per source Markdown file, converted from its frontmatter + body.
- Images embed as real inline attachments (not links).

### Re-run / update behavior

State lives in `.quip2md/notes_state.json` (thread id/path key -> note id +
source hash). Re-running the same command:

- Skips notes whose source hasn't changed since the last run — no
  duplicates are created. With the `enex` writer, a run in which everything is
  unchanged writes no archive and never opens Notes at all.
- Handles a changed source differently per writer:
  - `applescript` updates the note in place (same note, same id).
  - `enex` **imports a second note**: Notes' importer cannot replace an
    existing note, and rewriting the body would destroy exactly the links and
    checklists this writer exists to preserve. The new note takes over the
    state entry, the old note's id is recorded under `superseded_note_ids`,
    and the run ends with a warning telling you how many stale copies are
    still in Notes. **Nothing is ever deleted for you** — delete the old
    copies by hand.
- `--force` re-pushes every note regardless of the state file (with `enex`,
  that means a duplicate of every note, each one superseding its predecessor).

If interrupted with Ctrl-C, the process exits with status 130. State is
flushed to disk per folder, so re-running the same command resumes safely
without duplicating notes already imported.

### Import writers

`import-notes` has two writers. `--writer enex` is the default.

**`enex` (default)** renders every document that has changed since the last run
into one Evernote archive and hands it to Notes' own importer. Notes shows a
single confirmation sheet — click **Import** — then fills a fresh
`Imported Notes N` folder, which the run polls until its note count stops
growing. Each note is then matched back to its source by the Quip URL on its
`Source:` provenance line (only that line counts, so a link to another exported
document cannot mis-file a note) and moved into its mirrored folder under
`Quip`. Notes it cannot match are left there and listed in the report, and the
run exits with status 1 so they are not mistaken for a clean import. This
writer preserves:

- clickable hyperlinks, including the link back to the original Quip document;
- native checklists, with checked and unchecked state;
- nested bullet lists, numbered lists, headings, code blocks, tables, images.

Its limitations: Notes' importer always creates checklist items at the top
level, so nested checklists arrive flat (`--indent-checklists` restores their
indentation by driving the Notes editor; it needs macOS Accessibility permission
and is off by default), and it cannot update a note in place — see
[Re-run / update behavior](#re-run--update-behavior). It also always imports
into the default account, so `--local` is rejected with this writer.

**`applescript`** is the original writer, kept for environments that cannot use
the importer. It cannot produce hyperlinks or checkboxes at all: Notes discards
`<a href>` and every form of checkbox markup supplied through the scripting
interface, so links become plain text and checklists become bullets containing
literal `[x]` / `[ ]`. `--indent-checklists` is rejected with this writer:
there are no checklists to indent.

### What formatting survives

Verified against the real corpus by reading the imported notes back out of
Notes and comparing them with what was sent:

| Carried over | How it lands in Notes |
|---|---|
| **Bold**, *italic*, ~~strikethrough~~, `inline code` | Real character formatting |
| Headings (`#`, `##`, `###`) | Notes' Title / Heading / Subheading styles — stored as bold + a larger font rather than as `<h2>` tags |
| Tables | A real Notes table |
| Nested bullet and numbered lists | Real nested lists |
| Checklists, checked and unchecked | Real tappable checkboxes |
| Hyperlinks, including the source link | Real clickable links |
| Images | Embedded attachments |

Across all 492 documents the renderer raises only 40 fidelity warnings, so the
list below is genuinely everything that does not survive.

### Fidelity caveats

With the default `enex` writer, Apple Notes still normalizes some things:

- Nested checklists import flat unless `--indent-checklists` is used.
- Blockquotes keep their text but lose the quote styling (1 in this corpus).
- Horizontal rules are dropped (6).
- Links with schemes outside `https`, `http`, `mailto` and `tel` render as
  text (5, all malformed in the source).
- A list that mixes checklist and plain items keeps the plain ones as bullets
  at their own depth (28).

Two things are worth knowing because they are Notes bugs rather than choices:

- **Angle brackets are escaped twice.** Notes decodes an imported note's
  content and then re-parses the result as HTML, so a correctly escaped
  `&lt;profile name&gt;` becomes an element on the second pass and the text
  disappears. Writing `&amp;lt;` instead survives. 141 occurrences in this
  corpus depended on it.
- **`--indent-checklists` changes a note's modification date, and that cannot
  be undone.** Notes exposes `modification date` as read-only (AppleScript
  error `-10006`), and the only supported way to set it is the `<updated>`
  value in the archive at *import* time — which is necessarily before the
  indentation is applied. The **creation** date is preserved, so sorting by
  *Date Created* still reflects the original document; sorting by *Date
  Edited* will show indented notes as recently touched. `export/` keeps the
  true `updated` timestamp in each file's frontmatter either way.

`export/` remains the full-fidelity Markdown backup by design — Notes is
the working copy, `export/` is the archive.

### Time expectations

Measured on the same real personal account (492 notes):

- Full import: 92.6 seconds, zero failures.
- Immediate re-run (nothing changed): ~16 seconds, all 492 notes
  skipped-unchanged, zero duplicates.

## Cleaning up after an import

`import-notes` never deletes anything. A re-imported document leaves its
previous copy in Notes (recorded under `superseded_note_ids`), and every run
leaves an empty `Imported Notes N` folder behind. `prune-notes` is the one
command that removes them, and it does nothing without `--apply`:

```bash
uv run quip2md prune-notes --superseded --empty-landing
```

| Flag | Effect |
|---|---|
| `--superseded` | Delete the previous copy of every re-imported note, by the exact id recorded in `notes_state.json`, then clear those records. Never deletes an id that is still some document's current note. |
| `--empty-landing` | Delete every *empty* `Imported Notes N` folder. |
| `--folder NAME` | Delete a named top-level folder and everything in it. Repeatable. Refuses anything that is not a top-level folder of the account, and refuses `Quip` outright. |
| `--apply` | Actually delete. Without it the plan is printed and nothing is touched. |

Deleted notes and folders go to Notes' **Recently Deleted**, so this is
recoverable for thirty days.

**Order matters.** Notes does not persist deleting a folder that still holds
notes — it reports success and the folder is back moments later, which was
observed live on iCloud with a 492-note folder while the empty ones stayed
gone. Empty a folder first (`--superseded`), then delete it. `prune-notes`
re-reads the account after deleting and reports any folder that came back
rather than claiming a deletion that undid itself.

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
| `1` | One or more threads failed to export, or one or more notes failed to import or could not be matched back to a source (the `enex` writer leaves those in its landing folder, whose name the report prints). See the printed report and `.quip2md/last_run.json` / `.quip2md/last_notes_run.json`. |
| `2` | A rejected flag combination (`--local` with `--writer enex`, `--indent-checklists` with `--writer applescript`), or a configuration, manifest, or Notes-state error: missing `QUIP_TOKEN` (export), a corrupted `state.json`/`notes_state.json`, an API error during the initial folder walk, or, for `import-notes`, a non-macOS platform or a missing "On My Mac" account. A clear message is printed to stderr, no traceback. |
| `130` | Interrupted (Ctrl-C). The manifest/state file was already flushed; re-run the same command to resume. |
