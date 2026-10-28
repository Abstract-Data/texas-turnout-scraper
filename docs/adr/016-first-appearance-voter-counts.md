# ADR-016: Distinct voters and county FIPS in the aggregate API

**Date:** 2026-10-27
**Status:** Accepted

## Context

The aggregate routes summed each day's roster file. The portal re-lists voters on later days
(ADR-012), so on 2026-10-27 `/aggregates/totals` reported 4,339,686 for election 53815, while
live D1 held 3,682,793 distinct VUIDs: 656,893 voters counted twice (#108). `/aggregates/by-day`
reported 703,579 rows for 2026-10-24, of which 46,686 were new voters. Aggregates-scope consumers
cannot see rows, so they could not detect this.

ADR-012's repeat tables would answer it, but the repeat build is paused until after 2026-11-03
(ADR-014) and its tables are empty in `ev-53815`. Its full build peaked at 802 MB.

Aggregate rows also carried county names only. The roster spells `LASALLE`; Census-keyed sources
spell `La Salle`, so a name join drops the county without an error (#109).

## Decision

1. **`early_vote_first_seen`** holds, per `(ev_date, county, voting_method)`, the number of voters
   whose earliest live roster row is there. `first_seen_build` fills it with set-based SQL: one
   `MIN(day) ... GROUP BY id_voter` query per last character of the VUID (ten digits plus a
   catch-all), because one query over all 4.3M rows took 15.6 s, near D1's limit. On
   2026-10-27 the partitions took 2.3 to 9.6 s each; their sum equalled `COUNT(DISTINCT id_voter)`
   (3,682,793), and their 2026-10-24 count was 46,686.
2. The build replaces the table in one batch: signature deleted first, written last. The
   signature is sha256 over the election's `early_vote_roster_version` rows and
   `FIRST_SEEN_VERSION`. A run whose stored signature matches stops after two small reads. The sync
   workflow runs it after the roster rows load.
3. **The Worker publishes counts only while the signature matches** the live versions. Raw row
   counts keep their names (`total`, `in_person`, `mail_in`) so no existing field changes meaning.
   Added fields:
   - cumulative bodies: `rows_reported`, `unique_voters`, `unique_in_person`, `unique_mail_in`;
   - one-day bodies and county-by-day rows: `rows_reported`, `new_voters`, `new_in_person`,
     `new_mail_in`, `restated_rows`;
   - by-day entries: the same, plus `restated` (true when the day re-lists earlier voters) and
     `cumulative_unique_voters`;
   - every body: `dedupe.status` (`current`, `stale`, `missing`, or `not_applicable` for turnout
     payloads), `county_fips_unmatched`, and `county_fips` on every county row.
   When the status is not `current` the count fields are null, and the body is served without an
   ETag and never cached in KV, so it cannot outlive the build.
4. **`county_fips`** is the 5-digit state-plus-county code (`48283`), from `tx_counties`, keyed by
   the letters-only `county_key` (moved there from `geo_load`). The table is generated from
   `data/geo/tx_geo.json`; a test pins it to that file and to the vendored voterfile table. All 254
   county names in live D1 resolve. An unknown name gets null and is counted, never dropped.
5. The KV cache key gains a body-shape segment (`BODY_SHAPE`), so bodies cached before this change
   at the same published version are not served again.

## Consequences

- A voter listed twice on their first day is attributed to whichever row SQLite's `MIN` returns.
  ADR-012 broke that tie by row hash; the totals do not depend on it.
- `restated_rows` is the day's rows minus its new voters, so rejected rows in a day file would
  count as restated. On 2026-10-27 live rows equalled the published row total, so there were none.
- An aggregate cache miss adds four small D1 reads (table check, signature, versions, counts).
- Between a new roster day and the next build the counts are `stale` and null. That is the honest
  state; it does not fall back to row counts.
