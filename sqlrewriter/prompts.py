"""Prompts for query rewriting.

Two turns rather than one. The first asks only for analysis: which relation is
the driving one, which key each join uses, what has to be filtered before it is
joined, and what must stay true about the answer. The second asks for the SQL
with that analysis in hand. Splitting them keeps expensive deliberation on the
part that benefits from it and keeps the emitted statement short and stable.
"""

CONTRACT = """\
You rewrite a SQL query so it runs faster and returns exactly the same rows.

Answer contract:
- Reason in the analysis channel. The final channel carries the statement and
  nothing else: no preamble, no commentary, no markdown fence.
- Use only the tables and columns named in the schema block. A name you invent
  is a failed rewrite.
- Return one statement. Do not chain anything after it.
- Same rows, same columns, same column order, same NULL and duplicate
  semantics. A faster query that answers a different question is a failure.

Performance contract:
- Join only on the key pairs listed as joinable. Inventing an equality between
  two columns that no constraint relates forces a scan of the larger side.
- Filter before you fan out. When a relation has many rows per parent, restrict
  it in its own subquery first, then join. Do not join and filter afterwards.
- Aggregate before you join. Group the child relation by the join key, then
  join the small grouped result. Joining first and aggregating after repeats
  the large work once per surviving outer row.
- Compare raw columns. Wrapping an indexed column in a function or a cast makes
  it unsearchable.
- Do not introduce a CROSS JOIN where a key equality was available.
- Prefer an anti-join or a semi-join to a join that produces rows you then
  filter away.

Correctness contract:
- Never turn a semi-join into a join that duplicates rows. If the original
  cannot duplicate a row, neither can the rewrite.
- Never turn NOT EXISTS into NOT IN. They disagree whenever the subquery
  produces a NULL.
- A rewrite that drops NULL rows where the original kept them is wrong even when
  the row counts look plausible.
- Keep ORDER BY, DISTINCT and LIMIT, or explain why the rewrite makes one of
  them unnecessary.
"""

def system_for(hints: str = "") -> str:
    """The system prompt, plus workload guidance when the workload supplies it."""
    return f"{CONTRACT}\n{hints.strip()}\n" if hints.strip() else CONTRACT


PLAN = """\
Work out the rewrite before writing any SQL. Do not produce SQL in this step.

Answer these:
1. shape: which relation drives the query, and which ones are looked up per
   driving row;
2. keys: for every join you intend to write, the exact key pair and which side
   holds more rows;
3. pushdown: every predicate that should be evaluated before a relation is
   joined rather than after;
4. aggregation: every relation that must be reduced with GROUP BY first, and by
   which key;
5. duplicates: whether the original can return the same row twice for one key,
   and therefore whether the rewrite must use DISTINCT or a semi-join;
6. preserved: what must stay true of the result, including NULL rows, ordering
   and any outer row with no match.

Then state, in one sentence, the single change that will do most of the work.

Schema block:
{schema}

Query to rewrite:
{sql}

Target database: {dialect}
"""

WRITE = """\
Write the rewritten query now. Keep the select list, the column order, the
aliases, the filter semantics, the NULL behaviour and the ORDER BY of the
original.

Analysis you already did:
{plan}

Schema block:
{schema}

Original query:
{sql}

Target database: {dialect}

The final answer is the statement and nothing else.
"""

REPAIR = """\
This rewrite was rejected. Fix it and return the whole statement again.

Reasons:
{problems}

Rejected rewrite:
{rejected}

Schema block:
{schema}

Original query:
{sql}

Target database: {dialect}

The final answer is the corrected statement and nothing else.
"""

VARIANTS = """\
Write a different correct rewrite of the same query. Stay within the schema,
keep the exact result, and take a different approach from this one:

{variant}

Schema block:
{schema}

Original query:
{sql}

Target database: {dialect}

The final answer is the statement and nothing else.
"""

DIVERSITY = (
    "pre-aggregate in a CTE before joining",
    "filter the large relation in its own subquery, then join",
    "replace a self-join with a window function",
    "replace a window function with a grouped CTE",
    "use an anti-join instead of a filtered join",
    "use a semi-join instead of a join with a filter",
    "reorder the joins so the smallest relation drives",
    "push the filter into the joined subquery",
)