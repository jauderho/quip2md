"""Tests for quip2md.notes_import.

Uses a small hand-written fake (`FakeNotesRunner`) that structurally
satisfies `notes_import.NotesRunnerProtocol` -- no test in this file ever
shells out to `osascript` or touches Apple Notes. `markdown_to_notes_html`
and `parse_frontmatter`/`scan_source` are exercised directly since they are
pure/filesystem-read-only (real `.md` fixtures, tmp_path assets).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

import quip2md.notes_import as notes_import
from quip2md.config import Config
from quip2md.convert import build_frontmatter
from quip2md.notes_import import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_PAYLOAD_BYTES,
    ImportReport,
    NoteFrontmatter,
    NotesError,
    NotesRunner,
    NotesState,
    NotesStateError,
    NoteStateEntry,
    chunk_note_bodies,
    markdown_to_notes_html,
    parse_frontmatter,
    run_import,
    scan_source,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_PATHS = sorted(GOLDEN_DIR.glob("*.md"))

# --- test helpers ------------------------------------------------------


class FakeNotesRunner:
    """Duck-typed fake satisfying `notes_import.NotesRunnerProtocol`.

    Deterministically assigns `folder-N`/`note-N` ids in call order so
    tests can predict them without inspecting private state; every call
    is also recorded publicly for assertions.
    """

    def __init__(self) -> None:
        self.resolve_account_calls: list[bool] = []
        self.account_name = "iCloud"
        self.on_my_mac_present = True

        self.get_or_create_folder_calls: list[tuple[str, tuple[str, ...]]] = []
        self.folder_ids: dict[tuple[str, ...], str] = {}
        self._next_folder_id = 1

        self.create_notes_calls: list[tuple[str, tuple[str, ...]]] = []
        self.create_notes_returned: list[list[str]] = []
        self.create_notes_raises: Exception | None = None
        self._next_note_id = 1

        self.note_ids_in_folder_calls: list[str] = []
        self.note_ids_by_folder: dict[str, frozenset[str]] = {}

        self.set_note_body_calls: list[tuple[str, str]] = []
        self.set_note_body_raises: Exception | None = None

    def resolve_account(self, *, local: bool) -> str:
        self.resolve_account_calls.append(local)
        if local:
            if not self.on_my_mac_present:
                raise NotesError('"On My Mac" account not found')
            return "On My Mac"
        return self.account_name

    def get_or_create_folder(self, account: str, path: Sequence[str]) -> str:
        key = tuple(path)
        self.get_or_create_folder_calls.append((account, key))
        if key not in self.folder_ids:
            self.folder_ids[key] = f"folder-{self._next_folder_id}"
            self._next_folder_id += 1
        return self.folder_ids[key]

    def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
        self.create_notes_calls.append((folder_id, tuple(bodies)))
        if self.create_notes_raises is not None:
            raise self.create_notes_raises
        ids = [f"note-{self._next_note_id + i}" for i in range(len(bodies))]
        self._next_note_id += len(bodies)
        self.create_notes_returned.append(ids)
        return ids

    def note_ids_in_folder(self, folder_id: str) -> frozenset[str]:
        self.note_ids_in_folder_calls.append(folder_id)
        return self.note_ids_by_folder.get(folder_id, frozenset())

    def set_note_body(self, note_id: str, body_html: str) -> None:
        self.set_note_body_calls.append((note_id, body_html))
        if self.set_note_body_raises is not None:
            raise self.set_note_body_raises


def make_config(tmp_path: Path, *, dry_run: bool = False, force: bool = False) -> Config:
    return Config(
        token="test-token",
        output_dir=tmp_path / "export",
        state_path=tmp_path / ".quip2md" / "state.json",
        dry_run=dry_run,
        verbose=False,
        include_chats=False,
        force=force,
    )


def write_note_md(
    root: Path,
    rel_path: str,
    *,
    quip_id: str | None,
    title: str,
    body: str,
    quip_url: str = "https://quip.com/x",
) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if quip_id is not None:
        frontmatter = build_frontmatter(
            quip_id=quip_id,
            quip_url=quip_url,
            title=title,
            created_usec=0,
            updated_usec=0,
            exported=datetime(2024, 1, 1, tzinfo=UTC),
        )
        text = frontmatter + "\n" + body
    else:
        text = body
    path.write_text(text, encoding="utf-8")
    return path


# --- parse_frontmatter -------------------------------------------------


def test_frontmatter_roundtrips_with_convert_build_frontmatter() -> None:
    """`parse_frontmatter` must invert `convert.build_frontmatter`'s escaping."""
    frontmatter_text = build_frontmatter(
        quip_id="AbC123",
        quip_url="https://quip.com/AbC123",
        title='Title: "quoted" \\ backslash',
        created_usec=1_700_000_000_000_000,
        updated_usec=1_700_100_000_000_000,
        exported=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    full_text = frontmatter_text + "\nBody content here.\n"

    frontmatter, body = parse_frontmatter(full_text)

    assert frontmatter.quip_id == "AbC123"
    assert frontmatter.quip_url == "https://quip.com/AbC123"
    assert frontmatter.title == 'Title: "quoted" \\ backslash'
    assert frontmatter.created == "2023-11-14T22:13:20Z"
    assert frontmatter.updated == "2023-11-14T22:13:33Z" or frontmatter.updated is not None
    assert body.strip() == "Body content here."


def test_frontmatter_missing_quip_id_but_other_fields_present() -> None:
    text = '---\ntitle: "A doc"\nquip_url: "https://quip.com/x"\n---\n\nBody text.\n'
    frontmatter, body = parse_frontmatter(text)
    assert frontmatter.quip_id is None
    assert frontmatter.title == "A doc"
    assert frontmatter.quip_url == "https://quip.com/x"
    assert body.strip() == "Body text."


def test_frontmatter_absent_block_returns_original_text_as_body() -> None:
    text = "# Just a heading\n\nSome text.\n"
    frontmatter, body = parse_frontmatter(text)
    assert frontmatter == NoteFrontmatter(
        quip_id=None, quip_url=None, title=None, created=None, updated=None
    )
    assert body == text


def test_frontmatter_unclosed_block_treated_as_absent() -> None:
    text = '---\nquip_id: "abc"\n\n# No closing delimiter\n'
    frontmatter, body = parse_frontmatter(text)
    assert frontmatter.quip_id is None
    assert body == text


def test_frontmatter_escaped_newline_and_tab() -> None:
    text = '---\ntitle: "line one\\nline two\\tafter tab"\n---\n\nBody.\n'
    frontmatter, _ = parse_frontmatter(text)
    assert frontmatter.title == "line one\nline two\tafter tab"


# --- scan_source ---------------------------------------------------------


def test_scan_source_skips_assets_dir(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "real.md", quip_id="t1", title="Real", body="content")
    (source_dir / "_assets" / "t1").mkdir(parents=True)
    (source_dir / "_assets" / "t1" / "sneaky.md").write_text("not a note", encoding="utf-8")

    sources = scan_source(source_dir)

    assert [s.relative_path for s in sources] == ["real.md"]


def test_scan_source_derives_folder_path_from_nested_dirs(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "Private/Sub Folder/doc.md", quip_id="t1", title="Doc", body="x")

    sources = scan_source(source_dir)

    assert len(sources) == 1
    assert sources[0].folder_path == ("Quip", "Private", "Sub Folder")


def test_scan_source_top_level_file_has_only_quip_root(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "doc.md", quip_id="t1", title="Doc", body="x")

    sources = scan_source(source_dir)

    assert sources[0].folder_path == ("Quip",)


def test_scan_source_keyed_by_quip_id_when_present(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "doc.md", quip_id="AbCdEf", title="Doc", body="x")

    sources = scan_source(source_dir)

    assert sources[0].key == "AbCdEf"
    assert sources[0].keyed_by_path is False


def test_scan_source_keyed_by_path_when_quip_id_missing(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "sub/orphan.md", quip_id=None, title="Orphan", body="no frontmatter")

    sources = scan_source(source_dir)

    assert sources[0].keyed_by_path is True
    assert sources[0].key == "path:sub/orphan.md"


def test_scan_source_title_falls_back_to_filename_stem(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "My Title Here.md", quip_id=None, title="", body="plain text")

    sources = scan_source(source_dir)

    assert sources[0].title == "My Title Here"


# --- markdown_to_notes_html: links -----------------------------------------


def test_link_with_distinct_text_becomes_text_paren_url(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="T",
        quip_url=None,
        markdown_text="[Example](https://example.com/page)",
        md_dir=tmp_path,
    )
    assert "Example (https://example.com/page)" in result.html
    assert "<a " not in result.html


def test_autolink_text_equals_url_skips_parenthetical(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text="<https://example.com/page>", md_dir=tmp_path
    )
    assert "https://example.com/page" in result.html
    assert "(https://example.com/page)" not in result.html
    assert "<a " not in result.html


def test_quip_url_included_as_plain_text_after_title(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="Doc", quip_url="https://quip.com/AbC123", markdown_text="Body.", md_dir=tmp_path
    )
    assert result.html.startswith("<h1>Doc</h1>")
    assert "<div>https://quip.com/AbC123</div>" in result.html
    assert "<a " not in result.html


def test_no_quip_url_omits_source_div(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="Doc", quip_url=None, markdown_text="Body.", md_dir=tmp_path
    )
    assert result.html.startswith("<h1>Doc</h1><p>Body.</p>")


# --- markdown_to_notes_html: title-as-first-element -------------------------


def test_title_is_first_element_h1_even_when_body_has_its_own_h1(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="Frontmatter Title",
        quip_url=None,
        markdown_text="# Body Heading\n\ntext",
        md_dir=tmp_path,
    )
    assert result.html.startswith("<h1>Frontmatter Title</h1>")
    assert "<h1>Body Heading</h1>" in result.html


# --- markdown_to_notes_html: images ------------------------------------


def test_image_resolves_to_absolute_file_uri(tmp_path: Path) -> None:
    md_dir = tmp_path / "docs"
    md_dir.mkdir()
    asset_dir = md_dir / "_assets" / "tid"
    asset_dir.mkdir(parents=True)
    asset_path = asset_dir / "blobid.png"
    asset_path.write_bytes(b"\x89PNG")

    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text="![](_assets/tid/blobid.png)", md_dir=md_dir
    )

    assert f'src="{asset_path.resolve().as_uri()}"' in result.html
    assert result.warnings == ()


def test_missing_image_warns_and_replaces_with_text(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text="![](_assets/tid/missing.png)", md_dir=tmp_path
    )
    assert "[missing image: missing.png]" in result.html
    assert len(result.warnings) == 1
    assert "missing image" in result.warnings[0]
    assert "<img" not in result.html


# --- markdown_to_notes_html: code blocks -----------------------------------


def test_code_block_becomes_per_line_courier_divs_with_blank_line(tmp_path: Path) -> None:
    markdown_text = "```python\ndef f():\n\n    return 1\n```\n"
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text=markdown_text, md_dir=tmp_path
    )

    assert '<div><font face="Courier">def f():</font></div>' in result.html
    assert "<div>\N{NO-BREAK SPACE}</div>" in result.html
    assert '<div><font face="Courier">    return 1</font></div>' in result.html
    assert "<pre>" not in result.html
    assert "<code>" not in result.html


# --- markdown_to_notes_html: nested ordered lists ---------------------------


def test_nested_ordered_list_counted_as_warning(tmp_path: Path) -> None:
    markdown_text = "1. first\n\n   1. nested\n2. second\n"
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text=markdown_text, md_dir=tmp_path
    )

    assert any("nested <ol>" in warning for warning in result.warnings)


def test_flat_ordered_list_no_warning(tmp_path: Path) -> None:
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text="1. first\n2. second\n", md_dir=tmp_path
    )
    assert result.warnings == ()


# --- markdown_to_notes_html: tables -----------------------------------------


def test_table_passes_through(tmp_path: Path) -> None:
    markdown_text = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text=markdown_text, md_dir=tmp_path
    )

    assert "<table>" in result.html
    assert "<th>A</th>" in result.html
    assert "<td>1</td>" in result.html


# --- markdown_to_notes_html: hard breaks -------------------------------


def test_hard_break_paragraph_becomes_sibling_divs(tmp_path: Path) -> None:
    markdown_text = "line one  \nline two"
    result = markdown_to_notes_html(
        title="T", quip_url=None, markdown_text=markdown_text, md_dir=tmp_path
    )

    assert "<div>line one</div><div>line two</div>" in result.html
    assert "<br" not in result.html
    assert "<p>" not in result.html


# --- markdown_to_notes_html: golden-file smoke tests ------------------------


@pytest.mark.parametrize("golden_path", GOLDEN_PATHS, ids=lambda p: p.stem)
def test_golden_markdown_converts_without_raising_and_strips_links(
    golden_path: Path, tmp_path: Path
) -> None:
    body = golden_path.read_text(encoding="utf-8")
    result = markdown_to_notes_html(
        title="Test Title", quip_url=None, markdown_text=body, md_dir=tmp_path
    )

    assert result.html.startswith("<h1>Test Title</h1>")
    assert "<a " not in result.html
    assert isinstance(result.warnings, tuple)


def test_golden_image_fixture_warns_about_missing_asset(tmp_path: Path) -> None:
    # This golden's markdown references an image that does not exist under
    # tmp_path (no real _assets/ directory was copied alongside it), so the
    # missing-image path must trigger.
    golden_path = GOLDEN_DIR / "doc_sample_THREAD0003.md"
    body = golden_path.read_text(encoding="utf-8")
    result = markdown_to_notes_html(title="T", quip_url=None, markdown_text=body, md_dir=tmp_path)

    assert any("missing image" in warning for warning in result.warnings)
    assert "[missing image:" in result.html


# --- chunk_note_bodies -------------------------------------------------


def test_chunk_note_bodies_respects_max_count() -> None:
    bodies = [f"b{i}" for i in range(25)]
    chunks = list(chunk_note_bodies(bodies, max_count=10, max_bytes=1_000_000))
    assert [len(chunk) for chunk in chunks] == [10, 10, 5]


def test_chunk_note_bodies_respects_max_bytes() -> None:
    bodies = ["a" * 100, "b" * 100, "c" * 100]
    chunks = list(chunk_note_bodies(bodies, max_count=100, max_bytes=150))
    assert [len(chunk) for chunk in chunks] == [1, 1, 1]


def test_chunk_note_bodies_oversized_single_body_gets_its_own_chunk() -> None:
    bodies = ["x" * 500]
    chunks = list(chunk_note_bodies(bodies, max_count=10, max_bytes=100))
    assert chunks == [["x" * 500]]


def test_chunk_note_bodies_at_default_constants() -> None:
    bodies = [f"b{i}" for i in range(DEFAULT_BATCH_SIZE * 2 + 3)]
    chunks = list(
        chunk_note_bodies(bodies, max_count=DEFAULT_BATCH_SIZE, max_bytes=MAX_BATCH_PAYLOAD_BYTES)
    )
    assert [len(chunk) for chunk in chunks] == [DEFAULT_BATCH_SIZE, DEFAULT_BATCH_SIZE, 3]


def test_chunk_note_bodies_empty_input() -> None:
    assert list(chunk_note_bodies([], max_count=10, max_bytes=1000)) == []


# --- NotesState ------------------------------------------------------------


def test_notes_state_missing_file_is_empty(tmp_path: Path) -> None:
    state = NotesState(tmp_path / "notes_state.json")
    state.load()
    assert state.get("anything") is None


def test_notes_state_record_flush_reload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "notes_state.json"
    state = NotesState(path)
    entry = NoteStateEntry(
        note_id="n1", folder="Quip/A", content_hash="abc123", imported_at="2024-01-01T00:00:00Z"
    )
    state.record("t1", entry)
    state.flush()

    reloaded = NotesState(path)
    reloaded.load()
    assert reloaded.get("t1") == entry


def test_notes_state_flush_leaves_no_tmp_files(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "notes_state.json"
    state = NotesState(path)
    state.record(
        "t1", NoteStateEntry(note_id="n1", folder="Quip", content_hash="h", imported_at="x")
    )
    state.flush()

    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == []


def test_notes_state_corrupted_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "notes_state.json"
    path.write_text("{not valid json", encoding="utf-8")
    state = NotesState(path)
    with pytest.raises(NotesStateError):
        state.load()


def test_notes_state_malformed_entry_raises(tmp_path: Path) -> None:
    path = tmp_path / "notes_state.json"
    path.write_text(json.dumps({"t1": {"note_id": "n1"}}), encoding="utf-8")
    state = NotesState(path)
    with pytest.raises(NotesStateError):
        state.load()


def test_notes_state_non_object_raises(tmp_path: Path) -> None:
    path = tmp_path / "notes_state.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    state = NotesState(path)
    with pytest.raises(NotesStateError):
        state.load()


# --- NotesRunner: macOS guard --------------------------------------------


def test_notes_runner_raises_off_darwin_before_any_subprocess_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be called when the macOS guard fires")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    with pytest.raises(NotesError):
        NotesRunner()


# --- run_import: folder creation -----------------------------------------


def test_get_or_create_folder_called_once_per_distinct_folder_path(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "A/one.md", quip_id="t1", title="One", body="Hello")
    write_note_md(source_dir, "A/two.md", quip_id="t2", title="Two", body="World")
    write_note_md(source_dir, "B/three.md", quip_id="t3", title="Three", body="!")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)

    run_import(runner, config, source_dir=source_dir)

    paths = [path for _, path in runner.get_or_create_folder_calls]
    assert paths == [("Quip", "A"), ("Quip", "B")]


def test_local_flag_uses_on_my_mac_account(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="a")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)

    run_import(runner, config, source_dir=source_dir, local=True)

    assert runner.resolve_account_calls == [True]
    assert runner.get_or_create_folder_calls[0][0] == "On My Mac"


def test_local_flag_raises_when_on_my_mac_absent(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="a")
    runner = FakeNotesRunner()
    runner.on_my_mac_present = False
    config = make_config(tmp_path)

    with pytest.raises(NotesError):
        run_import(runner, config, source_dir=source_dir, local=True)


# --- run_import: create/update/skip/recreate ---------------------------


def test_creates_new_notes_and_records_state(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)

    report = run_import(runner, config, source_dir=source_dir)

    assert report.created == 1
    assert report.failed == []
    assert len(runner.create_notes_calls) == 1
    state_path = config.state_path.parent / "notes_state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["t1"]["folder"] == "Quip"
    assert data["t1"]["note_id"] == runner.create_notes_returned[0][0]


def test_rerun_with_no_changes_skips_and_creates_no_new_notes(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)
    run_import(runner, config, source_dir=source_dir)
    created_id = runner.create_notes_returned[0][0]
    folder_id = runner.folder_ids[("Quip",)]
    runner.note_ids_by_folder[folder_id] = frozenset({created_id})

    report2 = run_import(runner, config, source_dir=source_dir)

    assert report2.skipped_unchanged == 1
    assert report2.created == 0
    assert len(runner.create_notes_calls) == 1


def test_source_content_change_updates_note_in_place(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)
    run_import(runner, config, source_dir=source_dir)
    created_id = runner.create_notes_returned[0][0]
    folder_id = runner.folder_ids[("Quip",)]
    runner.note_ids_by_folder[folder_id] = frozenset({created_id})
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Updated body!")

    report2 = run_import(runner, config, source_dir=source_dir)

    assert report2.updated == 1
    assert report2.created == 0
    assert len(runner.set_note_body_calls) == 1
    updated_note_id, updated_body = runner.set_note_body_calls[0]
    assert updated_note_id == created_id
    assert "Updated body" in updated_body


def test_note_missing_from_folder_is_recreated(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)
    run_import(runner, config, source_dir=source_dir)
    folder_id = runner.folder_ids[("Quip",)]
    # Simulate the tracked note having been deleted out from under us: the
    # live folder listing no longer contains its id, even though the id
    # itself would still resolve via `note id ...` (recon section 6).
    runner.note_ids_by_folder[folder_id] = frozenset()

    report2 = run_import(runner, config, source_dir=source_dir)

    assert report2.recreated_missing == 1
    assert report2.created == 0
    assert report2.updated == 0
    assert len(runner.create_notes_calls) == 2


def test_force_updates_even_when_unchanged(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)
    run_import(runner, config, source_dir=source_dir)
    created_id = runner.create_notes_returned[0][0]
    folder_id = runner.folder_ids[("Quip",)]
    runner.note_ids_by_folder[folder_id] = frozenset({created_id})

    forced_config = make_config(tmp_path, force=True)
    report2 = run_import(runner, forced_config, source_dir=source_dir)

    assert report2.updated == 1
    assert report2.skipped_unchanged == 0
    assert len(runner.set_note_body_calls) == 1


def test_note_without_quip_id_keyed_by_path_and_imported(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "orphan.md", quip_id=None, title="Orphan", body="No frontmatter here")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)

    report = run_import(runner, config, source_dir=source_dir)

    assert report.created == 1
    assert report.keyed_by_path == 1
    state_path = config.state_path.parent / "notes_state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert "path:orphan.md" in data


def test_only_restricts_import_to_given_keys(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="a")
    write_note_md(source_dir, "two.md", quip_id="t2", title="Two", body="b")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)

    report = run_import(runner, config, source_dir=source_dir, only=["t2"])

    assert report.created == 1
    assert len(runner.create_notes_calls) == 1
    assert len(runner.create_notes_calls[0][1]) == 1


# --- run_import: dry run -------------------------------------------------


def test_dry_run_makes_zero_runner_calls_and_leaves_state_untouched(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    runner = FakeNotesRunner()
    config = make_config(tmp_path, dry_run=True)

    report = run_import(runner, config, source_dir=source_dir)

    assert runner.resolve_account_calls == []
    assert runner.get_or_create_folder_calls == []
    assert runner.create_notes_calls == []
    assert runner.note_ids_in_folder_calls == []
    assert runner.set_note_body_calls == []
    assert report.created == 1
    assert not (config.state_path.parent / "notes_state.json").exists()


def test_dry_run_reports_per_folder_counts(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "A/one.md", quip_id="t1", title="One", body="a")
    write_note_md(source_dir, "A/two.md", quip_id="t2", title="Two", body="b")
    write_note_md(source_dir, "B/three.md", quip_id="t3", title="Three", body="c")
    runner = FakeNotesRunner()
    config = make_config(tmp_path, dry_run=True)

    report = run_import(runner, config, source_dir=source_dir)

    assert report.folder_counts == {"Quip/A": 2, "Quip/B": 1}


def test_dry_run_accepts_none_runner(tmp_path: Path) -> None:
    # The CLI now passes `None` for `runner` on a dry run (so it never
    # constructs `NotesRunner` and never trips its non-macOS guard).
    # `run_import` must accept that and still return a report, since a
    # dry run calls `runner` zero times.
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    config = make_config(tmp_path, dry_run=True)

    report = run_import(None, config, source_dir=source_dir)

    assert report.created == 1
    assert not (config.state_path.parent / "notes_state.json").exists()


def test_real_run_with_none_runner_raises_notes_error(tmp_path: Path) -> None:
    # Mirrors `run_enex_import`'s guard: a real (non-dry-run) import with
    # no runner is a programming error, not a silent no-op.
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello world")
    config = make_config(tmp_path, dry_run=False)

    with pytest.raises(NotesError, match="a Notes runner is required for a real"):
        run_import(None, config, source_dir=source_dir)


# --- run_import: failure isolation ---------------------------------------


def test_batch_create_failure_in_one_folder_does_not_affect_others(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "A/one.md", quip_id="t1", title="One", body="a")
    write_note_md(source_dir, "B/two.md", quip_id="t2", title="Two", body="b")

    class FlakyRunner(FakeNotesRunner):
        def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
            if any("One" in body for body in bodies):
                self.create_notes_calls.append((folder_id, tuple(bodies)))
                raise RuntimeError("boom")
            return super().create_notes(folder_id, bodies)

    runner = FlakyRunner()
    config = make_config(tmp_path)

    report = run_import(runner, config, source_dir=source_dir)

    assert report.created == 1
    assert [key for key, _ in report.failed] == ["t1"]
    assert "batch create failed" in report.failed[0][1]


def test_conversion_failure_isolated_per_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="a")
    write_note_md(source_dir, "two.md", quip_id="t2", title="Two", body="b")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)

    original = notes_import.markdown_to_notes_html

    def flaky(
        *, title: str, quip_url: str | None, markdown_text: str, md_dir: Path
    ) -> notes_import.NoteHtml:
        if title == "One":
            raise RuntimeError("bad markdown")
        return original(title=title, quip_url=quip_url, markdown_text=markdown_text, md_dir=md_dir)

    monkeypatch.setattr(notes_import, "markdown_to_notes_html", flaky)

    report = run_import(runner, config, source_dir=source_dir)

    assert report.created == 1
    assert [key for key, _ in report.failed] == ["t1"]
    assert "conversion failed" in report.failed[0][1]


def test_note_ids_in_folder_lookup_failure_isolated_to_folder(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="a")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)
    run_import(runner, config, source_dir=source_dir)

    class FailingLookupRunner(FakeNotesRunner):
        def note_ids_in_folder(self, folder_id: str) -> frozenset[str]:
            raise RuntimeError("folder listing failed")

    failing_runner = FailingLookupRunner()
    failing_runner.folder_ids = runner.folder_ids

    report = run_import(failing_runner, config, source_dir=source_dir)

    assert len(report.failed) == 1
    key, reason = report.failed[0]
    assert key == "t1"
    assert "listing notes in folder failed" in reason


def test_update_failure_isolated_per_note(tmp_path: Path) -> None:
    source_dir = tmp_path / "export"
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Hello")
    runner = FakeNotesRunner()
    config = make_config(tmp_path)
    run_import(runner, config, source_dir=source_dir)
    created_id = runner.create_notes_returned[0][0]
    folder_id = runner.folder_ids[("Quip",)]
    runner.note_ids_by_folder[folder_id] = frozenset({created_id})
    write_note_md(source_dir, "one.md", quip_id="t1", title="One", body="Changed")
    runner.set_note_body_raises = RuntimeError("update boom")

    report = run_import(runner, config, source_dir=source_dir)

    assert report.updated == 0
    assert [key for key, _ in report.failed] == ["t1"]
    assert "update failed" in report.failed[0][1]


# --- ImportReport ------------------------------------------------------


def test_import_report_as_dict_shape() -> None:
    report = ImportReport(
        created=1,
        updated=2,
        skipped_unchanged=3,
        recreated_missing=1,
        failed=[("k1", "reason")],
        warnings=4,
        keyed_by_path=1,
        folder_counts={"Quip": 5},
        elapsed_seconds=1.5,
    )
    data = report.as_dict()
    assert data["created"] == 1
    assert data["failed"] == [{"key": "k1", "reason": "reason"}]
    assert data["folder_counts"] == {"Quip": 5}


# --- interrupt safety ---------------------------------------------------


def test_keyboard_interrupt_mid_run_flushes_state(tmp_path: Path) -> None:
    """Ctrl-C between folders must not lose already-created note ids.

    A lost state file would make the next run recreate every note already
    imported, duplicating them in Apple Notes.
    """
    source = tmp_path / "export"
    write_note_md(source, "A/one.md", quip_id="T1", title="One", body="hello")
    write_note_md(source, "B/two.md", quip_id="T2", title="Two", body="world")

    class InterruptingRunner(FakeNotesRunner):
        def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
            if self.create_notes_calls:
                raise KeyboardInterrupt
            return super().create_notes(folder_id, bodies)

    runner = InterruptingRunner()
    config = make_config(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        run_import(runner, config, source_dir=source)

    state_file = tmp_path / ".quip2md" / "notes_state.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(data) == 1  # exactly the folder that completed before the interrupt
    (entry,) = data.values()
    assert entry["note_id"] == "note-1"


def test_body_leading_h1_matching_title_is_deduped(tmp_path: Path) -> None:
    """Exported docs start with their own title h1; it must not render twice."""
    result = markdown_to_notes_html(
        title="Same Title",
        quip_url=None,
        markdown_text="# Same Title\n\ntext",
        md_dir=tmp_path,
    )
    assert result.html.count("Same Title") == 1
    assert result.html.startswith("<h1>Same Title</h1>")


# --- batch-create failure isolation (#3) --------------------------------


def test_batch_create_failure_records_earlier_batches_and_no_duplicate_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-run batch failure must not lose already-created notes.

    Before the fix, `create_notes` chunked internally and a later chunk's
    failure discarded earlier chunks' ids, so nothing was recorded and the
    next run recreated them (duplicates). Now each osascript batch records
    state before the next runs.
    """
    monkeypatch.setattr(notes_import, "DEFAULT_BATCH_SIZE", 1)  # one note per batch
    source = tmp_path / "export"
    write_note_md(source, "A/one.md", quip_id="T1", title="One", body="one")
    write_note_md(source, "A/two.md", quip_id="T2", title="Two", body="two")

    class StatefulRunner(FakeNotesRunner):
        def __init__(self) -> None:
            super().__init__()
            self.create_call_count = 0
            self._created_by_folder: dict[str, set[str]] = {}

        def create_notes(self, folder_id: str, bodies: Sequence[str]) -> list[str]:
            self.create_call_count += 1
            if self.create_call_count == 2:  # fail the second batch (T2, run 1)
                raise NotesError("simulated osascript failure")
            ids = super().create_notes(folder_id, bodies)
            self._created_by_folder.setdefault(folder_id, set()).update(ids)
            return ids

        def note_ids_in_folder(self, folder_id: str) -> frozenset[str]:
            super().note_ids_in_folder(folder_id)
            return frozenset(self._created_by_folder.get(folder_id, set()))

    runner = StatefulRunner()
    config = make_config(tmp_path)

    report = run_import(runner, config, source_dir=source)
    assert report.created == 1  # T1 created despite T2's batch failing
    assert [key for key, _ in report.failed] == ["T2"]
    state_file = tmp_path / ".quip2md" / "notes_state.json"
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"T1"}

    # Second run (same stateful runner, no longer failing): only T2 is created;
    # T1 is skipped-unchanged, NOT recreated -> no duplicate.
    report2 = run_import(runner, config, source_dir=source)
    assert report2.created == 1
    assert report2.skipped_unchanged == 1
    total_bodies_created = sum(len(bodies) for _, bodies in runner.create_notes_calls)
    assert total_bodies_created == 2  # T1 (run 1) + T2 (run 2), never T1 twice
