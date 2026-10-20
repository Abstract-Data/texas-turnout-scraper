"""HTTP client for the results.texas-election.com static Civix JSON archive."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..http_transport import is_transient_json_http_error
from .discover import (
    election_id_sort_key,
    election_ref_from_row,
    iter_election_info_rows,
    normalized_type_codes,
    year_in_range,
)
from .ids import normalize_election_id
from .models import (
    CivixArchiveElectionBundle,
    CivixArchiveElectionVersion,
    CivixElectionRef,
)
from .transport import DEFAULT_ARCHIVE_BASE_URL, DEFAULT_JSON_TIMEOUT_SECONDS, fetch_json

logger = logging.getLogger(__name__)

_DEFAULT_PACING_SECONDS = 0.15


class CivixArchiveNotFoundError(Exception):
    """Raised when an archive JSON file returns HTTP 404."""


def _coerce_version_no(value: Any) -> int:
    """Read a version counter. This is not an election id."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_constants_version(payload: dict[str, Any]) -> int:
    """Extract the ``ElectionConstants_{version}.json`` suffix from root Version.json."""
    version_no = _coerce_version_no(payload.get("___versionNo"))
    if version_no <= 0:
        version_no = _coerce_version_no(payload.get("versionNo"))
    if version_no <= 0:
        version_no = _coerce_version_no(payload.get("version"))
    if version_no <= 0:
        msg = "Archive Version.json missing ___versionNo"
        raise ValueError(msg)
    return version_no


def parse_election_version(
    election_id: str | int,
    payload: dict[str, Any],
) -> CivixArchiveElectionVersion:
    """Parse per-election Version.json."""
    version_no = _coerce_version_no(payload.get("___versionNo"))
    election_key = normalize_election_id(election_id) or str(election_id)
    if version_no <= 0:
        msg = f"Election {election_key} Version.json missing ___versionNo"
        raise ValueError(msg)
    elec_date = str(payload.get("elecDate") or "").strip()
    if len(elec_date) != 8 or not elec_date.isdigit():
        msg = f"Election {election_key} Version.json missing elecDate"
        raise ValueError(msg)
    return CivixArchiveElectionVersion(
        election_id=election_key,
        version_no=version_no,
        elec_date=elec_date,
    )


def discover_archive_election_refs(
    election_constants: dict[str, Any],
    *,
    min_year: str = "2019",
    max_year: str = "2024",
    year: str | None = None,
    type_codes: set[str] | None = None,
    election_id: str | int | None = None,
) -> list[CivixElectionRef]:
    """Extract election refs from plain ElectionConstants JSON.

    Archive constants use the map key as the election id when ``ID`` is null.
    """
    refs = _collect_archive_refs(
        election_constants,
        min_year=min_year,
        max_year=max_year,
        year=year,
        type_codes=type_codes,
        election_id=election_id,
    )
    refs.sort(key=election_id_sort_key)
    return refs


def _collect_archive_refs(
    election_constants: dict[str, Any],
    *,
    min_year: str,
    max_year: str,
    year: str | None,
    type_codes: set[str] | None,
    election_id: str | int | None,
) -> list[CivixElectionRef]:
    desired_year = year.strip() if year else ""
    allowed_types = normalized_type_codes(type_codes)
    wanted = normalize_election_id(election_id) if election_id is not None else None
    refs: list[CivixElectionRef] = []
    for year_value, type_value, election_key, election_data in iter_election_info_rows(
        election_constants
    ):
        if not _archive_row_selected(
            year_value,
            type_value,
            desired_year=desired_year,
            min_year=min_year,
            max_year=max_year,
            allowed_types=allowed_types,
        ):
            continue
        ref = election_ref_from_row(
            year_value=year_value,
            type_value=type_value,
            election_key=election_key,
            election_data=election_data,
            id_from_key=True,
        )
        if ref is None:
            continue
        if election_id is not None and ref.election_id != wanted:
            continue
        refs.append(ref)
    return refs


def _archive_row_selected(
    year_value: str,
    type_value: str,
    *,
    desired_year: str,
    min_year: str,
    max_year: str,
    allowed_types: set[str],
) -> bool:
    if desired_year and year_value != desired_year:
        return False
    if not year_in_range(year_value, min_year=min_year, max_year=max_year):
        return False
    return not (allowed_types and type_value not in allowed_types)


def _archive_retryable(exc: BaseException) -> bool:
    if isinstance(exc, CivixArchiveNotFoundError):
        return False
    return is_transient_json_http_error(exc)


class CivixArchiveClient:
    """Polite httpx client for the static Civix archive CDN."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = DEFAULT_ARCHIVE_BASE_URL,
        timeout: float = DEFAULT_JSON_TIMEOUT_SECONDS,
        pacing_seconds: float = _DEFAULT_PACING_SECONDS,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._base_url = base_url
        self._timeout = timeout
        self._pacing_seconds = max(pacing_seconds, 0.0)

    async def __aenter__(self) -> CivixArchiveClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pace(self) -> None:
        if self._pacing_seconds > 0:
            await asyncio.sleep(self._pacing_seconds)

    async def _get_json(self, path: str) -> dict[str, Any]:
        if self._client is None:
            msg = "CivixArchiveClient is not open"
            raise RuntimeError(msg)

        def _raise_if_missing(response: Any) -> None:
            if response.status_code == 404:
                raise CivixArchiveNotFoundError(path)

        return await fetch_json(
            path,
            base_url=self._base_url,
            client=self._client,
            timeout=self._timeout,
            before_attempt=self._pace,
            on_response=_raise_if_missing,
            retry_predicate=_archive_retryable,
        )

    async def fetch_constants_version(self) -> int:
        """Fetch root Version.json and return the constants file version number."""
        payload = await self._get_json("/static/data/Version.json")
        return parse_constants_version(payload)

    async def fetch_election_constants(self, version: int | None = None) -> dict[str, Any]:
        """Fetch ``ElectionConstants_{version}.json``, deriving version when omitted."""
        resolved_version = version if version is not None else await self.fetch_constants_version()
        return await self._get_json(f"/static/data/ElectionConstants_{resolved_version}.json")

    async def fetch_election_version(self, election_id: str | int) -> CivixArchiveElectionVersion:
        """Fetch ``election/{id}/Version.json``."""
        election_key = normalize_election_id(election_id)
        if election_key is None:
            msg = "election id must be a positive numeric string"
            raise ValueError(msg)
        payload = await self._get_json(f"/static/data/election/{election_key}/Version.json")
        return parse_election_version(election_key, payload)

    async def fetch_office_summary(
        self,
        election_id: str | int,
        version_no: int,
    ) -> dict[str, Any]:
        """Fetch race-level OfficeSummary.json for one election/version."""
        election_key = normalize_election_id(election_id)
        if election_key is None:
            msg = "election id must be a positive numeric string"
            raise ValueError(msg)
        return await self._get_json(
            f"/static/data/election/{election_key}/{version_no}/OfficeSummary.json"
        )

    async def fetch_county_detail(
        self,
        election_id: str | int,
        version_no: int,
    ) -> dict[str, Any]:
        """Fetch per-county County.json for one election/version.

        County.json holds per-county vote rows. OfficeSummary is race-level
        totals and does not include county candidate breakdowns.
        """
        election_key = normalize_election_id(election_id)
        if election_key is None:
            msg = "election id must be a positive numeric string"
            raise ValueError(msg)
        return await self._get_json(
            f"/static/data/election/{election_key}/{version_no}/County.json"
        )

    async def fetch_election_bundle(
        self,
        election_ref: CivixElectionRef,
    ) -> CivixArchiveElectionBundle:
        """Fetch version, OfficeSummary, and County.json for one discovered ref."""
        version = await self.fetch_election_version(election_ref.election_id)
        office_summary = await self.fetch_office_summary(
            election_ref.election_id,
            version.version_no,
        )
        county_detail = await self._county_detail_or_none(election_ref, version.version_no)
        return CivixArchiveElectionBundle(
            election_ref=election_ref,
            version=version,
            office_summary=office_summary,
            county_detail=county_detail,
        )

    async def _county_detail_or_none(
        self,
        election_ref: CivixElectionRef,
        version_no: int,
    ) -> dict[str, Any] | None:
        try:
            return await self.fetch_county_detail(election_ref.election_id, version_no)
        except CivixArchiveNotFoundError as exc:
            logger.warning(
                "County detail missing for election_id=%s (%s)",
                election_ref.election_id,
                exc,
            )
            return None
