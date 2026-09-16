"""Thread export orchestration: fetch, convert, and write files to disk.

`run_export()` walks the account's folder tree (via `quip2md.walker.walk`),
fetches thread metadata + HTML in batches (via `QuipClient.threads_batch()`),
converts each DOCUMENT/SPREADSHEET (and, if `--include-chats`, CHAT) thread
to Markdown (via `quip2md.convert.html_to_markdown`), and writes the result
to disk under `Config.output_dir`, mirroring the discovered folder path.

Design notes:
  * Batch-fetch failure isolation: `threads_batch()` is called once per
    `THREAD_BATCH_SIZE`-sized chunk from *this* module (not once for the
    whole work list) so that one chunk's request failure only fails the
    threads in that chunk -- the run continues with the next chunk. Each
    chunk is sized at exactly `THREAD_BATCH_SIZE`, so `QuipClient
    .threads_batch()`'s own internal chunking never splits a chunk further;
    the two chunking layers use the same constant deliberately.
  * Filename collisions are resolved by `_NameAllocator`: within a single
    run, names claimed by an earlier thread in the same directory are
    tracked in memory; across runs, a name already present on disk is only
    considered "free" (safe to overwrite without a suffix) when the
    manifest -- reloaded directly from `Config.state_path` here, independent
    of `Manifest`'s own in-memory state -- maps that exact relative path to
    this same thread id, OR the file is at this thread's own deterministic
    base (unsuffixed) name with no manifest entry at all, in which case it is
    treated as this thread's crash-orphan and overwritten (rather than
    duplicated to a " (2)" name). A file owned by a *different* thread id is
    taken and forces a suffix.
  * `.quip2md/last_run.json` is written with a plain `write_text()` (not the
    atomic tmp+replace pattern `Manifest` uses): it is a disposable report,
    not resume-critical state, so a torn write on crash is acceptable and
    the simpler code is preferred.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from quip2md.client import THREAD_BATCH_SIZE as _CLIENT_THREAD_BATCH_SIZE
from quip2md.client import QuipFolder, QuipUser, ThreadContent, ThreadType
from quip2md.config import Config
from quip2md.convert import AssetResolver, ConversionResult, build_frontmatter, html_to_markdown
from quip2md.walker import Manifest, ThreadWork, sanitize_component, walk

logger = logging.getLogger("quip2md.export")

# Re-exported under a local name for clarity at call sites in this module;
# kept equal to the client's own batching constant deliberately (see module
# docstring) rather than introducing a second, independently-tunable value.
THREAD_BATCH_SIZE = _CLIENT_THREAD_BATCH_SIZE

_EXT_BY_CONTENT_TYPE: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


class ExportClient(Protocol):
    """The subset of `QuipClient` the export loop needs.

    A structural `Protocol` (like `walker.FolderSource`, which this is a
    superset of) so tests can exercise `run_export()` against a small
    hand-written fake with no network and no real `QuipClient`.
    """

    def current_user(self) -> QuipUser: ...

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]: ...

    def threads_batch(self, ids: Sequence[str]) -> dict[str, ThreadContent]: ...

    def blob(self, thread_id: str, blob_id: str) -> tuple[bytes, str | None]: ...

    def export_xlsx(self, thread_id: str) -> bytes: ...


@dataclass(slots=True)
class ExportReport:
    """Counts and outcomes for one `run_export()` call."""

    exported: int = 0
    skipped_unchanged: int = 0
    skipped_chats: int = 0
    skipped_other: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    xlsx_backups: int = 0
    blobs_downloaded: int = 0
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "exported": self.exported,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_chats": self.skipped_chats,
            "skipped_other": self.skipped_other,
            "failed": [{"thread_id": tid, "reason": reason} for tid, reason in self.failed],
            "xlsx_backups": self.xlsx_backups,
            "blobs_downloaded": self.blobs_downloaded,
            "elapsed_seconds": self.elapsed_seconds,
        }


def run_export(
    client: ExportClient, config: Config, *, only: Sequence[str] | None = None
) -> ExportReport:
    """Walk, fetch, convert, and write the account's threads to disk.

    `only`, if given, restricts the export to threads with one of the given
    ids (applied after the walk, since thread ids -- but not titles/types --
    are known from `ThreadWork` alone).

    In `config.dry_run` mode: performs the walk only (folder-listing
    requests, no thread-html/blob fetches, no writes), prints the would-be
    folder tree, and returns immediately -- the manifest is never loaded or
    touched.

    On `KeyboardInterrupt`, the manifest is flushed and the partial report
    is written before the interrupt is re-raised to the caller.
    """
    start_time = time.monotonic()
    only_ids = frozenset(only) if only is not None else None

    work_items = list(walk(client, config))
    if only_ids is not None:
        work_items = [item for item in work_items if item.thread_id in only_ids]

    if config.dry_run:
        _print_dry_run_tree(work_items)
        report = ExportReport(elapsed_seconds=time.monotonic() - start_time)
        return report

    report = ExportReport()
    manifest = Manifest(config)
    manifest.load()
    allocator = _NameAllocator(config.output_dir, _load_path_owners(config.state_path))

    try:
        for chunk_start in range(0, len(work_items), THREAD_BATCH_SIZE):
            chunk = work_items[chunk_start : chunk_start + THREAD_BATCH_SIZE]
            _process_chunk(client, config, manifest, allocator, chunk, report)
            # Flush after every batch (not just every `DEFAULT_FLUSH_EVERY`
            # records, and not just on the final `finally` below): this
            # shrinks the crash window in which `.md` files exist on disk
            # with no manifest entry (a hard kill -- SIGKILL/OOM/power loss,
            # not a graceful KeyboardInterrupt, which the `finally` already
            # covers) to at most one batch. Cheap (~0.3ms/dump), so paid on
            # every batch rather than tuning `flush_every` down instead.
            manifest.flush()
    finally:
        manifest.flush()
        report.elapsed_seconds = time.monotonic() - start_time
        _write_report_json(config, report)

    return report


def _process_chunk(
    client: ExportClient,
    config: Config,
    manifest: Manifest,
    allocator: _NameAllocator,
    chunk: Sequence[ThreadWork],
    report: ExportReport,
) -> None:
    chunk_ids = [item.thread_id for item in chunk]
    try:
        contents = client.threads_batch(chunk_ids)
    except Exception as exc:  # broad by design: batch-fetch isolation is the contract
        reason = f"batch fetch failed: {_error_reason(exc)}"
        for item in chunk:
            logger.error("thread %s failed: %s", item.thread_id, reason)
            report.failed.append((item.thread_id, reason))
        return

    for item in chunk:
        content = contents.get(item.thread_id)
        if content is None:
            reason = "missing from threads_batch response"
            logger.error("thread %s failed: %s", item.thread_id, reason)
            report.failed.append((item.thread_id, reason))
            continue
        try:
            _export_one(client, config, manifest, allocator, item, content, report)
        except Exception as exc:  # broad by design: per-thread isolation is the contract
            reason = _error_reason(exc)
            logger.error("thread %s failed: %s", item.thread_id, reason)
            report.failed.append((item.thread_id, reason))


def _export_one(
    client: ExportClient,
    config: Config,
    manifest: Manifest,
    allocator: _NameAllocator,
    item: ThreadWork,
    content: ThreadContent,
    report: ExportReport,
) -> None:
    thread_type = content.thread_type
    if thread_type is ThreadType.CHAT:
        if not config.include_chats:
            report.skipped_chats += 1
            logger.debug("skipping chat thread %s (--include-chats not set)", item.thread_id)
            return
    elif thread_type not in (ThreadType.DOCUMENT, ThreadType.SPREADSHEET):
        report.skipped_other += 1
        logger.warning("skipping thread %s: unsupported type %s", item.thread_id, thread_type.value)
        return

    updated_usec = content.updated_usec if content.updated_usec is not None else 0
    if not manifest.should_export(item.thread_id, updated_usec):
        report.skipped_unchanged += 1
        logger.debug("skipping unchanged thread %s", item.thread_id)
        return

    old_relative = manifest.path_for(item.thread_id)

    dir_path = config.output_dir.joinpath(*item.folder_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    stem = sanitize_component(content.title or item.thread_id)
    filename = allocator.allocate(dir_path, stem, item.thread_id)
    md_path = dir_path / filename

    asset_resolver = _make_asset_resolver(client, dir_path, report)
    result = html_to_markdown(content.html, asset_resolver)

    exported_at = datetime.now(UTC)
    frontmatter = build_frontmatter(
        quip_id=content.id,
        quip_url=content.link or "",
        title=content.title,
        created_usec=content.created_usec if content.created_usec is not None else 0,
        updated_usec=updated_usec,
        exported=exported_at,
    )
    md_path.write_text(frontmatter + "\n" + result.markdown, encoding="utf-8")

    if thread_type is ThreadType.SPREADSHEET and _needs_xlsx_backup(result):
        _write_xlsx_backup(client, item.thread_id, md_path, report)

    relative_path = _relative_posix(md_path, config.output_dir)
    # A Quip thread whose title (or folder) changed between two exports
    # sanitizes to a different basename, so `_NameAllocator` allocates a new
    # file rather than overwriting the old one. `Manifest.record` below then
    # re-points the single per-`thread_id` entry at the new path, but without
    # this cleanup the previous `.md` would stay on disk carrying the same
    # `quip_id` frontmatter -- an orphan `scan_source` returns as a *second*
    # `NoteSource`, which (with `NotesState` keyed one-entry-per-`quip_id`)
    # makes every default `import-notes` re-run re-import one note forever.
    # Removing it only after the new write succeeds preserves the existing
    # same-basename crash-orphan recovery (same path -> no unlink), and
    # `os.path.samefile` guards the case-insensitive-filesystem case where a
    # case-only title change makes `old_relative` and `relative_path` differ
    # as strings but resolve to the same on-disk file.
    if old_relative is not None and old_relative != relative_path:
        old_abs = config.output_dir / Path(old_relative)
        if old_abs.is_file() and not _same_file(old_abs, md_path):
            old_abs.unlink()
    manifest.record(item.thread_id, relative_path, updated_usec, _iso8601_utc(exported_at))
    report.exported += 1


def _needs_xlsx_backup(result: ConversionResult) -> bool:
    return result.wide_table or bool(result.warnings)


def _write_xlsx_backup(
    client: ExportClient, thread_id: str, md_path: Path, report: ExportReport
) -> None:
    try:
        xlsx_bytes = client.export_xlsx(thread_id)
    except Exception as exc:  # broad by design: xlsx backup failure is a warning, not a failure
        logger.warning("xlsx backup failed for thread %s: %s", thread_id, _error_reason(exc))
        return
    md_path.with_suffix(".xlsx").write_bytes(xlsx_bytes)
    report.xlsx_backups += 1


# --- filename collision resolution ----------------------------------------


class _NameAllocator:
    """Resolves `.md` filename collisions within a folder deterministically.

    See the module docstring for the exact rule distinguishing "this
    thread's own file, safe to overwrite" from "taken by something else".
    """

    def __init__(self, output_dir: Path, path_owners: Mapping[str, str]) -> None:
        self._output_dir = output_dir
        self._path_owners = path_owners
        self._claimed: dict[Path, dict[str, str]] = {}

    def allocate(self, directory: Path, stem: str, thread_id: str) -> str:
        claimed = self._claimed.setdefault(directory, {})
        candidate_stem = stem
        suffix_index = 1
        while True:
            filename = f"{candidate_stem}.md"
            lower_name = filename.lower()
            is_base = candidate_stem == stem
            owner = claimed.get(lower_name)
            if owner is None:
                owner = self._disk_owner(directory, filename)
            # owner is None -> free; owner == thread_id -> our own prior file.
            # owner == "" on the BASE (unsuffixed) candidate -> a file exists on
            # disk with no manifest entry: a crash-orphan from a prior run whose
            # write completed but whose manifest record did not flush. The base
            # filename is deterministic per (folder, title), so it is this
            # thread's own orphan -- overwrite it rather than writing a " (2)"
            # duplicate and leaking the orphan forever. (A same-run collision
            # between two distinct threads is caught earlier via `claimed`, so
            # `owner` there is the other thread's id, not "".)
            if owner is None or owner == thread_id or (owner == "" and is_base):
                claimed[lower_name] = thread_id
                return filename
            suffix_index += 1
            candidate_stem = f"{stem} ({suffix_index})"

    def _disk_owner(self, directory: Path, filename: str) -> str | None:
        candidate = directory / filename
        if not candidate.exists():
            return None
        relative_lower = _relative_posix(candidate, self._output_dir).lower()
        # "" (never a real thread id) marks "exists on disk, owner unknown"
        # so it is always treated as taken by `allocate()`'s `owner ==
        # thread_id` check.
        return self._path_owners.get(relative_lower, "")


def _load_path_owners(state_path: Path) -> dict[str, str]:
    """Reverse-index the manifest file directly: relative path -> thread id.

    Reads `state_path` independently of `Manifest` (which exposes no public
    accessor for its loaded entries) purely to answer "who, if anyone,
    already owns this filename". Tolerant of a missing or unreadable file
    (empty index): by the time this is called, `Manifest.load()` has
    already validated the same file and raised `ManifestError` on real
    corruption, so a best-effort re-read here is safe.
    """
    if not state_path.is_file():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    owners: dict[str, str] = {}
    for thread_id, value in raw.items():
        if isinstance(value, dict):
            path_value = value.get("path")
            if isinstance(path_value, str):
                owners[path_value.lower()] = thread_id
    return owners


# --- asset (blob) resolution ------------------------------------------------


def _make_asset_resolver(client: ExportClient, md_dir: Path, report: ExportReport) -> AssetResolver:
    def resolve(thread_id: str, blob_id: str, suggested_ext: str | None) -> str:
        # `suggested_ext` (a best-effort extension parsed from the <img> src
        # URL by convert.py) is intentionally unused: the extension written
        # to disk is derived from the blob's actual Content-Type instead,
        # per the resolver's content-type-driven extension contract.
        del suggested_ext
        assets_dir = md_dir / "_assets" / thread_id
        existing = _find_existing_blob(assets_dir, blob_id)
        if existing is not None:
            return _relative_posix(existing, md_dir)

        content, content_type = client.blob(thread_id, blob_id)
        assets_dir.mkdir(parents=True, exist_ok=True)
        target = assets_dir / f"{blob_id}{_extension_for(content_type)}"
        target.write_bytes(content)
        report.blobs_downloaded += 1
        return _relative_posix(target, md_dir)

    return resolve


def _find_existing_blob(assets_dir: Path, blob_id: str) -> Path | None:
    if not assets_dir.is_dir():
        return None
    exact = assets_dir / blob_id
    if exact.is_file():
        return exact
    matches = sorted(assets_dir.glob(f"{blob_id}.*"))
    return matches[0] if matches else None


def _extension_for(content_type: str | None) -> str:
    if content_type is None:
        return ""
    base_type = content_type.split(";", 1)[0].strip().lower()
    return _EXT_BY_CONTENT_TYPE.get(base_type, "")


# --- dry-run tree printing --------------------------------------------------


def _print_dry_run_tree(work_items: Sequence[ThreadWork]) -> None:
    counts: dict[tuple[str, ...], int] = {}
    for item in work_items:
        counts[item.folder_path] = counts.get(item.folder_path, 0) + 1

    print("Dry run: folder tree (folder listing only -- no thread/blob fetches, no writes)")
    for folder_path in sorted(counts):
        depth = max(len(folder_path) - 1, 0)
        label = folder_path[-1] if folder_path else "(root)"
        print(f"{'  ' * depth}{label}/  [{counts[folder_path]} thread(s)]")
    print(f"\nTotal threads discovered: {len(work_items)}")
    print(
        "Per-type counts: unknown until fetch "
        "(dry-run performs no threads_batch() calls, so document/spreadsheet/"
        "chat/other breakdown is unavailable)."
    )


# --- report persistence -----------------------------------------------------


def _write_report_json(config: Config, report: ExportReport) -> None:
    report_path = config.state_path.parent / "last_run.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True), encoding="utf-8")


# --- small shared helpers ---------------------------------------------------


def _relative_posix(path: Path, base: Path) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


def _same_file(a: Path, b: Path) -> bool:
    """True if `a` and `b` resolve to the same on-disk file (inode/device).

    Used to keep a case-only title change on a case-insensitive filesystem
    (where the old and new basenames are the same file) from deleting the
    file that was just written: deleting it would drop the only on-disk copy
    while a manifest entry for a now-missing file would be recorded.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        # Missing path or a filesystem without stat (extremely rare) -- be
        # non-destructive: treat them as the same file so the unlink is
        # skipped rather than risking the just-written `.md`.
        return False


def _iso8601_utc(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
