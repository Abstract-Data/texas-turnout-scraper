"""Election identifiers stay strings.

Civix JSON sometimes sends these as JSON numbers. Callers may pass ``int`` at
the boundary; the stored value is always a digit string. This module never
returns an ``int`` id. ``int()`` is used only to reject zero and to sort.
"""

from __future__ import annotations


def normalize_election_id(value: object) -> str | None:
    """Return a positive election id string, or ``None`` when unusable.

    Digit strings keep their original text, including leading zeros.
    ``bool`` is rejected because it is a subclass of ``int``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        # Validation only. The returned value is still ``text``.
        if int(text) <= 0:
            return None
        return text
    return None


def require_election_id(value: object) -> str:
    """Normalize ``value`` or raise ``ValueError``."""
    normalized = normalize_election_id(value)
    if normalized is None:
        msg = "election id must be a positive numeric string"
        raise ValueError(msg)
    return normalized
