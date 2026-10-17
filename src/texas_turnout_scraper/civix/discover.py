"""Walk Civix ``electionInfo`` trees for ENR and archive discovery."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .ids import normalize_election_id
from .models import CivixElectionRef


def iter_election_info_rows(
    election_constants: dict[str, Any],
) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    """Yield ``(year, type_code, election_key, election_data)`` rows."""
    election_info = election_constants.get("electionInfo")
    if not isinstance(election_info, dict):
        return
    for year_key, type_map in election_info.items():
        if not isinstance(type_map, dict):
            continue
        year_value = str(year_key).strip()
        for type_code, election_map in type_map.items():
            if not isinstance(election_map, dict):
                continue
            type_value = str(type_code).strip().upper()
            for election_key, election_data in election_map.items():
                if not isinstance(election_data, dict):
                    continue
                yield year_value, type_value, str(election_key), election_data


def election_ref_from_row(
    *,
    year_value: str,
    type_value: str,
    election_key: str,
    election_data: dict[str, Any],
    id_from_key: bool,
) -> CivixElectionRef | None:
    """Build a ref from one electionInfo row.

    Archive constants store a null ``ID`` and use the map key as the id.
    ENR constants carry ``ID`` and ignore the key.
    """
    resolved = normalize_election_id(election_data.get("ID"))
    if resolved is None and id_from_key:
        resolved = normalize_election_id(election_key)
    if resolved is None:
        return None
    return CivixElectionRef(
        election_id=resolved,
        name=str(election_data.get("N") or "").strip(),
        year=str(election_data.get("Y") or year_value).strip(),
        election_type_code=str(election_data.get("ET") or type_value).strip(),
        election_class_code=str(election_data.get("EC") or "").strip(),
    )


def normalized_type_codes(type_codes: set[str] | None) -> set[str]:
    """Uppercase type-code filter. An empty set means no filter."""
    return {code.strip().upper() for code in (type_codes or set()) if code.strip()}


def year_in_range(year_value: str, *, min_year: str, max_year: str) -> bool:
    """Apply an inclusive numeric year window. Non-digit years are kept."""
    if not year_value.isdigit():
        return True
    numeric = int(year_value)
    return int(min_year) <= numeric <= int(max_year)


def election_id_sort_key(ref: CivixElectionRef) -> tuple[str, int]:
    """Sort by year, then numeric id. The stored id stays a string."""
    return (ref.year, int(ref.election_id))
