"""first_seen_build exit policy when Cloudflare rate-limits after publish steps."""

from __future__ import annotations

import pytest

from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.first_seen_build import main as first_seen_main


class _RateLimitedStore:
    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db-1"

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        raise EarlyVoteSyncError(
            "Cloudflare HTTP 429 for POST /accounts/x/d1/database/db-1/query",
            http_status=429,
        )

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        return None

    def close(self) -> None:
        return None


def test_first_seen_rate_limit_after_publish_is_degraded_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "texas_turnout_scraper.first_seen_build._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.first_seen_build.CloudflareEarlyVoteStore",
        lambda **kwargs: _RateLimitedStore(),
    )
    code = first_seen_main(["--election-id", "53815", "--apply"])
    out = capsys.readouterr().out
    assert code == 0
    assert "status=degraded" in out
    assert "http_status=429" in out
    assert "exit_code=0" in out


def test_first_seen_non_retryable_error_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadStore(_RateLimitedStore):
        def _query_rows(
            self, database_id: str, sql: str, params: list[str]
        ) -> list[dict[str, object]]:
            raise EarlyVoteSyncError("Cloudflare API error for POST", http_status=None)

    monkeypatch.setattr(
        "texas_turnout_scraper.first_seen_build._cloudflare_config_from_env",
        lambda: ("acct", "token", "ns"),
    )
    monkeypatch.setattr(
        "texas_turnout_scraper.first_seen_build.CloudflareEarlyVoteStore",
        lambda **kwargs: _BadStore(),
    )
    with pytest.raises(EarlyVoteSyncError):
        first_seen_main(["--election-id", "53815", "--apply"])
