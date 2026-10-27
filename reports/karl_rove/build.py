"""Build orchestration for live API and fixture modes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from reports.karl_rove.ev_roster import (
    GENERAL_2026_ELECTION_ID,
    GENERAL_2026_LABEL,
    fetch_early_voted_vuids,
    fetch_latest_ev_date,
)
from reports.karl_rove.fixtures import example_report
from reports.karl_rove.join import assemble_report
from reports.karl_rove.models import KarlRoveReportData
from reports.karl_rove.render import render_html
from reports.karl_rove.vep import fetch_vep_years, load_vep_indexes, open_vep_client_with_fallback

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


async def build_live_report(
    *,
    vep_api_key: str,
    vep_base_url: str | None,
    ev_api_base_url: str,
    ev_access_client_id: str | None,
    ev_access_client_secret: str | None,
    ev_aggregates_bearer_token: str,
    ev_roster_bearer_token: str,
    source_election_id: str = GENERAL_2026_ELECTION_ID,
    election_label: str = GENERAL_2026_LABEL,
) -> KarlRoveReportData:
    with open_vep_client_with_fallback(vep_api_key, vep_base_url) as vep_client:
        vep_years = fetch_vep_years(vep_client)
        vep_index = load_vep_indexes(vep_client, vep_years)
        cohort_vuids = vep_index.cohort_frozen()
        registration_year_vuids = vep_index.registration_frozen()

    as_of = await fetch_latest_ev_date(
        base_url=ev_api_base_url,
        access_client_id=ev_access_client_id,
        access_client_secret=ev_access_client_secret,
        bearer_token=ev_aggregates_bearer_token,
        source_election_id=source_election_id,
    )
    early_voted = await fetch_early_voted_vuids(
        base_url=ev_api_base_url,
        access_client_id=ev_access_client_id,
        access_client_secret=ev_access_client_secret,
        bearer_token=ev_roster_bearer_token,
        source_election_id=source_election_id,
        ev_date=as_of,
    )
    return assemble_report(
        source_election_id=source_election_id,
        election_label=election_label,
        as_of_ev_date=as_of,
        generated_at_iso=_now_iso(),
        vep_years=vep_years,
        cohort_vuids=cohort_vuids,
        registration_year_vuids=registration_year_vuids,
        early_voted_vuids=early_voted,
        is_fixture=False,
    )


def build_fixture_report() -> KarlRoveReportData:
    return example_report()


def write_outputs(
    data: KarlRoveReportData,
    *,
    html_path: Path,
    pdf_path: Path | None,
) -> None:
    html = render_html(data)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    logger.info("wrote HTML path=%s", html_path)
    if pdf_path is not None:
        from reports.karl_rove.pdf_export import html_to_pdf

        html_to_pdf(html, pdf_path)


def run_build(
    *,
    fixture: bool,
    output_dir: Path,
    write_pdf: bool,
    **live_kwargs: object,
) -> KarlRoveReportData:
    if fixture:
        data = build_fixture_report()
    else:
        data = asyncio.run(
            build_live_report(**live_kwargs),  # type: ignore[arg-type]  # reason: **live_kwargs is object-typed; ty cannot verify unpack to build_live_report kwargs
        )
    pdf_path = output_dir / "karl-rove-report.pdf" if write_pdf else None
    write_outputs(data, html_path=output_dir / "karl-rove-report.html", pdf_path=pdf_path)
    return data
