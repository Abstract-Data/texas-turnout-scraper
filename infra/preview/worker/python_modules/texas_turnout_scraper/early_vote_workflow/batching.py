from __future__ import annotations

from collections.abc import Sequence

DEFAULT_ROWS_PER_STATEMENT = 4000
DEFAULT_STATEMENTS_PER_BATCH = 4


def batch_row_groups(
    rows: Sequence[object],
    *,
    rows_per_statement: int = DEFAULT_ROWS_PER_STATEMENT,
    statements_per_batch: int = DEFAULT_STATEMENTS_PER_BATCH,
) -> list[list[list[object]]]:
    """Group rows into D1 ``batch()`` calls (statements × rows per statement)."""
    if rows_per_statement < 1 or statements_per_batch < 1:
        raise ValueError("batch sizes must be positive")
    statements: list[list[object]] = []
    for start in range(0, len(rows), rows_per_statement):
        statements.append(list(rows[start : start + rows_per_statement]))
    batches: list[list[list[object]]] = []
    for start in range(0, len(statements), statements_per_batch):
        batches.append(statements[start : start + statements_per_batch])
    return batches


def halve_batch_size(rows_per_statement: int) -> int:
    """Retry helper when D1 returns overloaded / timeout."""
    return max(1, rows_per_statement // 2)
