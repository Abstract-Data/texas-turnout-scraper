# WS-1G — CountyRoster count properties
# Version: 1.1.0 (workstream)
# Parent: [v1.1.0.md](../v1.1.0.md) · Manifest: [parallel-manifest.md](../parallel-manifest.md)

## Agent contract

| Field | Value |
|-------|-------|
| Branch | `feature/review-remediation/ws-1g-models-props` |
| Base | `feature/review-remediation` |
| Model | claude-sonnet-4-6 |
| Wave | 1 |
| Exec | parallel |

### File lock (MAY edit)

- `src/texas_turnout_scraper/models.py` — add `CountyRoster.in_person_count` / `mail_in_count` only

### Forbidden (MUST NOT touch)

- `cli.py`, `mcp_server.py` call sites (WS-1H)
- Other `models.py` changes

### Close issues

- RF-DRY-003 (partial — properties only)

### On conflict

Stop; WS-1H wires call sites after this merges.

## Verification subset

```bash
uv run ty check
uv run pytest tests/unit/test_writer.py -q -k roster
```

## Mechanical spec (from v1.0.0)

### RF-DRY-003 — Add `in_person_count` / `mail_in_count` to `CountyRoster` (properties only)

**Files:** `src/texas_turnout_scraper/models.py` (add properties); call sites deferred to WS-1H.

The same `sum(1 for r in roster.records if r.voting_method.value == "IN-PERSON")` is
duplicated three times. Comparing against `.value` instead of the enum member is a smell
on its own. Add:

```python
class CountyRoster(BaseModel):
    ...
    @property
    def in_person_count(self) -> int:
        return sum(1 for r in self.records if r.voting_method is VoteMethod.IN_PERSON)

    @property
    def mail_in_count(self) -> int:
        return sum(1 for r in self.records if r.voting_method is VoteMethod.MAIL_IN)
```

WS-1H replaces call sites in `cli.py:730-731`, `mcp_server.py:155-156, 348-349` with
`roster.in_person_count` / `roster.mail_in_count`.
