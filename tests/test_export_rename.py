"""A renamed or moved Quip thread must not leave its old `.md` behind.

A title change gives a thread a new sanitized filename. The exporter used to
write the new file and re-point the manifest, but it left the old file on
disk with the same `quip_id` frontmatter. `scan_source` then found two
sources for one id, and every `import-notes` run re-imported one of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from quip2md import export
from quip2md.client import (
    FolderChild,
    FolderChildKind,
    QuipFolder,
    QuipUser,
    ThreadContent,
    ThreadType,
)
from quip2md.config import Config
from quip2md.export import run_export


def _config(tmp_path: Path, *, force: bool = False) -> Config:
    return Config(
        token="",
        output_dir=tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=False,
        include_chats=False,
        force=force,
    )


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


def test_a_samefile_error_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the two paths cannot be compared, the unlink must not happen."""
    _run_export(tmp_path, _content("t1", "Old title", updated_usec=100))

    def unreadable(a: object, b: object) -> bool:
        raise OSError("stat failed")

    monkeypatch.setattr(export.os.path, "samefile", unreadable)
    config = _run_export(tmp_path, _content("t1", "New title", updated_usec=200))

    assert (config.output_dir / "Private" / "Old title.md").is_file()
    assert (config.output_dir / "Private" / "New title.md").is_file()
