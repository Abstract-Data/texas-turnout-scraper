"""Synthetic record strategies for the validation properties: VUIDs, cells, CSV files, voter rows.

Every value is generated. Names are fake tokens, never real voters.
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from hypothesis import strategies as st

from texas_turnout_scraper.voter_tables import (
    CALENDAR_BY_KEY,
    DATED_KEYS,
    DATED_MODES,
    GENERAL_CODES,
    HISTORY_COLUMNS,
    PRIMARY_CODES,
    SEXES,
)

SENTINEL = "ZQXSENTINEL"

# Characters that make CSV and normalization hard: separators, quotes, embedded line feeds,
# padding, a non-breaking space, non-ASCII letters, a combining accent, CJK and an emoji, a BOM.
# A bare carriage return is left out here; it has its own property.
_NASTY = ',"\n \t éÑ́中\U0001f600﻿'
cell_chars = st.one_of(
    st.sampled_from(string.ascii_letters + string.digits + "-_.'"),
    st.sampled_from(_NASTY),
    st.characters(codec="utf-8", exclude_characters="\r\x00"),
)
cell_text = st.text(cell_chars, max_size=8)


def _non_blank(text: str) -> str:
    return text if text.strip() else f"FAKE{text}"


fake_name = st.builds(_non_blank, cell_text)
county_name = st.builds(_non_blank, st.text(cell_chars, min_size=1, max_size=6))

vuid_digits = st.text(string.digits, min_size=1, max_size=10)
padding = st.sampled_from(["", " ", "\t", "  ", " "])


@st.composite
def padded_vuid(draw: st.DrawFn) -> tuple[str, str]:
    """``(cell, expected)``: a valid VUID cell, possibly padded, and its 10-digit form."""
    digits = draw(vuid_digits)
    return draw(padding) + digits + draw(padding), digits.zfill(10)


bad_vuid = st.one_of(
    st.sampled_from(["", " ", "\t", " "]),
    st.text(string.digits, min_size=11, max_size=14),
    st.builds(lambda d: d[:5] + "x" + d[5:], vuid_digits),
    st.builds(lambda d: "-" + d, vuid_digits),
    st.builds(lambda d: d + ".0", vuid_digits),
    st.sampled_from(["٣١", "１２３", "1²", SENTINEL + "42"]),
)

method_variants = st.sampled_from(
    ["IN-PERSON", "MAIL-IN", "in-person", "mail-in", " In-Person ", "MAIL-IN\t"]
)
bad_method = st.sampled_from(["", "INPERSON", "MAIL", "EARLY", "MAIL IN", SENTINEL])


# Header spellings the loader must map, with case and whitespace variants.
_KNOWN_SPELLINGS = {
    "COUNTY": ["COUNTY", "county", " County "],
    "VOTER_NAME": ["VOTER_NAME", "voter_name", "Voter_Name "],
    "ID_VOTER": ["ID_VOTER", "id_voter", " ID_VOTER"],
    "VOTING_METHOD": ["VOTING_METHOD", "Voting_Method"],
    "PRECINCT": ["PRECINCT", "precinct "],
}
_EXTRA_NAMES = ["PARTY", "Ballot Style", "notes", "COL,1", 'Q"uote', "État", "X\nY"]


@dataclass(frozen=True)
class RosterHeader:
    cells: tuple[str, ...]
    canonical: tuple[str, ...]  # stripped, upper-cased; "" for an extra column

    def position(self, name: str) -> int | None:
        return self.canonical.index(name) if name in self.canonical else None


@st.composite
def roster_headers(draw: st.DrawFn) -> RosterHeader:
    required = ["COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD"]
    names = required + (["PRECINCT"] if draw(st.booleans()) else [])
    columns = [(draw(st.sampled_from(_KNOWN_SPELLINGS[n])), n) for n in names]
    extras = draw(st.lists(st.sampled_from(_EXTRA_NAMES), unique=True, max_size=3))
    columns += [(name, "") for name in extras]
    columns = draw(st.permutations(columns))
    return RosterHeader(tuple(c for c, _ in columns), tuple(n for _, n in columns))


# What is wrong with a generated roster line, if anything.
GOOD = "good"
DEFECTS = ("bad_vuid", "blank_county", "blank_name", "bad_method", "short", "long")


@dataclass(frozen=True)
class RosterLine:
    cells: tuple[str, ...]
    defect: str
    expected_vuid: str = ""


@st.composite
def roster_line(draw: st.DrawFn, header: RosterHeader) -> RosterLine:
    defect = draw(st.sampled_from((GOOD, GOOD, GOOD, *DEFECTS)))
    vuid_cell, expected = draw(padded_vuid())
    values = {
        "COUNTY": draw(county_name),
        "VOTER_NAME": SENTINEL + draw(fake_name),
        "ID_VOTER": vuid_cell,
        "VOTING_METHOD": draw(method_variants),
        "PRECINCT": draw(cell_text),
    }
    if defect == "bad_vuid":
        values["ID_VOTER"] = draw(bad_vuid)
    elif defect == "blank_county":
        values["COUNTY"] = draw(st.sampled_from(["", " ", "\t"]))
    elif defect == "blank_name":
        values["VOTER_NAME"] = draw(st.sampled_from(["", "  ", " "]))
    elif defect == "bad_method":
        values["VOTING_METHOD"] = draw(bad_method)
    cells = [values[name] if name else draw(cell_text) for name in header.canonical]
    if defect == "short":
        cells = cells[: draw(st.integers(1, len(cells) - 1))]
    elif defect == "long":
        cells += draw(st.lists(cell_text, min_size=1, max_size=2))
    return RosterLine(tuple(cells), defect, expected)


@st.composite
def roster_files(draw: st.DrawFn, max_lines: int = 12) -> tuple[RosterHeader, list[RosterLine]]:
    """A header and its data lines, with some lines repeated (identical lines are a multiset)."""
    header = draw(roster_headers())
    lines = draw(st.lists(roster_line(header), max_size=max_lines))
    if lines:
        repeats = draw(st.lists(st.sampled_from(lines), max_size=3))
        lines = draw(st.permutations(lines + repeats))
    return header, lines


@dataclass(frozen=True)
class CsvStyle:
    terminator: str
    bom: bool
    quote_all: bool


csv_styles = st.builds(CsvStyle, st.sampled_from(["\n", "\r\n"]), st.booleans(), st.booleans())


def _quote(cell: str, row_width: int, quote_all: bool) -> str:
    needs = any(c in cell for c in ',"\n\r') or (row_width == 1 and cell == "")
    if quote_all or needs:
        return '"' + cell.replace('"', '""') + '"'
    return cell


def serialize(rows: Sequence[Sequence[str]], style: CsvStyle) -> bytes:
    """An independent CSV writer, so the parsers are not checked against their own serializer."""
    text = "".join(
        ",".join(_quote(cell, len(row), style.quote_all) for cell in row) + style.terminator
        for row in rows
    )
    return ("﻿" if style.bom else "").encode("utf-8") + text.encode("utf-8")


generic_rows = st.integers(1, 4).flatmap(
    lambda width: st.lists(
        st.lists(cell_text, min_size=width, max_size=width),
        max_size=8,
    ).map(lambda rows: (width, rows))
)


# Voterfile rows for parse_voter.
_YMD_VALID = st.dates(min_value=date(1880, 1, 1)).map(lambda d: d.strftime("%Y%m%d"))
date_cells = st.one_of(
    st.none(),
    st.sampled_from(["", " ", "\t"]),
    _YMD_VALID,
    st.builds(lambda c, p: p + c + p, _YMD_VALID, padding),
    st.sampled_from(
        [
            "18791231",
            "18800101",
            "18991231",
            "19000101",
            "19000229",
            "20000229",
            "20230229",
            "20240229",
            "20231301",
            "20230000",
            "00000101",
            "2024-01-01",
            "2024011",
            "202401011",
            "２０２４０１０１",
            "21000101",
            SENTINEL,
        ]
    ),
    st.text(string.digits, min_size=8, max_size=8),
)

_GENERAL = sorted(GENERAL_CODES)
_PRIMARY = sorted(PRIMARY_CODES)


def history_cells(column: str) -> st.SearchStrategy[str | None]:
    valid = _GENERAL if column.startswith("GEN") else _PRIMARY
    wrong = _PRIMARY if column.startswith("GEN") else _GENERAL
    return st.one_of(
        st.none(),
        st.sampled_from(["", " "]),
        st.sampled_from(valid),
        st.builds(lambda c, p: p + c + p, st.sampled_from(valid), padding),
        st.sampled_from([*wrong, "v", "X", "VX", "RD", SENTINEL]),
    )


sex_cells = st.one_of(
    st.sampled_from(sorted(SEXES)),
    st.builds(lambda c, p: p + c + p, st.sampled_from(sorted(SEXES)), padding),
    st.sampled_from([None, "", "f", "MALE", "X", SENTINEL]),
)


@st.composite
def voterfile_rows(draw: st.DrawFn) -> dict[str, str | None]:
    vuid = draw(st.one_of(padded_vuid().map(lambda pair: pair[0]), bad_vuid, st.none()))
    row: dict[str, str | None] = {
        "VUID": vuid,
        "SEX": draw(sex_cells),
        "DOB": draw(date_cells),
        "EDR": draw(date_cells),
    }
    for column in HISTORY_COLUMNS:
        row[column] = draw(history_cells(column))
    for extra in draw(st.lists(st.sampled_from(["NAME", "ADDRESS", "PCT"]), unique=True)):
        row[extra] = SENTINEL + draw(fake_name)
    return row


@st.composite
def dated_entries(draw: st.DrawFn) -> dict[str, tuple[int, str] | None]:
    letters = sorted(DATED_MODES.values())
    return {
        key: draw(
            st.none()
            | st.tuples(st.integers(1, CALENDAR_BY_KEY[key].early_days), st.sampled_from(letters))
        )
        for key in DATED_KEYS
    }
