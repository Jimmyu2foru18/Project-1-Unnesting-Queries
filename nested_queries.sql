-- Nested (correlated) query catalog for IMDb schema
-- Each query is annotated with a unique identifier (Q01, Q02, ...) and description.
-- These queries use correlated subqueries, EXISTS, NOT IN, IN, and aggregation
-- that reference outer query columns — making them candidates for unnesting.
--
-- IMPORTANT: Run these queries in the `imdb` database context.
-- The imdb.sql dump creates the tables in the `imdb` database.
-- Use: USE imdb;  or  mysql -D imdb -f nested_queries.sql

USE imdb;

-- =====================================================================
-- Q01: Movies rated above their year's average rating
-- Type: correlated subquery (scalar, AVG)
-- Complexity: low
-- Description: Find movies whose rating exceeds the average rating of all
--              movies released in the same year. The subquery correlates
--              on the `year` column from the outer query.
-- =====================================================================
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE r.rating > (
    SELECT AVG(r2.rating)
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    WHERE m2.year = m.year
)
ORDER BY m.year, r.rating DESC;


-- =====================================================================
-- Q02: Movies with director-actor overlap
-- Type: correlated EXISTS (nested)
-- Complexity: medium
-- Description: Find movies where at least one director has also acted
--              (appears in `stars`) in a *different* movie. Uses a nested
--              EXISTS correlated on `person_id` and `movie_id`.
-- =====================================================================
SELECT
    m.title,
    m.year
FROM movies m
WHERE EXISTS (
    SELECT 1
    FROM directors d
    WHERE d.movie_id = m.id
    AND EXISTS (
        SELECT 1
        FROM stars s
        WHERE s.person_id = d.person_id
        AND s.movie_id <> m.id
    )
)
ORDER BY m.year DESC, m.title;


-- =====================================================================
-- Q03: Actors who starred in more movies than the average per-movie cast size
-- Type: correlated subquery (COUNT vs overall average)
-- Complexity: high
-- Description: Find people who have starred in more movies than the
--              average number of actors per movie. The subquery computes
--              the overall average cast size; the outer reference ties
--              `person_id` to count each person's appearances.
-- =====================================================================
SELECT
    p.name,
    p.birth,
    (
        SELECT COUNT(*)
        FROM stars s
        WHERE s.person_id = p.id
    ) AS movie_count
FROM people p
WHERE (
    SELECT COUNT(*)
    FROM stars s
    WHERE s.person_id = p.id
) > (
    SELECT AVG(cast_size)
    FROM (
        SELECT COUNT(*) AS cast_size
        FROM stars
        GROUP BY movie_id
    ) t
)
ORDER BY movie_count DESC, p.name;


-- =====================================================================
-- Q04: Movies without ratings
-- Type: NOT IN (correlated subquery)
-- Complexity: low
-- Description: Find movies that have no entry in the `ratings` table.
--              Uses `NOT IN` with a subquery selecting all rated movie IDs.
-- =====================================================================
SELECT
    m.title,
    m.year
FROM movies m
WHERE m.id NOT IN (
    SELECT r.movie_id
    FROM ratings r
    WHERE r.movie_id IS NOT NULL
)
ORDER BY m.year DESC, m.title;


-- =====================================================================
-- Q05: Highest-rated movie per year
-- Type: correlated subquery (MAX)
-- Complexity: medium
-- Description: Find the highest-rated movie for each release year. The
--              subquery computes MAX(rating) filtered by matching `year`.
-- =====================================================================
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE r.rating = (
    SELECT MAX(r2.rating)
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    WHERE m2.year = m.year
)
ORDER BY m.year DESC;


-- =====================================================================
-- Q06: Movies sharing at least one director with a specific movie
-- Type: IN (correlated subquery)
-- Complexity: medium
-- Description: Find movies that share at least one director with the movie
--              titled 'The Matrix' (case-insensitive). The subquery
--              correlates on `person_id` from the directors table.
-- =====================================================================
SELECT
    m.title,
    m.year
FROM movies m
WHERE m.id IN (
    SELECT DISTINCT d1.movie_id
    FROM directors d1
    WHERE d1.person_id IN (
        SELECT d2.person_id
        FROM directors d2
        JOIN movies m2 ON m2.id = d2.movie_id
        WHERE m2.title ILIKE '%Matrix%'
    )
    AND d1.movie_id <> m.id
)
ORDER BY m.year DESC, m.title;


-- =====================================================================
-- Q07: Movies with no directors
-- Type: NOT EXISTS (correlated subquery)
-- Complexity: low
-- Description: Find all movies that have no entry in the `directors` table.
--              Uses NOT EXISTS correlated on `movie_id`.
-- =====================================================================
SELECT
    m.title,
    m.year
FROM movies m
WHERE NOT EXISTS (
    SELECT 1
    FROM directors d
    WHERE d.movie_id = m.id
)
ORDER BY m.year DESC, m.title;


-- =====================================================================
-- Q08: Actors who have also directed a movie
-- Type: correlated EXISTS with semi-join
-- Complexity: medium
-- Description: Find people who have appeared as an actor (in `stars`) in
--              at least one movie AND have directed (in `directors`) at
--              least one movie. The EXISTS subquery correlates on
--              `person_id`.
-- =====================================================================
SELECT
    p.name,
    p.birth
FROM people p
WHERE EXISTS (
    SELECT 1
    FROM stars s
    WHERE s.person_id = p.id
)
AND EXISTS (
    SELECT 1
    FROM directors d
    WHERE d.person_id = p.id
)
ORDER BY p.name;


-- =====================================================================
-- Q09: Movies where every actor was born after 1950
-- Type: NOT EXISTS with correlated < comparison
-- Complexity: high
-- Description: Find movies where there does NOT exist any actor born
--              before or in 1950. Uses a double-negation pattern:
--              NOT EXISTS (actor who appeared in this movie AND born <= 1950).
-- =====================================================================
SELECT
    m.title,
    m.year
FROM movies m
WHERE NOT EXISTS (
    SELECT 1
    FROM stars s
    JOIN people pp ON pp.id = s.person_id
    WHERE s.movie_id = m.id
    AND pp.birth <= 1950
)
ORDER BY m.year DESC, m.title;


-- =====================================================================
-- Q10: Movies in the top 75th percentile of rating per year
-- Type: correlated subquery with percentile threshold
-- Complexity: high
-- Description: Find movies whose rating exceeds the 75th percentile
--              rating of all movies from the same year. The subquery
--              computes the percentile as a constant threshold correlated
--              on `year`.
-- =====================================================================
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE r.rating > (
    SELECT PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY r2.rating)
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    WHERE m2.year = m.year
)
ORDER BY m.year DESC, r.rating DESC;


-- =====================================================================
-- Q11: Movies with above-average number of distinct directors
-- Type: correlated subquery (COUNT vs overall average)
-- Complexity: high
-- Description: Find movies whose director count exceeds the average
--              number of directors per movie across the entire dataset.
--              The correlated subquery counts directors for each movie;
--              the threshold subquery computes the global average.
-- =====================================================================
SELECT
    m.title,
    m.year,
    (
        SELECT COUNT(*)
        FROM directors d
        WHERE d.movie_id = m.id
    ) AS num_directors
FROM movies m
WHERE (
    SELECT COUNT(*)
    FROM directors d
    WHERE d.movie_id = m.id
) > (
    SELECT AVG(dir_count)
    FROM (
        SELECT COUNT(*) AS dir_count
        FROM directors
        GROUP BY movie_id
    ) t
)
ORDER BY num_directors DESC, m.title;


-- =====================================================================
-- Q12: Actors who worked with directors they also co-starred with
-- Type: nested correlated EXISTS (double nesting)
-- Complexity: high
-- Description: Find (movie, actor, director) triples where the actor and
--              director appeared together in some other movie as co-stars.
--              Uses a nested EXISTS: the outer EXISTS finds directors of
--              this movie, the inner EXISTS confirms the director also
--              acted in a different movie alongside this actor.
-- =====================================================================
SELECT DISTINCT
    m.title,
    p.name AS actor_name
FROM movies m
JOIN stars s ON s.movie_id = m.id
JOIN people p ON p.id = s.person_id
WHERE EXISTS (
    SELECT 1
    FROM directors d1
    WHERE d1.movie_id = m.id
    AND EXISTS (
        SELECT 1
        FROM stars s2
        WHERE s2.person_id = d1.person_id
        AND s2.movie_id <> m.id
        AND EXISTS (
            SELECT 1
            FROM stars s3
            WHERE s3.movie_id = s2.movie_id
            AND s3.person_id = s.person_id
        )
    )
)
ORDER BY m.year DESC, p.name;


-- =====================================================================
-- Q13: Movies rated higher than their director's average rating
-- Type: correlated subquery (AVG over director's films)
-- Complexity: high
-- Description: Find movies whose rating exceeds the average rating of all
--              movies directed by the same person(s). The subquery
--              correlates on the `person_id`(s) in the `directors` table
--              for the current movie.
-- =====================================================================
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE r.rating > (
    SELECT AVG(r2.rating)
    FROM ratings r2
    JOIN directors d ON d.movie_id = r2.movie_id
    WHERE d.movie_id = m.id
)
ORDER BY m.year DESC, r.rating DESC;


-- =====================================================================
-- Q14: Movies whose release year had a higher total vote count than the
--      previous year's total
-- Type: correlated subquery (temporal comparison)
-- Complexity: medium
-- Description: Find movies released in a year where the total votes across
--              all movies in that year exceeds the total votes from the
--              previous year. The subquery correlates on `year` using
--              `year - 1`.
-- =====================================================================
SELECT
    m.title,
    m.year
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE (
    SELECT SUM(r2.votes)
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    WHERE m2.year = m.year
) > (
    SELECT SUM(r3.votes)
    FROM ratings r3
    JOIN movies m3 ON m3.id = r3.movie_id
    WHERE m3.year = m.year - 1
)
ORDER BY m.year DESC, m.title;


-- =====================================================================
-- Q15: Movies outside the top-3 rated per year
-- Type: correlated subquery (COUNT above threshold)
-- Complexity: medium
-- Description: Find movies that are NOT in the top 3 highest-rated movies
--              for their release year. The correlation counts how many
--              movies from the same year have a strictly higher rating;
--              if that count is >= 3, the movie is excluded from the top 3.
-- =====================================================================
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE (
    SELECT COUNT(*)
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    WHERE m2.year = m.year
    AND r2.rating > r.rating
) >= 3
ORDER BY m.year DESC, r.rating DESC;
