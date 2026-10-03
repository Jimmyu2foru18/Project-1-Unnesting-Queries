"""Deciding whether a rewrite returns the same rows as the original.

Both statements are run against the sampled copy and their results compared as
multisets, so a candidate that drops rows, duplicates them or changes a value is
caught regardless of what the model said about it. Row order is ignored, because
a rewrite that reorders without reordering the ORDER BY is not a different
answer. Floats are compared with a tolerance, because two runs of the same
aggregate can differ in the last bits depending on how the work is split.

Three outcomes are possible and they are kept apart on purpose: equal,
different, and unknown. "Unknown" means the check could not be run at all, for
instance because the result was too large to hold, and it must not be reported
to a caller as agreement.
"""
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlrewriter.catalog import Catalog
from sqlrewriter.dialects import Dialect
from sqlrewriter.sampling import sample_predicate_sql

DEFAULT_LIMIT = 200_000
DEFAULT_TOLERANCE = 1e-9


@dataclass
class Equivalence:
    """The outcome of comparing two statements."""

    equal: bool | None = None
    reason: str = ""
    rows: int = 0
    columns: int = 0
    truncated: bool = False
    missing: list[tuple] = field(default_factory=list)
    extra: list[tuple] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "equal" if self.equal is True else ("unknown" if self.equal is None else "different")

    def describe(self) -> str:
        if self.equal is True:
            return f"equal on {self.rows} rows"
        if self.equal is None:
            return f"not compared: {self.reason}"
        return self.reason


def _normalise(value: Any, tolerance: float) -> Any:
    if value is None or isinstance(value, (bool, int, str, bytes)):
        return value
    if isinstance(value, float):
        return round(value, 9) if math.isfinite(value) else value
    if isinstance(value, (list, tuple)):
        return tuple(_normalise(v, tolerance) for v in value)
    if isinstance(value, (int,)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return value
    return round(number / tolerance) if tolerance else round(number, 9)


def multiset(rows: list[tuple], tolerance: float = DEFAULT_TOLERANCE) -> Counter:
    """Rows as an order-insensitive multiset with floats quantised."""
    return Counter(tuple(_normalise(v, tolerance) for v in row) for row in rows)


def compare(
    dialect: Dialect,
    conn,
    original_sql: str,
    candidate_sql: str,
    *,
    catalog: Catalog | None = None,
    schema: str | None = None,
    limit: int = DEFAULT_LIMIT,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Equivalence:
    """Run both statements and compare their results.

    When ``schema`` is given, both statements are pointed at that schema first,
    which is how the sampled copy is used.
    """
    left_sql = original_sql
    right_sql = candidate_sql
    if schema is not None:
        if catalog is None:
            raise ValueError("a catalog is needed to retarget a query at a sample schema")
        left_sql = sample_predicate_sql(dialect, original_sql, catalog, schema)
        right_sql = sample_predicate_sql(dialect, candidate_sql, catalog, schema)

    try:
        left = dialect.fetch(conn, left_sql, limit)
    except Exception as err:  # noqa: BLE001
        return Equivalence(False, f"the original did not run on the sample: {_short(err)}")
    try:
        right = dialect.fetch(conn, right_sql, limit)
    except Exception as err:  # noqa: BLE001
        return Equivalence(False, f"the candidate did not run on the sample: {_short(err)}")

    if left.truncated or right.truncated:
        return Equivalence(
            None,
            f"results exceed the {limit} row comparison limit",
            columns=len(right.columns),
            truncated=True,
        )
    if len(left.columns) != len(right.columns):
        return Equivalence(
            False,
            f"the candidate returns {len(right.columns)} columns, the original returns "
            f"{len(left.columns)}",
            columns=len(right.columns),
        )

    left_set = multiset(left.rows, tolerance)
    right_set = multiset(right.rows, tolerance)
    if left_set == right_set:
        return Equivalence(True, rows=left_set.total(), columns=len(left.columns))

    missing = list((left_set - right_set).elements())[:3]
    extra = list((right_set - left_set).elements())[:3]
    parts = [f"{len(list((left_set - right_set).elements()))} rows missing, "
             f"{len(list((right_set - left_set).elements()))} rows unexpected"]
    if missing:
        parts.append(f"only the original returns {missing}")
    if extra:
        parts.append(f"only the candidate returns {extra}")
    return Equivalence(
        False,
        "; ".join(parts),
        rows=left_set.total(),
        columns=len(left.columns),
        missing=missing,
        extra=extra,
    )


def _short(err: Exception) -> str:
    return " ".join(str(err).split())[:240]