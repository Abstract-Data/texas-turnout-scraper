from __future__ import annotations


def day_unchanged(stored_sha256: str | None, canonical_sha256: str) -> bool:
    """True when ``early_vote_day.sha256`` already matches the staged file."""
    if not stored_sha256:
        return False
    return stored_sha256.strip() == canonical_sha256.strip()
