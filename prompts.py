"""The system prompt: relational-algebra rules for turning correlated subqueries into joins."""

RULES = """\
Rewrite the query by replacing every correlated subquery with an explicit join.

Return only the SQL. No prose, no explanation, no markdown code fences.
Keep the original select list, aliases, filter semantics and ORDER BY.
Use only the tables and columns in the schema below; never invent a name.

Rules, chosen by the subquery's pattern:

scalar correlated aggregate (AVG/MAX/MIN/SUM/COUNT)
  Pre-aggregate in a CTE grouped by the correlation key, then equi-join on that key.
  An outer row whose key has no matching group must drop out, which is what the
  subquery's NULL already does. Do not add an outer join and do not substitute 0.

EXISTS
  Join to a DISTINCT set of the correlation key. A semi-join, so no row duplicates.

NOT EXISTS
  LEFT JOIN that DISTINCT set and keep rows where the key is NULL. An anti-join.
  Never rewrite NOT EXISTS as NOT IN.

IN
  Join to a DISTINCT set of matching keys.

NOT IN
  Use the anti-join above. If the source filters NULLs out of the subquery, keep that
  filter: dropping it changes the answer, because NOT IN is never true for a NULL.

count of peers beating a threshold ("more than N rows", "not in the top N")
  Use COUNT(*) OVER (PARTITION BY key). Do not use DENSE_RANK: it counts distinct
  values, not rows, so ties rank differently from the original.

nested EXISTS
  Compose one semi-join per level, and keep the middle query's correlation key in
  the join condition.

Every CTE must yield exactly one row per correlation key. A window function or an
extra join that emits two rows for a key will duplicate that key's outer rows.
"""

SYSTEM_PROMPT = RULES + """

Schema:
{schema}

The query to rewrite:
{sql}
"""

REPAIR_PROMPT = """

Your previous attempt was rejected:
{problems}

Rejected SQL:
{rejected}

Return a corrected query that returns the same rows. Reply with the SQL only.
"""
