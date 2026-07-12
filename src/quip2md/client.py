"""Rate-limited Quip API client.

Wraps `httpx.Client` with a token-bucket + hourly-budget rate limiter,
adaptive pacing from `X-RateLimit-*` response headers, retry/backoff on
429/503/5xx/transport errors, and typed response models.

The per-minute limit and the `X-RateLimit-*` header names/semantics below
were observed live (see `docs/API_NOTES.md`); the per-hour figure is a
conservative assumption (the headers do not disambiguate the window). All
tuning values are isolated as named constants so a future recon pass can
update them in one place. See PLAN.md section 3 for the policy this
implements.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum

import httpx

from quip2md.config import Config

# --- Bootstrap constants (adjust after recon; see docs/API_NOTES.md) -------

RATE_LIMIT_PER_MINUTE = 50
RATE_LIMIT_PER_HOUR = 750
THROTTLE_FRACTION = 0.8
FOLDER_BATCH_SIZE = 100

# Batch size for `threads_batch()` (GET /1/threads/?ids=), observed live in
# docs/API_NOTES.md #3/#7: this account's export loop processes 492 threads
# in ~35 requests at this batch size instead of ~1000 single-thread requests.
THREAD_BATCH_SIZE = 15

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 60.0

RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_MAX_DELAY_SECONDS = 120.0
MAX_ATTEMPTS_429_503 = 6
MAX_ATTEMPTS_OTHER_5XX = 3

# Upper bound on v2 thread-html pages, so a server returning an unbounded
# stream of ever-changing cursors cannot stall one thread indefinitely.
# Real documents paginate in a handful of pages; 1000 is far above any
# legitimate case.
MAX_HTML_PAGES = 1000

LOW_REMAINING_THRESHOLD = 5
_REMAINING_HEADER_NAMES = ("x-ratelimit-remaining", "x-company-ratelimit-remaining")
_RESET_HEADER_NAMES = ("x-ratelimit-reset", "x-company-ratelimit-reset")

# Heuristic: a reset value above this is treated as an absolute Unix
# timestamp; below it, as seconds-until-reset. Not observed (NOT VERIFIED
# in docs/API_NOTES.md) -- both conventions exist across real-world APIs,
# so this guards against either without crashing.
_EPOCH_HEURISTIC_THRESHOLD = 1_000_000_000.0

logger = logging.getLogger("quip2md.client")


# --- Errors ----------------------------------------------------------------


class QuipApiError(Exception):
    """A non-retryable Quip API error, or retry-budget exhaustion.

    Never carries the auth token: only the status code, Quip's own error
    message (if the body was parseable JSON), and the request path.
    """

    def __init__(self, *, status_code: int, message: str | None, path: str) -> None:
        self.status_code = status_code
        self.message = message
        self.path = path
        display_message = message if message is not None else "<no error message>"
        super().__init__(f"Quip API error {status_code} on {path}: {display_message}")


class _ThreadHtmlV2Unavailable(Exception):
    """Internal signal that the v2 thread-html endpoint is not usable here."""


# --- Models ------------------------------------------------------------


class ThreadType(StrEnum):
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    SLIDES = "slides"
    CHAT = "chat"
    OTHER = "other"


class FolderChildKind(StrEnum):
    FOLDER = "folder"
    THREAD = "thread"


@dataclass(slots=True, frozen=True)
class QuipUser:
    id: str
    name: str
    private_folder_id: str | None
    desktop_folder_id: str | None
    archive_folder_id: str | None
    starred_folder_id: str | None
    shared_folder_ids: tuple[str, ...]
    group_folder_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class FolderChild:
    kind: FolderChildKind
    id: str


@dataclass(slots=True, frozen=True)
class QuipFolder:
    id: str
    title: str
    children: tuple[FolderChild, ...]


@dataclass(slots=True, frozen=True)
class QuipThread:
    id: str
    title: str
    thread_type: ThreadType
    updated_usec: int | None
    link: str | None


@dataclass(slots=True, frozen=True)
class ThreadContent:
    """One thread's metadata plus its full document HTML.

    Returned by `threads_batch()`, backed by `GET /1/threads/?ids=`: each
    response entry carries a top-level `html` field (the full document,
    unlike `thread.html` which is absent -- see docs/API_NOTES.md #3) and a
    nested `thread` object with the metadata fields below.
    """

    id: str
    title: str
    thread_type: ThreadType
    created_usec: int | None
    updated_usec: int | None
    link: str | None
    html: str


# --- Small typed JSON helpers (contain the `Any` from response.json()) -----


def _find_header(headers: Mapping[str, str], names: Sequence[str]) -> str | None:
    """Case-insensitive lookup across one or more header name variants."""
    lower_map = {key.lower(): value for key, value in headers.items()}
    for name in names:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    return None


def _resolve_reset_wait(reset_value: str, now: float) -> float:
    try:
        value = float(reset_value)
    except ValueError:
        return 0.0
    if value > _EPOCH_HEURISTIC_THRESHOLD:
        return max(0.0, value - now)
    return max(0.0, value)


def _json_object(response: httpx.Response, path: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise QuipApiError(
            status_code=response.status_code,
            message="Response body was not valid JSON",
            path=path,
        ) from exc
    if not isinstance(payload, dict):
        raise QuipApiError(
            status_code=response.status_code,
            message="Unexpected JSON response shape (expected an object)",
            path=path,
        )
    return payload


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _str_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _require_str(value: object, field: str, path: str) -> str:
    if not isinstance(value, str):
        raise QuipApiError(
            status_code=0,
            message=f"Missing or invalid '{field}' field in response",
            path=path,
        )
    return value


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _parse_user(data: dict[str, object], path: str) -> QuipUser:
    return QuipUser(
        id=_require_str(data.get("id"), "id", path),
        name=_require_str(data.get("name"), "name", path),
        private_folder_id=_str_or_none(data.get("private_folder_id")),
        desktop_folder_id=_str_or_none(data.get("desktop_folder_id")),
        archive_folder_id=_str_or_none(data.get("archive_folder_id")),
        starred_folder_id=_str_or_none(data.get("starred_folder_id")),
        shared_folder_ids=_str_tuple(data.get("shared_folder_ids")),
        group_folder_ids=_str_tuple(data.get("group_folder_ids")),
    )


def _parse_folder(fallback_id: str, raw_entry: dict[str, object], path: str) -> QuipFolder:
    folder_data = _as_object_dict(raw_entry.get("folder")) or raw_entry
    title = _str_or_none(folder_data.get("title")) or ""
    resolved_id = _str_or_none(folder_data.get("id")) or fallback_id

    children: list[FolderChild] = []
    children_raw = raw_entry.get("children")
    if isinstance(children_raw, list):
        for item in children_raw:
            item_dict = _as_object_dict(item)
            if item_dict is None:
                continue
            folder_child_id = item_dict.get("folder_id")
            thread_child_id = item_dict.get("thread_id")
            if isinstance(folder_child_id, str):
                children.append(FolderChild(kind=FolderChildKind.FOLDER, id=folder_child_id))
            elif isinstance(thread_child_id, str):
                children.append(FolderChild(kind=FolderChildKind.THREAD, id=thread_child_id))
            else:
                logger.warning(
                    "Unrecognized folder child shape in %s: keys=%s", path, sorted(item_dict)
                )
    return QuipFolder(id=resolved_id, title=title, children=tuple(children))


def _parse_thread_type(value: object) -> ThreadType:
    if isinstance(value, str):
        try:
            return ThreadType(value.lower())
        except ValueError:
            return ThreadType.OTHER
    return ThreadType.OTHER


def _parse_thread(payload: dict[str, object], path: str) -> QuipThread:
    thread_data = _as_object_dict(payload.get("thread")) or payload
    thread_id = _require_str(thread_data.get("id"), "id", path)
    title = _str_or_none(thread_data.get("title")) or ""
    thread_type = _parse_thread_type(thread_data.get("type"))
    updated_raw = thread_data.get("updated_usec")
    updated_usec = updated_raw if isinstance(updated_raw, int) else None
    link = _str_or_none(thread_data.get("link")) or _str_or_none(thread_data.get("url"))
    return QuipThread(
        id=thread_id,
        title=title,
        thread_type=thread_type,
        updated_usec=updated_usec,
        link=link,
    )


def _parse_thread_content(thread_id: str, entry: dict[str, object]) -> ThreadContent:
    """Parse one entry of the `/1/threads/?ids=` batch response.

    Unlike `_parse_thread` (used for the v2 single-thread metadata shape),
    the id here is taken from the response dict's own key rather than
    required inside `thread_data`, and tolerant of a missing/malformed
    nested `thread` object: this is a bulk endpoint where one odd entry
    should not fail the whole batch.
    """
    thread_data = _as_object_dict(entry.get("thread")) or {}
    title = _str_or_none(thread_data.get("title")) or ""
    thread_type = _parse_thread_type(thread_data.get("type"))
    created_raw = thread_data.get("created_usec")
    created_usec = created_raw if isinstance(created_raw, int) else None
    updated_raw = thread_data.get("updated_usec")
    updated_usec = updated_raw if isinstance(updated_raw, int) else None
    link = _str_or_none(thread_data.get("link")) or _str_or_none(thread_data.get("url"))
    resolved_id = _str_or_none(thread_data.get("id")) or thread_id
    html_raw = entry.get("html")
    html = html_raw if isinstance(html_raw, str) else ""
    return ThreadContent(
        id=resolved_id,
        title=title,
        thread_type=thread_type,
        created_usec=created_usec,
        updated_usec=updated_usec,
        link=link,
        html=html,
    )


def _extract_next_cursor(payload: dict[str, object]) -> str:
    metadata = _as_object_dict(payload.get("response_metadata"))
    if metadata is not None:
        cursor = metadata.get("next_cursor")
        if isinstance(cursor, str):
            return cursor
    return ""


def _default_jitter(capped_delay: float) -> float:
    """Full-jitter backoff: a uniform random delay in [0, capped_delay]."""
    return random.uniform(0.0, capped_delay)


# --- Rate limiter ------------------------------------------------------


class RateLimiter:
    """Client-side pacing: a token bucket plus an hourly budget.

    Takes an injectable clock and sleep function so tests never really
    sleep. `clock` must return seconds (any consistent epoch works for
    the token-bucket math; the default `time.time` also lets header
    `Reset` values be compared as absolute Unix timestamps).
    """

    def __init__(
        self,
        *,
        per_minute_limit: int = RATE_LIMIT_PER_MINUTE,
        per_hour_limit: int = RATE_LIMIT_PER_HOUR,
        throttle_fraction: float = THROTTLE_FRACTION,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._minute_capacity = per_minute_limit * throttle_fraction
        self._hour_capacity = per_hour_limit * throttle_fraction
        self._minute_tokens = self._minute_capacity
        self._hour_tokens = self._hour_capacity
        self._minute_rate = self._minute_capacity / 60.0
        self._hour_rate = self._hour_capacity / 3600.0
        self._last_refill = self._clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._minute_tokens = min(
            self._minute_capacity, self._minute_tokens + elapsed * self._minute_rate
        )
        self._hour_tokens = min(self._hour_capacity, self._hour_tokens + elapsed * self._hour_rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Block (via the injected sleep) until a request may proceed."""
        self._refill()
        wait_for_minute = 0.0
        if self._minute_tokens < 1.0:
            wait_for_minute = (1.0 - self._minute_tokens) / self._minute_rate
        wait_for_hour = 0.0
        if self._hour_tokens < 1.0:
            wait_for_hour = (1.0 - self._hour_tokens) / self._hour_rate
        wait = max(wait_for_minute, wait_for_hour)
        if wait > 0:
            self._sleep(wait)
            self._refill()
        self._minute_tokens -= 1.0
        self._hour_tokens -= 1.0

    def observe_headers(self, headers: Mapping[str, str]) -> None:
        """React to rate-limit headers from a response.

        Missing headers fall back to bootstrap pacing silently. When
        `remaining` is present and below `LOW_REMAINING_THRESHOLD`,
        sleeps until the `reset` deadline before returning.
        """
        remaining_raw = _find_header(headers, _REMAINING_HEADER_NAMES)
        reset_raw = _find_header(headers, _RESET_HEADER_NAMES)
        if remaining_raw is None or reset_raw is None:
            return
        try:
            remaining = int(float(remaining_raw))
        except ValueError:
            return
        if remaining >= LOW_REMAINING_THRESHOLD:
            return
        wait_seconds = _resolve_reset_wait(reset_raw, self._clock())
        if wait_seconds > 0:
            self._sleep(wait_seconds)


# --- Client --------------------------------------------------------------


class QuipClient:
    """Rate-limited, retrying Quip API client.

    The auth token is kept private and never appears in `repr()`, log
    records, or exception messages.
    """

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float], float] = _default_jitter,
    ) -> None:
        self._base_url = config.base_url
        self._clock = clock
        self._sleep = sleep
        self._jitter_fn = jitter_fn
        self._limiter = RateLimiter(clock=clock, sleep=sleep)
        self._html_api_version: str | None = None
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=READ_TIMEOUT_SECONDS,
            pool=READ_TIMEOUT_SECONDS,
        )
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"QuipClient(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    # -- request plumbing ----------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        capped = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
        return self._jitter_fn(capped)

    def _parse_retry_after(self, value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return parsed.timestamp() - self._clock()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            parsed = self._parse_retry_after(retry_after)
            if parsed is not None:
                return max(0.0, parsed)
        return self._backoff_delay(attempt)

    def _error_from_response(self, response: httpx.Response, path: str) -> QuipApiError:
        message: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            error_description = body.get("error_description")
            error_code = body.get("error")
            if isinstance(error_description, str):
                message = error_description
            elif isinstance(error_code, str):
                message = error_code
        return QuipApiError(status_code=response.status_code, message=message, path=path)

    def _log_response(self, method: str, path: str, response: httpx.Response) -> None:
        remaining = _find_header(response.headers, _REMAINING_HEADER_NAMES)
        remaining_display = remaining if remaining is not None else "unknown"
        logger.info(
            "%s %s -> %d remaining=%s", method, path, response.status_code, remaining_display
        )

    def _request(
        self, method: str, path: str, *, params: Mapping[str, str] | None = None
    ) -> httpx.Response:
        attempts_429_503 = 0
        attempts_other = 0
        while True:
            self._limiter.acquire()
            try:
                response = self._http.request(method, path, params=params)
            except httpx.TransportError as exc:
                attempts_other += 1
                if attempts_other > MAX_ATTEMPTS_OTHER_5XX:
                    raise QuipApiError(
                        status_code=0, message=type(exc).__name__, path=path
                    ) from exc
                self._sleep(self._backoff_delay(attempts_other))
                continue

            self._limiter.observe_headers(response.headers)
            self._log_response(method, path, response)
            status = response.status_code

            if status in (429, 503):
                attempts_429_503 += 1
                if attempts_429_503 > MAX_ATTEMPTS_429_503:
                    raise self._error_from_response(response, path)
                self._sleep(self._retry_delay(response, attempts_429_503))
                continue

            if 500 <= status < 600:
                attempts_other += 1
                if attempts_other > MAX_ATTEMPTS_OTHER_5XX:
                    raise self._error_from_response(response, path)
                self._sleep(self._backoff_delay(attempts_other))
                continue

            if 400 <= status < 500:
                raise self._error_from_response(response, path)

            return response

    # -- public API -------------------------------------------------------

    def current_user(self) -> QuipUser:
        path = "/1/users/current"
        response = self._request("GET", path)
        return _parse_user(_json_object(response, path), path)

    def folders(self, ids: Sequence[str]) -> dict[str, QuipFolder]:
        result: dict[str, QuipFolder] = {}
        ids_list = list(ids)
        path = "/1/folders/"
        for start in range(0, len(ids_list), FOLDER_BATCH_SIZE):
            batch = ids_list[start : start + FOLDER_BATCH_SIZE]
            if not batch:
                continue
            response = self._request("GET", path, params={"ids": ",".join(batch)})
            payload = _json_object(response, path)
            for folder_id, raw_entry in payload.items():
                entry_dict = _as_object_dict(raw_entry)
                if entry_dict is None:
                    continue
                result[folder_id] = _parse_folder(folder_id, entry_dict, path)
        return result

    def threads_batch(self, ids: Sequence[str]) -> dict[str, ThreadContent]:
        """Fetch metadata + full HTML for many threads via `/1/threads/?ids=`.

        Chunks `ids` into groups of `THREAD_BATCH_SIZE`, issuing one request
        per chunk (mirroring `folders()`'s batching pattern). An id the API
        omits from a chunk's response dict is simply absent from the
        returned mapping -- callers must treat that as "not found" rather
        than crashing.
        """
        result: dict[str, ThreadContent] = {}
        ids_list = list(ids)
        path = "/1/threads/"
        for start in range(0, len(ids_list), THREAD_BATCH_SIZE):
            batch = ids_list[start : start + THREAD_BATCH_SIZE]
            if not batch:
                continue
            response = self._request("GET", path, params={"ids": ",".join(batch)})
            payload = _json_object(response, path)
            for thread_id, raw_entry in payload.items():
                entry_dict = _as_object_dict(raw_entry)
                if entry_dict is None:
                    continue
                result[thread_id] = _parse_thread_content(thread_id, entry_dict)
        return result

    def thread(self, thread_id: str) -> QuipThread:
        path = f"/2/threads/{thread_id}"
        response = self._request("GET", path)
        return _parse_thread(_json_object(response, path), path)

    def _thread_html_v2(self, thread_id: str) -> str:
        path = f"/2/threads/{thread_id}/html"
        parts: list[str] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else None
            try:
                response = self._request("GET", path, params=params)
            except QuipApiError as exc:
                if exc.status_code in (404, 403, 410):
                    raise _ThreadHtmlV2Unavailable from exc
                raise
            payload = _json_object(response, path)
            html_piece = payload.get("html")
            if isinstance(html_piece, str):
                parts.append(html_piece)
            next_cursor = _extract_next_cursor(payload)
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise QuipApiError(
                    status_code=response.status_code,
                    message="Repeated pagination cursor from thread-html endpoint",
                    path=path,
                )
            if len(seen_cursors) >= MAX_HTML_PAGES:
                raise QuipApiError(
                    status_code=response.status_code,
                    message=f"thread-html pagination exceeded {MAX_HTML_PAGES} pages",
                    path=path,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return "".join(parts)

    def _thread_html_v1(self, thread_id: str) -> str:
        path = "/1/threads/"
        response = self._request("GET", path, params={"ids": thread_id})
        payload = _json_object(response, path)
        entry = _as_object_dict(payload.get(thread_id))
        if entry is None:
            entry = next(
                (candidate for value in payload.values() if (candidate := _as_object_dict(value))),
                {},
            )
        html_value = entry.get("html")
        return html_value if isinstance(html_value, str) else ""

    def thread_html(self, thread_id: str) -> str:
        """Fetch full thread HTML, trying v2 (paginated) then falling back to v1.

        Which API version worked is cached on the instance so the v2
        probe only happens once per client instance, not once per thread.
        """
        if self._html_api_version != "v1":
            try:
                html = self._thread_html_v2(thread_id)
            except _ThreadHtmlV2Unavailable:
                self._html_api_version = "v1"
            else:
                self._html_api_version = "v2"
                return html
        return self._thread_html_v1(thread_id)

    def blob(self, thread_id: str, blob_id: str) -> tuple[bytes, str | None]:
        path = f"/1/blob/{thread_id}/{blob_id}"
        response = self._request("GET", path)
        return response.content, response.headers.get("Content-Type")

    def export_xlsx(self, thread_id: str) -> bytes:
        path = f"/1/threads/{thread_id}/export/xlsx"
        response = self._request("GET", path)
        return response.content

    def export_pdf(self, thread_id: str) -> bytes:
        path = f"/1/threads/{thread_id}/export/pdf"
        response = self._request("GET", path)
        return response.content
