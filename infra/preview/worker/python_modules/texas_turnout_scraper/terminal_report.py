"""Rich terminal rendering for CLI report summaries."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import TurnoutRosterGapReport, VoterfileMatchReport

_console = Console(highlight=False)


def print_gap_report_summary(report: TurnoutRosterGapReport) -> None:
    """Render turnout vs roster gap analysis for terminal output."""
    subtitle = (
        f"[bold]{report.election_name or report.election_id}[/bold] "
        f"([cyan]{report.election_id}[/cyan])  •  EV [cyan]{report.ev_date}[/cyan]  •  "
        f"Turnout source [magenta]{report.turnout_source}[/magenta]"
    )

    summary = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY)
    summary.add_column("", style="bold", min_width=18)
    summary.add_column("In-person", justify="right")
    summary.add_column("Mail", justify="right")
    summary.add_column("Total", justify="right")

    summary.add_row(
        "Turnout (online)",
        f"{report.turnout_in_person:,}",
        f"{report.turnout_mail:,}",
        f"{report.turnout_total:,}",
    )
    summary.add_row(
        "Roster (scraped)",
        f"{report.roster_in_person:,}",
        f"{report.roster_mail:,}",
        f"{report.roster_total:,}",
    )
    gap_style = "bold yellow" if report.gap_total > 0 else "bold green"
    summary.add_row(
        "Gap",
        Text(f"{report.gap_in_person:,}", style=gap_style),
        Text(f"{report.gap_mail:,}", style=gap_style),
        Text(f"{report.gap_total:,} ({report.gap_pct:.1%})", style=gap_style),
    )

    meta = Table.grid(padding=(0, 1))
    meta.add_row("Roster file:", report.roster_path)
    meta.add_row(
        "Counties with gap:",
        f"{report.counties_with_gap} / {len(report.counties)}",
    )
    if report.counties_roster_over_turnout:
        meta.add_row(
            "Roster > turnout:",
            str(report.counties_roster_over_turnout),
        )

    top_rows = [
        row
        for row in sorted(report.counties, key=lambda item: item.gap_total, reverse=True)
        if row.gap_total > 0
    ][:10]

    body = Table.grid(padding=(0, 0))
    body.add_row(subtitle)
    body.add_row("")
    body.add_row(summary)
    body.add_row("")
    body.add_row(meta)
    county_table = _county_gap_table(top_rows)
    if county_table is not None:
        body.add_row("")
        body.add_row(county_table)

    _console.print(
        Panel(
            body,
            title="Turnout vs Roster Gap",
            border_style="blue",
            padding=(1, 2),
        )
    )
    _console.print()


def _county_gap_table(top_rows: list[object]) -> Table | None:
    if not top_rows:
        return None
    county_table = Table(
        title="Largest County Gaps",
        show_header=True,
        header_style="bold",
        box=box.SIMPLE,
        title_style="bold",
    )
    county_table.add_column("County", style="cyan")
    county_table.add_column("Turnout", justify="right")
    county_table.add_column("Roster", justify="right")
    county_table.add_column("Gap", justify="right", style="yellow")
    county_table.add_column("Gap %", justify="right")
    for row in top_rows:
        county_table.add_row(
            row.county,
            f"{row.turnout_total:,}",
            f"{row.roster_total:,}",
            f"{row.gap_total:,}",
            f"{row.gap_pct:.1%}",
        )
    return county_table


def _match_stats_table(report: VoterfileMatchReport) -> Table:
    stats = Table(show_header=True, header_style="bold green", box=box.SIMPLE_HEAVY)
    stats.add_column("Metric", style="bold")
    stats.add_column("Count", justify="right")
    stats.add_row("Roster records", f"{report.total_roster_records:,}")
    stats.add_row("Matched", f"{report.matched_count:,}  ({report.match_rate:.1%})")
    stats.add_row("Unmatched", f"{report.unmatched_count:,}")
    if report.total_voterfile_records is not None:
        stats.add_row("Voterfile rows", f"{report.total_voterfile_records:,}")
    return stats


def _age_bracket_table(report: VoterfileMatchReport) -> Table | None:
    if not report.by_age_bracket:
        return None
    age_table = Table(
        title="Age Brackets (matched voters)",
        show_header=True,
        header_style="bold",
        box=box.SIMPLE,
    )
    age_table.add_column("Bracket")
    age_table.add_column("Count", justify="right")
    age_table.add_column("Share", justify="right")
    for bracket, count in sorted(report.by_age_bracket.items()):
        share = count / report.matched_count if report.matched_count else 0
        age_table.add_row(bracket, f"{count:,}", f"{share:.1%}")
    return age_table


def _voting_method_table(report: VoterfileMatchReport) -> Table | None:
    if not report.by_voting_method:
        return None
    method_table = Table(
        title="Voting Method (matched voters)",
        show_header=True,
        header_style="bold",
        box=box.SIMPLE,
    )
    method_table.add_column("Method")
    method_table.add_column("Count", justify="right")
    for method, count in sorted(report.by_voting_method.items()):
        method_table.add_row(method, f"{count:,}")
    return method_table


def _findings_table(report: VoterfileMatchReport) -> Table | None:
    if not report.findings:
        return None
    findings_table = Table(
        title="Audit Findings",
        show_header=True,
        header_style="bold",
        box=box.SIMPLE,
    )
    findings_table.add_column("Severity")
    findings_table.add_column("Type")
    findings_table.add_column("Detail")
    for finding in report.findings:
        severity_style = {
            "error": "bold red",
            "warning": "bold yellow",
            "info": "cyan",
        }.get(finding.severity.lower(), "white")
        findings_table.add_row(
            Text(finding.severity.upper(), style=severity_style),
            finding.finding_type,
            finding.detail,
        )
    return findings_table


def _output_paths_table(
    *,
    report_path: Path,
    enriched_path: Path | None,
    gap_report_path: Path | None,
    gap_counties_path: Path | None,
) -> Table:
    outputs = Table.grid(padding=(0, 1))
    outputs.add_row("Match report", str(report_path))
    if enriched_path is not None:
        outputs.add_row("Enriched CSV", str(enriched_path))
    if gap_report_path is not None:
        outputs.add_row("Gap report", str(gap_report_path))
    if gap_counties_path is not None:
        outputs.add_row("Gap counties", str(gap_counties_path))
    return outputs


def print_voterfile_match_summary(
    report: VoterfileMatchReport,
    *,
    report_path: Path,
    enriched_path: Path | None = None,
    gap_report_path: Path | None = None,
    gap_counties_path: Path | None = None,
) -> None:
    """Render voterfile match summary for terminal output."""
    body = Table.grid(padding=(0, 0))
    body.add_row(_match_stats_table(report))

    for optional in (
        _age_bracket_table(report),
        _voting_method_table(report),
    ):
        if optional is not None:
            body.add_row("")
            body.add_row(optional)

    district_bits: list[str] = []
    if report.by_cd:
        district_bits.append(f"CD: {len(report.by_cd)}")
    if report.by_hd:
        district_bits.append(f"HD: {len(report.by_hd)}")
    if report.by_sd:
        district_bits.append(f"SD: {len(report.by_sd)}")
    if district_bits:
        body.add_row("")
        body.add_row(Text("  ".join(district_bits), style="dim"))

    findings = _findings_table(report)
    if findings is not None:
        body.add_row("")
        body.add_row(findings)

    body.add_row("")
    body.add_row(Text("Outputs", style="bold"))
    body.add_row(
        _output_paths_table(
            report_path=report_path,
            enriched_path=enriched_path,
            gap_report_path=gap_report_path,
            gap_counties_path=gap_counties_path,
        )
    )

    _console.print(
        Panel(
            body,
            title="Match Summary",
            border_style="green",
            padding=(1, 2),
        )
    )

    if report.turnout_roster_gap is not None:
        print_gap_report_summary(report.turnout_roster_gap)
