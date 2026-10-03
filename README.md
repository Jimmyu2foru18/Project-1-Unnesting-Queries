# SQL Rewriter

A query-rewriting tool. It reads a database's catalog and statistics, asks a large
language model for faster rewrites of a query, checks that each rewrite returns the
same rows, then times the survivors and ranks them.

It works on PostgreSQL, MySQL and DuckDB. Nothing in it is specific to one dataset or
one model provider.

## How it works

```
dialect ──> catalog ──> rewriter ──> verify ──> equivalence ──> rank
             (schema      (LLM)       (static     (on a sample)   (timing
             + stats)                  gates)                      + plans)
```

1. **Introspect** (`sqlrewriter/dialects.py`, `sqlrewriter/catalog.py`). Collect
   columns, types, nullability, primary keys, unique constraints, foreign keys,
   indexes and approximate row counts. Only the tables a query actually touches are
   sent to the model, plus their immediate neighbours across foreign keys.
2. **Rewrite** (`sqlrewriter/rewrite.py`). One planning turn produces an analysis of
   the query, then several statements are emitted from it, each pushed along a
   different route so the candidates are genuinely different rather than re-samplings
   of one idea. Candidates are deduplicated and checked; a rejected one is repaired
   with its specific failures attached rather than a request to try again.
3. **Verify** (`sqlrewriter/verify.py`). Static gates before anything runs: read-only,
   one statement, parses in the target dialect, and only known tables and columns.
   Then structural checks that catch the rewrites which break results quietly:
   `DISTINCT` disappearing under a fan-out, grouping cardinality changing, a filter
   moving outside the aggregate it belonged to, and predicates that stop being
   sargable.
4. **Check equivalence** (`sqlrewriter/equivalence.py`). The original and the
   candidate are both run inside a foreign-key-preserving sample
   (`sqlrewriter/sampling.py`) and their results compared as multisets, with float
   tolerance. `ORDER BY` is respected when both queries have one. The answer is
   tri-state: equal, different, or unknown when the result set was too large to
   compare. Unknown is never reported as equal.
5. **Rank** (`sqlrewriter/rank.py`). Median wall-clock over several runs, plus the
   original measured the same way on the same connection, so speedups are
   like-for-like. Plans are then inspected for the two things timing cannot show: a
   rescanned node and a sequential scan of a large table that has an index.

A candidate that is correct but slower than the original is kept and ranked last.
On some queries the honest answer is that the database was already doing better.

## Install

```bash
pip install -r requirements.txt
python scripts/setup.py --check
```

Needs Python 3.11 or newer.

## Configure

```bash
python scripts/configure_keys.py            # writes .env, never prints secrets
```

Only one provider is needed. Ollama needs nothing:

```bash
# .env
OLLAMA_MODEL=gpt-oss:20b
OLLAMA_HOST=http://localhost:11434
```

For a hosted provider set `OPENAI_API_KEY` or `GEMINI_API_KEY`. For a database,
set `SQLRW_DSN` and `SQLRW_ENGINE`. See `.env.example` for every variable.

## Load the sample data

The IMDb dataset is a convenience, not a dependency. Any database works.

```bash
python scripts/load_data.py --dump imdb.7z --engine duckdb --out data/imdb.duckdb --rebuild
```

The loader extracts the archive, strips what DuckDB cannot parse, orders the tables
by their foreign-key dependencies, and loads in batches. Load order matters: the
dump declares foreign keys before the parent tables exist, and the `movies` table has
duplicate primary keys, so rows are inserted with `INSERT OR IGNORE` and the
duplicates are reported rather than dropped silently.

## Run it

```bash
# what would be sent to the model
python -m sqlrewriter show --engine duckdb --dsn data/imdb.duckdb --schema main

# the whole experiment
python -m sqlrewriter rewrite \
    --engine duckdb --dsn data/imdb.duckdb --schema main \
    --workload workloads/imdb_nested \
    --model ollama/gpt-oss:20b \
    --out benchmarks/results/imdb_nested.json
```

Every subcommand:

| Command | What it does |
| --- | --- |
| `show` | Prints the schema block sent to the model. The quickest way to check introspection and pruning. |
| `catalog` | Dumps the catalog as JSON. |
| `verify` | Checks one candidate against the original, optionally inside a sample. Exits non-zero when they differ. |
| `rewrite` | Samples, generates, verifies and ranks a workload. |

Useful flags for `rewrite`:

| Flag | Default | Why you would change it |
| --- | --- | --- |
| `--variants` | 3 | Candidates per query. More costs more and may add duplicates. |
| `--attempts` | 2 | Repair attempts per rejected candidate. |
| `--sample-percent` | 1.0 | Size of the verification sample. Bigger catches more, costs more. |
| `--no-sample` | off | Check on the full data. Slow, but conclusive. |
| `--runs` | 5 | Timing runs per statement. More stabilises small differences. |
| `--baseline-budget-ms` | 0 (off) | Stop re-timing an original that is already known to be slow. |
| `--only` / `--limit` | all | Narrow to specific query ids. |

### Workloads

A workload is a directory with `queries.sql` and an optional `workload.json`.

```sql
-- Q01: movies rated above eight
-- Description: a scalar correlated subquery in the predicate
SELECT m.title FROM movies m
WHERE (SELECT avg(r.rating) FROM ratings r WHERE r.movie_id = m.id) > 8.0;
```

```json
{
  "name": "imdb_nested",
  "engine": "duckdb",
  "schema": "main",
  "dsn": "data/imdb.duckdb",
  "hints": "prefer pre-aggregation over a correlated subquery",
  "tags": ["imdb", "nested"]
}
```

Annotation lines start with `-- Q<id>:` and the description is the `-- Description:`
line. Annotation comments are stripped before the statement is used, including ones
that end in a semicolon.

Two are bundled: `workloads/imdb_nested` (Q01–Q15, subqueries in the predicate,
select list, `FROM`, `HAVING` and `ORDER BY`) and `workloads/imdb_join` (J01–J12,
joins and aggregation).

## Models

`--model` takes `provider/model`.

| Spec | Backend |
| --- | --- |
| `ollama/gpt-oss:20b` | Local Ollama. Sends `think` as the reasoning effort. |
| `openai/gpt-4.1` | OpenAI chat completions. Sends `reasoning_effort` where supported. |
| `gemini/gemini-2.5-pro` | Gemini via `google-genai`. Sets a thinking budget. |
| `echo/SELECT 1` | Returns a fixed statement. For exercising the pipeline offline. |

On a reasoning model the thinking budget and the answer budget come out of the same
allowance, so a long deliberation can consume the whole thing and leave the final
message empty. A reply with no SQL is retried at a cheaper reasoning effort rather
than being handed to the verifier.

Planning runs at high effort; writing a statement runs at low, because writing SQL is
not a reasoning problem. Repair runs in between.

## Tests

```bash
python -m pytest -q
```

244 tests. Most run against a real in-memory DuckDB rather than a mock, so sampling,
equivalence, timing and plans are exercised for real. No test needs a network
connection, an API key or a database server.

## Layout

```
sqlrewriter/
  dialects.py     connections, read-only checks, catalog queries, execution, timing, plans
  catalog.py      the Catalog, introspection, DDL imports, scoping to a query
  sampling.py     FK-preserving sample schemas, retargeting a query onto one
  equivalence.py  tri-state result comparison
  llm.py          Ollama, OpenAI, Gemini, echo backends
  prompts.py      the prompts
  verify.py       static and structural gates
  rewrite.py      plan, emit, diversify, repair
  rank.py         timing, plan inspection, reports
  workloads.py    workload loading
  cli.py          show, catalog, verify, rewrite
scripts/          setup, data loading, key configuration, experiment runner
workloads/        bundled workloads
```

## Known limits

- Verification is sampling. A rewrite can match on the sample and differ on the full
  data; that is inherent to sampling. Use `--no-sample` when a query must be
  conclusive.
- Speedups are wall-clock on one machine and reflect this data, this cache state and
  this engine. Compare within a run, not across runs.
- PostgreSQL and MySQL need a server. Only DuckDB is tested end to end here.