"""Checks a rewrite must pass before anything else happens to it."""

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from _01_core import Verdict
from _02_dialects import DuckDB
from _03_catalog import Catalog


def _single(sql: str, dialect: str) -> tuple[exp.Expression | None, list[str]]:
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except (sqlglot.ParseError, ValueError) as err:
        return None, [f"syntax error: {str(err).splitlines()[0]}"]
    if not statements:
        return None, ["the model returned no SQL"]
    if len(statements) != 1:
        return None, [f"expected one statement, found {len(statements)}"]
    return statements[0], []


def _outer_sources(scope) -> set[str]:
    names: set[str] = set()
    parent = scope.parent
    while parent is not None:
        names |= set(parent.sources)
        parent = parent.parent
    return names


def correlated_columns(tree: exp.Expression) -> list[str]:
    """Columns a subquery borrows from an enclosing query: real correlation."""
    found = []
    for scope in traverse_scope(tree):
        if scope.parent is None:
            continue
        own, outer = set(scope.sources), _outer_sources(scope)
        found.extend(
            column.sql()
            for column in scope.external_columns
            if column.table and column.table not in own and column.table in outer
        )
    return sorted(set(found))


def alias_map(tree: exp.Expression) -> dict[str, str]:
    return {table.alias_or_name: table.name for table in tree.find_all(exp.Table)}


def _group_keys(select: exp.Select) -> set[str]:
    group = select.args.get("group")
    return {c.name for c in group.expressions if isinstance(c, exp.Column)} if group else set()


def _projections(select: exp.Select) -> dict[str, str | None]:
    """Output name to the grouped column it carries, or None if computed."""
    out: dict[str, str | None] = {}
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star):
            continue
        out[projection.alias_or_name] = inner.name if isinstance(inner, exp.Column) else None
    return out


def _single_source_key(select: exp.Select, wanted: set[str], catalog: Catalog) -> bool:
    """Whether an ungrouped CTE is already unique on wanted."""
    tables = list(select.find_all(exp.Table))
    if len(tables) != 1 or list(select.find_all(exp.Join)):
        return False
    name = tables[0].name
    if name not in catalog.columns:
        return False
    outputs = _projections(select)
    keys = {(column,) for column in catalog.primary.get(name, [])}
    keys |= set(catalog.unique.get(name, []))
    return all((outputs.get(key),) in keys for key in wanted)


def fan_out_problems(tree: exp.Expression, catalog: Catalog) -> list[str]:
    """Every relation feeding a join must yield at most one row per join key."""
    ctes = {cte.alias_or_name: cte for cte in tree.find_all(exp.CTE)}
    if not ctes:
        return []

    names_by_cte: dict[str, set[str]] = {}
    for table in tree.find_all(exp.Table):
        if table.name in ctes:
            names_by_cte.setdefault(table.name, set()).update({table.name, table.alias_or_name})

    join_keys = [
        side
        for join in tree.find_all(exp.Join)
        if join.args.get("on") is not None
        for equality in join.args["on"].find_all(exp.EQ)
        for side in (equality.left, equality.right)
        if isinstance(side, exp.Column)
    ]

    problems = []
    for name, cte in ctes.items():
        names = names_by_cte.get(name)
        select = cte.this
        if not names or not isinstance(select, exp.Select):
            continue
        wanted = {side.name for side in join_keys if side.table in names}
        if not wanted:
            continue
        if select.args.get("distinct"):
            continue
        if list(select.find_all(exp.Window)):
            problems.append(
                f"cte {name!r} uses a window function and is joined on {sorted(wanted)}; a window "
                "function emits one row per input row, so the join duplicates rows"
            )
            continue
        grouped = _group_keys(select)
        if grouped:
            outputs = _projections(select)
            unsafe = sorted(key for key in wanted if outputs.get(key) not in grouped)
            if unsafe:
                problems.append(
                    f"cte {name!r} is joined on {unsafe}, which its group by {sorted(grouped)} does "
                    "not make unique; the join is not key to key and can duplicate rows"
                )
            continue
        if _single_source_key(select, wanted, catalog):
            continue
        problems.append(
            f"cte {name!r} is joined on {sorted(wanted)} but has no group by and no distinct, so it "
            "can emit several rows per key and duplicate the outer rows"
        )
    return problems


def _has_not_exists(tree: exp.Expression) -> bool:
    return any(isinstance(node.parent, exp.Not) for node in tree.find_all(exp.Exists))


def _has_not_in(tree: exp.Expression) -> bool:
    return any(isinstance(node.parent, exp.Not) for node in tree.find_all(exp.In))


def static_problems(sql: str, dialect: DuckDB, catalog: Catalog) -> list[str]:
    """Parses, is one read-only statement, and is grounded in the catalog."""
    try:
        dialect.check_statement(sql)
    except Exception as err:
        return [" ".join(str(err).split())[:200]]

    tree, problems = _single(sql, dialect.sqlglot)
    if tree is None:
        return problems

    from sqlglot.optimizer.qualify import qualify

    referenced = {t.name for t in tree.find_all(exp.Table)} - {
        cte.alias_or_name for cte in tree.find_all(exp.CTE)
    }
    unknown = sorted(name for name in referenced if not catalog.known(name))
    if unknown:
        return [f"table {name!r} does not exist" for name in unknown]

    try:
        qualify(
            tree.copy(), schema=catalog.columns, dialect=dialect.sqlglot,
            validate_qualify_columns=True,
        )
    except Exception as err:
        return [f"{type(err).__name__}: {str(err).splitlines()[0]}"]
    return []


def structural_problems(
    sql: str, original_sql: str, dialect: DuckDB, catalog: Catalog
) -> list[str]:
    """The rewrite happened, and it did not change the question."""
    tree, problems = _single(sql, dialect.sqlglot)
    if tree is None:
        return problems

    leftover = correlated_columns(tree)
    if leftover:
        problems.append(
            "the query is still correlated on "
            + ", ".join(leftover)
            + "; the inner query borrows outer columns"
        )
    problems.extend(fan_out_problems(tree, catalog))

    source, source_errors = _single(original_sql, dialect.sqlglot)
    if source is not None and not source_errors:
        if _has_not_exists(source) and _has_not_in(tree):
            problems.append(
                "not exists was rewritten as not in; they disagree whenever the subquery "
                "produces a null"
            )
        if source.find(exp.Distinct) and not tree.find(exp.Distinct):
            problems.append("the original used distinct but the rewrite dropped it")
        if source.find(exp.Order) is not None and tree.find(exp.Order) is None:
            problems.append("the original ordered its output but the rewrite does not")
        if source.find(exp.Limit) and not tree.find(exp.Limit):
            problems.append("the original used limit but the rewrite does not")
    return problems


def sargability_problems(sql: str, dialect: DuckDB, catalog: Catalog) -> list[str]:
    """A cast or function around an indexed column discards its index."""
    tree, problems = _single(sql, dialect.sqlglot)
    if tree is None:
        return problems
    tables = alias_map(tree)
    found = []
    for equality in tree.find_all(exp.EQ):
        for side in (equality.left, equality.right):
            node = side.unnest() if isinstance(side, exp.Paren) else side
            if not isinstance(node, (exp.Cast, exp.Anonymous)):
                continue
            for column in node.find_all(exp.Column):
                table = tables.get(column.table, column.table)
                if column.name in catalog.indexed.get(table, ()):
                    found.append(
                        f"{node.sql()} wraps the indexed column {table}.{column.name}; compare "
                        "the raw column instead"
                    )
    return found


def check(
    candidate_sql: str, original_sql: str, dialect: DuckDB, catalog: Catalog
) -> Verdict:
    """Run every static gate, cheapest first."""
    verdict = Verdict()
    verdict.absorb(static_problems(candidate_sql, dialect, catalog))
    if not verdict.ok:
        return verdict
    verdict.absorb(structural_problems(candidate_sql, original_sql, dialect, catalog))
    verdict.absorb(sargability_problems(candidate_sql, dialect, catalog))
    if verdict.ok:
        verdict.note("static and structural gates passed")
    return verdict