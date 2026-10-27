"""VEP NDJSON line parsing — three allowed fields only; no PII retention."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from reports.karl_rove.vuid import normalize_vuid

_FIELD_VEP_YEAR = "vep_year"
_FIELD_VUID = "Voter_voter_id"
_FIELD_REG_DATE = "Voter_registration_date"


@dataclass(frozen=True, slots=True)
class VepRowCore:
    """In-memory join keys only (never written to report output)."""

    vep_cohort_year: int
    vuid: str
    registration_year: int | None


def registration_year_from_date(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) >= 4 and text[:4].isdigit():
        year = int(text[:4])
        if 1900 <= year <= 2100:
            return year
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return None
    return parsed.year


def parse_ndjson_line(line: bytes) -> VepRowCore | None:
    """Parse one NDJSON object; discard all fields except the three join fields."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    vep_raw = payload.get(_FIELD_VEP_YEAR)
    vuid_raw = payload.get(_FIELD_VUID)
    reg_raw = payload.get(_FIELD_REG_DATE)
    if vep_raw is None or vuid_raw is None:
        return None
    reg_text = str(reg_raw).strip() if reg_raw is not None else None
    return VepRowCore(
        vep_cohort_year=int(vep_raw),
        vuid=normalize_vuid(str(vuid_raw)),
        registration_year=registration_year_from_date(reg_text),
    )
