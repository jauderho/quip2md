"""Tests for quip2md.walker.

Uses a small hand-written fake client (no network, no real `QuipClient`)
that structurally satisfies `walker.FolderSource`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from quip2md.client import FolderChild, FolderChildKind, QuipFolder, QuipUser
from quip2md.config import Config
from quip2md.walker import (
    DEFAULT_FLUSH_EVERY,
    Manifest,
    ManifestError,
    ThreadWork,
    sanitize_component,
    walk,
)

# --- test helpers ------------------------------------------------------


class FakeFolderSource:
    """Duck-typed fake satisfying `walker.FolderSource`. No network."""

    def __init__(self, user: QuipUser, folders_by_id: dict[str, QuipFolder]) -> None:
        self._user = user
        self._folders_by_id = folders_by_id
        self.folder_fetch_calls: list[tuple[str, ...]] = []

    def current_user(self) -> QuipUser:
        return self._user

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]:
        self.folder_fetch_calls.append(tuple(ids))
        return {fid: self._folders_by_id[fid] for fid in ids if fid in self._folders_by_id}


def make_user(
    *,
    private: str | None = None,
    desktop: str | None = None,
    archive: str | None = None,
    starred: str | None = None,
    shared: tuple[str, ...] = (),
    group: tuple[str, ...] = (),
) -> QuipUser:
    return QuipUser(
        id="user1",
        name="Test User",
        private_folder_id=private,
        desktop_folder_id=desktop,
        archive_folder_id=archive,
        starred_folder_id=starred,
        shared_folder_ids=shared,
        group_folder_ids=group,
    )


def make_folder(folder_id: str, title: str, children: Sequence[FolderChild] = ()) -> QuipFolder:
    return QuipFolder(id=folder_id, title=title, children=tuple(children))


def thread_child(thread_id: str) -> FolderChild:
    return FolderChild(kind=FolderChildKind.THREAD, id=thread_id)


def folder_child(folder_id: str) -> FolderChild:
    return FolderChild(kind=FolderChildKind.FOLDER, id=folder_id)


def make_config(tmp_path: Path, *, force: bool = False) -> Config:
    return Config(
        token="test-token",
        output_dir=tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=False,
        verbose=False,
        include_chats=False,
        force=force,
    )


# --- walk(): cycle safety -----------------------------------------------


def test_cycle_terminates() -> None:
    # desktop -> A -> B -> A (cycle back), B also has a thread.
    user = make_user(desktop="A")
    folders = {
        "A": make_folder("A", "FolderA", children=[folder_child("B")]),
        "B": make_folder("B", "FolderB", children=[folder_child("A"), thread_child("tc")]),
    }
    fake = FakeFolderSource(user, folders)

    config = make_config(Path("/tmp/unused"))
    results = list(walk(fake, config))

    assert results == [ThreadWork(thread_id="tc", folder_path=("Desktop", "FolderB"))]


def test_cycle_does_not_refetch_visited_folder() -> None:
    user = make_user(desktop="A")
    folders = {
        "A": make_folder("A", "FolderA", children=[folder_child("B")]),
        "B": make_folder("B", "FolderB", children=[folder_child("A")]),
    }
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    list(walk(fake, config))

    # Level 0: {A}. Level 1: {B}. Level 2: empty (A already visited, so no
    # third fetch). Exactly two folders() calls, each level batched.
    assert fake.folder_fetch_calls == [("A",), ("B",)]


# --- walk(): dedup priority ----------------------------------------------


def test_thread_reachable_from_two_folders_yields_once_at_priority_path() -> None:
    # "dup" lives in Private (priority 1) and also in Archive (priority 5,
    # a pointer). Private must win.
    user = make_user(private="priv", archive="arch")
    folders = {
        "priv": make_folder("priv", "Private", children=[thread_child("dup")]),
        "arch": make_folder("arch", "Archive", children=[thread_child("dup")]),
    }
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    results = list(walk(fake, config))

    assert results == [ThreadWork(thread_id="dup", folder_path=("Private",))]


def test_dedup_priority_order_desktop_beats_starred() -> None:
    user = make_user(desktop="desk", starred="star")
    folders = {
        "desk": make_folder("desk", "Desktop", children=[thread_child("dup")]),
        "star": make_folder("star", "Starred", children=[thread_child("dup")]),
    }
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    results = list(walk(fake, config))

    assert results == [ThreadWork(thread_id="dup", folder_path=("Desktop",))]


def test_duplicate_sighting_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    user = make_user(private="priv", archive="arch")
    folders = {
        "priv": make_folder("priv", "Private", children=[thread_child("dup")]),
        "arch": make_folder("arch", "Archive", children=[thread_child("dup")]),
    }
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    with caplog.at_level(logging.DEBUG, logger="quip2md.walker"):
        list(walk(fake, config))

    assert any(
        record.levelno == logging.DEBUG and "dup" in record.getMessage()
        for record in caplog.records
    )


def test_trash_folder_never_walked() -> None:
    # QuipUser has no trash_folder_id field at all (client.py omits it by
    # design); walk() only ever reads the fields the model exposes, so a
    # trash pointer simply cannot be reached.
    user = make_user(private="priv")
    folders = {"priv": make_folder("priv", "Private", children=[thread_child("t1")])}
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    results = list(walk(fake, config))

    assert [r.thread_id for r in results] == ["t1"]


def test_shared_and_group_roots_use_own_folder_title_as_second_component() -> None:
    user = make_user(shared=("shared1",), group=("group1",))
    folders = {
        "shared1": make_folder("shared1", "Team Docs", children=[thread_child("t1")]),
        "group1": make_folder("group1", "Eng Group", children=[thread_child("t2")]),
    }
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    results = list(walk(fake, config))

    by_id = {r.thread_id: r.folder_path for r in results}
    assert by_id["t1"] == ("Shared", "Team Docs")
    assert by_id["t2"] == ("Group", "Eng Group")


def test_folders_batched_per_level_not_one_at_a_time() -> None:
    user = make_user(private="priv", desktop="desk")
    folders = {
        "priv": make_folder("priv", "Private", children=[thread_child("t1")]),
        "desk": make_folder("desk", "Desktop", children=[thread_child("t2")]),
    }
    fake = FakeFolderSource(user, folders)
    config = make_config(Path("/tmp/unused"))

    list(walk(fake, config))

    # Two separate root subtrees are walked (private fully, then desktop
    # fully) -- each root's single level is one batched call.
    assert fake.folder_fetch_calls == [("priv",), ("desk",)]


# --- sanitize_component --------------------------------------------------


def test_sanitize_strips_reserved_characters() -> None:
    assert sanitize_component('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"


def test_sanitize_all_reserved_characters_falls_back() -> None:
    assert sanitize_component('/\\:*?"<>|') == "untitled"


def test_sanitize_dot_only_name_falls_back() -> None:
    assert sanitize_component("..") == "untitled"


def test_sanitize_empty_name_falls_back() -> None:
    assert sanitize_component("") == "untitled"


def test_sanitize_preserves_emoji() -> None:
    assert sanitize_component("Party Notes \U0001f389") == "Party Notes \U0001f389"


def test_sanitize_trims_to_120_chars() -> None:
    result = sanitize_component("A" * 300)
    assert len(result) == 120
    assert result == "A" * 120


def test_sanitize_caps_utf8_bytes_for_cjk_title() -> None:
    # 200 CJK codepoints = 600 UTF-8 bytes but only 200 codepoints, so the
    # codepoint cap alone (120) would still leave ~360 bytes -- over the
    # filesystem limit. The byte cap must bring it to <= 200 bytes.
    result = sanitize_component("漢" * 200)  # 漢 * 200
    assert len(result.encode("utf-8")) <= 200
    assert result  # never empty
    # Must not split a codepoint: round-trips cleanly.
    assert result.encode("utf-8").decode("utf-8") == result


def test_sanitize_caps_utf8_bytes_for_emoji_title() -> None:
    result = sanitize_component("\U0001f389" * 100)  # 🎉 * 100 (4 bytes each)
    assert len(result.encode("utf-8")) <= 200
    assert result.encode("utf-8").decode("utf-8") == result  # no split codepoint


def test_sanitize_strips_leading_trailing_dots_and_spaces() -> None:
    assert sanitize_component("  .hidden file.  ") == "hidden file"


def test_sanitize_collapses_internal_whitespace() -> None:
    assert sanitize_component("too   many    spaces") == "too many spaces"


def test_sanitize_strips_control_characters() -> None:
    assert sanitize_component("bad\x00name\x1f\x7f") == "badname"


def test_sanitize_fallback_via_caller_supplied_id() -> None:
    # Empty title: caller passes the id as the fallback name.
    assert sanitize_component("" or "thread123") == "thread123"


# --- Manifest: load ------------------------------------------------------


def test_manifest_fresh_load_no_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)

    manifest.load()

    assert manifest.should_export("t1", 100) is True


def test_manifest_load_corrupted_file_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.state_path.parent.mkdir(parents=True)
    config.state_path.write_text("{not valid json", encoding="utf-8")
    manifest = Manifest(config)

    with pytest.raises(ManifestError):
        manifest.load()


def test_manifest_load_non_object_json_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.state_path.parent.mkdir(parents=True)
    config.state_path.write_text("[1, 2, 3]", encoding="utf-8")
    manifest = Manifest(config)

    with pytest.raises(ManifestError):
        manifest.load()


def test_manifest_load_malformed_entry_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.state_path.parent.mkdir(parents=True)
    config.state_path.write_text(json.dumps({"t1": {"path": "x.md"}}), encoding="utf-8")
    manifest = Manifest(config)

    with pytest.raises(ManifestError):
        manifest.load()


def test_manifest_truncated_json_error_includes_state_path(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.state_path.parent.mkdir(parents=True)
    # Mid-write truncation: valid JSON prefix cut off before it closes.
    config.state_path.write_text('{"t1": {"path": "x.md", "updated', encoding="utf-8")
    manifest = Manifest(config)

    with pytest.raises(ManifestError) as exc_info:
        manifest.load()

    assert str(config.state_path) in str(exc_info.value)


def test_manifest_entry_with_float_updated_usec_raises(tmp_path: Path) -> None:
    # JSON has no int/float distinction on the wire; a decimal-looking
    # updated_usec (e.g. "100.0") parses to a Python float, which fails the
    # entry's strict `isinstance(..., int)` check -- treated as malformed,
    # matching the module's "never silently reset" contract for the manifest.
    config = make_config(tmp_path)
    config.state_path.parent.mkdir(parents=True)
    entry = {"path": "x.md", "updated_usec": 100.0, "exported_at": "2026-01-01T00:00:00Z"}
    config.state_path.write_text(json.dumps({"t1": entry}), encoding="utf-8")
    manifest = Manifest(config)

    with pytest.raises(ManifestError) as exc_info:
        manifest.load()

    assert str(config.state_path) in str(exc_info.value)


def test_manifest_entry_tolerates_extra_unknown_keys(tmp_path: Path) -> None:
    # _parse_manifest_entry only reads path/updated_usec/exported_at; an
    # extra key (e.g. from a future schema version) must be ignored, not
    # rejected -- forward-compatible tolerance is the documented intent.
    config = make_config(tmp_path)
    config.state_path.parent.mkdir(parents=True)
    entry = {
        "path": "x.md",
        "updated_usec": 100,
        "exported_at": "2026-01-01T00:00:00Z",
        "future_field": "ignore me",
    }
    config.state_path.write_text(json.dumps({"t1": entry}), encoding="utf-8")
    manifest = Manifest(config)

    manifest.load()

    assert manifest.should_export("t1", 100) is False
    assert manifest.should_export("t1", 200) is True


# --- Manifest: should_export ----------------------------------------------


def test_manifest_skip_unchanged(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")

    assert manifest.should_export("t1", 100) is False


def test_manifest_changed_updated_usec_reexports(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")

    assert manifest.should_export("t1", 200) is True


def test_manifest_force_overrides_unchanged(tmp_path: Path) -> None:
    config = make_config(tmp_path, force=True)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")

    assert manifest.should_export("t1", 100) is True


def test_manifest_should_export_persists_across_reload(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")
    manifest.flush()

    reloaded = Manifest(make_config(tmp_path))
    reloaded.load()

    assert reloaded.should_export("t1", 100) is False
    assert reloaded.should_export("t1", 999) is True


# --- Manifest: path_for ---------------------------------------------------


def test_manifest_path_for_returns_recorded_path(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Foo.md", 100, "2026-01-01T00:00:00Z")

    assert manifest.path_for("t1") == "Private/Foo.md"


def test_manifest_path_for_returns_none_for_unknown_thread(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Foo.md", 100, "2026-01-01T00:00:00Z")

    assert manifest.path_for("missing") is None


def test_manifest_path_for_returns_none_on_empty_manifest(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()

    assert manifest.path_for("anything") is None


def test_manifest_path_for_tracks_re_record(tmp_path: Path) -> None:
    # The exporter overwrites the single per-thread_id slot on a rename
    # re-export; `path_for` must follow that overwrite so the prior path can
    # be cleaned up. This is the lookup `_export_one` relies on.
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Foo.md", 100, "2026-01-01T00:00:00Z")
    manifest.record("t1", "Private/Bar.md", 100, "2026-01-02T00:00:00Z")

    assert manifest.path_for("t1") == "Private/Bar.md"


def test_manifest_path_for_reflects_loaded_state(tmp_path: Path) -> None:
    # `path_for` reads the in-memory entries populated by `load()`, so a
    # reloaded manifest reports the path previously flushed to disk.
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Flushed.md", 100, "2026-01-01T00:00:00Z")
    manifest.flush()

    reloaded = Manifest(make_config(tmp_path))
    reloaded.load()

    assert reloaded.path_for("t1") == "Private/Flushed.md"


# --- Manifest: atomic writes + auto-flush ---------------------------------


def test_manifest_flush_writes_valid_json_no_tmp_litter(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")

    manifest.flush()

    data = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert data == {
        "t1": {"path": "Private/Doc.md", "updated_usec": 100, "exported_at": "2026-01-01T00:00:00Z"}
    }
    assert list(config.state_path.parent.glob("*.tmp")) == []


def test_manifest_flush_failure_leaves_previous_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")
    manifest.flush()
    original_content = config.state_path.read_text(encoding="utf-8")

    manifest.record("t2", "Private/Other.md", 200, "2026-01-01T00:00:00Z")

    def failing_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash between tmp write and replace")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        manifest.flush()

    # The file on disk is exactly what it was before the failed flush.
    assert config.state_path.read_text(encoding="utf-8") == original_content
    # No leftover temp file from the failed attempt.
    assert list(config.state_path.parent.glob("*.tmp")) == []


def test_manifest_flush_recreates_deleted_parent_directory(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")
    manifest.flush()
    assert config.state_path.is_file()

    # Simulate the state directory disappearing mid-run (e.g. a concurrent
    # cleanup or an interrupted `--output` rename); flush() must recreate it
    # rather than crash-looping on the next write.
    shutil.rmtree(config.state_path.parent)

    manifest.record("t2", "Private/Other.md", 200, "2026-01-01T00:00:00Z")
    manifest.flush()

    assert config.state_path.is_file()
    data = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"t1", "t2"}


def test_manifest_auto_flush_default_is_20() -> None:
    assert DEFAULT_FLUSH_EVERY == 20


def test_manifest_auto_flush_triggers_at_threshold(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config, flush_every=3)
    manifest.load()

    manifest.record("t0", "p0.md", 100, "2026-01-01T00:00:00Z")
    manifest.record("t1", "p1.md", 101, "2026-01-01T00:00:00Z")
    assert not config.state_path.exists()

    manifest.record("t2", "p2.md", 102, "2026-01-01T00:00:00Z")

    assert config.state_path.exists()
    data = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"t0", "t1", "t2"}


def test_manifest_auto_flush_at_default_threshold_of_20(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config)
    manifest.load()

    for i in range(19):
        manifest.record(f"t{i}", f"p{i}.md", 100 + i, "2026-01-01T00:00:00Z")
    assert not config.state_path.exists()

    manifest.record("t19", "p19.md", 119, "2026-01-01T00:00:00Z")

    assert config.state_path.exists()
    data = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert len(data) == 20


def test_manifest_explicit_flush_persists_without_reaching_threshold(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manifest = Manifest(config, flush_every=20)
    manifest.load()
    manifest.record("t1", "Private/Doc.md", 100, "2026-01-01T00:00:00Z")

    manifest.flush()

    assert config.state_path.exists()
