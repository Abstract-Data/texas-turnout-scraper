"""The preview Worker's cron handler dispatches the early-vote sync three times a day."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest
from ev_api.cron import (
    DispatchError,
    body_text_for_log,
    dispatch_error_message,
    dispatch_request,
    run_scheduled,
    should_dispatch,
)

TOKEN = "github_pat_example_not_real_0123456789"
DISPATCH_URL = (
    "https://api.github.com/repos/Abstract-Data/texas-turnout-scraper"
    "/actions/workflows/early-vote-sync.yml/dispatches"
)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class Env:
    def __init__(self, token: str | None) -> None:
        if token is not None:
            self.GITHUB_DISPATCH_TOKEN = token


class Sender:
    def __init__(self, status: int = 204, response_body: str = "") -> None:
        self.status = status
        self.response_body = response_body
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    async def __call__(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> tuple[int, str]:
        self.calls.append((url, headers, body))
        return self.status, self.response_body


def run(env: Env, now: datetime, send: Sender) -> None:
    asyncio.run(run_scheduled(env, now, send))


@pytest.mark.parametrize(
    "now",
    [
        "2026-10-31T15:00:00",
        "2026-10-31T22:00:00",
        "2026-10-31T03:00:00",
        "2026-11-02T16:00:00",
        "2026-11-02T23:00:00",
        "2026-11-02T04:00:00",
    ],
)
def test_ten_five_and_ten_pm_central_proceed_on_cdt_and_cst_days(now: str) -> None:
    assert should_dispatch(utc(now))


@pytest.mark.parametrize(
    "now",
    [
        "2026-10-31T16:00:00",
        "2026-10-31T23:00:00",
        "2026-10-31T04:00:00",
        "2026-11-02T15:00:00",
        "2026-11-02T22:00:00",
        "2026-11-02T03:00:00",
    ],
)
def test_the_other_seasons_hours_do_not_proceed(now: str) -> None:
    assert not should_dispatch(utc(now))


def test_the_dst_switch_day_uses_cdt_before_07_utc_and_cst_after() -> None:
    assert should_dispatch(utc("2026-11-01T03:00:00"))
    assert not should_dispatch(utc("2026-11-01T04:00:00"))
    assert not should_dispatch(utc("2026-11-01T15:00:00"))
    assert should_dispatch(utc("2026-11-01T16:00:00"))
    assert should_dispatch(utc("2026-11-01T23:00:00"))
    assert not should_dispatch(utc("2026-11-01T22:00:00"))


def test_the_window_ends_at_11_59_pm_central_on_november_3() -> None:
    assert should_dispatch(utc("2026-11-04T04:00:00"))
    assert should_dispatch(utc("2026-11-04T04:59:59"))
    assert not should_dispatch(utc("2026-11-04T06:00:00"))
    assert not should_dispatch(utc("2026-11-04T16:00:00"))
    assert not should_dispatch(utc("2026-11-05T04:00:00"))


def test_minutes_within_the_hour_do_not_matter() -> None:
    assert should_dispatch(utc("2026-10-31T15:59:59"))
    assert should_dispatch(utc("2026-10-31T15:17:00"))
    assert not should_dispatch(utc("2026-10-31T14:59:59"))


def test_dispatch_request_targets_the_workflow_on_main_with_string_inputs() -> None:
    url, headers, body = dispatch_request(TOKEN)
    assert url == DISPATCH_URL
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"]
    assert body == {
        "ref": "main",
        "inputs": {
            "sync_mode": "backfill",
            "election_id": "53815",
            "election_date": "",
            "dry_run": "false",
            "payload_kind": "statewide_roster",
            "trigger": "cron",
        },
    }


def test_outside_the_window_nothing_is_sent() -> None:
    send = Sender()
    run(Env(TOKEN), utc("2026-10-31T16:00:00"), send)
    run(Env(TOKEN), utc("2026-11-05T04:00:00"), send)
    assert send.calls == []


@pytest.mark.parametrize("token", [None, ""])
def test_missing_token_skips_with_a_log_line(
    token: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    send = Sender()
    with caplog.at_level(logging.INFO, logger="ev_api.cron"):
        run(Env(token), utc("2026-10-31T15:00:00"), send)
    assert send.calls == []
    assert "GITHUB_DISPATCH_TOKEN" in caplog.text


def test_a_204_sends_one_dispatch_and_logs_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    send = Sender(204)
    with caplog.at_level(logging.DEBUG):
        run(Env(TOKEN), utc("2026-10-31T15:00:00"), send)
    assert send.calls == [dispatch_request(TOKEN)]
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text


@pytest.mark.parametrize("status", [200, 401, 404, 422, 500])
def test_any_other_status_raises_with_the_number_only(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(DispatchError) as raised:
        run(Env(TOKEN), utc("2026-11-02T23:00:00"), Sender(status))
    message = str(raised.value)
    assert str(status) in message
    assert TOKEN not in message
    assert "Bearer" not in message
    assert TOKEN not in caplog.text


def test_422_disabled_workflow_logs_body_and_hints_in_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = '{"message":"Workflow early-vote-sync.yml is disabled"}'
    with caplog.at_level(logging.ERROR, logger="ev_api.cron"), pytest.raises(DispatchError) as raised:
        run(
            Env(TOKEN),
            utc("2026-11-02T23:00:00"),
            Sender(422, body),
        )
    assert "disabled" in str(raised.value).lower()
    assert "workflow appears disabled" in str(raised.value)
    assert "HTTP 422" in caplog.text
    assert "disabled" in caplog.text
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text


def test_non_422_failure_logs_truncated_body_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = "Bad credentials " + ("x" * 400)
    with caplog.at_level(logging.ERROR, logger="ev_api.cron"), pytest.raises(DispatchError) as raised:
        run(Env(TOKEN), utc("2026-11-02T23:00:00"), Sender(401, body))
    assert str(raised.value) == "early-vote dispatch failed: HTTP 401"
    assert "workflow appears disabled" not in str(raised.value)
    logged = body_text_for_log(body)
    assert len(logged) <= 301
    assert logged.endswith("…")
    assert logged in caplog.text
    assert TOKEN not in caplog.text


def test_dispatch_error_message_disabled_only_for_422_with_disabled_text() -> None:
    assert "disabled" in dispatch_error_message(422, "Workflow is disabled").lower()
    assert dispatch_error_message(422, "ref not found") == "early-vote dispatch failed: HTTP 422"
    assert dispatch_error_message(401, "disabled") == "early-vote dispatch failed: HTTP 401"
