"""Self-contained HTML report (inline CSS, print-ready)."""

from __future__ import annotations

from html import escape

from reports.karl_rove.models import CohortEvRow, KarlRoveReportData, RegistrationYearEvRow


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def _cohort_table_rows(rows: tuple[CohortEvRow, ...], total: CohortEvRow) -> str:
    parts: list[str] = []
    for row in rows:
        parts.append(
            "<tr>"
            f"<td>{row.vep_cohort_year}</td>"
            f"<td class='num'>{_fmt_int(row.registrants)}</td>"
            f"<td class='num'>{_fmt_int(row.early_voted)}</td>"
            f"<td class='num'>{_fmt_pct(row.percent_early_voted)}</td>"
            "</tr>"
        )
    parts.append(
        "<tr class='total'>"
        "<td><strong>Total</strong></td>"
        f"<td class='num'><strong>{_fmt_int(total.registrants)}</strong></td>"
        f"<td class='num'><strong>{_fmt_int(total.early_voted)}</strong></td>"
        f"<td class='num'><strong>{_fmt_pct(total.percent_early_voted)}</strong></td>"
        "</tr>"
    )
    return "\n".join(parts)


def _registration_table_rows(
    rows: tuple[RegistrationYearEvRow, ...], total: RegistrationYearEvRow
) -> str:
    parts: list[str] = []
    for row in rows:
        flag = ""
        if row.future_year_data_quality:
            flag = " <span class='dq-flag' title='Registration year after as-of date'>†</span>"
        parts.append(
            "<tr>"
            f"<td>{row.label}{flag}</td>"
            f"<td class='num'>{_fmt_int(row.registrants)}</td>"
            f"<td class='num'>{_fmt_int(row.early_voted)}</td>"
            f"<td class='num'>{_fmt_pct(row.percent_early_voted)}</td>"
            "</tr>"
        )
    parts.append(
        "<tr class='total'>"
        "<td><strong>Total</strong></td>"
        f"<td class='num'><strong>{_fmt_int(total.registrants)}</strong></td>"
        f"<td class='num'><strong>{_fmt_int(total.early_voted)}</strong></td>"
        f"<td class='num'><strong>{_fmt_pct(total.percent_early_voted)}</strong></td>"
        "</tr>"
    )
    return "\n".join(parts)


def render_html(data: KarlRoveReportData) -> str:
    banner = ""
    if data.is_fixture:
        banner = (
            "<div class='fixture-banner' role='status'>"
            "EXAMPLE DATA — not live API output"
            "</div>"
        )
    vep_rows = "\n".join(
        f"<tr><td>{y.vep_cohort_year}</td><td class='num'>{_fmt_int(y.registrants)}</td></tr>"
        for y in data.vep_registrations.years
    )
    cohort_body = _cohort_table_rows(data.cohort_early_vote, data.cohort_early_vote_total)
    registration_body = _registration_table_rows(
        data.registration_year_early_vote,
        data.registration_year_early_vote_total,
    )
    title = escape(data.election_label)
    generated = escape(data.generated_at_iso)
    as_of = escape(data.as_of_ev_date)
    election_id = escape(data.source_election_id)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Karl Rove Report — {title}</title>
<style>
:root {{
  --ink: #111111;
  --paper: #ffffff;
  --accent: #1a4480;
  --border: #333333;
  --muted: #444444;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: Georgia, "Times New Roman", serif;
  color: var(--ink);
  background: var(--paper);
  line-height: 1.45;
  font-size: 11pt;
}}
.fixture-banner {{
  background: #b45309;
  color: #fff;
  text-align: center;
  font-weight: bold;
  padding: 0.5rem 1rem;
  font-family: system-ui, sans-serif;
}}
.page {{
  max-width: 8.5in;
  margin: 0 auto;
  padding: 0.6in 0.75in 0.9in;
}}
h1 {{
  font-size: 1.35rem;
  margin: 0 0 0.25rem;
  color: var(--accent);
}}
.meta {{
  color: var(--muted);
  font-size: 0.95rem;
  margin-bottom: 1.25rem;
}}
h2 {{
  font-size: 1.05rem;
  margin: 1.25rem 0 0.5rem;
  border-bottom: 2px solid var(--border);
  padding-bottom: 0.2rem;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  margin: 0.5rem 0 1rem;
  break-inside: avoid;
}}
th, td {{
  border: 1px solid var(--border);
  padding: 0.35rem 0.5rem;
  text-align: left;
}}
th {{
  background: #e8eef5;
  font-weight: bold;
}}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
tr.total td {{ background: #f3f4f6; }}
.dq-flag {{ color: #b45309; font-weight: bold; }}
.reg-note {{ font-size: 0.9rem; color: var(--muted); margin: 0 0 0.5rem; }}
.confidential {{
  font-size: 0.85rem;
  color: var(--muted);
  margin-top: 2rem;
  border-top: 1px solid var(--border);
  padding-top: 0.5rem;
}}
@page {{
  size: letter;
  margin: 0.65in 0.75in 0.85in;
}}
@page {{
  @top-center {{
    content: "Karl Rove Report — Confidential";
    font-size: 9pt;
    color: #555;
  }}
  @bottom-center {{
    content: "Generated {generated}";
    font-size: 8pt;
    color: #555;
  }}
}}
@media print {{
  .fixture-banner {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  table, tr, thead, tbody {{ break-inside: avoid; page-break-inside: avoid; }}
  h2 {{ break-after: avoid; }}
}}
</style>
</head>
<body>
{banner}
<div class="page">
  <h1>Karl Rove Report</h1>
  <p class="meta">
    {title} (SOS ID {election_id})<br/>
    Early vote as of <strong>{as_of}</strong> · Generated {generated}
  </p>

  <h2>Section 1 — Total VEP registrations by VEP cohort year</h2>
  <table>
    <thead>
      <tr><th>VEP cohort year</th><th class="num">Registrants</th></tr>
    </thead>
    <tbody>
      {vep_rows}
      <tr class="total">
        <td><strong>Total</strong></td>
        <td class="num"><strong>{_fmt_int(data.vep_registrations.total)}</strong></td>
      </tr>
    </tbody>
  </table>

  <h2>Section 2 — Early voted (2026 General) by VEP cohort year</h2>
  <table>
    <thead>
      <tr>
        <th>VEP cohort year</th>
        <th class="num">Registrants</th>
        <th class="num">Early voted</th>
        <th class="num">% early voted</th>
      </tr>
    </thead>
    <tbody>
      {cohort_body}
    </tbody>
  </table>

  <h2>By registration year (from voter registration date)</h2>
  <p class="reg-note">† Registration year is after the report as-of date (data quality review).</p>
  <table>
    <thead>
      <tr>
        <th>Registration year</th>
        <th class="num">Registrants</th>
        <th class="num">Early voted</th>
        <th class="num">% early voted</th>
      </tr>
    </thead>
    <tbody>
      {registration_body}
    </tbody>
  </table>

  <p class="confidential">Confidential — aggregate counts only. Do not redistribute.</p>
</div>
</body>
</html>
"""
