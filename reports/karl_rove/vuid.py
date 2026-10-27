"""In-memory VUID normalization (never log values)."""

from __future__ import annotations


def normalize_vuid(raw: str) -> str:
    text = str(raw).strip()
    if not text:
        msg = "empty vuid"
        raise ValueError(msg)
    return text.zfill(10)
