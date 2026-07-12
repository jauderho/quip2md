"""Tests for quip2md.client.

Uses `httpx.MockTransport` (no real network) and fake clock/sleep doubles
(no real `time.sleep`) throughout.
"""

from __future__ import annotations

from collections.abc import Callable
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest

from quip2md.client import (
    FOLDER_BATCH_SIZE,
    MAX_ATTEMPTS_429_503,
    MAX_ATTEMPTS_OTHER_5XX,
    THREAD_BATCH_SIZE,
    QuipApiError,
    QuipClient,
    RateLimiter,
    _find_header,
)
from quip2md.config import Config

TOKEN = "sekrit-token-should-never-leak-9f3a"


class FakeClock:
    """An injectable clock that only advances when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleeper:
    """An injectable sleep function that records calls and advances a clock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


def make_config() -> Config:
    return Config(
        token=TOKEN,
        output_dir=Path("export"),
        state_path=Path(".quip2md/state.json"),
        dry_run=False,
        verbose=False,
        include_chats=False,
        force=False,
    )


def identity_jitter(capped_delay: float) -> float:
    return capped_delay


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[QuipClient, RecordingSleeper, FakeClock]:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    transport = httpx.MockTransport(handler)
    client = QuipClient(
        make_config(),
        transport=transport,
        clock=clock,
        sleep=sleeper,
        jitter_fn=identity_jitter,
    )
    return client, sleeper, clock


def json_response(
    status_code: int, payload: object, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status_code, json=payload, headers=headers or {})


# --- RateLimiter: token bucket + hourly budget ------------------------


def test_token_bucket_spacing_math() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(
        per_minute_limit=10,
        per_hour_limit=1_000_000,
        throttle_fraction=0.5,
        clock=clock,
        sleep=sleeper,
    )
    # capacity = 10 * 0.5 = 5 tokens; rate = 5/60 tokens/sec.
    for _ in range(5):
        limiter.acquire()
    assert sleeper.calls == []

    limiter.acquire()
    assert sleeper.calls == pytest.approx([12.0])


def test_hourly_budget_pacing() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(
        per_minute_limit=1_000_000,
        per_hour_limit=10,
        throttle_fraction=0.5,
        clock=clock,
        sleep=sleeper,
    )
    # capacity = 10 * 0.5 = 5 tokens; rate = 5/3600 tokens/sec.
    for _ in range(5):
        limiter.acquire()
    assert sleeper.calls == []

    limiter.acquire()
    assert sleeper.calls == pytest.approx([720.0])


# --- RateLimiter: header-driven pacing ---------------------------------


def test_find_header_is_case_insensitive() -> None:
    headers = {"X-RateLimit-Remaining": "3"}
    assert _find_header(headers, ("x-ratelimit-remaining",)) == "3"
    assert _find_header({"x-ratelimit-remaining": "3"}, ("X-RateLimit-Remaining",)) == "3"


def test_header_sleep_when_remaining_low_relative_reset() -> None:
    clock = FakeClock(start=0.0)
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"x-RateLimit-Remaining": "3", "X-Ratelimit-RESET": "42"})

    assert sleeper.calls == [42.0]


def test_header_sleep_uses_company_variant_case_insensitively() -> None:
    clock = FakeClock(start=0.0)
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers(
        {"x-company-ratelimit-remaining": "1", "X-COMPANY-RATELIMIT-RESET": "9"}
    )

    assert sleeper.calls == [9.0]


def test_header_sleep_treats_large_reset_as_absolute_epoch() -> None:
    clock = FakeClock(start=2_000_000_000.0)
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000000050"})

    assert sleeper.calls == [50.0]


def test_no_header_sleep_when_remaining_is_sufficient() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "40", "X-RateLimit-Reset": "999"})

    assert sleeper.calls == []


def test_missing_headers_fall_back_silently() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({})

    assert sleeper.calls == []


# --- RateLimiter: clock edge cases (T7 hardening) ------------------------


def test_reset_timestamp_in_the_past_absolute_epoch_no_sleep() -> None:
    # now=2_000_000_100, reset=2_000_000_050 -- 50s in the past. Absolute
    # epoch convention (reset value above the heuristic threshold) must
    # clamp the wait to zero rather than going negative.
    clock = FakeClock(start=2_000_000_100.0)
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000000050"})

    assert sleeper.calls == []


def test_reset_timestamp_in_the_past_relative_convention_no_sleep() -> None:
    # A negative relative-seconds reset value (below the epoch heuristic
    # threshold) must also clamp to zero, not sleep a negative amount.
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "-100"})

    assert sleeper.calls == []


def test_remaining_exactly_zero_still_triggers_wait() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "10"})

    assert sleeper.calls == [10.0]


def test_reset_present_but_remaining_missing_no_sleep() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Reset": "10"})

    assert sleeper.calls == []


def test_garbage_remaining_header_no_sleep() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "not-a-number", "X-RateLimit-Reset": "10"})

    assert sleeper.calls == []


def test_garbage_reset_header_treated_as_zero_wait() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper)

    limiter.observe_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "not-a-number"})

    assert sleeper.calls == []


def test_hourly_budget_exact_single_token_exhaustion() -> None:
    # capacity = 1 token exactly (per_hour_limit=1, throttle_fraction=1.0):
    # the first acquire() must consume it to precisely 0, not negative, and
    # the second must wait exactly one full hour for a single token to refill.
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    limiter = RateLimiter(
        per_minute_limit=1_000_000,
        per_hour_limit=1,
        throttle_fraction=1.0,
        clock=clock,
        sleep=sleeper,
    )

    limiter.acquire()
    assert sleeper.calls == []

    limiter.acquire()
    assert sleeper.calls == pytest.approx([3600.0])


# --- Retry / backoff policy ---------------------------------------------


def test_429_without_retry_after_uses_backoff_sequence_and_exhausts() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(429, {"error": "rate_limited"})

    client, sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError) as exc_info:
        client.current_user()

    assert exc_info.value.status_code == 429
    assert len(calls) == MAX_ATTEMPTS_429_503 + 1
    assert sleeper.calls == pytest.approx([2.0, 4.0, 8.0, 16.0, 32.0, 64.0])


def test_429_with_retry_after_seconds_is_honored() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return json_response(429, {"error": "rate_limited"}, headers={"Retry-After": "5"})
        return json_response(200, {"id": "u1", "name": "Alice"})

    client, sleeper, _clock = make_client(handler)

    user = client.current_user()

    assert user.id == "u1"
    assert call_count == 2
    assert sleeper.calls == [5.0]


def test_429_with_retry_after_http_date_is_honored() -> None:
    epoch_start = 1_700_000_000.0
    reset_at = epoch_start + 10.0
    retry_after_header = formatdate(reset_at, usegmt=True)
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return json_response(
                429, {"error": "rate_limited"}, headers={"Retry-After": retry_after_header}
            )
        return json_response(200, {"id": "u1", "name": "Alice"})

    clock = FakeClock(start=epoch_start)
    sleeper = RecordingSleeper(clock)
    transport = httpx.MockTransport(handler)
    client = QuipClient(
        make_config(), transport=transport, clock=clock, sleep=sleeper, jitter_fn=identity_jitter
    )

    client.current_user()

    assert call_count == 2
    assert len(sleeper.calls) == 1
    assert sleeper.calls[0] == pytest.approx(10.0, abs=1.0)


def test_other_5xx_backoff_and_exhaustion() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(500, {"error": "server_error"})

    client, sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError) as exc_info:
        client.current_user()

    assert exc_info.value.status_code == 500
    assert len(calls) == MAX_ATTEMPTS_OTHER_5XX + 1
    assert sleeper.calls == pytest.approx([2.0, 4.0, 8.0])


def test_transport_error_retries_like_other_5xx() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("boom", request=request)

    client, sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError) as exc_info:
        client.current_user()

    assert exc_info.value.status_code == 0
    assert len(calls) == MAX_ATTEMPTS_OTHER_5XX + 1
    assert sleeper.calls == pytest.approx([2.0, 4.0, 8.0])


def test_4xx_other_than_429_raises_immediately_without_retry() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(400, {"error": "invalid_request", "error_description": "nope"})

    client, sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError) as exc_info:
        client.current_user()

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "nope"
    assert exc_info.value.path == "/1/users/current"
    assert len(calls) == 1
    assert sleeper.calls == []


# --- thread_html: v2 pagination, cursor guard, v1 fallback --------------


def test_v2_pagination_stitches_three_pages() -> None:
    calls: list[httpx.Request] = []
    pages = {
        None: {"html": "<p>one</p>", "response_metadata": {"next_cursor": "c1"}},
        "c1": {"html": "<p>two</p>", "response_metadata": {"next_cursor": "c2"}},
        "c2": {"html": "<p>three</p>", "response_metadata": {"next_cursor": ""}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        cursor = request.url.params.get("cursor")
        return json_response(200, pages[cursor])

    client, _sleeper, _clock = make_client(handler)

    html = client.thread_html("t1")

    assert html == "<p>one</p><p>two</p><p>three</p>"
    assert len(calls) == 3


def test_v2_empty_html_page_mid_sequence_does_not_break_stitching() -> None:
    pages = {
        None: {"html": "<p>one</p>", "response_metadata": {"next_cursor": "c1"}},
        "c1": {"html": "", "response_metadata": {"next_cursor": "c2"}},
        "c2": {"html": "<p>three</p>", "response_metadata": {"next_cursor": ""}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        return json_response(200, pages[cursor])

    client, _sleeper, _clock = make_client(handler)

    html = client.thread_html("t1")

    assert html == "<p>one</p><p>three</p>"


def test_v2_cursor_present_but_html_field_missing_from_page() -> None:
    pages = {
        None: {"html": "<p>one</p>", "response_metadata": {"next_cursor": "c1"}},
        # No "html" key at all on this page -- must not KeyError.
        "c1": {"response_metadata": {"next_cursor": ""}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        return json_response(200, pages[cursor])

    client, _sleeper, _clock = make_client(handler)

    html = client.thread_html("t1")

    assert html == "<p>one</p>"


def test_v2_single_page_with_immediately_empty_next_cursor() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(200, {"html": "<p>x</p>", "response_metadata": {"next_cursor": ""}})

    client, _sleeper, _clock = make_client(handler)

    html = client.thread_html("t1")

    assert html == "<p>x</p>"
    assert len(calls) == 1


def test_v2_repeated_cursor_guard_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return json_response(200, {"html": "a", "response_metadata": {"next_cursor": "c1"}})
        if cursor == "c1":
            return json_response(200, {"html": "b", "response_metadata": {"next_cursor": "c2"}})
        return json_response(200, {"html": "c", "response_metadata": {"next_cursor": "c1"}})

    client, _sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError, match="cursor"):
        client.thread_html("t1")


def test_v2_pagination_page_cap_raises() -> None:
    # Server that always returns a fresh, never-repeating cursor -- the
    # repeated-cursor guard never trips, so the page cap must stop it.
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        n = 0 if cursor is None else int(cursor) + 1
        return json_response(200, {"html": "x", "response_metadata": {"next_cursor": str(n)}})

    client, _sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError, match="pagination exceeded"):
        client.thread_html("t1")


def test_v2_to_v1_fallback_and_version_cached_across_calls() -> None:
    v2_calls: list[httpx.Request] = []
    v1_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/2/threads/") and path.endswith("/html"):
            v2_calls.append(request)
            return json_response(404, {"error": "not_found"})
        if path == "/1/threads/":
            v1_calls.append(request)
            thread_id = request.url.params.get("ids", "")
            return json_response(200, {thread_id: {"html": f"<p>{thread_id}</p>"}})
        raise AssertionError(f"unexpected path {path}")

    client, _sleeper, _clock = make_client(handler)

    html_one = client.thread_html("t1")
    html_two = client.thread_html("t2")

    assert html_one == "<p>t1</p>"
    assert html_two == "<p>t2</p>"
    assert len(v2_calls) == 1  # v2 probed exactly once per client instance
    assert len(v1_calls) == 2


# --- folders(): batching ------------------------------------------------


def test_folders_batches_at_batch_size_constant() -> None:
    total_ids = [f"f{i}" for i in range(FOLDER_BATCH_SIZE * 2 + 7)]
    seen_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get("ids", "").split(",")
        seen_batches.append(ids)
        payload = {
            folder_id: {"folder": {"id": folder_id, "title": folder_id}, "children": []}
            for folder_id in ids
        }
        return json_response(200, payload)

    client, _sleeper, _clock = make_client(handler)

    result = client.folders(total_ids)

    assert len(seen_batches) == 3
    assert [len(batch) for batch in seen_batches] == [FOLDER_BATCH_SIZE, FOLDER_BATCH_SIZE, 7]
    assert len(result) == len(total_ids)


def test_folders_parses_children_discriminating_folder_and_thread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200,
            {
                "f1": {
                    "folder": {"id": "f1", "title": "My Folder"},
                    "children": [{"folder_id": "f2"}, {"thread_id": "th1"}],
                }
            },
        )

    client, _sleeper, _clock = make_client(handler)

    result = client.folders(["f1"])

    folder = result["f1"]
    assert folder.title == "My Folder"
    kinds_and_ids = {(child.kind.value, child.id) for child in folder.children}
    assert kinds_and_ids == {("folder", "f2"), ("thread", "th1")}


# --- threads_batch(): batching, field parsing, missing-entry tolerance ---


def test_threads_batch_chunks_at_batch_size_constant() -> None:
    total_ids = [f"t{i}" for i in range(THREAD_BATCH_SIZE * 2 + 4)]
    seen_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get("ids", "").split(",")
        seen_batches.append(ids)
        payload = {
            thread_id: {"html": f"<p>{thread_id}</p>", "thread": {"id": thread_id}}
            for thread_id in ids
        }
        return json_response(200, payload)

    client, _sleeper, _clock = make_client(handler)

    result = client.threads_batch(total_ids)

    assert len(seen_batches) == 3
    assert [len(batch) for batch in seen_batches] == [
        THREAD_BATCH_SIZE,
        THREAD_BATCH_SIZE,
        4,
    ]
    assert len(result) == len(total_ids)


def test_threads_batch_parses_fields_including_lowercase_type_normalization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200,
            {
                "t1": {
                    "html": "<p>full document html</p>",
                    "thread": {
                        "id": "t1",
                        "title": "My Doc",
                        "type": "DOCUMENT",
                        "created_usec": 1000,
                        "updated_usec": 2000,
                        "link": "https://example.quip.com/t1",
                    },
                }
            },
        )

    client, _sleeper, _clock = make_client(handler)

    result = client.threads_batch(["t1"])

    content = result["t1"]
    assert content.id == "t1"
    assert content.title == "My Doc"
    assert content.thread_type.value == "document"
    assert content.created_usec == 1000
    assert content.updated_usec == 2000
    assert content.link == "https://example.quip.com/t1"
    assert content.html == "<p>full document html</p>"


def test_threads_batch_missing_entry_is_tolerated_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The API omits "t2" entirely from the response.
        return json_response(
            200, {"t1": {"html": "<p>t1</p>", "thread": {"id": "t1", "type": "document"}}}
        )

    client, _sleeper, _clock = make_client(handler)

    result = client.threads_batch(["t1", "t2"])

    assert "t1" in result
    assert "t2" not in result


# --- token hygiene --------------------------------------------------------


def test_token_never_in_client_repr() -> None:
    client, _sleeper, _clock = make_client(lambda request: json_response(200, {}))

    assert TOKEN not in repr(client)


def test_token_never_in_api_error_str() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(400, {"error": "invalid_request", "error_description": "bad token?"})

    client, _sleeper, _clock = make_client(handler)

    with pytest.raises(QuipApiError) as exc_info:
        client.current_user()

    assert TOKEN not in str(exc_info.value)


def test_token_never_in_log_records(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"id": "u1", "name": "Alice"})

    client, _sleeper, _clock = make_client(handler)

    with caplog.at_level("INFO", logger="quip2md.client"):
        client.current_user()

    assert caplog.records, "expected at least one log record"
    for record in caplog.records:
        assert TOKEN not in record.getMessage()


# --- basic typed-model parsing (sanity coverage for the public methods) --


def test_current_user_parses_folder_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200,
            {
                "id": "u1",
                "name": "Alice",
                "private_folder_id": "priv",
                "shared_folder_ids": ["s1", "s2"],
            },
        )

    client, _sleeper, _clock = make_client(handler)

    user = client.current_user()

    assert user.id == "u1"
    assert user.private_folder_id == "priv"
    assert user.desktop_folder_id is None
    assert user.shared_folder_ids == ("s1", "s2")


def test_thread_parses_type_with_other_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200,
            {"thread": {"id": "t1", "title": "Doc", "type": "weird-future-type", "link": "u"}},
        )

    client, _sleeper, _clock = make_client(handler)

    thread = client.thread("t1")

    assert thread.thread_type.value == "other"


def test_blob_returns_bytes_and_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG", headers={"Content-Type": "image/png"})

    client, _sleeper, _clock = make_client(handler)

    content, content_type = client.blob("t1", "b1")

    assert content == b"\x89PNG"
    assert content_type == "image/png"


def test_export_xlsx_and_pdf_return_raw_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"binary-data")

    client, _sleeper, _clock = make_client(handler)

    assert client.export_xlsx("t1") == b"binary-data"
    assert client.export_pdf("t1") == b"binary-data"
