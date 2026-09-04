# PLAN.md — Quip → Markdown Bulk Exporter

**Status:** Phase 1 (T1–T8) COMPLETE and live-verified 2026-07-11 (492/492 threads,
0 failures). Phase 2 (T9–T12, Apple Notes import) added 2026-07-12 — see §7.
**Deadline pressure:** Quip retires March 2027. No urgency, but don't let this rot.

---

## 1. Mission

Build a working CLI tool that logs into a **personal Quip account** (via the API
token already in `.env` as `QUIP_TOKEN`, generated at https://quip.com/dev/token)
and bulk-exports every document to Markdown files on disk, **mirroring the folder
structure** of the Quip account. Must respect Quip API rate limits and be safely
re-runnable (resume/incremental).

### Decisions already made (do not re-litigate)

| Decision | Choice | Rationale |
|---|---|---|
| Language | **Python 3.14+, `uv`-managed** | HTML→Markdown ecosystem (markdownify/BS4) is far stronger than Go's; the two reference projects (coxley/quip2md is Python, official `quip.py` client is Python) port directly; this is an I/O-bound, rate-limit-bound tool where Go's performance buys nothing. |
| Packaging | Small `uv` project (`pyproject.toml`, `src/quip2md/`, `tests/`) — not a single PEP 723 script | Needs a real test suite per AGENTS.md. |
| HTML source | **API HTML** (v2 paginated `/2/threads/{id}/html`, fallback v1 `/1/threads/?ids=`) → local conversion with `markdownify` + BeautifulSoup post-processing | Quip has no native Markdown export. DOCX round-tripping (what quip-export does) loses less-common structures and adds a heavy dependency. |
| Reference repos | https://github.com/coxley/quip2md (Python, 8 yrs stale — mine it for Quip HTML quirks and blob handling), https://github.com/jmoraispk/quip-export (Node — mine it for folder-traversal and edge-case handling) | **Reference only. Do not vendor or port wholesale.** Both predate the v2 API. |
| Output layout | `export/<Quip folder path>/<sanitized title>.md` + sibling `_assets/<thread_id>/` for images | Mirrors account structure as required. |
| Frontmatter | Each `.md` gets YAML frontmatter: `quip_id`, `quip_url`, `title`, `created`, `updated`, `exported` | Traceability + enables incremental re-export. |
| Spreadsheets | Markdown table conversion **and** an `.xlsx` fidelity backup via `/1/threads/{id}/export/xlsx` | Tables >~50 cols/complex formulas don't survive MD. |
| Chats | Skipped by default; `--include-chats` exports as transcript `.md` | User asked for "notes"; chats are noise for most. |
| Slides/PDF-only types | Export via `/1/threads/{id}/export/pdf` into place, log a warning | Quip Slides is long-deprecated; PDF is best-available. |
| Resume state | `.quip2md/state.json` keyed by `thread_id` storing `updated_usec` + output path; skip unchanged threads on re-run; `--force` overrides | 750 req/hr means a big account takes hours — resumability is mandatory, not nice-to-have. |

### Non-goals
- No two-way sync, no watch mode, no GUI.
- No export of comments/annotations (out of scope v1; noted as possible v2).
- No support for company/enterprise admin APIs — personal token only.

---

## 2. Model Contract — Assignments

Per AGENTS.md: orchestrator = **Fable Medium** (fallback: Opus 4.8 High).
Implementer ceiling = **Fable Low**. Orchestrator reviews every diff before accepting.

| Task | Title | Implementer model | Why this model |
|---|---|---|---|
| T1 | API ground-truth recon | **Fable Low** | Exploratory, ambiguous, everything downstream depends on getting the facts right. |
| T2 | Project scaffold + config | **Haiku 4.5** | Purely mechanical, fully specified. |
| T3 | Rate-limited API client | **Fable Low** | Subtle correctness: throttling, backoff, header-driven pacing, retry semantics. |
| T4 | Folder walker + manifest/resume | **Sonnet 5** | Moderate: recursion, cycles, dedup, state file. Well-specified after T1. |
| T5 | HTML→Markdown converter + assets | **Fable Low** | Highest-fidelity-risk component; Quip HTML is quirky. |
| T6 | CLI wiring, `--dryrun`/`--verbose`, progress | **Sonnet 5** | Well-specified glue. |
| T7 | Test suite hardening | **Sonnet 5** | Each prior task ships its own tests; this pass adds edge/error-path coverage. |
| T8 | Live E2E run + README | **Sonnet 5** (execution), **orchestrator reviews results** | Running and documenting; judgment stays with orchestrator. |

Ordering: T1 → T2 → (T3, T5 in parallel) → T4 → T6 → T7 → T8.
T5 only needs T1's sample HTML fixtures, not the live client — hence parallelizable with T3.

**Review gate after every task:** orchestrator reads the actual diff (not the
implementer's summary), runs `uv run pytest`, `ruff check`, `ty check`
independently, and rejects-with-sharper-prompt rather than hand-patching.

---

## 3. Architecture

```
.env (QUIP_TOKEN)
   │
   ▼
QuipClient (T3) ── token-bucket throttle + header pacing + backoff
   │
   ├─► FolderWalker (T4) ── users/current → root folder IDs → recursive
   │        │                folder listing → ordered work queue of threads
   │        ▼
   │   state.json manifest (skip unchanged, resume after crash)
   │
   ├─► ThreadExporter ── fetch HTML (v2 paginated, v1 fallback)
   │        │
   │        ▼
   │   HtmlToMarkdown (T5) ── markdownify + Quip-specific fixups
   │        │
   │        ▼
   │   AssetFetcher ── /1/blob/{tid}/{bid} → _assets/, rewrite <img> links
   │
   ▼
export/ tree mirroring Quip folders  +  export report (counts, failures, skips)
```

### Rate-limit policy (binding for T3)
- Assume defaults for personal tokens: **50 req/min and 750 req/hour** (T1 verifies
  actual values from response headers).
- Client-side token bucket set to **80% of the advertised limit** (safety margin).
- Read `X-RateLimit-Remaining` / `X-RateLimit-Reset` on every response; when
  remaining is low, sleep until reset. Treat header names case-insensitively; T1
  records the exact names.
- On HTTP **429 or 503**: honor `Retry-After` if present, else exponential backoff
  with jitter (base 2 s, cap 120 s, max 6 retries). On 5xx: same backoff, max 3.
- **Never** parallelize requests beyond the bucket. Sequential is fine — the hourly
  cap is the real constraint, not latency.
- Budget math to set expectations: ~N documents costs roughly `N × (1–3)` requests
  (HTML pages + blobs) plus folder listing. At 750/hr, 1,000 docs ≈ 3–5 hours.
  This is why resume (T4) is mandatory.

### Security & hygiene (all tasks)
- Token read from `.env` (or `QUIP_TOKEN` env var) only. Never logged, never in
  errors, never in state.json. T2 adds `.env` to the **repo-local** `.gitignore`
  (currently only ignored via the user's global gitignore — not portable).
- `--dryrun` and `--verbose` per AGENTS.md General Standards.
- Signed commits (`git commit -S -s`), one logical change per task.

---

## 4. Tasks — self-contained implementer prompts

Each block below is a ready-to-delegate prompt. Acceptance criteria are checkable
by the implementer; the orchestrator re-verifies independently.

---

### T1 — API ground-truth recon  · **Fable Low**

> **Task:** In `/Users/jauderho/projects/quip2md`, write a standalone PEP 723 script
> `scripts/recon.py` (run with `uv run scripts/recon.py`) that probes the Quip
> Automation API using the token in `.env` (`QUIP_TOKEN`), and produces
> `docs/API_NOTES.md` plus HTML fixtures. Base URL to try first:
> `https://platform.quip.com`. Auth: `Authorization: Bearer <token>`.
>
> Probe and record in `docs/API_NOTES.md`:
> 1. `GET /1/users/current` — confirm auth works; record which root folder ID
>    fields exist for this personal account (expected some of: `private_folder_id`,
>    `desktop_folder_id`, `archive_folder_id`, `starred_folder_id`,
>    `shared_folder_ids`, `group_folder_ids`).
> 2. `GET /1/folders/?ids=<comma-separated>` — confirm batch folder fetch, record
>    max batch size tolerated (try 1, 10, 50), and the shape of `children[]`
>    (folder_id vs thread_id entries).
> 3. `GET /2/threads/{id}` and `GET /2/threads/{id}/html` — confirm the v2
>    endpoints work for a personal token; record pagination mechanism
>    (`cursor` / `response_metadata.next_cursor`) and page size behavior.
>    If v2 is unavailable, confirm `GET /1/threads/?ids=` returns full `html`.
> 4. `GET /1/blob/{thread_id}/{blob_id}` — pick a doc with an image, confirm blob
>    download works, record how image refs appear in the HTML (expected
>    `src="/blob/{tid}/{bid}"` or `/-/blob/...`).
> 5. Spreadsheet + export endpoints: on one spreadsheet thread (if any exists),
>    try `GET /1/threads/{id}/export/xlsx`; on one doc, try `/export/docx` and
>    `/export/pdf`. Record which exist and their content types.
> 6. Rate-limit headers: dump all `X-RateLimit-*` / `X-Company-RateLimit-*` headers
>    from a few responses; record exact names, values, and whether the limit is
>    per-minute, per-hour, or both. Do NOT deliberately trigger a 429.
> 7. Thread `type` values observed in this account (DOCUMENT, SPREADSHEET, CHAT,
>    SLIDES, …) and counts, plus total thread count and max folder depth — this
>    sizes the export.
>
> Save 3–5 representative HTML payloads (a doc with headings/lists/code, one with
> images, one spreadsheet if present) to `tests/fixtures/`, **scrubbed of any
> content you consider sensitive is NOT required** — they stay local; but strip
> the token from anything written to disk.
>
> **Hard constraints:** read-only GETs only — no mutating endpoints, ever. Stay
> under 50 requests total for the whole recon. `set -euo pipefail`-equivalent
> rigor: `httpx` with explicit timeouts, no bare `Any`, PEP 723 header with
> `requires-python = ">=3.14"`. Never print or write the token.
>
> **Acceptance:** `docs/API_NOTES.md` exists answering all 7 points with observed
> (not assumed) values; fixtures present; script re-runnable and idempotent.

**Orchestrator note:** everything in sections 3–4 of this plan marked "expected"
is a hypothesis from training data + the two reference repos. T1's observed
values override this plan wherever they conflict. Update T3/T4/T5 prompts with
observed values before delegating them.

---

### T2 — Project scaffold + config  · **Haiku 4.5**

> **Task:** In `/Users/jauderho/projects/quip2md`, create a uv-managed Python
> project. Deliverables:
> 1. `pyproject.toml`: package `quip2md`, `requires-python = ">=3.14"`,
>    dependencies `httpx`, `beautifulsoup4`, `markdownify`, `python-dotenv`;
>    dev deps `pytest`, `ruff`, `ty`. Entry point: `quip2md = "quip2md.cli:main"`.
> 2. Layout: `src/quip2md/__init__.py`, `src/quip2md/config.py`, empty stubs
>    `client.py`, `walker.py`, `convert.py`, `export.py`, `cli.py` with module
>    docstrings only. `tests/` with a trivial passing test.
> 3. `src/quip2md/config.py`: frozen `dataclass(slots=True)` `Config` holding
>    `token: str`, `base_url: str = "https://platform.quip.com"`,
>    `output_dir: Path`, `state_path: Path`, `dry_run: bool`, `verbose: bool`,
>    `include_chats: bool`, `force: bool`. A `load_config()` that reads `.env`
>    via python-dotenv and the process env (env wins), raising a clear error if
>    `QUIP_TOKEN` is missing. Full type annotations.
> 4. Append `.env`, `.quip2md/`, `export/` to the repo-local `.gitignore`.
> 5. `ruff` and `ty` configured in `pyproject.toml`; both pass; `uv run pytest` passes.
>
> **Hard constraints:** do not touch `AGENTS.md`, `.github/`, `scripts/check*`,
> `docs/ACTIONS.md`. No `pip`, no `requirements.txt`. No logic beyond config
> loading — stubs stay empty.
>
> **Acceptance:** `uv sync && uv run pytest && uv run ruff check . && uv run ty check`
> all green; `git status` shows no `.env` exposure.

---

### T3 — Rate-limited API client  · **Fable Low**

> **Task:** Implement `src/quip2md/client.py` in `/Users/jauderho/projects/quip2md`
> (scaffold from T2 exists; observed API facts are in `docs/API_NOTES.md` — read it
> first and follow it over any assumption). Deliverables:
> 1. `QuipClient` wrapping `httpx.Client` with `Authorization: Bearer` auth,
>    explicit timeouts (connect 10 s, read 60 s), and a **token-bucket throttle**
>    capped at 80% of the per-minute limit from API_NOTES.md, plus hourly-budget
>    pacing using the observed `X-RateLimit-*` headers (case-insensitive lookup):
>    when remaining < 5, sleep until the reset timestamp.
> 2. Retry policy: 429/503 → honor `Retry-After` else exponential backoff with
>    jitter (base 2 s, cap 120 s, max 6 attempts); other 5xx → same, max 3;
>    4xx (except 429) → raise immediately with a typed error carrying status,
>    quip error message, and request path (token never included).
> 3. Typed methods (return `dataclass(slots=True)` models, no raw-dict leakage
>    across the module boundary): `current_user()`, `folders(ids)` (batched at
>    the max size recorded in API_NOTES.md), `thread(id)`, `thread_html(id)`
>    (handles v2 pagination loop per API_NOTES.md; falls back to v1 if v2
>    unavailable), `blob(thread_id, blob_id) -> bytes`,
>    `export_xlsx(id) -> bytes`, `export_pdf(id) -> bytes`.
> 4. A `RateLimiter` class isolated enough to unit-test with a fake clock —
>    no real sleeping in tests. Verbose mode logs each request line
>    (`GET /1/... -> 200, remaining=42`) via the `logging` module.
> 5. Tests (`tests/test_client.py`): use `httpx.MockTransport` — throttle math,
>    backoff sequence and `Retry-After` honoring, pagination stitching,
>    4xx raise behavior, header case-insensitivity. No network in tests.
>
> **Hard constraints:** no request parallelism; no `time.sleep` in test paths
> (inject clock/sleeper); no retries on 4xx≠429; full annotations, no `Any`.
> Do not modify files outside `client.py`, its models, and `tests/test_client.py`.
>
> **Acceptance:** `uv run pytest tests/test_client.py -v` green; ruff+ty green;
> a 10-line smoke script hitting `current_user()` live returns the user dict
> (run once, manually, reported honestly).

---

### T4 — Folder walker + manifest/resume  · **Sonnet 5**

> **Task:** Implement `src/quip2md/walker.py` and the manifest in
> `/Users/jauderho/projects/quip2md` (T2 scaffold + T3 client exist; API facts in
> `docs/API_NOTES.md`). Deliverables:
> 1. `walk(client, config) -> Iterator[ThreadWork]` where `ThreadWork` is a
>    `dataclass(slots=True)`: `thread_id`, `title`, `thread_type`, `updated_usec`,
>    `folder_path: tuple[str, ...]` (Quip folder titles root→leaf). Start from the
>    root folder IDs on `current_user()` per API_NOTES.md (private/desktop/starred
>    etc. — use the set observed there), BFS with batched `folders()` calls.
> 2. Cycle safety: track visited folder IDs, skip revisits. Dedup threads: a
>    thread reachable from multiple folders exports **once**, at the first path
>    encountered; log the duplicates at verbose level.
> 3. Path building: sanitize folder/doc titles for the filesystem (strip `/:\\*?"<>|`,
>    collapse whitespace, trim to 120 chars, never empty — fall back to thread_id).
>    Collisions within a folder get ` (2)`, ` (3)` suffixes deterministically.
> 4. Manifest `Manifest` class over `.quip2md/state.json`: `{thread_id: {path,
>    updated_usec, exported_at}}`. Atomic writes (tmp file + `os.replace`) after
>    every N=20 exports and on clean shutdown. `should_export(work)` returns False
>    when the stored `updated_usec` matches and `--force` is off.
> 5. Tests (`tests/test_walker.py`): fake client fixture — cycle graph, thread in
>    two folders, title collisions, unicode/emoji titles, empty title, manifest
>    skip/force logic, atomic-write behavior (crash between tmp and replace
>    leaves old state readable).
>
> **Hard constraints:** walker does no HTML fetching and no file writing besides
> the manifest — it yields work items only. Don't touch `client.py` or `convert.py`.
>
> **Acceptance:** `uv run pytest tests/test_walker.py -v` green; ruff+ty green.

---

### T5 — HTML→Markdown converter + assets  · **Fable Low**

> **Task:** Implement `src/quip2md/convert.py` in `/Users/jauderho/projects/quip2md`
> using the real Quip HTML fixtures in `tests/fixtures/` (from T1) as ground truth.
> Deliverables:
> 1. `html_to_markdown(html: str, asset_resolver) -> ConversionResult` built on
>    `markdownify` + BeautifulSoup pre/post-processing. Must handle Quip's HTML
>    dialect correctly: heading levels; nested and mixed ordered/unordered lists
>    (Quip encodes list nesting in ways naive converters flatten — verify against
>    fixtures); checklists → `- [ ]` / `- [x]`; code blocks with language hints
>    preserved as fenced blocks; blockquotes; tables → GFM pipe tables (escape
>    `|` in cells); `<del>/<s>`, bold/italic/links; horizontal rules; Quip
>    `@`-mentions of people/docs → plain text or `[title](quip_url)` respectively;
>    date mentions → plain text.
> 2. Image/blob handling: for each `<img>` whose src matches the blob pattern in
>    `docs/API_NOTES.md`, call the injected `asset_resolver(thread_id, blob_id,
>    suggested_ext) -> relative path` and rewrite to a relative Markdown image
>    link (`_assets/<thread_id>/<blob_id>.<ext>`). The resolver is injected so
>    this module stays network-free.
> 3. Spreadsheet HTML → GFM table (best-effort; formulas as displayed values);
>    return a flag when the table exceeds 30 columns so the exporter also saves
>    the xlsx backup.
> 4. YAML frontmatter builder: `quip_id`, `quip_url`, `title`, `created`,
>    `updated` (ISO 8601 from usec), `exported`. YAML-escape titles safely.
> 5. Unknown/unhandled elements degrade gracefully (unwrap, keep text) and are
>    counted in `ConversionResult.warnings` — never crash, never silently drop
>    text content. That property is the module's contract: **no text loss**.
> 6. Tests (`tests/test_convert.py`): golden-file tests against every fixture
>    (commit expected `.md` outputs); targeted unit tests for each element type
>    above; a text-preservation property test (all visible text nodes in input
>    appear in output, modulo whitespace).
>
> **Hard constraints:** no network, no filesystem writes (pure functions +
> injected resolver). Don't touch other modules. If a fixture reveals a structure
> this prompt doesn't cover, handle it and note it in the PR description — don't
> ignore it.
>
> **Acceptance:** `uv run pytest tests/test_convert.py -v` green; golden files
> committed; ruff+ty green; text-preservation test passes on all fixtures.

---

### T6 — CLI + export orchestration  · **Sonnet 5**

> **Task:** Implement `src/quip2md/export.py` and `src/quip2md/cli.py` in
> `/Users/jauderho/projects/quip2md`, wiring T3+T4+T5 together. Deliverables:
> 1. `argparse` CLI: `quip2md export [--output DIR (default ./export)]
>    [--dryrun] [--verbose] [--force] [--include-chats] [--only THREAD_ID ...]`.
>    Script-header docstring documenting every switch (AGENTS.md requirement).
> 2. Export loop: for each `ThreadWork` the manifest approves — fetch HTML,
>    convert, download blobs via an asset_resolver backed by `client.blob()`
>    (skip blob downloads already on disk), write
>    `export/<folder_path>/<title>.md` and assets; spreadsheets also get
>    `.xlsx` backup when T5 flags it or conversion fails; CHAT threads only with
>    `--include-chats`; unknown types → PDF fallback with a warning.
> 3. `--dryrun`: full walk, prints the would-be file tree and per-type counts,
>    zero writes and zero HTML/blob fetches (folder listing only).
> 4. Failure isolation: one thread failing (after client-level retries) is logged
>    with id+title+error, recorded in the report, and the run continues. Ctrl-C
>    flushes the manifest and prints resume instructions.
> 5. End-of-run report: exported / skipped-unchanged / failed / chats-skipped
>    counts, elapsed time, and the failed-thread list; also written to
>    `.quip2md/last_run.json`.
> 6. Tests (`tests/test_export.py`): fake client + tmp_path — dryrun writes
>    nothing, failure isolation, xlsx-backup trigger, resume skips unchanged,
>    Ctrl-C manifest flush (simulate via exception injection).
>
> **Hard constraints:** silent by default, step-level progress under `--verbose`
> (AGENTS.md). No new dependencies. Don't modify T3/T4/T5 modules — if their
> interfaces don't fit, stop and report the mismatch instead of patching around it.
>
> **Acceptance:** full `uv run pytest` green; ruff+ty green;
> `uv run quip2md export --dryrun` against the live account prints a plausible
> tree (run once, manually).

---

### T7 — Test hardening  · **Sonnet 5**

> **Task:** In `/Users/jauderho/projects/quip2md`, raise test coverage on error
> paths and boundaries without changing product code. Focus: rate-limiter clock
> edge cases (reset in past, remaining=0, header missing entirely); pagination
> cursor loops (empty page, repeated cursor → must terminate with error);
> manifest corruption (truncated JSON → clear error, not crash-loop);
> filesystem edges (path >255 bytes total, case-insensitive collision on macOS
> — `Foo` vs `foo`); converter fuzz: run `html_to_markdown` over 200 random
> slices/mutations of the fixtures asserting no exception and no text loss.
> If a test finds a real bug, write the failing test, then fix the bug in a
> separate commit, per AGENTS.md (repro first, then fix).
>
> **Hard constraints:** no weakened assertions, no suppressions, no product-code
> changes except genuine bug fixes with their repro test.
>
> **Acceptance:** `uv run pytest -v` green; any bugs found are listed explicitly
> in the report with their fix commits.

---

### T8 — Live E2E run + README  · **Sonnet 5** (orchestrator reviews output)

> **Task:** In `/Users/jauderho/projects/quip2md`:
> 1. Run `uv run quip2md export --dryrun --verbose`, sanity-check the tree.
> 2. Run a scoped real export (`--only` on ~5 diverse threads incl. one with
>    images, one spreadsheet). Open the resulting `.md` files and verify fidelity
>    by eye against the Quip originals; fix nothing — file issues for T5/T6 rework
>    if fidelity is off.
> 3. Then run the full export. Record duration, request counts, failures.
>    Re-run immediately after: verify the second run is near-instant (all
>    skipped-unchanged). Kill a run mid-flight once and verify resume works.
> 4. Rewrite `README.md` for this tool: what it is, `uv` setup, token setup
>    (link https://quip.com/dev/token, `.env` format), every CLI flag, rate-limit
>    expectations (time estimate math), resume behavior, troubleshooting
>    (401 → token expired; 429 → just wait, the tool handles it).
> 5. Report honestly: exact counts, every failed thread and why, any fidelity gaps.
>
> **Acceptance:** full export completes; re-run is incremental; README accurate;
> failures (if any) triaged into concrete follow-up items.

---

## 5. Risks & fallbacks

| Risk | Likelihood | Mitigation |
|---|---|---|
| v2 `/2/threads/{id}/html` not enabled for personal tokens | Medium | T1 detects; T3 falls back to v1 `/1/threads/?ids=` full-HTML. Both paths specified. |
| Rate limits stricter than 50/min·750/hr, or headers renamed | Low | Client derives pacing from **observed headers**, not constants; constants are only the bootstrap. |
| Quip HTML structures not in fixtures (weird embeds, live apps) | High | T5's no-text-loss contract + warning counter; T8 eyeball pass; iterate on T5 with new fixtures. |
| Token expiry mid-export (personal tokens can expire) | Medium | Clear 401 error message telling user to mint a new token; manifest makes restart cheap. |
| Very large account → multi-hour run interrupted | Medium | Manifest resume (T4) is a hard requirement, tested in T7/T8. |
| Quip API shutdown before March 2027 cutoff | Low | Ship this soon; the tool is disposable after one clean export. |
| Perplexity research links (in original request) are login-walled | Confirmed pattern | Ignored by design: T1 gets ground truth from the live API, which beats any secondhand research. |

## 6. Definition of done

- [ ] `uv run quip2md export` completes a full export of the personal account with zero unexplained failures.
- [ ] Output tree mirrors Quip folder structure; docs are readable Markdown with working local image links; frontmatter present.
- [ ] Immediate re-run is incremental (skips unchanged).
- [ ] Rate limits never hard-tripped in normal operation (no unhandled 429s in logs).
- [ ] `pytest`, `ruff`, `ty` all green; no suppressions.
- [ ] README lets a stranger run this from scratch.
- [ ] All commits signed (`git commit -S -s`), one logical change each.

---

## 7. Phase 2 — Apple Notes import (T9–T12)

**Mission:** take the exported Markdown in `export/` and import it into Apple
Notes, mirroring the folder tree, so the user cleanly transitions Quip → Apple
Notes with `export/` remaining the durable backup. Gated behind a new
`quip2md import-notes` subcommand (the `export` command is untouched).

### User decisions (asked and answered 2026-07-12 — do not re-litigate)
| Decision | Choice |
|---|---|
| Images | Embed in the note if AppleScript attachment creation actually works (T9 verifies); otherwise a clickable `file://` link to the `export/_assets/` backup copy. |
| Target account | Notes' default account (usually iCloud); `--local` flag targets "On My Mac" instead. |
| Re-run behavior | Update-changed: `.quip2md/notes_state.json` maps thread_id → note id + source hash; re-runs replace changed notes, skip unchanged, never duplicate. |

### Orchestrator decisions
| Decision | Choice | Rationale |
|---|---|---|
| Gate | New subcommand `import-notes` with `--source DIR` (default ./export), `--local`, `--dryrun`, `--verbose`, `--force`, `--only THREAD_ID` | Decoupled from export; independently re-runnable; "flag-gated" per request. |
| Mechanism | AppleScript via `osascript` (subprocess, argv-passed scripts, never shell=True, note content passed as arguments not interpolated into script text) | Only supported automation surface for Notes; no third-party deps; SQLite manipulation is off the table. |
| Content path | export/*.md → parse frontmatter (quip_id = state key) → Markdown → HTML subset Notes accepts (T9 determines the subset) | The .md files are the source of truth per the user's framing. |
| MD→HTML | `markdown` package (tables extension) + post-processing to the Notes-safe subset | Well-maintained, small; hand-rolling MD parsing is a bug farm. |
| Folder layout | Everything under one top-level "Quip" folder mirroring the export tree (nesting depth per T9 findings) | Tidy, easy to delete/redo. |
| Non-macOS / Notes unavailable | Clear error, exit 2 | |
| Scale | ~492 notes; per-note osascript round-trips accepted (est. 5–15 min); batch multiple notes per osascript call only if T9 shows it's reliable | Correctness over speed; one-shot migration. |

### Task assignments (same contract: orchestrator Fable, implementer ceiling Fable Low)
| Task | Title | Implementer | Why |
|---|---|---|---|
| T9 | Apple Notes AppleScript recon | **Fable Low** | Exploratory; Notes automation folklore is unreliable; everything downstream depends on observed facts. |
| T10 | Importer module (md→Notes HTML, osascript bridge, notes state) | **Fable Low** | Fidelity + subprocess/quoting safety + idempotency logic. |
| T11 | CLI subcommand + tests | **Sonnet 5** | Well-specified glue once T10's API exists. |
| T12 | Live E2E import + README update | **Sonnet 5** (execution), orchestrator reviews | Judgment stays with orchestrator. |

Ordering: T9 → T10 → T11 → T12. Review gate after each, as in Phase 1.

### T9 must answer (write docs/NOTES_API_NOTES.md, all observed on this machine)
1. Default account name/id; "On My Mac" account existence (may require enabling — report, don't enable).
2. Folder creation at top level and NESTED (folder-in-folder) via AppleScript; max practical depth; duplicate-name behavior.
3. Note creation with HTML body: which elements survive (h1/h2/h3, b/i/u/strike, ul/ol/li nested, checklists?, tables, pre/code, blockquote, hr, a href, file:// links)? What gets stripped/mangled?
4. Attachment/image embedding: can `make new attachment` (or body <img>) actually embed a local image file? (This decides embed-vs-link.)
5. Note update in place: `set body of note X`; note id stability across edits/restarts.
6. Deletion semantics (→ Recently Deleted), and reliable lookup: note by id, notes by folder.
7. Timing: seconds per note create for a realistic ~5 KB HTML body; whether batching N notes per osascript call works reliably.
8. TCC/automation permission: what prompt appears, one-time or per-run.
Probes operate ONLY inside a throwaway folder named "quip2md-recon" (created by the probe, deleted at the end); never touch existing user notes/folders. Budget: ≤40 osascript invocations.

### Definition of done (Phase 2)
- [ ] `uv run quip2md import-notes` imports all exported docs into Notes under "Quip", tree mirrored, zero unexplained failures.
- [ ] Re-run after nothing changed: all skipped, no duplicates. After a source change: that note updated in place.
- [ ] `--local`, `--dryrun`, `--force`, `--only` behave as documented; non-macOS fails cleanly.
- [ ] Images embedded or linked per T9 findings; spreadsheets import as best-effort tables (xlsx backup remains in export/).
- [ ] Tests green (osascript fully faked in tests — no live Notes calls in CI); ruff/ty clean; README updated.
