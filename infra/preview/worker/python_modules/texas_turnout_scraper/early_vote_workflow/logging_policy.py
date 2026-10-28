from __future__ import annotations

ALLOWED_LOG_KEYS = frozenset(
    {
        "election_id",
        "run_id",
        "ev_date",
        "mode",
        "status",
        "step",
        "range_index",
        "sha256",
        "raw_sha256",
        "decoded_bytes",
        "rows",
        "rows_added",
        "rows_removed",
        "rows_live",
        "rejects_added",
        "bytes",
        "cpu_ms",
        "attempt",
        "days_changed",
        "days_unchanged",
        "days_failed",
        "files_written",
        "kv_bumped",
        "batch_calls",
        "error_class",
    }
)


def sanitize_log_fields(fields: dict[str, object]) -> dict[str, object]:
    """Drop any key not on the allow-list (unit-tested)."""
    return {key: value for key, value in fields.items() if key in ALLOWED_LOG_KEYS}
