"""Deciding whether a rewrite returns the same rows as the original."""
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from _03_catalog import Catalog
from _04_sampling import sample_predicate_sql

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
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return value
    return round(number / tolerance) if tolerance else round(number, 9)


def multiset(rows: list[tuple], tolerance: float = DEFAULT_TOLERANCE) -> Counter:
    """Rows as an order insensitive multiset with floats quantised."""
    return Counter(tuple(_normalise(v, tolerance) for v in row) for row in rows)


def compare(
    dialect: Any,
    conn: Any,
    original_sql: str,
    candidate_sql: str,
    *,
    catalog: Catalog | None = None,
    schema: str | None = None,
    limit: int = DEFAULT_LIMIT,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Equivalence:
    """Run both statements and compare their results."""
    left_sql = original_sql
    right_sql = candidate_sql
    if schema is not None:
        if catalog is None:
            raise ValueError("a catalog is needed to retarget a query at a sample schema")
        left_sql = sample_predicate_sql(dialect, original_sql, catalog, schema)
        right_sql = sample_predicate_sql(dialect, candidate_sql, catalog, schema)

    try:
        left = dialect.fetch(conn, left_sql, limit)
    except Exception as err:
        return Equivalence(False, f"the original did not run on the sample: {_short(err)}")
    try:
        right = dialect.fetch(conn, right_sql, limit)
    except Exception as err:
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
            f"the candidate returns {len(right.columns)} columns, the original returns {len(left.columns)}",
            columns=len(right.columns),
        )

    left_set = multiset(left.rows, tolerance)
    right_set = multiset(right.rows, tolerance)
    if left_set == right_set:
        return Equivalence(True, rows=left_set.total(), columns=len(left.columns))

    missing_diff = left_set - right_set
    extra_diff = right_set - left_set
    missing_elements = list(missing_diff.elements())
    extra_elements = list(extra_diff.elements())
    missing = missing_elements[:3]
    extra = extra_elements[:3]

    parts = [f"{len(missing_elements)} rows missing, {len(extra_elements)} rows unexpected"]
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