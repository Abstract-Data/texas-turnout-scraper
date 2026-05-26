# WS-5E — accumulate_roster single-pass
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-5e-writer-perf` |
| Base | `feature/review-remediation` (after WS-4C) |
| Model | claude-sonnet-4-6 |
| Wave | 5 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/writer.py` (`accumulate_roster` / duplicate tracking only)

### Forbidden (MUST NOT touch)

- `audit.py` / `audit_records` (WS-3)

### Close issues

- Performance bonus (writer)

## Verification subset

```bash
uv run pytest tests/unit/test_writer.py -v
```

## Mechanical spec (from v1.0.0)

### Performance bonus — `accumulate_roster` single-pass

**File:** `src/texas_turnout_scraper/writer.py:100-117`

Five `defaultdict[set]` + one `defaultdict[list]` over the full record set. For a
statewide ~2M-record roster, that's 5×2M Python sets in memory. Replace with one
`defaultdict[str, _VuidAggregate]` and a single pass:

```python
@dataclass
class _VuidAggregate:
    counties: set[str] = field(default_factory=set)
    methods: set[VoteMethod] = field(default_factory=set)
    dates: set[date] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    precincts: set[str] = field(default_factory=set)
    rows: list[int] = field(default_factory=list)
```
