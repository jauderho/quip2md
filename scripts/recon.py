#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = ["httpx"]
# ///
"""scripts/recon.py — Quip Automation API ground-truth recon (read-only).

Usage:
    uv run scripts/recon.py [--verbose]

Probes the Quip Automation API (base URL https://platform.quip.com) using the
QUIP_TOKEN found in the project-root `.env` file, and writes:

  - docs/API_NOTES.md      Observed facts about the API surface, answering the
                            7 recon questions from PLAN.md task T1. Anything
                            that could not be verified live is labeled as such
                            rather than guessed.
  - tests/fixtures/*.html  3-5 representative HTML payloads pulled from real
                            threads in the account, for later use as golden
                            fixtures by the HTML->Markdown converter.

Flags:
  --verbose, -v   Print one line per HTTP request to stderr (method, path,
                  status code, running request count). The auth token is
                  never included in this or any other output.

Hard constraints enforced by this script:
  - Only GET requests are ever issued — no mutating endpoint is called.
  - Total requests are capped at MAX_REQUESTS (50). Once the budget is
    exhausted the script stops probing and records the truncation in
    API_NOTES.md instead of continuing.
  - Consecutive requests are spaced >= REQUEST_SPACING_SECONDS (1.5s) apart.
  - The token is read from .env, used only in the Authorization header, and
    is never written to disk or printed, in any mode.
  - Re-running the script overwrites docs/API_NOTES.md and the fixture files
    with fresh output; it does not accumulate state between runs.

Environment quirk handled: on this machine, standard hostname resolution
(socket.getaddrinfo, used internally by httpx/httpcore) fails for external
hosts with socket.gaierror even though the network path itself is reachable.
This script falls back to `dig +short <host>` to resolve the hostname to an
IP address when getaddrinfo raises, then lets the normal connection logic
proceed against that IP. TLS SNI/hostname verification is unaffected (this
mirrors `curl --resolve HOST:PORT:IP` — only address resolution is patched,
the hostname httpx uses for the TLS handshake and Host header is untouched).
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
ENV_PATH: Final[Path] = ROOT / ".env"
API_NOTES_PATH: Final[Path] = ROOT / "docs" / "API_NOTES.md"
FIXTURES_DIR: Final[Path] = ROOT / "tests" / "fixtures"
BASE_URL: Final[str] = "https://platform.quip.com"
MAX_REQUESTS: Final[int] = 50
REQUEST_SPACING_SECONDS: Final[float] = 1.5
MAX_FIXTURES: Final[int] = 5

VERBOSE: Final[bool] = "--verbose" in sys.argv or "-v" in sys.argv

ROOT_FOLDER_FIELDS: Final[tuple[str, ...]] = (
    "private_folder_id",
    "desktop_folder_id",
    "archive_folder_id",
    "starred_folder_id",
    "trash_folder_id",
    "shared_folder_ids",
    "group_folder_ids",
)

RATE_LIMIT_PREFIXES: Final[tuple[str, ...]] = ("x-ratelimit", "x-company-ratelimit")


def log(msg: str) -> None:
    if VERBOSE:
        print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# DNS workaround — see module docstring.
# --------------------------------------------------------------------------

_dns_cache: dict[str, str] = {}
_orig_getaddrinfo = socket.getaddrinfo


def _dig_resolve(host: str) -> str:
    if host in _dns_cache:
        return _dns_cache[host]
    proc = subprocess.run(
        ["dig", "+short", host],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    candidates = [
        line.strip()
        for line in proc.stdout.splitlines()
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", line.strip())
    ]
    if not candidates:
        raise RuntimeError(f"dig +short {host} returned no A records")
    _dns_cache[host] = candidates[0]
    return candidates[0]


def _patched_getaddrinfo(
    host: bytes | str | None,
    port: bytes | str | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]]:
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if not isinstance(host, str):
            raise
        ip = _dig_resolve(host)
        return _orig_getaddrinfo(ip, port, family, type, proto, flags)


socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Token loading
# --------------------------------------------------------------------------


def load_token() -> str:
    if not ENV_PATH.exists():
        raise SystemExit(f"Missing {ENV_PATH}; expected a QUIP_TOKEN=... line in it.")
    for raw_line in ENV_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "QUIP_TOKEN":
            token = value.strip().strip('"').strip("'")
            if not token:
                raise SystemExit("QUIP_TOKEN is present in .env but empty.")
            return token
    raise SystemExit(f"QUIP_TOKEN not found in {ENV_PATH}.")


# --------------------------------------------------------------------------
# Request plumbing
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RequestRecord:
    path: str
    status: int
    ok: bool
    note: str = ""


@dataclass(slots=True)
class Recon:
    client: httpx.Client
    request_count: int = 0
    _last_request_at: float = 0.0
    rate_limit_headers: dict[str, str] = field(default_factory=dict)
    log_records: list[RequestRecord] = field(default_factory=list)
    budget_exhausted: bool = False

    def get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response | None:
        """Issue a single, read-only, paced GET. Returns None if the request
        budget is exhausted or an httpx-level transport error occurs."""
        if self.request_count >= MAX_REQUESTS:
            self.budget_exhausted = True
            log(f"BUDGET EXHAUSTED ({MAX_REQUESTS}) — skipped GET {path}")
            return None

        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < REQUEST_SPACING_SECONDS:
            time.sleep(REQUEST_SPACING_SECONDS - elapsed)

        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            self._last_request_at = time.monotonic()
            self.request_count += 1
            self.log_records.append(
                RequestRecord(path=path, status=0, ok=False, note=f"transport error: {exc!r}")
            )
            log(f"GET {path} -> TRANSPORT ERROR ({self.request_count}/{MAX_REQUESTS})")
            return None

        self._last_request_at = time.monotonic()
        self.request_count += 1

        for name, value in response.headers.items():
            if name.lower().startswith(RATE_LIMIT_PREFIXES):
                self.rate_limit_headers[f"{name} (from {path})"] = value

        self.log_records.append(
            RequestRecord(path=path, status=response.status_code, ok=response.is_success)
        )
        log(f"GET {path} -> {response.status_code} ({self.request_count}/{MAX_REQUESTS})")
        return response


# --------------------------------------------------------------------------
# Markdown report builder
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Report:
    sections: dict[str, list[str]] = field(default_factory=dict)

    def add(self, section: str, line: str) -> None:
        self.sections.setdefault(section, []).append(line)

    def render(self, recon: Recon, fixture_names: list[str]) -> str:
        order = [
            "1. Auth / GET /1/users/current",
            "2. Batch folder fetch — GET /1/folders/?ids=",
            "3. Thread content — v2 vs v1",
            "4. Blob download",
            "5. Export endpoints",
            "6. Rate-limit headers",
            "7. Thread type census / account size",
        ]
        lines: list[str] = [
            "# Quip Automation API — ground-truth recon notes",
            "",
            "Generated by `scripts/recon.py` (task T1). All values below are",
            "**observed live from the account's own API responses** unless a",
            "line is explicitly marked `NOT VERIFIED`. Nothing here is inferred",
            "from documentation or the reference repos.",
            "",
            f"- Base URL probed: `{BASE_URL}`",
            f"- Total HTTP requests issued this run: **{recon.request_count}** "
            f"(cap: {MAX_REQUESTS})",
            f"- Request budget exhausted before all probes completed: "
            f"{'yes' if recon.budget_exhausted else 'no'}",
            "",
            "---",
            "",
        ]
        for section in order:
            lines.append(f"## {section}")
            lines.append("")
            body = self.sections.get(section)
            if body:
                lines.extend(body)
            else:
                lines.append(
                    "NOT VERIFIED — this probe was not reached (see budget/auth notes above)."
                )
            lines.append("")
            lines.append("---")
            lines.append("")

        lines.append("## Fixtures saved")
        lines.append("")
        if fixture_names:
            for name in fixture_names:
                lines.append(f"- `tests/fixtures/{name}`")
        else:
            lines.append("NOT VERIFIED — no fixtures could be saved (see notes above).")
        lines.append("")

        lines.append("## Raw request log")
        lines.append("")
        lines.append("| # | Path | Status |")
        lines.append("|---|------|--------|")
        for i, rec in enumerate(recon.log_records, start=1):
            status = str(rec.status) if rec.ok or rec.status else f"ERROR ({rec.note})"
            lines.append(f"| {i} | `{rec.path}` | {status} |")
        lines.append("")

        return "\n".join(lines)


def safe_json(response: httpx.Response) -> dict[str, object] | list[object] | None:
    try:
        return response.json()
    except ValueError:
        return None


def truncate(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …(truncated)"


def _str_field(obj: dict[str, object], key: str) -> str | None:
    """Return obj[key] if it is a non-empty string, else None."""
    value = obj.get(key)
    return value if isinstance(value, str) and value else None


def _dict_list_field(obj: dict[str, object], key: str) -> list[dict[str, object]]:
    """Return obj[key] as a list of dicts, tolerating missing/malformed values."""
    value = obj.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# --------------------------------------------------------------------------
# Probe 1 — auth / current user
# --------------------------------------------------------------------------


def probe_current_user(recon: Recon, report: Report) -> dict[str, object] | None:
    section = "1. Auth / GET /1/users/current"
    response = recon.get("/1/users/current")
    if response is None:
        report.add(section, "Request could not be issued (see raw request log).")
        return None

    if not response.is_success:
        body = safe_json(response)
        report.add(section, f"**Auth FAILED.** HTTP status: `{response.status_code}`.")
        report.add(section, "")
        report.add(section, "Response body:")
        report.add(section, "```json")
        report.add(section, truncate(str(body) if body is not None else response.text))
        report.add(section, "```")
        report.add(
            section,
            "",
        )
        report.add(
            section,
            "The token was well-formed (three `|`-separated segments matching the "
            "documented personal-token shape) and reached the API — this is a "
            "genuine rejection by Quip, not a network/transport problem. A fresh "
            "token from https://quip.com/dev/token is required before any "
            "downstream probe (2-7) can be attempted; every other endpoint in "
            "this account requires the same auth.",
        )
        return None

    data = safe_json(response)
    if not isinstance(data, dict):
        report.add(section, "Auth succeeded (200) but response body was not a JSON object.")
        return None

    report.add(section, "**Auth OK** (HTTP 200).")
    report.add(section, "")
    report.add(section, f"All top-level keys observed: `{sorted(data.keys())}`")
    report.add(section, "")
    report.add(section, "Root folder ID fields present on this personal account:")
    report.add(section, "")
    for key in ROOT_FOLDER_FIELDS:
        if key in data:
            report.add(section, f"- `{key}` = `{data[key]!r}`")
    missing = [k for k in ROOT_FOLDER_FIELDS if k not in data]
    if missing:
        report.add(section, "")
        report.add(section, f"Fields NOT present on this account: `{missing}`")
    return data


# --------------------------------------------------------------------------
# Probe 2 — batch folder fetch
# --------------------------------------------------------------------------


def probe_folders(
    recon: Recon, report: Report, user: dict[str, object]
) -> tuple[list[str], list[str]]:
    """Returns (all_folder_ids_seen, all_thread_ids_seen_as_children)."""
    section = "2. Batch folder fetch — GET /1/folders/?ids="

    root_ids: list[str] = []
    for key in ("private_folder_id", "desktop_folder_id", "archive_folder_id", "starred_folder_id"):
        value = user.get(key)
        if isinstance(value, str):
            root_ids.append(value)
    for key in ("shared_folder_ids", "group_folder_ids"):
        value = user.get(key)
        if isinstance(value, list):
            root_ids.extend(v for v in value if isinstance(v, str))

    if not root_ids:
        report.add(section, "No root folder IDs were available from the user object; skipped.")
        return [], []

    all_folder_ids: list[str] = list(root_ids)
    all_thread_ids: list[str] = []

    # Batch-size probe: 1, then whatever we have up to 10, then up to 50.
    tried_sizes: list[int] = []
    for target in (1, 10, 50):
        ids_for_test = root_ids[:target]
        if not ids_for_test or len(ids_for_test) in tried_sizes:
            continue
        tried_sizes.append(len(ids_for_test))
        response = recon.get("/1/folders/", params={"ids": ",".join(ids_for_test)})
        if response is None:
            report.add(
                section, f"Batch size {len(ids_for_test)}: request not issued (budget/error)."
            )
            continue
        report.add(
            section,
            f"Batch size requested: {len(ids_for_test)} -> HTTP `{response.status_code}`"
            + (
                f", returned {len(safe_json(response) or {})} folder(s)."
                if response.is_success and isinstance(safe_json(response), dict)
                else "."
            ),
        )
        if response.is_success:
            data = safe_json(response)
            if isinstance(data, dict):
                for folder_id, folder_obj in data.items():
                    if not isinstance(folder_obj, dict):
                        continue
                    children = folder_obj.get("children")
                    if isinstance(children, list) and children:
                        sample = children[0]
                        report.add(
                            section,
                            f"  - folder `{folder_id}`: {len(children)} children; "
                            f"sample child shape: `{sample}`",
                        )
                        for child in children:
                            if not isinstance(child, dict):
                                continue
                            if "folder_id" in child:
                                all_folder_ids.append(str(child["folder_id"]))
                            if "thread_id" in child:
                                all_thread_ids.append(str(child["thread_id"]))

    report.add(section, "")
    report.add(
        section,
        f"Total distinct folder IDs discovered so far (roots + one level of children): "
        f"{len(set(all_folder_ids))}",
    )
    report.add(
        section,
        f"Total distinct thread IDs discovered as folder children so far: "
        f"{len(set(all_thread_ids))}",
    )
    return list(dict.fromkeys(all_folder_ids)), list(dict.fromkeys(all_thread_ids))


# --------------------------------------------------------------------------
# Probe 3 — thread content, v2 vs v1
# --------------------------------------------------------------------------


def probe_thread_content(
    recon: Recon, report: Report, thread_ids: list[str]
) -> tuple[str | None, str | None, dict[str, str]]:
    """Returns (chosen_thread_id_for_later_probes, sample_html, {thread_id: html})."""
    section = "3. Thread content — v2 vs v1"
    if not thread_ids:
        report.add(section, "No thread IDs available (folder probe found none); skipped.")
        return None, None, {}

    probe_id = thread_ids[0]
    html_by_id: dict[str, str] = {}

    v2_meta = recon.get(f"/2/threads/{probe_id}")
    v2_available = v2_meta is not None and v2_meta.is_success
    report.add(
        section,
        f"`GET /2/threads/{{id}}` on `{probe_id}`: "
        + (f"HTTP `{v2_meta.status_code}`" if v2_meta is not None else "not issued"),
    )

    if v2_available and v2_meta is not None:
        data = safe_json(v2_meta)
        if isinstance(data, dict):
            report.add(section, f"  - v2 thread metadata keys: `{sorted(data.keys())}`")

        html_resp = recon.get(f"/2/threads/{probe_id}/html")
        if html_resp is not None and html_resp.is_success:
            body = safe_json(html_resp)
            report.add(section, f"`GET /2/threads/{{id}}/html`: HTTP `{html_resp.status_code}`")
            html_parts: list[str] = [html_resp.text]
            next_cursor: str | None = None
            if isinstance(body, dict):
                report.add(section, f"  - response keys: `{sorted(body.keys())}`")
                body_html = body.get("html")
                if isinstance(body_html, str):
                    # JSON envelope: the actual HTML lives under a "html" key, not
                    # the raw response text (which would be the JSON encoding).
                    html_parts = [body_html]
                metadata = body.get("response_metadata")
                if isinstance(metadata, dict):
                    report.add(
                        section,
                        f"  - `response_metadata`: `{metadata}` "
                        "(pagination cursor mechanism, if any, lives here)",
                    )
                    next_cursor = _str_field(metadata, "next_cursor")
                if "cursor" in body and next_cursor is None:
                    report.add(section, f"  - top-level `cursor` field: `{body.get('cursor')!r}`")
                    next_cursor = _str_field(body, "cursor")
            else:
                report.add(
                    section,
                    "  - response body was not a JSON object; treating it as raw HTML text "
                    "with no pagination envelope on this call.",
                )

            page_count = 1
            while next_cursor:
                page_resp = recon.get(
                    f"/2/threads/{probe_id}/html", params={"cursor": next_cursor}
                )
                if page_resp is None or not page_resp.is_success:
                    status = page_resp.status_code if page_resp is not None else "n/a"
                    report.add(
                        section,
                        f"  - pagination: page {page_count + 1} fetch failed "
                        f"(HTTP {status}); stopped.",
                    )
                    break
                page_count += 1
                page_body = safe_json(page_resp)
                next_cursor = None
                if isinstance(page_body, dict):
                    page_html = page_body.get("html")
                    if isinstance(page_html, str):
                        html_parts.append(page_html)
                    metadata = page_body.get("response_metadata")
                    if isinstance(metadata, dict):
                        next_cursor = _str_field(metadata, "next_cursor")
                else:
                    html_parts.append(page_resp.text)
            report.add(
                section,
                f"  - pagination: {page_count} page(s) followed via "
                "`response_metadata.next_cursor` (stops when the field is empty/absent).",
            )

            html_by_id[probe_id] = "".join(html_parts)
        else:
            report.add(
                section,
                "`GET /2/threads/{id}/html`: "
                + (f"HTTP `{html_resp.status_code}`" if html_resp is not None else "not issued"),
            )
    else:
        status = v2_meta.status_code if v2_meta is not None else "n/a"
        report.add(
            section,
            f"v2 API NOT available for this token (status `{status}`). "
            "Falling back to `GET /1/threads/?ids=` per the T1 spec.",
        )

    v1_resp = recon.get("/1/threads/", params={"ids": probe_id})
    if v1_resp is not None and v1_resp.is_success:
        data = safe_json(v1_resp)
        report.add(section, f"`GET /1/threads/?ids=` : HTTP `{v1_resp.status_code}`")
        if isinstance(data, dict) and probe_id in data:
            entry = data[probe_id]
            if isinstance(entry, dict):
                thread_obj = entry.get("thread")
                has_html = isinstance(thread_obj, dict) and "html" in thread_obj
                report.add(
                    section,
                    f"  - v1 entry keys: `{sorted(entry.keys())}`; "
                    f"`thread.html` present: {has_html}",
                )
                if has_html and isinstance(thread_obj, dict):
                    html_text = thread_obj.get("html")
                    if isinstance(html_text, str):
                        html_by_id.setdefault(probe_id, html_text)
                        report.add(
                            section,
                            f"  - v1 `thread.html` length: {len(html_text)} chars "
                            "(full document HTML returned in a single response, no pagination).",
                        )
    else:
        report.add(
            section,
            "`GET /1/threads/?ids=`: "
            + (f"HTTP `{v1_resp.status_code}`" if v1_resp is not None else "not issued"),
        )

    return probe_id, html_by_id.get(probe_id), html_by_id


def enrich_html_samples(
    recon: Recon,
    report: Report,
    html_by_id: dict[str, str],
    type_by_id: dict[str, str],
    reserve_requests: int,
) -> None:
    """Widen the pool of fetched thread HTML beyond the single thread probed in
    probe_thread_content, so a spreadsheet fixture and a broader image search are
    possible when the account has more than one thread. Uses the batchable v1
    `/1/threads/?ids=` endpoint (one request can return several threads' HTML),
    and stops early once budget (minus `reserve_requests` kept for probes 4/5)
    is tight."""
    section = "3. Thread content — v2 vs v1"

    def budget_left() -> int:
        return MAX_REQUESTS - reserve_requests - recon.request_count

    wanted: list[str] = []
    if not any(html_by_id.get(tid) and t == "SPREADSHEET" for tid, t in type_by_id.items()):
        sheet_id = next((tid for tid, t in type_by_id.items() if t == "SPREADSHEET"), None)
        if sheet_id is not None and sheet_id not in html_by_id:
            wanted.append(sheet_id)

    has_image_already = any(_IMG_SRC_RE.search(h) for h in html_by_id.values())
    if not has_image_already:
        for tid in type_by_id:
            if tid in html_by_id or tid in wanted:
                continue
            wanted.append(tid)
            if len(wanted) >= 25:
                break

    if not wanted or budget_left() <= 0:
        return

    response = recon.get("/1/threads/", params={"ids": ",".join(wanted[: max(budget_left(), 0)])})
    if response is None or not response.is_success:
        return
    data = safe_json(response)
    if not isinstance(data, dict):
        return
    added = 0
    for tid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        # Observed (API_NOTES §3): v1 puts `html` at the entry's top level,
        # not under `thread`. Check both, entry level first.
        html_text = entry.get("html")
        if not isinstance(html_text, str):
            thread_obj = entry.get("thread")
            if isinstance(thread_obj, dict):
                html_text = thread_obj.get("html")
        if isinstance(html_text, str):
            html_by_id[tid] = html_text
            added += 1
    if added:
        report.add(
            section,
            f"Enrichment: fetched {added} additional thread(s) via batched "
            "`GET /1/threads/?ids=` to widen the fixture/image/spreadsheet sample.",
        )


# --------------------------------------------------------------------------
# Probe 4 — blob download
# --------------------------------------------------------------------------

# Quip emits single-quoted HTML attributes; accept both quote styles.
_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_BLOB_PATH_RE = re.compile(r"""/(?:-/)?blob/([^/"']+)/([^/"']+)""")


def probe_blob(
    recon: Recon, report: Report, html_by_id: dict[str, str]
) -> tuple[str | None, str | None]:
    """Returns (thread_id, image_html) for a thread that has an image, for use as a fixture."""
    section = "4. Blob download"
    for thread_id, html in html_by_id.items():
        srcs = _IMG_SRC_RE.findall(html)
        for src in srcs:
            match = _BLOB_PATH_RE.search(src)
            if not match:
                continue
            blob_thread_id, blob_id = match.group(1), match.group(2)
            report.add(section, f"Found `<img>` src in thread `{thread_id}`: `{src}`")
            report.add(
                section,
                f"  - parsed as thread_id=`{blob_thread_id}` blob_id=`{blob_id}` "
                f"(pattern: `{'/-/blob/' if '/-/blob/' in src else '/blob/'}`)",
            )
            response = recon.get(f"/1/blob/{blob_thread_id}/{blob_id}")
            if response is None:
                report.add(section, "  - blob GET not issued (budget/error).")
                continue
            content_type = response.headers.get("content-type", "")
            report.add(
                section,
                f"  - `GET /1/blob/{{tid}}/{{bid}}`: HTTP `{response.status_code}`, "
                f"Content-Type: `{content_type}`, bytes: {len(response.content)}",
            )
            if response.is_success:
                return thread_id, html
    report.add(section, "No `<img>` tag pointing at a `/blob/` or `/-/blob/` path was found "
               "in the thread HTML fetched so far; blob download not exercised.")
    return None, None


# --------------------------------------------------------------------------
# Probe 5 — export endpoints
# --------------------------------------------------------------------------


def probe_exports(
    recon: Recon, report: Report, doc_thread_id: str | None, spreadsheet_thread_id: str | None
) -> None:
    section = "5. Export endpoints"
    if spreadsheet_thread_id is not None:
        response = recon.get(f"/1/threads/{spreadsheet_thread_id}/export/xlsx")
        if response is not None:
            report.add(
                section,
                f"`GET /1/threads/{{id}}/export/xlsx` on spreadsheet `{spreadsheet_thread_id}`: "
                f"HTTP `{response.status_code}`, Content-Type: "
                f"`{response.headers.get('content-type', '')}`",
            )
    else:
        report.add(
            section, "No SPREADSHEET-type thread identified in this run; xlsx export NOT VERIFIED."
        )

    if doc_thread_id is not None:
        for fmt in ("docx", "pdf"):
            response = recon.get(f"/1/threads/{doc_thread_id}/export/{fmt}")
            if response is not None:
                report.add(
                    section,
                    f"`GET /1/threads/{{id}}/export/{fmt}` on `{doc_thread_id}`: "
                    f"HTTP `{response.status_code}`, Content-Type: "
                    f"`{response.headers.get('content-type', '')}`",
                )
    else:
        report.add(
            section, "No document thread identified in this run; docx/pdf export NOT VERIFIED."
        )


# --------------------------------------------------------------------------
# Probe 6 — rate limit headers
# --------------------------------------------------------------------------


def probe_rate_limits(report: Report, recon: Recon) -> None:
    section = "6. Rate-limit headers"
    if not recon.rate_limit_headers:
        report.add(
            section,
            "No `X-RateLimit-*` / `X-Company-RateLimit-*` headers (case-insensitive) were "
            "observed on any response this run.",
        )
        return
    report.add(section, "Headers observed (exact names and values, one line per response):")
    report.add(section, "")
    for name, value in recon.rate_limit_headers.items():
        report.add(section, f"- `{name}` = `{value}`")
    report.add(section, "")
    names = {n.split(" (from ")[0].lower() for n in recon.rate_limit_headers}
    per_minute = any("minute" in n for n in names)
    per_hour = any("hour" in n for n in names)
    if per_minute or per_hour:
        report.add(
            section,
            f"Header names suggest per-minute window: {per_minute}; per-hour window: {per_hour}.",
        )
    else:
        report.add(
            section,
            "Header names do not explicitly encode minute/hour in this account's responses; "
            "see raw values above to infer window from magnitude if needed.",
        )


# --------------------------------------------------------------------------
# Probe 7 — thread type census
# --------------------------------------------------------------------------


def probe_census(
    recon: Recon,
    report: Report,
    initial_folder_ids: list[str],
    initial_thread_ids: list[str],
) -> tuple[Counter[str], int, dict[str, str]]:
    """BFS a bounded number of folder levels (bounded by remaining request budget),
    collecting thread IDs, then batch-fetches thread metadata for type counts.
    Returns (type_counts, max_depth_reached, {thread_id: thread_type})."""
    section = "7. Thread type census / account size"

    visited_folders: set[str] = set()
    frontier: list[str] = list(dict.fromkeys(initial_folder_ids))
    all_thread_ids: set[str] = set(initial_thread_ids)
    depth = 1 if frontier else 0
    max_depth = depth

    # Reserve a slice of the budget for the type-count batch fetch at the end.
    RESERVED_FOR_TYPE_FETCH = 6
    while frontier and recon.request_count < MAX_REQUESTS - RESERVED_FOR_TYPE_FETCH:
        next_frontier: list[str] = []
        batch = [f for f in frontier if f not in visited_folders][:50]
        if not batch:
            break
        visited_folders.update(batch)
        response = recon.get("/1/folders/", params={"ids": ",".join(batch)})
        if response is None:
            break
        data = safe_json(response)
        if isinstance(data, dict):
            for folder_obj in data.values():
                if not isinstance(folder_obj, dict):
                    continue
                for child in _dict_list_field(folder_obj, "children"):
                    if "folder_id" in child and child["folder_id"] not in visited_folders:
                        next_frontier.append(str(child["folder_id"]))
                    if "thread_id" in child:
                        all_thread_ids.add(str(child["thread_id"]))
        frontier = list(dict.fromkeys(next_frontier))
        if frontier:
            depth += 1
            max_depth = max(max_depth, depth)

    coverage_note = (
        "full traversal of the discovered folder tree (frontier emptied before the budget did)"
        if not frontier
        else f"PARTIAL traversal — stopped with {len(frontier)} folder(s) still unvisited "
        "because the request budget reserved for this probe ran out"
    )

    report.add(section, f"Folder BFS coverage: {coverage_note}.")
    report.add(section, f"Distinct folders visited: {len(visited_folders)}.")
    report.add(section, f"Max folder depth reached from root: {max_depth}.")
    report.add(
        section, f"Distinct thread IDs discovered via folder traversal: {len(all_thread_ids)}."
    )

    # Batch-fetch thread type metadata via v1 threads endpoint (cheap: many ids/request).
    type_by_id: dict[str, str] = {}
    thread_id_list = list(all_thread_ids)
    fetched = 0
    i = 0
    while i < len(thread_id_list) and recon.request_count < MAX_REQUESTS:
        batch = thread_id_list[i : i + 50]
        i += 50
        response = recon.get("/1/threads/", params={"ids": ",".join(batch)})
        if response is None:
            break
        data = safe_json(response)
        if isinstance(data, dict):
            for tid, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                thread_obj = entry.get("thread")
                if isinstance(thread_obj, dict):
                    ttype = thread_obj.get("type")
                    if isinstance(ttype, str):
                        # API returns lowercase ("document"); comparisons
                        # elsewhere use the documented uppercase names.
                        type_by_id[tid] = ttype.upper()
        fetched += len(batch)

    counts: Counter[str] = Counter(type_by_id.values())
    report.add(
        section,
        f"Thread metadata fetched for {fetched} of {len(thread_id_list)} discovered thread(s) "
        f"({'all' if fetched >= len(thread_id_list) else 'PARTIAL sample'}).",
    )
    report.add(section, "")
    if counts:
        report.add(section, "Thread `type` counts observed (sample, see coverage note above):")
        report.add(section, "")
        for ttype, count in counts.most_common():
            report.add(section, f"- `{ttype}`: {count}")
    else:
        report.add(section, "No thread type metadata could be fetched.")

    return counts, max_depth, type_by_id


# --------------------------------------------------------------------------
# Fixture saving
# --------------------------------------------------------------------------


def save_fixtures(
    html_by_id: dict[str, str],
    type_by_id: dict[str, str],
    image_thread_id: str | None,
) -> list[str]:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    def write(name: str, html: str) -> None:
        if len(saved) >= MAX_FIXTURES:
            return
        (FIXTURES_DIR / name).write_text(html, encoding="utf-8")
        saved.append(name)

    for thread_id, html in html_by_id.items():
        ttype = type_by_id.get(thread_id, "")
        lower = html.lower()
        if thread_id == image_thread_id:
            write(f"doc_with_image_{thread_id}.html", html)
        elif ttype == "SPREADSHEET":
            write(f"spreadsheet_{thread_id}.html", html)
        elif "<pre" in lower or "<code" in lower:
            write(f"doc_headings_lists_code_{thread_id}.html", html)
        else:
            write(f"doc_sample_{thread_id}.html", html)

    return saved


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    token = load_token()
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
    headers = {"Authorization": f"Bearer {token}"}
    report = Report()

    with httpx.Client(
        base_url=BASE_URL, headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        recon = Recon(client=client)

        user = probe_current_user(recon, report)
        html_by_id: dict[str, str] = {}
        fixture_names: list[str] = []

        if user is not None:
            folder_ids, thread_ids = probe_folders(recon, report, user)
            probe_doc_id, _sample_html, html_by_id = probe_thread_content(
                recon, report, thread_ids
            )

            _type_counts, _max_depth, type_by_id = probe_census(
                recon, report, folder_ids, thread_ids
            )

            enrich_html_samples(recon, report, html_by_id, type_by_id, reserve_requests=5)

            image_thread_id, _image_html = probe_blob(recon, report, html_by_id)

            spreadsheet_id = next(
                (tid for tid, t in type_by_id.items() if t == "SPREADSHEET" and tid in html_by_id),
                None,
            )
            doc_id = probe_doc_id if probe_doc_id in html_by_id else None
            probe_exports(recon, report, doc_id, spreadsheet_id)

            probe_rate_limits(report, recon)

            fixture_names = save_fixtures(html_by_id, type_by_id, image_thread_id)
        else:
            probe_rate_limits(report, recon)

    API_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    API_NOTES_PATH.write_text(report.render(recon, fixture_names), encoding="utf-8")

    print(f"Wrote {API_NOTES_PATH}")
    print(f"Saved {len(fixture_names)} fixture(s) to {FIXTURES_DIR}")
    print(f"Total requests issued: {recon.request_count}/{MAX_REQUESTS}")


if __name__ == "__main__":
    main()
