"""Folder walker and resume/manifest state handling.

Walks a Quip account's folder tree (private, desktop, shared, group, archive,
starred -- never trash) via batched, level-by-level `folders()` calls and
yields one `ThreadWork` per distinct thread discovered, deduplicated so a
thread reachable from multiple folders (e.g. a "real" location and a starred
pointer to it) is only exported once, from the highest-priority path.

Also provides `Manifest`, the on-disk resume/skip-unchanged state, and
`sanitize_component`, the filesystem-safe title sanitizer shared with the
exporter.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from quip2md.client import FolderChildKind, QuipFolder, QuipUser
from quip2md.config import Config

logger = logging.getLogger("quip2md.walker")

# --- sanitize_component ----------------------------------------------------

_INVALID_CHARS_RE = re.compile(r'[/\\:*?"<>|]')
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_TRIM_CHARS = " ."
MAX_COMPONENT_LENGTH = 120
# Byte cap as well as the codepoint cap: a title dense with CJK/emoji (3-4
# UTF-8 bytes each) can be 120 codepoints yet ~480 bytes, exceeding the
# ~255-byte filename limit on APFS/ext4/most filesystems -- which would make
# every write for that thread fail on every run. 200 leaves headroom for a
# collision suffix and the ``.md`` extension.
MAX_COMPONENT_BYTES = 200
_FALLBACK_COMPONENT = "untitled"


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    """Truncate `text` so its UTF-8 encoding is <= `max_bytes`, never splitting
    a codepoint."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Decode the longest valid UTF-8 prefix within the byte budget.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_component(name: str) -> str:
    """Sanitize a single filesystem path component (a folder or thread title).

    Strips characters illegal on common filesystems (``/\\:*?"<>|``) and
    control characters, collapses internal whitespace, strips leading/
    trailing dots and spaces (Windows rejects trailing dots/spaces), and
    trims to `MAX_COMPONENT_LENGTH` codepoints *and* `MAX_COMPONENT_BYTES`
    UTF-8 bytes (the byte cap matters for CJK/emoji titles). Never returns an
    empty string -- if sanitization removes everything, falls back to a fixed
    placeholder. Callers whose title may itself be empty should pass the
    folder/thread id instead (e.g. ``sanitize_component(title or thread_id)``)
    so the fallback is stable and traceable rather than a generic label.
    """
    without_illegal = _INVALID_CHARS_RE.sub("", name)
    without_control = _CONTROL_CHARS_RE.sub("", without_illegal)
    collapsed = _WHITESPACE_RE.sub(" ", without_control).strip()
    trimmed = collapsed.strip(_TRIM_CHARS)[:MAX_COMPONENT_LENGTH]
    trimmed = _truncate_to_bytes(trimmed, MAX_COMPONENT_BYTES).strip(_TRIM_CHARS)
    return trimmed or _FALLBACK_COMPONENT


# --- ThreadWork --------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ThreadWork:
    """A thread discovered during the walk, with its containing folder path.

    Title/type/updated_usec are deliberately not fetched here: fetching each
    thread's metadata individually during the walk would double the request
    budget. The exporter's HTML fetch phase supplies that metadata.
    """

    thread_id: str
    folder_path: tuple[str, ...]


# --- walk --------------------------------------------------------------


class FolderSource(Protocol):
    """The subset of `QuipClient` the walker needs.

    A `Protocol` (rather than depending on the concrete `QuipClient`) lets
    tests exercise `walk()` against a small hand-written fake with no
    network and no real `QuipClient` instance, while `QuipClient` itself
    still satisfies this protocol structurally -- production callers pass a
    real client with no adapter needed.
    """

    def current_user(self) -> QuipUser: ...

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]: ...


def _root_specs(user: QuipUser) -> list[tuple[str, str, bool]]:
    """Root ``(label, folder_id, include_root_title)`` triples, dedup-priority order.

    Priority: private, desktop, shared, group, archive, starred -- so a
    thread's "real" location wins over an archive or starred pointer to it.
    `trash_folder_id` is deliberately never included.

    `include_root_title` distinguishes the singleton virtual folders
    (private/desktop/archive/starred -- one per account, whose own Quip
    title is a generic label, not meaningful) from shared/group folders
    (each a distinct, actually-named folder, potentially several per
    account) whose own title *is* meaningful and belongs in the path.
    """
    specs: list[tuple[str, str, bool]] = []
    if user.private_folder_id:
        specs.append(("Private", user.private_folder_id, False))
    if user.desktop_folder_id:
        specs.append(("Desktop", user.desktop_folder_id, False))
    specs.extend(("Shared", folder_id, True) for folder_id in user.shared_folder_ids)
    specs.extend(("Group", folder_id, True) for folder_id in user.group_folder_ids)
    if user.archive_folder_id:
        specs.append(("Archive", user.archive_folder_id, False))
    if user.starred_folder_id:
        specs.append(("Starred", user.starred_folder_id, False))
    return specs


def walk(client: FolderSource, config: Config) -> Iterator[ThreadWork]:
    """BFS the account's folder tree, yielding one `ThreadWork` per thread.

    `config` is accepted for interface symmetry with the rest of the
    pipeline; the walk itself needs no config value -- title/type filtering
    (e.g. `--include-chats`) happens later, once the exporter has fetched
    thread metadata.
    """
    _ = config
    user = client.current_user()
    visited_folders: set[str] = set()
    seen_threads: set[str] = set()
    for root_label, root_id, include_root_title in _root_specs(user):
        yield from _walk_root(
            client, root_label, root_id, include_root_title, visited_folders, seen_threads
        )


def _walk_root(
    client: FolderSource,
    root_label: str,
    root_id: str,
    include_root_title: bool,
    visited_folders: set[str],
    seen_threads: set[str],
) -> Iterator[ThreadWork]:
    if root_id in visited_folders:
        return
    visited_folders.add(root_id)
    # folder_id -> path of its *parent*. The root level's own path is
    # computed specially below: virtual singleton roots ("Private",
    # "Desktop", ...) use just (root_label,) since their own Quip title is
    # generic, while shared/group roots are real, distinctly-named folders
    # whose title belongs in the path.
    frontier: dict[str, tuple[str, ...]] = {root_id: ()}
    at_root_level = True
    while frontier:
        fetched = client.folders(list(frontier.keys()))
        next_frontier: dict[str, tuple[str, ...]] = {}
        for folder_id, parent_path in frontier.items():
            folder = fetched.get(folder_id)
            if folder is None:
                logger.debug("folder %s missing from folders() response; skipping", folder_id)
                continue
            if at_root_level:
                own_path = (
                    (root_label, sanitize_component(folder.title or folder_id))
                    if include_root_title
                    else (root_label,)
                )
            else:
                own_path = (*parent_path, sanitize_component(folder.title or folder_id))
            for child in folder.children:
                if child.kind is FolderChildKind.THREAD:
                    if child.id in seen_threads:
                        logger.debug(
                            "duplicate thread sighting: id=%s at path=%s (already recorded)",
                            child.id,
                            own_path,
                        )
                        continue
                    seen_threads.add(child.id)
                    yield ThreadWork(thread_id=child.id, folder_path=own_path)
                elif child.id in visited_folders:
                    logger.debug("duplicate/cyclic folder sighting: id=%s", child.id)
                else:
                    visited_folders.add(child.id)
                    next_frontier[child.id] = own_path
        frontier = next_frontier
        at_root_level = False


# --- Manifest ------------------------------------------------------------


class ManifestError(RuntimeError):
    """Raised when the manifest state file exists but cannot be parsed.

    The state file is the user's export record: a corrupted file is never
    silently reset -- the caller must resolve or delete it deliberately.
    """


@dataclass(slots=True, frozen=True)
class ManifestEntry:
    path: str
    updated_usec: int
    exported_at: str


DEFAULT_FLUSH_EVERY = 20


class Manifest:
    """Resume/skip-unchanged state, persisted atomically to `config.state_path`."""

    def __init__(self, config: Config, *, flush_every: int = DEFAULT_FLUSH_EVERY) -> None:
        self._path = config.state_path
        self._force = config.force
        self._flush_every = flush_every
        self._entries: dict[str, ManifestEntry] = {}
        self._unflushed_count = 0

    def load(self) -> None:
        """Load state from disk. Tolerant of a missing file (empty state then)."""
        if not self._path.is_file():
            self._entries = {}
            self._unflushed_count = 0
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ManifestError(f"Could not read manifest at {self._path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Manifest at {self._path} is corrupted: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError(
                f"Manifest at {self._path} has an unexpected shape (expected a JSON object)"
            )
        entries: dict[str, ManifestEntry] = {}
        for thread_id, value in raw.items():
            entries[thread_id] = _parse_manifest_entry(self._path, thread_id, value)
        self._entries = entries
        self._unflushed_count = 0

    def should_export(self, thread_id: str, updated_usec: int) -> bool:
        """False only when the stored `updated_usec` matches and `--force` is off."""
        if self._force:
            return True
        entry = self._entries.get(thread_id)
        if entry is None:
            return True
        return entry.updated_usec != updated_usec

    def path_for(self, thread_id: str) -> str | None:
        """The relative path currently recorded for `thread_id`, or `None`.

        The single per-`thread_id` entry is what the exporter overwrites on a
        re-export, so this is the authoritative "where this thread's `.md`
        lived before this run" lookup -- used by `_export_one` to remove a
        prior, differently-named file left behind when a Quip thread's title
        (and thus its sanitized filename) changed between two exports.
        """
        entry = self._entries.get(thread_id)
        return entry.path if entry is not None else None

    def record(self, thread_id: str, path: str, updated_usec: int, exported_at: str) -> None:
        """Record a successful export in memory; auto-flushes every N records."""
        self._entries[thread_id] = ManifestEntry(
            path=path, updated_usec=updated_usec, exported_at=exported_at
        )
        self._unflushed_count += 1
        if self._unflushed_count >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        """Persist current state atomically: write a temp file, then `os.replace`."""
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            thread_id: {
                "path": entry.path,
                "updated_usec": entry.updated_usec,
                "exported_at": entry.exported_at,
            }
            for thread_id, entry in self._entries.items()
        }
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{self._path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
            raise
        self._unflushed_count = 0


def _parse_manifest_entry(path: Path, thread_id: str, value: object) -> ManifestEntry:
    if not isinstance(value, dict):
        raise ManifestError(f"Manifest at {path} has a malformed entry for {thread_id!r}")
    entry_path = value.get("path")
    updated_usec = value.get("updated_usec")
    exported_at = value.get("exported_at")
    if (
        not isinstance(entry_path, str)
        or not isinstance(updated_usec, int)
        or not isinstance(exported_at, str)
    ):
        raise ManifestError(f"Manifest at {path} has a malformed entry for {thread_id!r}")
    return ManifestEntry(path=entry_path, updated_usec=updated_usec, exported_at=exported_at)
