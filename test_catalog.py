"""Catalog building, statistics and prompt rendering."""
import pytest

from sqlrewriter.catalog import (
    Catalog,
    ForeignKey,
    from_dump,
    from_mapping,
    from_rows,
    short_type,
    tables_in,
)

DUMP = """
CREATE TABLE movies (
        id INTEGER,
        title TEXT NOT NULL,
        year NUMERIC,
        PRIMARY KEY(id)
    );
CREATE TABLE people (
        id INTEGER,
        name TEXT NOT NULL,
        PRIMARY KEY(id)
    );
CREATE TABLE ratings (
        movie_id INTEGER NOT NULL,
        rating REAL NOT NULL,
        votes INTEGER NOT NULL,
        FOREIGN KEY(movie_id) REFERENCES movies(id)
    );
CREATE TABLE cast_list (
        movie_id INTEGER NOT NULL,
        person_id INTEGER NOT NULL,
        UNIQUE(movie_id, person_id),
        FOREIGN KEY(movie_id) REFERENCES movies(id),
        FOREIGN KEY(person_id) REFERENCES people(id)
    );
INSERT INTO movies VALUES(1, 'a', 1990);
"""


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "imdb.sql"
    path.write_text(DUMP, encoding="utf-8")
    return from_dump(path)


def test_columns_types_and_nullability_come_out_of_a_dump(dump):
    # REAL and FLOAT are normalised to one name so a prompt reads the same
    # whichever engine reported the type.
    assert dump.columns["ratings"] == {"movie_id": "INT", "rating": "FLOAT", "votes": "INT"}
    assert "rating" in dump.not_null["ratings"]
    assert "year" not in dump.not_null["movies"]


def test_a_primary_key_column_is_treated_as_not_null(dump):
    assert dump.primary["movies"] == ["id"]
    assert "id" in dump.not_null["movies"]


def test_foreign_keys_are_read_in_both_directions(dump):
    assert ForeignKey("ratings", "movie_id", "movies", "id") in dump.foreign_keys
    assert ForeignKey("cast_list", "person_id", "people", "id") in dump.foreign_keys


def test_a_composite_unique_stays_composite(dump):
    assert dump.unique["cast_list"] == [("movie_id", "person_id")]
    assert dump.unique.get("movies", []) == []


def test_key_columns_count_as_indexed(dump):
    assert dump.indexed["ratings"] == {"movie_id"}
    assert dump.indexed["movies"] == {"id"}


def test_a_dump_without_row_counts_says_so_rather_than_guessing(dump):
    assert "row counts unavailable" in dump.fan_in_label(dump.foreign_keys[0])


def test_a_measured_fan_in_is_reported_instead(dump):
    dump.rows = {"movies": 67_000, "ratings": 15_000_000}
    label = dump.fan_in_label(dump.foreign_keys[0])
    assert "223.9 rows per parent row" in label
    assert "many-to-one" in label


def test_row_counts_drive_the_fan_in_labels():
    catalog = from_mapping(
        {"big": {"id": "INT"}, "small": {"id": "INT"}},
        rows={"big": 1_000_000, "small": 10},
        foreign_keys=[ForeignKey("big", "id", "small", "id")],
    )
    label = catalog.fan_in_label(catalog.foreign_keys[0])
    assert "100000.0 rows per parent row" in label
    assert "many-to-one" in label


def test_a_sparse_child_key_is_described_as_near_unique_not_one_to_one():
    catalog = from_mapping(
        {"a": {"id": "INT"}, "b": {"id": "INT"}},
        rows={"a": 10, "b": 10_000},
        foreign_keys=[ForeignKey("a", "id", "b", "id")],
    )
    assert "close to unique" in catalog.fan_in_label(catalog.foreign_keys[0])


def test_render_closes_the_scope_and_announces_it():
    catalog = from_mapping(
        {f"t{i}": {"id": "INT", "parent": "INT"} for i in range(20)},
        primary={f"t{i}": ["id"] for i in range(20)},
        foreign_keys=[ForeignKey(f"t{i}", "parent", f"t{i + 1}", "id") for i in range(19)],
    )
    block = catalog.render("SELECT id FROM t7", "postgres", budget=3)
    assert "Only these 3 tables are in scope" in block
    assert "CREATE TABLE t19" not in block
    assert "PRIMARY KEY" in block


def test_sampling_order_puts_parents_first(dump):
    order = dump.order_for_sampling()
    assert order.index("movies") < order.index("ratings")
    assert order.index("people") < order.index("cast_list")
    assert set(order) == set(dump.columns)


def test_sampling_order_survives_a_foreign_key_cycle():
    catalog = from_mapping(
        {"a": {"id": "INT", "b_id": "INT"}, "b": {"id": "INT", "a_id": "INT"}},
        foreign_keys=[ForeignKey("a", "b_id", "b", "id"), ForeignKey("b", "a_id", "a", "id")],
    )
    assert sorted(catalog.order_for_sampling()) == ["a", "b"]


def test_scope_keeps_the_tables_the_query_names():
    catalog = from_mapping({f"t{i}": {"id": "INT"} for i in range(20)})
    assert catalog.scope("SELECT id FROM t7", "postgres", budget=3)[0] == "t7"


def test_scope_walks_outward_along_the_foreign_keys():
    catalog = from_mapping(
        {f"t{i}": {"id": "INT", "parent": "INT"} for i in range(6)},
        foreign_keys=[ForeignKey(f"t{i}", "parent", f"t{i + 1}", "id") for i in range(5)],
    )
    scope = catalog.scope("SELECT id FROM t0", "postgres", budget=3)
    assert scope == ["t0", "t1", "t2"]


def test_a_query_naming_nothing_known_falls_back_to_a_pruned_list():
    catalog = from_mapping({f"t{i}": {"id": "INT"} for i in range(20)})
    assert len(catalog.scope("SELECT id FROM nowhere", "postgres", budget=4)) == 4


def test_render_closes_the_scope_and_announces_it():
    catalog = from_mapping(
        {f"t{i}": {"id": "INT", "parent": "INT"} for i in range(20)},
        primary={f"t{i}": ["id"] for i in range(20)},
        foreign_keys=[ForeignKey(f"t{i}", "parent", f"t{i + 1}", "id") for i in range(19)],
    )
    block = catalog.render("SELECT id FROM t7", "postgres", budget=3)
    assert "Only these 3 tables are in scope" in block
    assert "CREATE TABLE t19" not in block
    assert "PRIMARY KEY" in block


def test_tables_in_drops_ctes_and_tolerates_broken_sql():
    found = tables_in("WITH x AS (SELECT id FROM movies) SELECT id FROM x JOIN ratings r ON r.movie_id = x.id")
    assert found == {"movies", "ratings"}
    assert tables_in("SELECT FROM WHERE") == set()


def test_short_type_maps_the_names_that_differ_between_engines():
    assert short_type("character varying") == "TEXT"
    assert short_type("timestamp without time zone") == "TIMESTAMP"
    assert short_type("UUID") == "UUID"


POSTGRES_ROWS = {
    "columns": [("movies", "id", "integer", "NO"), ("ratings", "rating", "real", "YES")],
    "keys": [("movies", "movies_pkey", "id", "PRIMARY KEY")],
    "foreign_keys": [("ratings", "movie_id", "movies", "id")],
    "indexed_columns": [("ratings", "movie_id")],
    "row_counts": [("movies", 67000), ("ratings", 15000000)],
}

DUCKDB_ROWS = {
    "columns": [("movies", "id", "INTEGER", "NO")],
    "keys": [("movies", "PRIMARY KEY", "id", "PRIMARY KEY")],
    "foreign_keys": [("ratings", "movie_id", "movies", "id")],
    "indexed_columns": [("ratings", "CREATE INDEX i ON ratings(movie_id)")],
    "row_counts": [("movies", 67000)],
}


@pytest.mark.parametrize("engine", ["postgres", "duckdb"])
def test_rows_build_the_same_catalog_shape_from_different_engines(engine):
    rows = POSTGRES_ROWS if engine == "postgres" else DUCKDB_ROWS
    catalog = from_rows(rows, "main", engine=engine)
    assert catalog.known("movies")
    assert catalog.primary["movies"] == ["id"]
    assert catalog.rows["movies"] == 67_000


def test_a_duckdb_index_definition_is_parsed_into_column_names():
    catalog = from_rows(DUCKDB_ROWS, "main", engine="duckdb")
    assert catalog.indexed["ratings"] == {"movie_id"}


def test_a_composite_duckdb_index_yields_every_column():
    catalog = from_rows(
        {**DUCKDB_ROWS, "indexed_columns": [("cast_list", "CREATE UNIQUE INDEX i ON cast_list(movie_id, person_id)")]},
        "main", engine="duckdb",
    )
    assert catalog.indexed["cast_list"] == {"movie_id", "person_id"}


def test_neighbours_and_parents_are_reported_in_both_directions(dump):
    assert dump.neighbours("movies") == {"ratings", "cast_list"}
    assert [k.parent_table for k in dump.parents_of("ratings")] == ["movies"]
    assert {k.child_table for k in dump.children_of("movies")} == {"ratings", "cast_list"}


def test_an_empty_catalog_renders_nothing_useful():
    assert Catalog().tables == []
    assert "CREATE TABLE" not in Catalog().render("SELECT 1", "postgres")