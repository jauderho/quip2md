"""Tests for quip2md.export.

Uses a small hand-written fake client (no network, no real `QuipClient`)
that structurally satisfies `export.ExportClient`, plus real (unmocked)
`html_to_markdown`/`build_frontmatter` calls from `quip2md.convert` since
those are pure functions -- only I/O (the client and the filesystem) is
faked/sandboxed (`tmp_path`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from quip2md.client import (
    THREAD_BATCH_SIZE,
    FolderChild,
    FolderChildKind,
    QuipFolder,
    QuipUser,
    ThreadContent,
    ThreadType,
)
from quip2md.config import Config
from quip2md.export import ExportReport, run_export

# --- test helpers ------------------------------------------------------


class FakeExportClient:
    """Duck-typed fake satisfying `export.ExportClient`. No network."""

    def __init__(
        self,
        user: QuipUser,
        folders_by_id: dict[str, QuipFolder],
        contents: dict[str, ThreadContent],
    ) -> None:
        self._user = user
        self._folders_by_id = folders_by_id
        self._contents = contents
        self.threads_batch_calls: list[tuple[str, ...]] = []
        self.blob_calls: list[tuple[str, str]] = []
        self.export_xlsx_calls: list[str] = []
        self.blob_responses: dict[tuple[str, str], tuple[bytes, str | None]] = {}
        self.export_xlsx_responses: dict[str, bytes] = {}
        self.threads_batch_side_effect: (
            Callable[[Sequence[str]], dict[str, ThreadContent]] | None
        ) = None
        self.raise_on_threads_batch_call: int | None = None
        self._threads_batch_call_count = 0

    def current_user(self) -> QuipUser:
        return self._user

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]:
        return {fid: self._folders_by_id[fid] for fid in ids if fid in self._folders_by_id}

    def threads_batch(self, ids: Sequence[str]) -> dict[str, ThreadContent]:
        self.threads_batch_calls.append(tuple(ids))
        self._threads_batch_call_count += 1
        if self._threads_batch_call_count == self.raise_on_threads_batch_call:
            raise KeyboardInterrupt
        if self.threads_batch_side_effect is not None:
            return self.threads_batch_side_effect(ids)
        return {tid: self._contents[tid] for tid in ids if tid in self._contents}

    def blob(self, thread_id: str, blob_id: str) -> tuple[bytes, str | None]:
        self.blob_calls.append((thread_id, blob_id))
        return self.blob_responses.get((thread_id, blob_id), (b"binary", "image/png"))

    def export_xlsx(self, thread_id: str) -> bytes:
        self.export_xlsx_calls.append(thread_id)
        return self.export_xlsx_responses.get(thread_id, b"xlsx-bytes")


def make_user(*, private: str | None = None, shared: tuple[str, ...] = ()) -> QuipUser:
    return QuipUser(
        id="user1",
        name="Test User",
        private_folder_id=private,
        desktop_folder_id=None,
        archive_folder_id=None,
        starred_folder_id=None,
        shared_folder_ids=shared,
        group_folder_ids=(),
    )


def make_folder(folder_id: str, title: str, children: Sequence[FolderChild] = ()) -> QuipFolder:
    return QuipFolder(id=folder_id, title=title, children=tuple(children))


def thread_child(thread_id: str) -> FolderChild:
    return FolderChild(kind=FolderChildKind.THREAD, id=thread_id)


def folder_child(folder_id: str) -> FolderChild:
    return FolderChild(kind=FolderChildKind.FOLDER, id=folder_id)


def make_content(
    thread_id: str,
    title: str,
    thread_type: ThreadType = ThreadType.DOCUMENT,
    html: str = "<p>Hello</p>",
    *,
    created_usec: int = 1_000_000,
    updated_usec: int = 2_000_000,
    link: str | None = "https://example.quip.com/doc",
) -> ThreadContent:
    return ThreadContent(
        id=thread_id,
        title=title,
        thread_type=thread_type,
        created_usec=created_usec,
        updated_usec=updated_usec,
        link=link,
        html=html,
    )


def make_config(
    tmp_path: Path,
    *,
    output_dir: Path | None = None,
    state_path: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    include_chats: bool = False,
) -> Config:
    return Config(
        token="test-token",
        output_dir=output_dir if output_dir is not None else tmp_path / "export",
        state_path=state_path if state_path is not None else tmp_path / ".quip2md" / "state.json",
        dry_run=dry_run,
        verbose=False,
        include_chats=include_chats,
        force=force,
    )


def write_manifest(state_path: Path, thread_id: str, path: str, updated_usec: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"path": path, "updated_usec": updated_usec, "exported_at": "2026-01-01T00:00:00Z"}
    state_path.write_text(json.dumps({thread_id: entry}), encoding="utf-8")


def simple_client(
    contents: dict[str, ThreadContent], *, children: Sequence[FolderChild] | None = None
) -> FakeExportClient:
    """One private-folder root directly containing the given thread(s)."""
    user = make_user(private="priv")
    resolved_children = (
        children if children is not None else tuple(thread_child(tid) for tid in contents)
    )
    folders = {"priv": make_folder("priv", "Private", children=resolved_children)}
    return FakeExportClient(user, folders, contents)


# --- dry run -------------------------------------------------------------


def test_dryrun_makes_zero_threads_batch_or_blob_calls_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contents = {"t1": make_content("t1", "Doc One")}
    client = simple_client(contents)
    config = make_config(tmp_path, dry_run=True)

    report = run_export(client, config)

    assert client.threads_batch_calls == []
    assert client.blob_calls == []
    assert not config.output_dir.exists()
    assert not config.state_path.exists()
    assert report.exported == 0
    assert report.failed == []
    captured = capsys.readouterr()
    assert "Dry run" in captured.out
    assert "Total threads discovered: 1" in captured.out


# --- happy path: nested path, frontmatter -------------------------------


def test_happy_path_writes_md_with_frontmatter_at_nested_path(tmp_path: Path) -> None:
    user = make_user(private="priv")
    folders = {
        "priv": make_folder("priv", "Private", children=[folder_child("sub")]),
        "sub": make_folder("sub", "Sub Folder", children=[thread_child("doc1")]),
    }
    content = make_content(
        "doc1",
        "My Document",
        html="<p>Hello <b>World</b></p>",
        created_usec=1_700_000_000_000_000,
        updated_usec=1_800_000_000_000_000,
        link="https://example.quip.com/doc1",
    )
    client = FakeExportClient(user, folders, {"doc1": content})
    config = make_config(tmp_path)

    report = run_export(client, config)

    md_path = config.output_dir / "Private" / "Sub Folder" / "My Document.md"
    assert md_path.is_file()
    text = md_path.read_text(encoding="utf-8")
    assert 'quip_id: "doc1"' in text
    assert 'quip_url: "https://example.quip.com/doc1"' in text
    assert 'title: "My Document"' in text
    assert "Hello **World**" in text
    assert report.exported == 1
    assert report.failed == []


# --- filename collisions --------------------------------------------------


def test_collision_suffixing_two_threads_titled_notes(tmp_path: Path) -> None:
    contents = {
        "t1": make_content("t1", "Notes"),
        "t2": make_content("t2", "Notes"),
    }
    client = simple_client(contents, children=[thread_child("t1"), thread_child("t2")])
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert (config.output_dir / "Private" / "Notes.md").is_file()
    assert (config.output_dir / "Private" / "Notes (2).md").is_file()
    assert report.exported == 2


def test_same_thread_reexport_overwrites_own_file_without_suffix(tmp_path: Path) -> None:
    content = make_content("t1", "Notes", updated_usec=100)
    client = simple_client({"t1": content})
    config = make_config(tmp_path)
    run_export(client, config)

    # Re-export the same thread (force, so it isn't skipped as unchanged).
    forced_config = make_config(tmp_path, force=True)
    report = run_export(client, forced_config)

    assert (config.output_dir / "Private" / "Notes.md").is_file()
    assert not (config.output_dir / "Private" / "Notes (2).md").exists()
    assert report.exported == 1


def test_crash_orphan_at_base_name_is_overwritten_not_duplicated(tmp_path: Path) -> None:
    # Simulate a hard crash: a `.md` exists on disk at the thread's own base
    # name but the manifest never recorded it (flush didn't happen). On the
    # next run the thread must OVERWRITE its own orphan, not write "Notes (2)".
    content = make_content("t1", "Notes", updated_usec=100)
    client = simple_client({"t1": content})
    config = make_config(tmp_path)

    orphan = config.output_dir / "Private" / "Notes.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("STALE ORPHAN CONTENT", encoding="utf-8")
    # No manifest entry exists for t1 (the crash lost it).

    report = run_export(client, config)

    assert not (config.output_dir / "Private" / "Notes (2).md").exists()
    assert "STALE ORPHAN CONTENT" not in orphan.read_text(encoding="utf-8")
    assert report.exported == 1


# --- manifest skip-unchanged / force ---------------------------------------


def test_skip_unchanged_honors_manifest(tmp_path: Path) -> None:
    content = make_content("t1", "Notes", updated_usec=100)
    client = simple_client({"t1": content})
    config = make_config(tmp_path)
    write_manifest(config.state_path, "t1", "Private/Notes.md", 100)

    report = run_export(client, config)

    assert report.skipped_unchanged == 1
    assert report.exported == 0
    assert client.threads_batch_calls == [("t1",)]


def test_force_reexports(tmp_path: Path) -> None:
    content = make_content("t1", "Notes", updated_usec=100)
    client = simple_client({"t1": content})
    config = make_config(tmp_path, force=True)
    write_manifest(config.state_path, "t1", "Private/Notes.md", 100)

    report = run_export(client, config)

    assert report.exported == 1
    assert report.skipped_unchanged == 0


# --- chats ------------------------------------------------------------


def test_chat_skipped_by_default_included_with_flag(tmp_path: Path) -> None:
    content = make_content("t1", "Chat Thread", thread_type=ThreadType.CHAT)
    client = simple_client({"t1": content})

    default_config = make_config(
        tmp_path, output_dir=tmp_path / "a", state_path=tmp_path / "a.json"
    )
    default_report = run_export(client, default_config)
    assert default_report.skipped_chats == 1
    assert default_report.exported == 0

    included_config = make_config(
        tmp_path, output_dir=tmp_path / "b", state_path=tmp_path / "b.json", include_chats=True
    )
    included_report = run_export(client, included_config)
    assert included_report.skipped_chats == 0
    assert included_report.exported == 1


# --- unsupported types ------------------------------------------------


def test_unsupported_type_logged_and_skipped(tmp_path: Path) -> None:
    content = make_content("t1", "Deck", thread_type=ThreadType.SLIDES)
    client = simple_client({"t1": content})
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert report.skipped_other == 1
    assert report.exported == 0


# --- batch-fetch failure isolation -----------------------------------


def test_batch_fetch_failure_marks_only_that_batchs_threads_failed(tmp_path: Path) -> None:
    thread_ids = [f"t{i}" for i in range(THREAD_BATCH_SIZE + 2)]
    contents = {tid: make_content(tid, tid) for tid in thread_ids}
    client = simple_client(
        contents, children=[thread_child(tid) for tid in thread_ids]
    )
    failing_ids = {f"t{THREAD_BATCH_SIZE}", f"t{THREAD_BATCH_SIZE + 1}"}

    def side_effect(ids: Sequence[str]) -> dict[str, ThreadContent]:
        if failing_ids & set(ids):
            raise RuntimeError("simulated batch failure")
        return {tid: contents[tid] for tid in ids}

    client.threads_batch_side_effect = side_effect
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert report.exported == THREAD_BATCH_SIZE
    assert {tid for tid, _reason in report.failed} == failing_ids
    assert all("batch fetch failed" in reason for _tid, reason in report.failed)
    assert len(client.threads_batch_calls) == 2


# --- blob resume-friendliness --------------------------------------------


def test_blob_download_skipped_when_file_exists(tmp_path: Path) -> None:
    content = make_content(
        "doc1", "Doc", html="<p>See <img src='/blob/doc1/blobABC.png'/></p>"
    )
    client = simple_client({"doc1": content})
    config = make_config(tmp_path)
    assets_dir = config.output_dir / "Private" / "_assets" / "doc1"
    assets_dir.mkdir(parents=True)
    (assets_dir / "blobABC.png").write_bytes(b"already-here")

    report = run_export(client, config)

    assert client.blob_calls == []
    assert report.blobs_downloaded == 0
    md_text = (config.output_dir / "Private" / "Doc.md").read_text(encoding="utf-8")
    assert "_assets/doc1/blobABC.png" in md_text


def test_blob_downloaded_when_missing(tmp_path: Path) -> None:
    content = make_content(
        "doc1", "Doc", html="<p>See <img src='/blob/doc1/blobABC'/></p>"
    )
    client = simple_client({"doc1": content})
    client.blob_responses[("doc1", "blobABC")] = (b"\x89PNG", "image/png")
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert client.blob_calls == [("doc1", "blobABC")]
    assert report.blobs_downloaded == 1
    asset_path = config.output_dir / "Private" / "_assets" / "doc1" / "blobABC.png"
    assert asset_path.read_bytes() == b"\x89PNG"


# --- spreadsheet xlsx backup ----------------------------------------------


def test_wide_spreadsheet_triggers_xlsx_backup(tmp_path: Path) -> None:
    header = "".join("<td>h</td>" for _ in range(31))
    row = "".join("<td>v</td>" for _ in range(31))
    html = f"<table><tr>{header}</tr><tr>{row}</tr></table>"
    content = make_content("sheet1", "Big Sheet", thread_type=ThreadType.SPREADSHEET, html=html)
    client = simple_client({"sheet1": content})
    client.export_xlsx_responses["sheet1"] = b"real-xlsx-bytes"
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert client.export_xlsx_calls == ["sheet1"]
    assert report.xlsx_backups == 1
    xlsx_path = config.output_dir / "Private" / "Big Sheet.xlsx"
    assert xlsx_path.read_bytes() == b"real-xlsx-bytes"


def test_narrow_spreadsheet_no_warnings_skips_xlsx_backup(tmp_path: Path) -> None:
    html = "<table><tr><td>a</td></tr><tr><td>b</td></tr></table>"
    content = make_content("sheet1", "Small Sheet", thread_type=ThreadType.SPREADSHEET, html=html)
    client = simple_client({"sheet1": content})
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert client.export_xlsx_calls == []
    assert report.xlsx_backups == 0


# --- KeyboardInterrupt -------------------------------------------------


def test_keyboard_interrupt_mid_run_flushes_manifest(tmp_path: Path) -> None:
    thread_ids = [f"t{i}" for i in range(THREAD_BATCH_SIZE + 2)]
    contents = {tid: make_content(tid, tid) for tid in thread_ids}
    client = simple_client(contents, children=[thread_child(tid) for tid in thread_ids])
    client.raise_on_threads_batch_call = 2  # interrupt on the second chunk
    config = make_config(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        run_export(client, config)

    assert config.state_path.is_file()
    data = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert set(data.keys()) == set(thread_ids[:THREAD_BATCH_SIZE])

    last_run_path = config.state_path.parent / "last_run.json"
    assert last_run_path.is_file()
    partial = json.loads(last_run_path.read_text(encoding="utf-8"))
    assert partial["exported"] == THREAD_BATCH_SIZE


# --- report JSON -----------------------------------------------------


def test_report_json_written_and_counts_correct(tmp_path: Path) -> None:
    contents = {
        "doc": make_content("doc", "Doc", thread_type=ThreadType.DOCUMENT),
        "chat": make_content("chat", "Chat", thread_type=ThreadType.CHAT),
        "slides": make_content("slides", "Slides", thread_type=ThreadType.SLIDES),
    }
    client = simple_client(
        contents, children=[thread_child("doc"), thread_child("chat"), thread_child("slides")]
    )
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert isinstance(report, ExportReport)
    assert report.exported == 1
    assert report.skipped_chats == 1
    assert report.skipped_other == 1

    last_run_path = config.state_path.parent / "last_run.json"
    data = json.loads(last_run_path.read_text(encoding="utf-8"))
    assert data["exported"] == 1
    assert data["skipped_chats"] == 1
    assert data["skipped_other"] == 1
    assert data["failed"] == []
    assert isinstance(data["elapsed_seconds"], float)


# --- filesystem edges (T7 hardening) --------------------------------------


def test_long_nested_folder_path_writes_successfully(tmp_path: Path) -> None:
    # Each raw title is 300 chars; sanitize_component trims every component
    # (folders and the doc stem) to 120 chars independently, so a folder path
    # several such components deep must still write successfully.
    user = make_user(private="priv")
    long_title = "A" * 300
    folders = {
        "priv": make_folder("priv", "Private", children=[folder_child("f1")]),
        "f1": make_folder("f1", long_title, children=[folder_child("f2")]),
        "f2": make_folder("f2", long_title, children=[folder_child("f3")]),
        "f3": make_folder("f3", long_title, children=[thread_child("doc1")]),
    }
    content = make_content("doc1", long_title)
    client = FakeExportClient(user, folders, {"doc1": content})
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert report.exported == 1
    assert report.failed == []
    expected_component = "A" * 120
    md_path = (
        config.output_dir
        / "Private"
        / expected_component
        / expected_component
        / expected_component
        / f"{expected_component}.md"
    )
    assert md_path.is_file()


def test_case_insensitive_title_collision_gets_suffixed(tmp_path: Path) -> None:
    # _NameAllocator tracks claims by lower-cased filename in memory, so
    # "Foo" vs "foo" collide even on a case-sensitive filesystem where the
    # two names would otherwise be distinct files.
    contents = {
        "t1": make_content("t1", "Foo"),
        "t2": make_content("t2", "foo"),
    }
    client = simple_client(contents, children=[thread_child("t1"), thread_child("t2")])
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert report.exported == 2
    foo_path = config.output_dir / "Private" / "Foo.md"
    foo2_path = config.output_dir / "Private" / "foo (2).md"
    assert foo_path.is_file()
    assert foo2_path.is_file()
    # Verify by content (not by asserting non-existence of a differently
    # cased path, which would be unreliable on case-insensitive filesystems).
    assert 'quip_id: "t1"' in foo_path.read_text(encoding="utf-8")
    assert 'quip_id: "t2"' in foo2_path.read_text(encoding="utf-8")


def test_title_that_sanitizes_to_fallback_uses_untitled(tmp_path: Path) -> None:
    content = make_content("weird-id", "///")
    client = simple_client({"weird-id": content})
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert report.exported == 1
    assert (config.output_dir / "Private" / "untitled.md").is_file()


def test_blob_content_type_with_parameters_still_resolves_extension(tmp_path: Path) -> None:
    content = make_content("doc1", "Doc", html="<p>See <img src='/blob/doc1/blobABC'/></p>")
    client = simple_client({"doc1": content})
    client.blob_responses[("doc1", "blobABC")] = (b"\x89PNG", "image/png; charset=binary")
    config = make_config(tmp_path)

    report = run_export(client, config)

    assert report.blobs_downloaded == 1
    asset_path = config.output_dir / "Private" / "_assets" / "doc1" / "blobABC.png"
    assert asset_path.is_file()
    assert asset_path.read_bytes() == b"\x89PNG"


# --- --only filtering --------------------------------------------------


def test_only_restricts_export_to_given_thread_ids(tmp_path: Path) -> None:
    contents = {
        "t1": make_content("t1", "One"),
        "t2": make_content("t2", "Two"),
    }
    client = simple_client(contents, children=[thread_child("t1"), thread_child("t2")])
    config = make_config(tmp_path)

    report = run_export(client, config, only=["t2"])

    assert report.exported == 1
    assert (config.output_dir / "Private" / "Two.md").is_file()
    assert not (config.output_dir / "Private" / "One.md").exists()
