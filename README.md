# SQL Rewriter

A tool that rewrites SQL queries using an LLM. It checks that rewrites are correct and measures their speed.

## What it does

1. Takes a SQL query and its database schema
2. Asks an LLM to rewrite the query to run faster
3. Checks that the rewrite returns exactly the same rows
4. Times both the original and rewritten queries
5. Reports which rewrites are correct and how much faster they are

## How it works

```
query + schema → LLM → candidate rewrites → verify → benchmark → report
```

The tool generates multiple candidate rewrites because some may be valid but perform differently. Each candidate is checked for correctness before it runs. Only rewrites that return the same results as the original are timed and ranked.

## Prompting strategies

You can compare how the LLM is prompted to rewrite queries:

- `existing` -- plan-then-write approach (default)
- `zero-shot` -- direct rewrite, no examples
- `one-shot` -- one example then rewrite
- `reasoning` -- structured analysis before SQL

## Setup

```bash
pip install -r requirements.txt
python scripts/setup.py --check
```

Needs Python 3.11+.

## Configure

```bash
python scripts/configure_keys.py
```

For Ollama, no API key is needed. Set `SQLRW_DSN` for your database.

## Load sample data

```bash
python scripts/load_data.py --dump imdb.7z --engine duckdb --out data/imdb.duckdb --rebuild
```

## Run

```bash
# See what schema info gets sent to the model
python -m sqlrewriter show --dsn data/imdb.duckdb --schema main

# Rewrite a workload
python -m sqlrewriter rewrite \
    --dsn data/imdb.duckdb --schema main \
    --workload workloads/imdb_nested \
    --model ollama/gpt-oss:20b \
    --strategy zero-shot \
    --out results.json

# Compare strategy results
python -m sqlrewriter compare results/*.json
```

## Commands

| Command | Description |
|---------|-------------|
| `show` | Print schema info sent to the model |
| `catalog` | Dump catalog as JSON |
| `verify` | Check a candidate against the original |
| `rewrite` | Full rewrite + verify + benchmark pipeline |
| `compare` | Compare strategy results from JSON files |

## Workloads

A workload is a directory with `queries.sql` and optional `workload.json`.

```sql
-- Q01: movies rated above eight
-- Description: scalar correlated subquery in predicate
SELECT m.title FROM movies m
WHERE (SELECT avg(r.rating) FROM ratings r WHERE r.movie_id = m.id) > 8.0;
```

```json
{
  "name": "imdb_nested",
  "schema": "main",
  "dsn": "data/imdb.duckdb",
  "hints": "prefer pre-aggregation over a correlated subquery"
}
```
