"""Reproduction and regression coverage for the rename-orphan churn bug.

A Quip thread whose title (and therefore sanitized filename) changed
between two `quip2md export` runs used to leave the old-titled `.md` on disk
while the manifest re-pointed at the new one. `scan_source` then returned two
`NoteSource`s sharing one `quip_id`; since `NotesState` keys exactly one
entry per `quip_id`, at most one source can match state per run, so every
default `import-notes` re-run re-imported one note and superseded the prior
-- forever, alternating which file was "unchanged". This violated the
README's re-run guarantee ("Skips notes whose source hasn't changed since
the last run -- no duplicates are created. ... With the `enex` writer, a
run in which everything is unchanged writes no archive and never opens
Notes at all.").

The fix has two halves, both exercised here:

* **Export side** -- `_export_one` captures the thread's previously-recorded
  path and removes the old file once the new one is written, so a rename
  re-export no longer orphans the old `.md` (Part 1; stops *new* orphans).
* **Import side** -- `scan_source` collapses same-`quip_id` sources down to
  a single canonical one, so an orphan left by a pre-fix exporter stops
  churning the one `NotesState` slot for that id (Part 2; the migration for
  *existing* orphan trees the export fix can no longer touch).

No test reaches the real Notes app: the export tests drive a hand-written
fake `QuipClient`, and the import tests drive a *faithful* fake enex runner
whose landing notes are derived from the actual `.enex` the run wrote (one
`ImportedNote` per `<note>`, each carrying that note's real provenance body
so `_extract_quip_url` matches it back to its source) -- exactly what Notes
itself creates. `tests/conftest.py`'s guard stays armed throughout.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quip2md import notes_enex
from quip2md.client import (
    FolderChild,
    FolderChildKind,
    QuipFolder,
    QuipUser,
    ThreadContent,
    ThreadType,
)
from quip2md.config import Config
from quip2md.convert import build_frontmatter
from quip2md.export import run_export
from quip2md.notes_enex import EnexImportReport, ImportedNote, run_enex_import
from quip2md.notes_import import scan_source
from quip2md.notes_prune import FolderInfo, prune_notes

# --- shared config / frontmatter helpers --------------------------------------


def _config(
    tmp_path: Path,
    *,
    output_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> Config:
    return Config(
        token="",
        output_dir=output_dir if output_dir is not None else tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=dry_run,
        verbose=False,
        include_chats=False,
        force=force,
    )


def _frontmatter(
    *,
    quip_id: str,
    url: str,
    title: str,
    updated_usec: int = 100,
    exported_at: datetime,
) -> str:
    return build_frontmatter(
        quip_id=quip_id,
        quip_url=url,
        title=title,
        created_usec=0,
        updated_usec=updated_usec,
        exported=exported_at,
    )


# === Export-side fake client (no network) =====================================


@dataclass
class _FakeExportClient:
    user: QuipUser
    folders_by_id: dict[str, QuipFolder]
    contents: dict[str, ThreadContent]

    def current_user(self) -> QuipUser:
        return self.user

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]:
        return {fid: self.folders_by_id[fid] for fid in ids if fid in self.folders_by_id}

    def threads_batch(self, ids: Sequence[str]) -> dict[str, ThreadContent]:
        return {tid: self.contents[tid] for tid in ids if tid in self.contents}

    def blob(self, thread_id: str, blob_id: str) -> tuple[bytes, str | None]:
        return (b"", None)

    def export_xlsx(self, thread_id: str) -> bytes:
        return b""


def _simple_client(contents: dict[str, ThreadContent]) -> _FakeExportClient:
    user = QuipUser(
        id="user1",
        name="Test User",
        private_folder_id="priv",
        desktop_folder_id=None,
        archive_folder_id=None,
        starred_folder_id=None,
        shared_folder_ids=(),
        group_folder_ids=(),
    )
    folders = {
        "priv": QuipFolder(
            id="priv",
            title="Private",
            children=tuple(FolderChild(kind=FolderChildKind.THREAD, id=tid) for tid in contents),
        )
    }
    return _FakeExportClient(user, folders, contents)


def _content(
    thread_id: str,
    title: str,
    *,
    updated_usec: int = 100,
    html: str = "<p>body</p>",
    link: str = "https://example.quip.com/doc1",
) -> ThreadContent:
    return ThreadContent(
        id=thread_id,
        title=title,
        thread_type=ThreadType.DOCUMENT,
        created_usec=1_000_000,
        updated_usec=updated_usec,
        link=link,
        html=html,
    )


def _run_export(tmp_path: Path, content: ThreadContent, *, force: bool = False) -> Config:
    config = _config(tmp_path, force=force)
    run_export(_simple_client({content.id: content}), config)
    return config


def _set_mtime(path: Path, seconds: float) -> None:
    ns = int(seconds * 1_000_000_000)
    os.utime(path, ns=(ns, ns))


# === Part 1: export side stops producing new orphans =========================


def test_export_force_after_rename_removes_the_old_file(tmp_path: Path) -> None:
    """`--force` re-export after a title rename deletes the prior `.md`.

    This is the certain orphan route (the bug report's demonstrated path):
    `updated_usec` held *unchanged*; nothing about whether Quip bumps it on a
    rename is assumed. Before the fix both `Foo.md` and `Bar.md` stayed on
    disk; now only `Bar.md` does, matching the manifest's single entry.
    """
    config = _run_export(tmp_path, _content("doc1", "Foo", updated_usec=100))
    private = config.output_dir / "Private"
    assert (private / "Foo.md").is_file()

    _run_export(tmp_path, _content("doc1", "Bar", updated_usec=100), force=True)

    remaining = sorted(p.name for p in private.glob("*.md"))
    assert remaining == ["Bar.md"]
    assert not (private / "Foo.md").exists()


def test_export_default_after_rename_removes_old_file_when_updated_usec_bumps(
    tmp_path: Path,
) -> None:
    """The non-forced path also cleans up when a rename makes `should_export`
    return True (Quip bumping `updated_usec` on a title edit -- the plausible
    but unverified default route). The fix is not `--force`-specific."""
    config = _run_export(tmp_path, _content("doc1", "Foo", updated_usec=100))
    private = config.output_dir / "Private"
    assert (private / "Foo.md").is_file()

    # Same thread, new title, new updated_usec -> should_export True without --force.
    _run_export(tmp_path, _content("doc1", "Bar", updated_usec=200))

    remaining = sorted(p.name for p in private.glob("*.md"))
    assert remaining == ["Bar.md"]
    assert not (private / "Foo.md").exists()


def test_export_same_basename_reexport_keeps_crash_orphan_recovery(
    tmp_path: Path,
) -> None:
    """A rename that sanitizes to the same stem (e.g. `My Doc?` -> `My Doc*`,
    both `?` and `*` are stripped to `My Doc`) re-writes the single file in
    place -- the fix must NOT unlink it. This preserves `_NameAllocator`'s
    crash-orphan overwrite path and is the regression guard for the
    `old_relative == relative_path` no-op branch (incl. the `samefile` guard
    that matters on case-insensitive filesystems)."""
    config = _run_export(tmp_path, _content("doc1", "My Doc?", updated_usec=100))
    md = config.output_dir / "Private" / "My Doc.md"
    assert md.is_file()
    assert 'title: "My Doc?"' in md.read_text(encoding="utf-8")

    # Re-export with a title that sanitizes to the SAME stem; --force so it is
    # not skipped as unchanged. The file is overwritten in place (new title
    # frontmatter), not unlinked-and-recreated, and no "My Doc (2).md" appears.
    _run_export(tmp_path, _content("doc1", "My Doc*", updated_usec=100), force=True)

    assert md.is_file()
    assert 'title: "My Doc*"' in md.read_text(encoding="utf-8")
    assert not (config.output_dir / "Private" / "My Doc (2).md").exists()
    assert sorted(p.name for p in (config.output_dir / "Private").glob("*.md")) == ["My Doc.md"]


def test_export_folder_move_removes_the_old_location_file(tmp_path: Path) -> None:
    """A thread moved to a different Quip folder changes its folder_path, so
    the new `.md` lands elsewhere and the old one was orphaned too. The fix
    keys off `thread_id`, not the basename, so it cleans up a folder-move
    orphan as well -- a strict improvement on the pre-fix behaviour."""
    thread_id = "doc1"
    user = QuipUser(
        id="user1",
        name="U",
        private_folder_id="priv",
        desktop_folder_id=None,
        archive_folder_id=None,
        starred_folder_id=None,
        shared_folder_ids=("shared",),
        group_folder_ids=(),
    )
    # Run 1: thread lives under Private.
    folders1 = {
        "priv": QuipFolder("priv", "Private", (FolderChild(FolderChildKind.THREAD, thread_id),)),
    }
    config = _config(tmp_path)
    run_export(
        _FakeExportClient(
            user, folders1, {thread_id: _content(thread_id, "Doc", updated_usec=100)}
        ),
        config,
    )
    old_md = config.output_dir / "Private" / "Doc.md"
    assert old_md.is_file()

    # Run 2: same thread now appears under a shared folder, --force re-export.
    folders2 = {
        "priv": QuipFolder("priv", "Private", ()),
        "shared": QuipFolder("shared", "Team", (FolderChild(FolderChildKind.THREAD, thread_id),)),
    }
    run_export(
        _FakeExportClient(
            user, folders2, {thread_id: _content(thread_id, "Doc", updated_usec=100)}
        ),
        _config(tmp_path, force=True),
    )

    # New path present, old path cleaned up -- no orphan at the old location.
    assert (config.output_dir / "Shared" / "Team" / "Doc.md").is_file()
    assert not old_md.exists()


def test_export_old_orphan_already_deleted_by_hand_is_tolerated(
    tmp_path: Path,
) -> None:
    """If the user already hand-deleted the stale `.md` (the report's noted
    escape hatch), the rename re-export must succeed: the unlink is gated on
    `old_abs.is_file()`, so a missing old file is a no-op, not an error."""
    config = _run_export(tmp_path, _content("doc1", "Foo", updated_usec=100))
    (config.output_dir / "Private" / "Foo.md").unlink()  # the user cleaned up by hand

    _run_export(tmp_path, _content("doc1", "Bar", updated_usec=100), force=True)

    assert sorted(p.name for p in (config.output_dir / "Private").glob("*.md")) == ["Bar.md"]


# === Part 2: scan_source collapses same-quip_id sources ======================


def _write_doc(
    root: Path,
    rel_path: str,
    *,
    quip_id: str,
    url: str,
    title: str,
    body: str = "body\n",
    updated_usec: int = 100,
    exported_sec: int = 0,
) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _frontmatter(
            quip_id=quip_id,
            url=url,
            title=title,
            updated_usec=updated_usec,
            exported_at=datetime(2026, 1, 1, 0, 0, exported_sec, tzinfo=UTC),
        )
        + "\n"
        + body,
        encoding="utf-8",
    )
    return path


def test_scan_source_collapses_same_quip_id_to_the_newest_export(tmp_path: Path) -> None:
    """Two `.md` files sharing a `quip_id` (a rename orphan) collapse to one
    `NoteSource`: the one whose `exported` frontmatter is newest (the latest
    export's file). `updated` (Quip's `updated_usec`) does not change on a
    pure title rename, so it cannot break the tie; `exported` always does."""
    root = tmp_path / "export"
    _write_doc(
        root,
        "Private/Foo.md",
        quip_id="doc1",
        url="https://x.quip.com/abc",
        title="Foo",
        exported_sec=1,
        body="# Foo\n",
    )
    _write_doc(
        root,
        "Private/Bar.md",
        quip_id="doc1",
        url="https://x.quip.com/abc",
        title="Bar",
        exported_sec=2,
        body="# Bar\n",
    )

    sources = scan_source(root)

    assert len(sources) == 1
    assert sources[0].key == "doc1"
    assert sources[0].title == "Bar"
    assert sources[0].relative_path == "Private/Bar.md"


def test_scan_source_ties_break_on_newest_mtime_then_smallest_path(
    tmp_path: Path,
) -> None:
    """Sub-second re-exports share the second-granularity `exported` stamp, so
    ties fall to the newest file `mtime` (the newer export is written second);
    any remaining tie is the smallest relative path, scan-order-independent.
    Both signals must be deterministic so the same file wins every run -- that
    stability is what stops the churn."""
    root = tmp_path / "export"
    aaa = _write_doc(
        root,
        "Private/Aaa.md",
        quip_id="doc1",
        url="https://x.quip.com/abc",
        title="Aaa",
        exported_sec=30,
        body="# Aaa\n",
    )
    zzz = _write_doc(
        root,
        "Private/Zzz.md",
        quip_id="doc1",
        url="https://x.quip.com/abc",
        title="Zzz",
        exported_sec=30,  # same exported second as Aaa -> `exported` ties
        body="# Zzz\n",
    )
    # `exported` ties (same second); the newer mtime wins (the second export is
    # written last). Zzz is newer here -> Zzz is canonical.
    _set_mtime(aaa, 100.0)
    _set_mtime(zzz, 200.0)
    assert [s.title for s in scan_source(root)] == ["Zzz"]

    # An exact mtime tie too falls to the smallest relative path, so the choice
    # never depends on scan order: "Private/Aaa.md" < "Private/Zzz.md" -> Aaa.
    _set_mtime(zzz, 100.0)
    assert [s.title for s in scan_source(root)] == ["Aaa"]


def test_scan_source_missing_exported_falls_back_to_mtime(tmp_path: Path) -> None:
    """A hand-authored file with no `exported` frontmatter still collapses by
    the remaining signal (newest mtime), so pre-existing loose pairs that predate
    the `exported` field are still migrated -- the collapse never raises."""
    root = tmp_path / "export"
    (root / "Private").mkdir(parents=True)
    (root / "Private" / "Foo.md").write_text(
        '---\nquip_id: "doc1"\nquip_url: "https://x.quip.com/abc"\n'
        'title: "Foo"\ncreated: "2020-01-01T00:00:00Z"\n'
        'updated: "2020-01-02T00:00:00Z"\n---\n\n# Foo\n',
        encoding="utf-8",
    )
    (root / "Private" / "Bar.md").write_text(
        '---\nquip_id: "doc1"\nquip_url: "https://x.quip.com/abc"\n'
        'title: "Bar"\ncreated: "2020-01-01T00:00:00Z"\n'
        'updated: "2020-01-02T00:00:00Z"\n---\n\n# Bar\n',
        encoding="utf-8",
    )
    _set_mtime(root / "Private" / "Foo.md", 100.0)
    _set_mtime(root / "Private" / "Bar.md", 200.0)

    sources = scan_source(root)

    assert len(sources) == 1
    assert sources[0].title == "Bar"


def test_scan_source_passes_through_unique_and_path_keyed_sources(
    tmp_path: Path,
) -> None:
    """Non-duplicate `quip_id` files and path-keyed files (no `quip_id`) are
    untouched: a normal one-file-per-doc export tree is byte-for-byte the
    same list, and a hand-authored doc without frontmatter keeps its own
    `path:` key (it can never collide with a real thread id)."""
    root = tmp_path / "export"
    _write_doc(root, "Private/A.md", quip_id="doc1", url="https://x.quip.com/a", title="A")
    _write_doc(root, "Private/B.md", quip_id="doc2", url="https://x.quip.com/b", title="B")
    (root / "Private").mkdir(parents=True, exist_ok=True)
    (root / "Private" / "Loose.md").write_text("no frontmatter\n", encoding="utf-8")

    sources = scan_source(root)

    rel_paths = [s.relative_path for s in sources]
    assert rel_paths == ["Private/A.md", "Private/B.md", "Private/Loose.md"]
    keys = {s.key for s in sources}
    assert "doc1" in keys and "doc2" in keys
    loose = next(s for s in sources if s.relative_path == "Private/Loose.md")
    assert loose.key == "path:Private/Loose.md"
    assert loose.keyed_by_path is True


def test_scan_source_does_not_collapse_different_quip_ids_sharing_a_url(
    tmp_path: Path,
) -> None:
    """Two documents with the SAME `quip_url` but DIFFERENT `quip_id`s are not
    collapsed -- the disambiguation of those is the enex writer's deliberate
    `by_url` collapse (`test_two_documents_sharing_a_quip_url_collapse...`),
    not scan_source's job. Collapsing here would be a regression of that
    pinned behaviour."""
    root = tmp_path / "export"
    _write_doc(
        root,
        "Alpha/A.md",
        quip_id="THREAD0013",
        url="https://quip.com/THREAD0013",
        title="A",
    )
    _write_doc(
        root,
        "Beta/B.md",
        quip_id="THREAD0014",
        url="https://quip.com/THREAD0013",
        title="B",
    )

    sources = scan_source(root)

    assert len(sources) == 2
    assert {s.key for s in sources} == {"THREAD0013", "THREAD0014"}


# === Faithful fake enex runner for end-to-end churn tests ====================
#
# The landing notes are derived from the actual `.enex` the run wrote: one
# `ImportedNote` per `<note>`, each carrying that note's real provenance body
# (the `Source:` line) so `_extract_quip_url` matches it back to its source.
# That models a Notes importer that creates one note per `<note>` (what Notes
# does). Note ids are drawn from a shared counter so the prune `live` check in
# the prune test resolves faithfully (real Notes assigns persistent unique ids).


_NOTE_RE = re.compile(r"<note>(.*?)</note>", re.DOTALL)
_PROV_RE = re.compile(r'Source: <a href="[^"]*">(https?://[^<]+)</a>')


@dataclass
class _FaithfulEnexRunner:
    """Fake `EnexNotesRunnerProtocol` deriving landing notes from the real enex.

    `id_counter` is shared across runner instances within a test so note ids
    stay globally unique and persistent across runs (real Notes ids never
    repeat) -- required for a faithful prune liveness check.
    """

    id_counter: itertools.count[int]
    landing_name: str = "Imported Notes 1"
    opened: list[Path] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    created_folders: list[tuple[str, ...]] = field(default_factory=list)
    _opened_yet: bool = False
    _polls: int = 0
    _cache: list[ImportedNote] = field(default_factory=list)

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def folder_names(self, account: str) -> frozenset[str]:
        if self._opened_yet:
            self._polls += 1
            if self._polls > 1:
                return frozenset({"Notes", self.landing_name})
        return frozenset({"Notes"})

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        self.created_folders.append(tuple(path))
        return "folder:" + "/".join(path)

    def folder_id_by_name(self, account: str, name: str) -> str:
        return "folder:" + name

    def move_note(self, note_id: str, folder_id: str) -> None:
        self.moved.append((note_id, folder_id))

    def open_enex(self, path: Path) -> None:
        self.opened.append(path)
        self._opened_yet = True
        self._cache = []
        self._polls = 0

    def notes_in_folder(self, folder_id: str) -> list[ImportedNote]:
        # Parse once on the first read after an open, then return the same
        # cached notes on every subsequent poll -- Notes' ids are stable.
        if not self._cache and self.opened:
            text = self.opened[-1].read_text(encoding="utf-8")
            for note_xml in _NOTE_RE.findall(text):
                m = _PROV_RE.search(note_xml)
                url = m.group(1) if m else None
                body = (
                    f"<div>Source: <u>{url}</u></div><div>body</div>" if url else "<div>body</div>"
                )
                self._cache.append(ImportedNote(f"note-{next(self.id_counter)}", "T", body))
        return list(self._cache)


@pytest.fixture(autouse=True)
def _instant_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every poll in `notes_enex` finite and free of real sleeps.

    The landing-folder and landing-notes poll loops take their real iteration
    counts (driven by the fake runner) but never block on `time.sleep`, and
    `time.monotonic` advances only when `sleep` is called -- so a fake that
    delivers can always satisfy its deadline."""
    now = [0.0]

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(notes_enex.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(notes_enex.time, "sleep", fake_sleep)


def _run_enex(
    tmp_path: Path,
    source: Path,
    id_counter: itertools.count[int],
    *,
    force: bool = False,
) -> tuple[EnexImportReport, _FaithfulEnexRunner]:
    runner = _FaithfulEnexRunner(id_counter=id_counter)
    report = run_enex_import(
        runner,
        _config(tmp_path, output_dir=source, force=force),
        source_dir=source,
        enex_path=tmp_path / "o.enex",
        confirm=False,
    )
    return report, runner


# === End-to-end: the no-op re-run guarantee is restored ======================


def test_fixed_exporter_creates_no_orphan_so_re_runs_are_a_clean_noop(
    tmp_path: Path,
) -> None:
    """The whole bug, built from the top with the FIXED exporter. Two exports
    of the same thread (`Foo` -> `Bar`, `--force` on the second, `updated_usec`
    held *unchanged* at 100) leave only `Bar.md` on disk, so the import path
    has one source per `quip_id`. Three default `import-notes` re-runs over
    byte-identical sources then honour the README's no-op guarantee for the
    `enex` writer: a first import, then runs that write NO archive and never
    open Notes at all."""
    config = _run_export(tmp_path, _content("doc1", "Foo", updated_usec=100))
    _run_export(tmp_path, _content("doc1", "Bar", updated_usec=100), force=True)
    private = config.output_dir / "Private"
    assert sorted(p.name for p in private.glob("*.md")) == ["Bar.md"]
    assert len(scan_source(config.output_dir)) == 1

    ids = itertools.count(1)
    # Run A: one canonical note imported.
    a, runner_a = _run_enex(tmp_path, config.output_dir, ids)
    assert a.moved == 1
    assert a.superseded == 0
    assert a.skipped_unchanged == 0
    assert a.documents == 1
    assert a.enex_path != ""
    assert runner_a.opened == [tmp_path / "o.enex"]

    # Runs B and C: nothing on disk changed -> no archive, no Notes open.
    for label in ("B", "C"):
        again, runner = _run_enex(tmp_path, config.output_dir, ids)
        assert again.moved == 0
        assert again.superseded == 0
        assert again.skipped_unchanged == 1
        assert again.documents == 0
        assert again.enex_path == ""
        assert runner.opened == [], f"run {label} opened Notes over unchanged sources"


def test_existing_orphan_tree_stops_churning_via_scan_source_collapse(
    tmp_path: Path,
) -> None:
    """The migration case: an orphan tree left by a PRE-FIX exporter (two
    same-`quip_id` .md files, never re-exported through the fix). The export
    fix cannot touch a tree the user never re-exports, so `scan_source`'s
    collapse is what restores the no-op guarantee here. Three default
    re-runs over the unchanged pair import one note once, then skip forever
    -- no archive, no Notes open on the re-runs."""
    source = tmp_path / "export"
    foo = _write_doc(
        source,
        "Private/Foo.md",
        quip_id="doc1",
        url="https://example.quip.com/doc1",
        title="Foo",
        exported_sec=1,
        body="# Foo\nbody of foo\n",
    )
    bar = _write_doc(
        source,
        "Private/Bar.md",
        quip_id="doc1",
        url="https://example.quip.com/doc1",
        title="Bar",
        exported_sec=2,
        body="# Bar\nbody of bar\n",
    )
    _set_mtime(foo, 100.0)
    _set_mtime(bar, 200.0)  # Bar is the newer export either way
    assert sorted(p.name for p in (source / "Private").glob("*.md")) == ["Bar.md", "Foo.md"]

    # The collapse reduces the two same-quip_id sources to one (the canonical
    # Bar), so the import path never sees the alternation that churned state.
    collapsed = scan_source(source)
    assert len(collapsed) == 1
    assert collapsed[0].title == "Bar"

    ids = itertools.count(1)
    a, _ = _run_enex(tmp_path, source, ids)
    assert a.moved == 1
    assert a.superseded == 0
    assert a.skipped_unchanged == 0
    assert a.documents == 1

    for label in ("B", "C"):
        again, runner = _run_enex(tmp_path, source, ids)
        assert again.moved == 0
        assert again.superseded == 0
        assert again.skipped_unchanged == 1
        assert again.documents == 0
        assert again.enex_path == ""
        assert runner.opened == [], f"run {label} opened Notes over unchanged sources"


@dataclass
class _FakePruneRunner:
    """Records deletions; the prune `live` set comes from the real state file."""

    folders: list[FolderInfo] = field(default_factory=list)
    deleted_notes: list[str] = field(default_factory=list)
    deleted_folders: list[str] = field(default_factory=list)

    def resolve_account(self, *, local: bool) -> str:
        return "iCloud"

    def top_level_folders(self, account: str) -> list[FolderInfo]:
        return list(self.folders)

    def delete_folder(self, folder_id: str) -> None:
        self.deleted_folders.append(folder_id)
        self.folders = [f for f in self.folders if f.folder_id != folder_id]

    def delete_note(self, note_id: str) -> None:
        self.deleted_notes.append(note_id)


def _notes_state(tmp_path: Path) -> dict[str, dict[str, object]]:
    import json

    return json.loads((tmp_path / ".quip2md" / "notes_state.json").read_text(encoding="utf-8"))


def test_prune_superseded_breaks_the_cycle_now(tmp_path: Path) -> None:
    """Before the fix, `prune-notes --superseded` cleared `superseded_note_ids`
    but the next default import re-imported and superseded again -- the cycle
    survived prune (`test_prune_superseded_does_not_stop_churn` in the bug
    report). With the `scan_source` collapse, the next default import is a
    no-op even after prune has emptied the supersede list: the canonical source
    is the same one whose hash is in state, so nothing is pending."""
    source = tmp_path / "export"
    foo = _write_doc(
        source,
        "Private/Foo.md",
        quip_id="doc1",
        url="https://example.quip.com/doc1",
        title="Foo",
        exported_sec=1,
        body="# Foo\n",
    )
    bar = _write_doc(
        source,
        "Private/Bar.md",
        quip_id="doc1",
        url="https://example.quip.com/doc1",
        title="Bar",
        exported_sec=2,
        body="# Bar\n",
    )
    _set_mtime(foo, 100.0)
    _set_mtime(bar, 200.0)

    ids = itertools.count(1)
    # Run A imports the canonical Bar once.
    _run_enex(tmp_path, source, ids)
    pre_hash = _notes_state(tmp_path)["doc1"]["content_hash"]
    first_note_id = _notes_state(tmp_path)["doc1"]["note_id"]
    assert _notes_state(tmp_path)["doc1"].get("superseded_note_ids", []) == []

    # Simulate a leftover supersede from the pre-fix churn era: a --force
    # re-import creates a new note and supersedes the prior, exactly as the
    # old steady-state churn did each run.
    forced, _ = _run_enex(tmp_path, source, ids, force=True)
    assert forced.moved == 1
    assert forced.superseded == 1
    entry = _notes_state(tmp_path)["doc1"]
    assert entry["note_id"] != first_note_id
    assert entry["superseded_note_ids"] == [first_note_id]
    assert entry["content_hash"] == pre_hash  # Bar is canonical; its hash is unchanged

    # Prune the superseded copy. Prune keys off `superseded_note_ids` (the live
    # set is read from the same state file), so it deletes exactly that id and
    # leaves `content_hash` untouched.
    pruner = _FakePruneRunner()
    prune_report = prune_notes(
        pruner, _config(tmp_path, output_dir=source), superseded=True, apply=True
    )
    assert prune_report.notes_deleted == 1
    assert pruner.deleted_notes == [first_note_id]
    post = _notes_state(tmp_path)["doc1"]
    # `NotesState.flush` omits the key when the list is empty, so a missing key
    # *is* the empty-list state -- both spell "no superseded copies left".
    assert post.get("superseded_note_ids", []) == []
    assert post["content_hash"] == pre_hash  # `_without_superseded` left the hash alone

    # The default import that used to re-import-and-supersede here is now a
    # no-op: the collapse keeps only Bar, and Bar's hash already matches state.
    again, runner = _run_enex(tmp_path, source, ids)
    assert again.moved == 0
    assert again.superseded == 0
    assert again.skipped_unchanged == 1
    assert again.documents == 0
    assert again.enex_path == ""
    assert runner.opened == []
    # State was not churned: same note id, same hash, still no superseded ids.
    final = _notes_state(tmp_path)["doc1"]
    assert final["note_id"] == post["note_id"]
    assert final["content_hash"] == pre_hash
    assert final.get("superseded_note_ids", []) == []
