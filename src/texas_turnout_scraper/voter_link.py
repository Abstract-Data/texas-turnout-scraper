"""Link the voters who turned out in an election to their voterfile attributes, history and scores.

Runs locally, never in CI: the voterfile is PII and stays on the owner's machine. Loader-only
(``loader`` extra for sqlmodel, ``voterfile`` extra for duckdb, imported inside the function that
reads the file). Steps:

1. Read the election's live roster VUIDs from D1, one day and one row-hash range per query.
2. Read only those VUIDs, and only the 21 chosen columns, from the voterfile with DuckDB.
3. With dated history, fetch every early day of the portal's dated elections (cached on disk by
   sha256, so a rerun resumes) and keep each target voter's first early day and mode.
4. Validate each row into a ``Voter`` or a ``VoterReject`` and derive its scores.
5. Compare ``src_hash`` with what D1 holds and upsert only new or changed voters in bounded
   ``json_each`` batches. Voters are never deleted. The meta rows are written last; a run whose
   meta already matches writes nothing.

A dry run does steps 1 to 4, compares against D1 if the tables exist, creates nothing and writes
nothing. Logs and output carry counts only, never names, VUIDs or voterfile values.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import zipfile
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from texas_turnout_scraper.civix.evr import _check_zip_limits
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_roster_rows import day_number, key_ranges
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)
from texas_turnout_scraper.geo_tables import ROSTER_DAYS_SQL
from texas_turnout_scraper.models import CivixElection
from texas_turnout_scraper.voter_models import Voter, VoterReject, parse_voter, voter_row
from texas_turnout_scraper.voter_tables import (
    CALENDAR_BY_KEY,
    DATED_KEYS,
    DATED_MODES,
    ELECTION_CALENDAR,
    FLAG_DOB_INVALID,
    FLAG_EDR_INVALID,
    META_SELECT_SQL,
    META_UPSERT_SQL,
    REJECT_DELETE_SQL,
    REJECT_UPSERT_SQL,
    REJECTS_SQL,
    SCORE_VERSION,
    TARGET_VUIDS_SQL,
    VOTER_HASHES_SQL,
    VOTER_TABLE_COUNT,
    VOTER_TABLES_PRESENT_SQL,
    VOTER_UPSERT_SQL,
    VOTERFILE_COLUMNS,
    VUID_WIDTH,
    seed_statements,
)

logger = logging.getLogger(__name__)

VOTER_ROWS_PER_STATEMENT = 2000
# The payload is one bound string; D1 allows 2 MB per string, and this keeps it under 1 MB.
MAX_PAYLOAD_BYTES = 900_000
# Each target query covers one day and 1/TARGET_KEY_RANGES of the row-hash space (about 50k rows
# of an 800k-row day). Each hash read covers at most VUID_SLICE target VUIDs.
TARGET_KEY_RANGES = 16
VUID_SLICE = 20_000
FETCH_ROWS = 10_000
CENTRAL = ZoneInfo("America/Chicago")
DEFAULT_CACHE_DIR = Path("~/.cache/texas-turnout-scraper/statewide")

LinkStatus = Literal["dry_run", "wrote", "unchanged"]
DayEntry = tuple[int, str]


class VoterStore(Protocol):
    def _query_rows(
        self, database_id: str, sql: str, params: list[str]
    ) -> list[dict[str, object]]: ...

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None: ...


class VoterLinkStore(VoterStore, Protocol):
    def _database_uuid_if_exists(self, election_id: str) -> str | None: ...

    def _ensure_schema(self, database_id: str) -> None: ...


class StatewideFetcher(Protocol):
    """The two ``CivixClient`` calls the dated history needs."""

    def list_elections(self) -> Sequence[CivixElection]: ...

    def fetch_statewide(self, election_id: int, election_date: date) -> bytes: ...


@dataclass(frozen=True)
class DatedHistory:
    days: dict[str, dict[str, DayEntry]]
    fetched: int = 0

    @property
    def found(self) -> dict[str, int]:
        return {key: len(voters) for key, voters in sorted(self.days.items())}


@dataclass(frozen=True)
class LinkResult:
    status: LinkStatus
    targets: int
    matched: int
    rejects: int
    invalid_dob: int
    invalid_edr: int
    written: int
    unchanged: int
    pending: int
    rejects_written: int
    dated_found: dict[str, int] = field(default_factory=dict)

    @property
    def unmatched(self) -> int:
        return self.targets - self.matched

    @property
    def matched_share(self) -> float:
        return self.matched / self.targets if self.targets else 0.0


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _vuid_key(value: str | None) -> str:
    return (value or "").strip().zfill(VUID_WIDTH)


def target_vuids(store: VoterStore, database_id: str, election_id: str) -> set[str]:
    """VUIDs of the election's live roster rows, read one day and one key range at a time."""
    targets: set[str] = set()
    for day_row in store._query_rows(database_id, ROSTER_DAYS_SQL, [election_id]):  # noqa: SLF001
        day = str(day_number(_text(day_row.get("ev_date"))))
        for low, high in key_ranges(TARGET_KEY_RANGES):
            for row in store._query_rows(database_id, TARGET_VUIDS_SQL, [day, low, high]):  # noqa: SLF001
                targets.add(_vuid_key(_text(row.get("id_voter"))))
    return targets


def voterfile_sql() -> str:
    """The 21 chosen columns of the rows whose VUID is in the target file. Parameters: voterfile
    path, target file path."""
    columns = ", ".join(f'"{column}"' for column in VOTERFILE_COLUMNS)
    return (
        f"SELECT {columns} FROM read_csv(?, header = true, all_varchar = true, "
        "strict_mode = false, ignore_errors = true) "
        f"WHERE lpad(trim(\"VUID\"), {VUID_WIDTH}, '0') IN "
        "(SELECT vuid FROM read_csv(?, header = true, columns = {'vuid': 'VARCHAR'}))"
    )


def read_voterfile(path: Path, targets: Iterable[str]) -> Iterator[dict[str, str | None]]:
    """Voterfile rows for ``targets`` only, keyed by the voterfile column names."""
    import duckdb

    with tempfile.TemporaryDirectory() as folder:
        target_file = Path(folder) / "targets.csv"
        target_file.write_text("vuid\n" + "".join(f"{vuid}\n" for vuid in targets))
        connection = duckdb.connect()
        try:
            try:
                cursor = connection.execute(voterfile_sql(), [str(path), str(target_file)])
            except duckdb.Error as error:
                # DuckDB messages can quote a line of the file; keep only the error type.
                raise EarlyVoteSyncError(f"voterfile read failed: {type(error).__name__}") from None
            while rows := cursor.fetchmany(FETCH_ROWS):
                for row in rows:
                    yield dict(zip(VOTERFILE_COLUMNS, row, strict=True))
        finally:
            connection.close()


def _check_calendar(elections: Mapping[int, CivixElection]) -> None:
    """Warn (counts only) when the portal disagrees with the committed calendar."""
    for expected in ELECTION_CALENDAR:
        for civix_id in expected.civix_ids:
            election = elections.get(civix_id)
            if election is None:
                logger.warning(
                    "voter link calendar %s: portal does not list election %s",
                    expected.key,
                    civix_id,
                )
                continue
            dates = sorted(d.date for d in election.early_voting_dates)
            seen = (
                election.election_date.isoformat(),
                len(dates),
                dates[0].isoformat() if dates else "",
            )
            if seen != (expected.election_date, expected.early_days, expected.first_early_date):
                logger.warning(
                    "voter link calendar %s: election %s portal has election_date=%s "
                    "early_days=%s first_early_date=%s, calendar has %s %s %s",
                    expected.key,
                    civix_id,
                    *seen,
                    expected.election_date,
                    expected.early_days,
                    expected.first_early_date,
                )


def _cached_day(
    fetcher: StatewideFetcher, cache_dir: Path, civix_id: int, day: date
) -> tuple[bytes, bool]:
    """One day's statewide file, from ``cache_dir`` when a copy with a matching sha256 is there."""
    folder = cache_dir / str(civix_id)
    for path in sorted(folder.glob(f"{day.isoformat()}.*.bin")):
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() == path.name.split(".")[1]:
            return body, False
        path.unlink()
    body = fetcher.fetch_statewide(civix_id, day)
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    partial = folder / f"{day.isoformat()}.{digest}.part"
    partial.write_bytes(body)
    partial.replace(folder / f"{day.isoformat()}.{digest}.bin")
    return body, True


def _csv_bytes(body: bytes) -> bytes:
    if body[:2] != b"PK":
        return body
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        _check_zip_limits(archive)
        members = sorted(n for n in archive.namelist() if n.lower().endswith(".csv"))
        if not members:
            raise EarlyVoteSyncError("statewide day archive holds no CSV")
        return archive.read(members[0])


def day_file_votes(body: bytes) -> Iterator[tuple[str, str]]:
    """``(vuid, mode letter)`` of each row of a statewide day file with a known voting method."""
    reader = csv.reader(io.TextIOWrapper(io.BytesIO(_csv_bytes(body)), "utf-8-sig", newline=""))
    header = [name.strip().upper() for name in next(reader, [])]
    missing = sorted({"ID_VOTER", "VOTING_METHOD"} - set(header))
    if missing:
        raise EarlyVoteSyncError(f"statewide day file is missing columns: {','.join(missing)}")
    vuid_at, method_at = header.index("ID_VOTER"), header.index("VOTING_METHOD")
    width = max(vuid_at, method_at)
    for cells in reader:
        if len(cells) > width:
            mode = DATED_MODES.get(cells[method_at].strip().upper())
            if mode is not None:
                yield _vuid_key(cells[vuid_at]), mode


def load_dated_history(
    fetcher: StatewideFetcher, targets: Container, cache_dir: Path
) -> DatedHistory:
    """Each target voter's first early day and mode in every dated election."""
    elections = {e.id: e for e in fetcher.list_elections()}
    _check_calendar(elections)
    days: dict[str, dict[str, DayEntry]] = {}
    fetched = 0
    for key in DATED_KEYS:
        voters = days.setdefault(key, {})
        for civix_id in CALENDAR_BY_KEY[key].civix_ids:
            election = elections.get(civix_id)
            if election is None:
                continue
            for early in sorted(election.early_voting_dates, key=lambda d: d.date_turnout_id):
                body, was_fetched = _cached_day(fetcher, cache_dir, civix_id, early.date)
                fetched += was_fetched
                k = early.date_turnout_id
                for vuid, mode in day_file_votes(body):
                    if vuid in targets and (vuid not in voters or k < voters[vuid][0]):
                        voters[vuid] = (k, mode)
        logger.info("voter link dated %s target_voters=%s", key, len(voters))
    return DatedHistory(days, fetched)


def _slices(vuids: Sequence[str]) -> Iterator[tuple[str, str]]:
    for start in range(0, len(vuids), VUID_SLICE):
        chunk = vuids[start : start + VUID_SLICE]
        yield chunk[0], chunk[-1]


def _stored(
    store: VoterStore, database_id: str, sql: str, vuids: Sequence[str]
) -> Iterator[dict[str, object]]:
    for low, high in _slices(vuids):
        yield from store._query_rows(database_id, sql, [low, high])  # noqa: SLF001


def _dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def payload_batches(
    rows: Iterable[object], *, batch_rows: int, max_bytes: int = MAX_PAYLOAD_BYTES
) -> Iterator[str]:
    """JSON arrays of at most ``batch_rows`` rows and about ``max_bytes`` bytes each."""
    chunk: list[str] = []
    size = 2
    for row in rows:
        text = _dump(row)
        if chunk and (len(chunk) == batch_rows or size + len(text) + 1 > max_bytes):
            yield "[" + ",".join(chunk) + "]"
            chunk.clear()
            size = 2
        chunk.append(text)
        size += len(text) + 1
    if chunk:
        yield "[" + ",".join(chunk) + "]"


def _write(
    store: VoterStore, database_id: str, sql: str, rows: Sequence[object], batch_rows: int
) -> None:
    """One bounded statement per request."""
    for payload in payload_batches(rows, batch_rows=batch_rows):
        store._statements(database_id, [(sql, [payload])])  # noqa: SLF001


def _file_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return {"name": path.name, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def _tables_present(store: VoterStore, database_id: str) -> bool:
    rows = store._query_rows(database_id, VOTER_TABLES_PRESENT_SQL, [])  # noqa: SLF001
    return bool(rows) and int(_text(rows[0].get("n")) or 0) == VOTER_TABLE_COUNT


def _parse_all(
    voterfile: Path, targets: set[str], dated: DatedHistory | None, today: date
) -> tuple[dict[str, Voter], dict[str, VoterReject]]:
    voters: dict[str, Voter] = {}
    rejects: dict[str, VoterReject] = {}
    for raw in read_voterfile(voterfile, sorted(targets)):
        key = _vuid_key(raw["VUID"])
        entries = {k: dated.days[k].get(key) for k in DATED_KEYS} if dated else {}
        parsed = parse_voter(raw, entries, today=today)
        if isinstance(parsed, Voter):
            voters[parsed.vuid] = parsed
            rejects.pop(parsed.vuid, None)
        else:
            rejects[parsed.vuid] = parsed
            voters.pop(parsed.vuid, None)
    return voters, rejects


@dataclass(frozen=True)
class LinkInputs:
    """What one link reads besides D1: the voterfile, the target VUIDs, the dated early days."""

    voterfile: Path
    targets: set[str]
    dated: DatedHistory | None
    today: date


def link_voters(
    store: VoterStore,
    database_id: str,
    election_id: str,
    inputs: LinkInputs,
    *,
    dry_run: bool,
    tables_present: bool = True,
    batch_rows: int = VOTER_ROWS_PER_STATEMENT,
) -> LinkResult:
    """Bring ``early_vote_voter`` to the voterfile for this election's voters; see the module."""
    voterfile, targets, dated = inputs.voterfile, inputs.targets, inputs.dated
    voters, rejects = _parse_all(voterfile, targets, dated, inputs.today)
    order = sorted(targets)
    hashes: dict[str, str] = {}
    stored_rejects: dict[str, tuple[str, object]] = {}
    stored_meta: dict[str, str] = {}
    if tables_present:
        for row in _stored(store, database_id, VOTER_HASHES_SQL, order):
            hashes[_text(row.get("vuid"))] = _text(row.get("src_hash"))
        for row in _stored(store, database_id, REJECTS_SQL, order):
            stored_rejects[_text(row.get("vuid"))] = (
                _text(row.get("reason")),
                json.loads(_text(row.get("raw")) or "null"),
            )
        stored_meta = {
            _text(row.get("name")): _text(row.get("value"))
            for row in store._query_rows(  # noqa: SLF001
                database_id, META_SELECT_SQL, []
            )
        }
    changed = [v for vuid, v in sorted(voters.items()) if hashes.get(vuid) != str(v.src_hash)]
    changed_rejects = [
        r for vuid, r in sorted(rejects.items()) if stored_rejects.get(vuid) != (r.reason, r.raw)
    ]
    cleared = sorted(vuid for vuid in stored_rejects if vuid in voters)
    counts = {
        "targets": len(targets),
        "matched": len(voters) + len(rejects),
        "rejects": len(rejects),
        "invalid_dob": sum(bool(v.flags & FLAG_DOB_INVALID) for v in voters.values()),
        "invalid_edr": sum(bool(v.flags & FLAG_EDR_INVALID) for v in voters.values()),
    }
    meta = {
        "voterfile": _dump(_file_identity(voterfile)),
        "election_id": election_id,
        "score_version": str(SCORE_VERSION),
        "seeds": hashlib.sha256(_dump(seed_statements()).encode()).hexdigest(),
        "counts": _dump(counts),
        "dated_found": _dump(dated.found if dated else {}),
    }
    work = bool(changed or changed_rejects or cleared or stored_meta != meta)
    status: LinkStatus = "dry_run" if dry_run else ("wrote" if work else "unchanged")
    if status == "wrote":
        store._statements(database_id, [(sql, p) for sql, p in seed_statements()])  # noqa: SLF001
        _write(store, database_id, VOTER_UPSERT_SQL, [voter_row(v) for v in changed], batch_rows)
        _write(
            store,
            database_id,
            REJECT_UPSERT_SQL,
            [[r.vuid, r.reason, r.raw] for r in changed_rejects],
            batch_rows,
        )
        _write(store, database_id, REJECT_DELETE_SQL, cleared, batch_rows)
        _write(store, database_id, META_UPSERT_SQL, sorted(meta.items()), batch_rows)
    result = LinkResult(
        status=status,
        targets=counts["targets"],
        matched=counts["matched"],
        rejects=counts["rejects"],
        invalid_dob=counts["invalid_dob"],
        invalid_edr=counts["invalid_edr"],
        written=len(changed) if status == "wrote" else 0,
        unchanged=len(voters) - len(changed),
        pending=len(changed),
        rejects_written=len(changed_rejects) if status == "wrote" else 0,
        dated_found=dated.found if dated else {},
    )
    logger.info(
        "voter link election_id=%s status=%s targets=%s matched=%s matched_share=%.4f "
        "unmatched=%s rejects=%s invalid_dob=%s invalid_edr=%s written=%s unchanged=%s "
        "pending=%s rejects_written=%s rejects_cleared=%s dated_found=%s",
        election_id,
        result.status,
        result.targets,
        result.matched,
        result.matched_share,
        result.unmatched,
        result.rejects,
        result.invalid_dob,
        result.invalid_edr,
        result.written,
        result.unchanged,
        result.pending,
        result.rejects_written,
        len(cleared) if status == "wrote" else 0,
        json.dumps(result.dated_found, sort_keys=True),
    )
    return result


def run_voter_link_on_store(
    store: VoterLinkStore,
    election_id: str,
    voterfile: Path,
    *,
    dry_run: bool,
    fetcher: StatewideFetcher | None = None,
    cache_dir: Path | None = None,
    today: date | None = None,
    batch_rows: int = VOTER_ROWS_PER_STATEMENT,
) -> LinkResult | None:
    """The whole link for one election. Without an election database there is nothing to do.

    With a ``fetcher`` the dated early days are loaded too (``--dated-history``).
    """
    election_id = require_election_id(election_id)
    current = next(e for e in ELECTION_CALENDAR if e.is_current)
    if int(election_id) not in current.civix_ids:
        logger.warning(
            "voter link election_id=%s is not the calendar's current election %s",
            election_id,
            current.key,
        )
    database_id = store._database_uuid_if_exists(election_id)  # noqa: SLF001
    if database_id is None:
        logger.info("voter link election_id=%s has no D1 database", election_id)
        return None
    if dry_run:
        present = _tables_present(store, database_id)
        if not present:
            logger.info("voter link dry run: voter tables absent, nothing is created")
    else:
        store._ensure_schema(database_id)  # noqa: SLF001
        present = True
    targets = target_vuids(store, database_id, election_id)
    dated = None
    if fetcher is not None:
        dated = load_dated_history(fetcher, targets, (cache_dir or DEFAULT_CACHE_DIR).expanduser())
    inputs = LinkInputs(voterfile, targets, dated, today or datetime.now(CENTRAL).date())
    return link_voters(
        store,
        database_id,
        election_id,
        inputs,
        dry_run=dry_run,
        tables_present=present,
        batch_rows=batch_rows,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Link turnout voters to voterfile attributes.")
    parser.add_argument("--voterfile", type=Path, default=os.environ.get("TX_VOTERFILE"))
    parser.add_argument("--election-id", required=True)
    parser.add_argument("--dated-history", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    if args.voterfile is None:
        parser.error("pass --voterfile or set TX_VOTERFILE")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = _cloudflare_config_from_env()
    if config is None:
        raise EarlyVoteSyncError(
            "voter link requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id, api_token=api_token, kv_namespace_id=namespace_id
    )
    fetcher = None
    if args.dated_history:
        from texas_turnout_scraper.civix.evr import CivixClient

        fetcher = CivixClient()
    try:
        result = run_voter_link_on_store(
            store,
            args.election_id,
            Path(args.voterfile),
            dry_run=args.dry_run,
            fetcher=fetcher,
            cache_dir=args.cache_dir,
        )
    finally:
        store.close()
        if fetcher is not None:
            fetcher.close()
    if result is None:
        sys.stdout.write("status=no_database\n")
        return 0
    lines = [
        f"status={result.status}",
        f"targets={result.targets}",
        f"matched={result.matched}",
        f"matched_share={result.matched_share:.4f}",
        f"unmatched={result.unmatched}",
        f"rejects={result.rejects}",
        f"invalid_dob={result.invalid_dob}",
        f"invalid_edr={result.invalid_edr}",
        f"written={result.written}",
        f"unchanged={result.unchanged}",
        f"pending={result.pending}",
        f"rejects_written={result.rejects_written}",
        *(f"dated_{key}={n}" for key, n in result.dated_found.items()),
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
