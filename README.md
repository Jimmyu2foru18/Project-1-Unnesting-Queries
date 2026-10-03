# SQL Rewriter

A tool that uses an LLM to rewrite SQL queries. It reads a database's catalog, asks the model for faster rewrites, checks that each rewrite returns the same rows, times the valid ones, and ranks them by speed.

Works on PostgreSQL, MySQL, and DuckDB. Not specific to one dataset or model provider.

## What it does

We start with a SQL query that runs slowly. The tool asks an LLM to rewrite it, then checks whether the rewrite is actually equivalent to the original before measuring performance. A rewrite is only considered better if it returns the same results faster.

## How the pipeline works

```
dialect → catalog → rewriter → verify → equivalence → rank
          (schema     (LLM)      (static    (on a sample)  (timing
          + stats)               gates)                     + plans)
```

1. **Introspect** (`sqlrewriter/dialects.py`, `sqlrewriter/catalog.py`). Read the database schema, keys, indexes, and approximate row counts. Only the tables the query actually uses (plus nearby tables linked by foreign keys) are sent to the model.

2. **Rewrite** (`sqlrewriter/rewrite.py`). The model first looks at how the query works, then generates several possible rewrites. We generate multiple rewrites because some may be valid but perform differently. Duplicates are removed. If a rewrite fails validation, it gets repaired with the specific error messages attached.

3. **Verify** (`sqlrewriter/verify.py`). Before anything runs, we check that the rewrite is read-only, parses correctly, only uses real tables and columns, and doesn't quietly change the query's meaning (dropping `DISTINCT`, moving filters outside aggregates, etc.).

4. **Check equivalence** (`sqlrewriter/equivalence.py`). Run the original and the rewrite on a sample of the data and compare the results as multisets (order doesn't matter). Floats use a tolerance. Three outcomes: equal, different, or unknown (when the result is too large to compare safely).

5. **Rank** (`sqlrewriter/rank.py`). Time each valid rewrite multiple times and use the median. The original is timed the same way, so speedups are like-for-like. We also inspect query plans to catch cases timing misses: a rescanned node or a sequential scan on a large table that has an index.

A rewrite that is correct but slower than the original is kept and ranked last. Sometimes the database was already doing a good job.

## Prompting strategies

The tool supports different ways of asking the LLM to rewrite a query. Select one with `--strategy`:

- `existing` -- the default plan-then-write approach. The model first explains the rewrite, then generates SQL.
- `zero-shot` -- a direct rewrite request with no example and no planning step.
- `one-shot` -- one worked example of unnesting, then the target query.
- `reasoning` -- asks for a short structured analysis before the SQL, without exposing private chain-of-thought.

The goal is to compare whether the prompting strategy affects success rate, correctness, or speedup. Everything else stays the same: same query, same schema, same verification.

## Install

```bash
pip install -r requirements.txt
python scripts/setup.py --check
```

Needs Python 3.11 or newer.

## Configure

```bash
python scripts/configure_keys.py
```

Only one model provider is needed. For local models, Ollama needs no API key:

```bash
# .env
OLLAMA_MODEL=gpt-oss:20b
OLLAMA_HOST=http://localhost:11434
```

For hosted providers, set `OPENAI_API_KEY` or `GEMINI_API_KEY`. For the database, set `SQLRW_DSN` and `SQLRW_ENGINE`. See `.env.example` for all options.

## Load the sample data

The IMDb dataset is included for convenience but is optional. Any database works.

```bash
python scripts/load_data.py --dump imdb.7z --engine duckdb --out data/imdb.duckdb --rebuild
```

The loader extracts the archive, skips SQL DuckDB can't parse, loads tables in dependency order, and uses `INSERT OR IGNORE` for duplicate primary keys in the `movies` table.

## Run it

```bash
# see what schema info gets sent to the model
python -m sqlrewriter show --engine duckdb --dsn data/imdb.duckdb --schema main

# run the full pipeline
python -m sqlrewriter rewrite \
    --engine duckdb --dsn data/imdb.duckdb --schema main \
    --workload workloads/imdb_nested \
    --model ollama/gpt-oss:20b \
    --strategy zero-shot \
    --out benchmarks/results/imdb_nested.json

# compare results from multiple strategy runs
python -m sqlrewriter compare benchmarks/results/*.json
```

Commands:

| Command | What it does |
| --- | --- |
| `show` | Print the schema block sent to the model. Good for checking introspection. |
| `catalog` | Dump the catalog as JSON. |
| `verify` | Check one candidate against the original, optionally inside a sample. |
| `rewrite` | Sample, generate, verify, and rank a workload. |
| `compare` | Summarise results from multiple strategy runs side by side. |

Useful flags for `rewrite`:

| Flag | Default | Why you would change it |
| --- | --- | --- |
| `--strategy` | `existing` | Which prompting approach to use. |
| `--variants` | 3 | Candidates per query. More costs more and may add duplicates. |
| `--attempts` | 2 | Repair attempts per rejected candidate. |
| `--sample-percent` | 1.0 | Size of the verification sample. Bigger catches more errors, costs more. |
| `--no-sample` | off | Check on the full data. Slow, but conclusive. |
| `--runs` | 5 | Timing runs per statement. More stabilises small differences. |
| `--baseline-budget-ms` | 0 (off) | Stop re-timing an original that is already known to be slow. |
| `--only` / `--limit` | all | Narrow to specific query ids. |

## Workloads

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

Queries are annotated with `-- Q<id>: description`. The `-- Description:` line is kept as metadata and stripped from the SQL before it runs.

Two workloads are included:
- `workloads/imdb_nested` -- Q01–Q15, subqueries in predicates, select lists, `FROM`, `HAVING`, and `ORDER BY`
- `workloads/imdb_join` -- J01–J12, joins and aggregation

## Models

`--model` takes `provider/model`.

| Spec | Backend |
| --- | --- |
| `ollama/gpt-oss:20b` | Local Ollama. Sends `think` as the reasoning effort. |
| `openai/gpt-4.1` | OpenAI chat completions. Sends `reasoning_effort` where supported. |
| `gemini/gemini-2.5-pro` | Gemini via `google-genai`. Sets a thinking budget. |
| `echo/SELECT 1` | Returns a fixed statement. Useful for exercising the pipeline offline. |

On reasoning models, thinking and answering share the same token budget. A long deliberation can leave no room for the final SQL. When that happens, the reply is retried at a cheaper reasoning effort instead of being sent to the verifier empty.

Planning uses high effort; writing SQL uses low effort, because writing SQL is not a reasoning problem. Repair uses medium effort.

## Tests

```bash
python -m pytest -q
```

269 tests. Most run against a real in-memory DuckDB, so sampling, equivalence, timing, and plans are exercised end to end. No test needs a network connection, an API key, or a database server.

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
  cli.py          show, catalog, verify, rewrite, compare
  strategies.py   prompting strategies for experiments
scripts/          setup, data loading, key configuration, experiment runner
workloads/        bundled workloads
```

## Current limitations

- Verification uses sampling. A rewrite can match on the sample and differ on the full data; that is inherent to sampling. Use `--no-sample` when a query must be conclusive.
- Speedups are wall-clock on one machine and reflect this data, this cache state, and this engine. Compare within a run, not across runs.
- PostgreSQL and MySQL need a running server. DuckDB is the only engine tested end to end here.
