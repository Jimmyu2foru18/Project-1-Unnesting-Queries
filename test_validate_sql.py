"""Unit tests for the Part 1 validation stage. No database or model required."""
import unnest_queries as uq
import validate_sql as v

SCHEMA = {
    "movies": {"id": "INT", "title": "TEXT", "year": "INT"},
    "ratings": {"movie_id": "INT", "rating": "FLOAT", "votes": "INT"},
    "directors": {"movie_id": "INT", "person_id": "INT"},
}

DDL = """
CREATE TABLE movies (
    id INTEGER,
    title TEXT NOT NULL,
    year NUMERIC,
    PRIMARY KEY(id)
);
CREATE TABLE directors (
    movie_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    FOREIGN KEY(movie_id) REFERENCES movies(id)
);
INSERT INTO movies VALUES(1, 'a', 1990);
"""


def test_load_schema_reads_tables_from_a_dump_and_ignores_constraints(tmp_path):
    path = tmp_path / "imdb.sql"
    path.write_text(DDL, encoding="utf-8")
    schema = v.load_schema(ddl_path=path)
    assert schema == {
        "movies": {"id": "INTEGER", "title": "TEXT", "year": "NUMERIC"},
        "directors": {"movie_id": "INTEGER", "person_id": "INTEGER"},
    }


def test_render_schema_emits_ddl_for_the_prompt():
    rendered = v.render_schema(SCHEMA)
    assert rendered.startswith("CREATE TABLE directors (movie_id INT, person_id INT);")


def test_a_correct_rewrite_passes():
    sql = ("WITH y AS (SELECT m2.year, AVG(r2.rating) a FROM ratings r2 "
           "JOIN movies m2 ON m2.id = r2.movie_id GROUP BY m2.year) "
           "SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id "
           "JOIN y ON y.year = m.year WHERE r.rating > y.a")
    assert v.validate(sql, SCHEMA) == []


def test_a_table_the_model_invented_is_rejected():
    assert v.validate("SELECT movie_id FROM movie_directors", SCHEMA) == [
        "table 'movie_directors' does not exist"
    ]


def test_a_column_the_model_invented_is_rejected():
    problems = v.validate("SELECT m.release_year FROM movies m", SCHEMA)
    assert problems and "release_year" in problems[0]


def test_a_cte_is_not_mistaken_for_a_missing_table():
    assert v.validate("WITH x AS (SELECT id FROM movies) SELECT id FROM x", SCHEMA) == []


def test_broken_syntax_is_rejected_without_a_schema_lookup():
    problems = v.validate("SELECT FROM WHERE", SCHEMA)
    assert problems and problems[0].startswith("syntax error")


def test_an_empty_reply_is_rejected():
    assert v.validate("", SCHEMA) != []


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """Returns canned replies so the repair loop can be driven without a model."""

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

        class Models:
            def generate_content(inner, model, contents, config):
                self.prompts.append(contents)
                return FakeResponse(self.replies.pop(0))

        self.models = Models()


def test_a_bad_rewrite_is_repaired_using_the_rejection_as_feedback():
    bad = "```sql\nSELECT * FROM movie_directors\n```"
    good = "SELECT id FROM movies"
    client = FakeClient([bad, good])
    assert uq.rewrite(client, SCHEMA, "ddl", "SELECT 1") == good
    assert "Your previous attempt was rejected" in client.prompts[1]
    assert "movie_directors" in client.prompts[1]


def test_a_rewrite_that_never_validates_is_discarded_not_written():
    client = FakeClient(["SELECT * FROM movie_directors"] * uq.ATTEMPTS)
    assert uq.rewrite(client, SCHEMA, "ddl", "SELECT 1") is None


def test_a_failed_query_stays_eligible_for_the_next_run():
    written = "-- Q01 (unnested): ok\nSELECT 1;\n\n-- Q02 FAILED: rejected\n"
    assert uq.COMPLETED.findall(written) == ["Q01"]
