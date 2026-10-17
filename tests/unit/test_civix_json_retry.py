"""Pins the Civix JSON retry policy (tenacity exponential jitter, 3 attempts)."""

from __future__ import annotations

from tenacity import wait_exponential_jitter

from texas_turnout_scraper.http_transport import (
    JSON_RETRY_ATTEMPTS,
    JSON_RETRY_JITTER_SECONDS,
    JSON_RETRY_MAX_SECONDS,
    JSON_RETRY_MULTIPLIER_SECONDS,
    json_retry_wait,
)


def test_json_retry_policy_is_three_attempt_exponential_jitter() -> None:
    assert JSON_RETRY_ATTEMPTS == 3
    wait = json_retry_wait()
    assert isinstance(wait, wait_exponential_jitter)
    assert wait.multiplier == JSON_RETRY_MULTIPLIER_SECONDS == 0.25
    assert wait.max == JSON_RETRY_MAX_SECONDS == 2.0
    assert wait.jitter == JSON_RETRY_JITTER_SECONDS == 0.2
