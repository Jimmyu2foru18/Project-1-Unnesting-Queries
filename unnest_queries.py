import os
import re
import sys
import time
from pathlib import Path

import prompts
import validate_sql
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError

load_dotenv()

DIR = Path(__file__).resolve().parent
MODEL, ATTEMPTS = os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), 3
PAUSE = int(os.getenv("GEMINI_PAUSE", "13"))  # free tier allows 5 requests a minute
FENCE = re.compile(r"^```(?:sql)?|\s*```$", re.MULTILINE)
COMPLETED = re.compile(r"^-- (Q\d+) \(unnested\)", re.MULTILINE)
HEADER = "-- Unnested query catalog\n-- Generated via Gemini API\n\n"


def parse_queries(src: Path) -> list[tuple[str, str, str]]:
    text = src.read_text(encoding="utf-8")
    queries = []
    qid, desc, sql_lines = None, "", []

    for line in text.splitlines():
        if line.startswith("-- Q"):
            if qid and sql_lines:
                queries.append((qid, desc, "\n".join(sql_lines).strip()))
                sql_lines = []

            header = line.strip()[3:]
            if ":" in header:
                qid, desc = map(str.strip, header.split(":", 1))
            else:
                qid, desc = header.strip(), ""
        elif qid:
            sql_lines.append(line)
            if line.strip().endswith(";"):
                queries.append((qid, desc, "\n".join(sql_lines).strip()))
                qid, desc, sql_lines = None, "", []

    return queries


def get_completed_ids(dst: Path) -> set[str]:
    """Ids already written as a real rewrite. Rejected queries stay eligible for a retry."""
    if not dst.exists():
        dst.write_text(HEADER, encoding="utf-8")
        return set()
    return set(COMPLETED.findall(dst.read_text(encoding="utf-8")))


def generate(client, prompt, attempt):
    """One call to the model, retrying the transient quota and overload errors."""
    for pause in (0, 15, 30, 60):
        if pause:
            time.sleep(pause)
        try:
            return client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=prompts.RULES, temperature=0.4 * attempt),
            ).text
        except (APIError, ClientError) as err:
            last = err
    raise last


def rewrite(client, schema, schema_ddl, sql):
    """Generate a rewrite and keep repairing it until the schema accepts it."""
    prompt = prompts.SYSTEM_PROMPT.format(schema=schema_ddl, sql=sql)
    for attempt in range(ATTEMPTS):
        candidate = FENCE.sub("", generate(client, prompt, attempt)).strip()
        problems = validate_sql.validate(candidate, schema)
        if not problems:
            return candidate
        print(f"  attempt {attempt + 1} rejected: {problems[0]}")
        prompt = prompts.SYSTEM_PROMPT.format(schema=schema_ddl, sql=sql) + prompts.REPAIR_PROMPT.format(
            problems="\n".join(f"- {p}" for p in problems), rejected=candidate
        )
    return None


def main():
    src, dst = DIR / "nested_queries.sql", DIR / "unnested_queries.sql"
    if not src.exists():
        sys.exit(f"Error: File not found: {src}")

    schema = validate_sql.load_schema(ddl_path=DIR / "imdb.sql", dsn=os.getenv("DATABASE_URL"))
    ddl = validate_sql.render_schema(schema)
    completed = get_completed_ids(dst)
    client = genai.Client()

    for qid, desc, sql in parse_queries(src):
        if qid in completed:
            continue
        print(f"Processing {qid}...")
        try:
            rewritten = rewrite(client, schema, ddl, sql)
        except (APIError, ClientError) as err:
            print(f"  API error, skipping: {err}")
            continue
        if rewritten:
            with dst.open("a", encoding="utf-8") as f:
                f.write(f"-- {qid} (unnested): {desc}\n{rewritten}\n\n")
        else:
            print(f"  still invalid after {ATTEMPTS} attempts, will retry next run")
        time.sleep(PAUSE)


if __name__ == "__main__":
    if "--force" in sys.argv:
        (DIR / "unnested_queries.sql").unlink(missing_ok=True)
    main()
