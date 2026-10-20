"""Aggregate-only JSON from early-vote D1 payloads (no row-level PII in output).

Turnout JSON and statewide roster CSV shapes are supported. Logs must never
include voter names or VUIDs.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any

from texas_turnout_scraper.civix.ids import require_election_id


@dataclass(frozen=True)
class MethodTotals:
    in_person: int
    mail_in: int

    @property
    def total(self) -> int:
        return self.in_person + self.mail_in


@dataclass(frozen=True)
class CountyMethodTotals:
    county: str
    in_person: int
    mail_in: int

    @property
    def total(self) -> int:
        return self.in_person + self.mail_in


def _normalize_method(raw: str) -> str | None:
    text = raw.strip().upper()
    if text in {"IN-PERSON", "IN PERSON", "INPERSON"}:
        return "in_person"
    if text in {"MAIL-IN", "MAIL IN", "MAILIN", "MAIL"}:
        return "mail_in"
    return None


def _sum_turnout_rows(rows: list[dict[str, Any]]) -> tuple[MethodTotals, list[CountyMethodTotals]]:
    statewide_ip = 0
    statewide_mail = 0
    by_county: list[CountyMethodTotals] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        county = str(row.get("county") or row.get("COUNTY") or "").strip()
        if not county:
            continue
        ip = int(row.get("total_in_person_votes_for_election") or 0)
        mail = int(row.get("total_mail_votes_for_election") or 0)
        statewide_ip += ip
        statewide_mail += mail
        by_county.append(CountyMethodTotals(county=county, in_person=ip, mail_in=mail))
    by_county.sort(key=lambda item: item.county)
    return MethodTotals(in_person=statewide_ip, mail_in=statewide_mail), by_county


def aggregates_from_turnout_json(
    decoded: bytes,
    *,
    source_election_id: str,
    ev_date: str | None = None,
) -> dict[str, object]:
    """Build aggregate JSON from one ``EVR_EARLYVOTING`` decoded payload."""
    election_id = require_election_id(source_election_id)
    payload = json.loads(decoded)
    if not isinstance(payload, dict):
        msg = "turnout payload is not an object"
        raise ValueError(msg)
    rows = payload.get("turnout_by_county")
    if not isinstance(rows, list):
        msg = "turnout_by_county missing"
        raise ValueError(msg)
    statewide, by_county = _sum_turnout_rows(rows)
    body: dict[str, object] = {
        "source_election_id": election_id,
        "payload_kind": "turnout",
        "statewide": {
            "total": statewide.total,
            "in_person": statewide.in_person,
            "mail_in": statewide.mail_in,
        },
        "by_county": [
            {
                "county": item.county,
                "total": item.total,
                "in_person": item.in_person,
                "mail_in": item.mail_in,
            }
            for item in by_county
        ],
    }
    if ev_date is not None:
        body["ev_date"] = ev_date
    return body


def aggregates_from_roster_csv(
    decoded: bytes,
    *,
    source_election_id: str,
    ev_date: str | None = None,
) -> dict[str, object]:
    """Build aggregate JSON from one redacted statewide roster CSV."""
    election_id = require_election_id(source_election_id)
    text = decoded.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        empty = MethodTotals(0, 0)
        body: dict[str, object] = {
            "source_election_id": election_id,
            "payload_kind": "statewide_roster",
            "statewide": {
                "total": empty.total,
                "in_person": empty.in_person,
                "mail_in": empty.mail_in,
            },
            "by_county": [],
        }
        if ev_date is not None:
            body["ev_date"] = ev_date
        return body

    county_key = "COUNTY" if "COUNTY" in reader.fieldnames else "county"
    method_key = "VOTING_METHOD" if "VOTING_METHOD" in reader.fieldnames else "voting_method"
    per_county: dict[str, MethodTotals] = {}
    statewide = MethodTotals(0, 0)
    for row in reader:
        county = str(row.get(county_key) or "").strip().upper()
        if not county:
            continue
        method = _normalize_method(str(row.get(method_key) or ""))
        if method is None:
            continue
        bucket = per_county.setdefault(county, MethodTotals(0, 0))
        if method == "in_person":
            per_county[county] = MethodTotals(bucket.in_person + 1, bucket.mail_in)
            statewide = MethodTotals(statewide.in_person + 1, statewide.mail_in)
        else:
            per_county[county] = MethodTotals(bucket.in_person, bucket.mail_in + 1)
            statewide = MethodTotals(statewide.in_person, statewide.mail_in + 1)

    by_county = [
        CountyMethodTotals(county=name, in_person=totals.in_person, mail_in=totals.mail_in)
        for name, totals in sorted(per_county.items())
    ]
    body = {
        "source_election_id": election_id,
        "payload_kind": "statewide_roster",
        "statewide": {
            "total": statewide.total,
            "in_person": statewide.in_person,
            "mail_in": statewide.mail_in,
        },
        "by_county": [
            {
                "county": item.county,
                "total": item.total,
                "in_person": item.in_person,
                "mail_in": item.mail_in,
            }
            for item in by_county
        ],
    }
    if ev_date is not None:
        body["ev_date"] = ev_date
    return body


def build_by_day_series(
    *,
    source_election_id: str,
    days: tuple[tuple[str, dict[str, object]], ...],
) -> dict[str, object]:
    """Roll up per-day statewide totals already computed for each EV date."""
    election_id = require_election_id(source_election_id)
    series = []
    for ev_date, day_body in days:
        statewide = day_body.get("statewide")
        if not isinstance(statewide, dict):
            continue
        series.append(
            {
                "ev_date": ev_date,
                "total": int(statewide.get("total") or 0),
                "in_person": int(statewide.get("in_person") or 0),
                "mail_in": int(statewide.get("mail_in") or 0),
            }
        )
    return {
        "source_election_id": election_id,
        "series": series,
    }


def build_county_by_day_series(
    *,
    source_election_id: str,
    days: tuple[tuple[str, dict[str, object]], ...],
) -> dict[str, object]:
    """County totals per EV date (no voter rows)."""
    election_id = require_election_id(source_election_id)
    series: list[dict[str, object]] = []
    for ev_date, day_body in days:
        counties = day_body.get("by_county")
        if not isinstance(counties, list):
            continue
        series.append({"ev_date": ev_date, "by_county": counties})
    return {
        "source_election_id": election_id,
        "series": series,
    }


def slice_by_method(body: dict[str, object]) -> dict[str, object]:
    """Return statewide + per-county method split from a totals body."""
    statewide = body.get("statewide")
    by_county = body.get("by_county")
    if not isinstance(statewide, dict) or not isinstance(by_county, list):
        msg = "aggregate body missing statewide or by_county"
        raise ValueError(msg)
    return {
        "source_election_id": body.get("source_election_id"),
        "ev_date": body.get("ev_date"),
        "payload_kind": body.get("payload_kind"),
        "statewide": {
            "in_person": int(statewide.get("in_person") or 0),
            "mail_in": int(statewide.get("mail_in") or 0),
        },
        "by_county": [
            {
                "county": row.get("county"),
                "in_person": int(row.get("in_person") or 0),
                "mail_in": int(row.get("mail_in") or 0),
            }
            for row in by_county
            if isinstance(row, dict)
        ],
    }


def detect_payload_kind(decoded: bytes) -> str:
    """Return ``turnout`` or ``statewide_roster`` for one decoded publish."""
    if decoded[:1] in {b"{", b"["}:
        return "turnout"
    return "statewide_roster"


def aggregates_from_decoded(
    decoded: bytes,
    *,
    source_election_id: str,
    ev_date: str | None = None,
) -> dict[str, object]:
    kind = detect_payload_kind(decoded)
    if kind == "turnout":
        return aggregates_from_turnout_json(
            decoded, source_election_id=source_election_id, ev_date=ev_date
        )
    return aggregates_from_roster_csv(
        decoded, source_election_id=source_election_id, ev_date=ev_date
    )
