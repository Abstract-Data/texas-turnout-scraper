"""Typer-free CLI for the Karl Rove report."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from reports.karl_rove.build import run_build

logger = logging.getLogger(__name__)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build Karl Rove HTML/PDF report")
    parser.add_argument(
        "command",
        nargs="?",
        default="build",
        choices=["build"],
        help="subcommand (build)",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="use EXAMPLE data (no API keys)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist/karl-rove"),
        help="directory for HTML/PDF output (not committed)",
    )
    parser.add_argument("--no-pdf", action="store_true", help="skip Playwright PDF")
    parser.add_argument("--election-id", default="53815")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.command != "build":
        parser.error(f"unknown command {args.command}")

    write_pdf = not args.no_pdf
    if args.fixture:
        run_build(fixture=True, output_dir=args.output_dir, write_pdf=write_pdf)
        return

    vep_key = _env("VEP_API_KEY")
    if vep_key is None:
        logger.error("VEP_API_KEY is required (or pass --fixture)")
        sys.exit(1)
    ev_roster_token = _env("EV_API_TOKEN")
    if ev_roster_token is None:
        logger.error("EV_API_TOKEN (roster scope) is required (or pass --fixture)")
        sys.exit(1)
    ev_agg_token = _env("EV_API_AGGREGATES_TOKEN") or ev_roster_token
    ev_base = _env("EV_API_BASE_URL") or _env("EV_API_HOST")
    if ev_base is None:
        logger.error("EV_API_BASE_URL or EV_API_HOST is required")
        sys.exit(1)
    if not ev_base.startswith("http"):
        ev_base = f"https://{ev_base}"

    run_build(
        fixture=False,
        output_dir=args.output_dir,
        write_pdf=write_pdf,
        vep_api_key=vep_key,
        vep_base_url=_env("VEP_BASE_URL"),
        ev_api_base_url=ev_base,
        ev_access_client_id=_env("CF_ACCESS_CLIENT_ID"),
        ev_access_client_secret=_env("CF_ACCESS_CLIENT_SECRET"),
        ev_aggregates_bearer_token=ev_agg_token,
        ev_roster_bearer_token=ev_roster_token,
        source_election_id=args.election_id,
    )
