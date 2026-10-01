-- Unnested query catalog
-- Generated via Gemini API

-- Q04 (unnested): Movies without ratings
SELECT
    m.title,
    m.year
FROM movies m
LEFT JOIN (
    SELECT DISTINCT r.movie_id
    FROM ratings r
    WHERE r.movie_id IS NOT NULL
) r ON m.id = r.movie_id
WHERE r.movie_id IS NULL
ORDER BY m.year DESC, m.title;

-- Q06 (unnested): Movies sharing at least one director with a specific movie
SELECT
    m.title,
    m.year
FROM movies m
JOIN (
    SELECT DISTINCT d1.movie_id
    FROM directors d1
    WHERE d1.person_id IN (
        SELECT d2.person_id
        FROM directors d2
        JOIN movies m2 ON m2.id = d2.movie_id
        WHERE m2.title ILIKE '%Matrix%'
    )
) d1 ON m.id = d1.movie_id AND d1.movie_id <> m.id
ORDER BY m.year DESC, m.title;

-- Q01 (unnested): Movies rated above their year's average rating
WITH year_avg AS (
    SELECT
        m2.year,
        AVG(r2.rating) AS avg_rating
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    GROUP BY m2.year
)
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
JOIN year_avg ya ON ya.year = m.year
WHERE r.rating > ya.avg_rating
ORDER BY m.year, r.rating DESC;

-- Q02 (unnested): Movies with director-actor overlap
WITH qualifying_directors AS (
    SELECT DISTINCT d.movie_id
    FROM directors d
    JOIN stars s
      ON d.person_id = s.person_id
     AND d.movie_id <> s.movie_id
)
SELECT
    m.title,
    m.year
FROM movies m
JOIN qualifying_directors qd ON m.id = qd.movie_id
ORDER BY m.year DESC, m.title;

-- Q03 (unnested): Actors who starred in more movies than the average per-movie cast size
WITH person_movie_counts AS (
    SELECT
        person_id,
        COUNT(*) AS movie_count
    FROM stars
    GROUP BY person_id
)
SELECT
    p.name,
    p.birth,
    pmc.movie_count
FROM people p
JOIN person_movie_counts pmc
    ON p.id = pmc.person_id
WHERE pmc.movie_count > (
    SELECT AVG(cast_size)
    FROM (
        SELECT COUNT(*) AS cast_size
        FROM stars
        GROUP BY movie_id
    ) t
)
ORDER BY movie_count DESC, p.name;

-- Q05 (unnested): Highest-rated movie per year
WITH max_ratings AS (
    SELECT
        m2.year,
        MAX(r2.rating) AS max_rating
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    GROUP BY m2.year
)
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
JOIN max_ratings mr ON mr.year = m.year AND r.rating = mr.max_rating
ORDER BY m.year DESC

-- Q07 (unnested): Movies with no directors
WITH distinct_directors AS (
    SELECT DISTINCT movie_id
    FROM directors
)
SELECT
    m.title,
    m.year
FROM movies m
LEFT JOIN distinct_directors d ON m.id = d.movie_id
WHERE d.movie_id IS NULL
ORDER BY m.year DESC, m.title;

-- Q08 (unnested): Actors who have also directed a movie
SELECT
    p.name,
    p.birth
FROM people p
JOIN (
    SELECT DISTINCT person_id
    FROM stars
) s ON p.id = s.person_id
JOIN (
    SELECT DISTINCT person_id
    FROM directors
) d ON p.id = d.person_id
ORDER BY p.name;

-- Q09 (unnested): Movies where every actor was born after 1950
WITH older_stars AS (
    SELECT DISTINCT s.movie_id
    FROM stars s
    JOIN people pp ON pp.id = s.person_id
    WHERE pp.birth <= 1950
)
SELECT
    m.title,
    m.year
FROM movies m
LEFT JOIN older_stars s ON m.id = s.movie_id
WHERE s.movie_id IS NULL
ORDER BY m.year DESC, m.title

-- Q10 (unnested): Movies in the top 75th percentile of rating per year
WITH year_percentiles AS (
    SELECT
        m2.year,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY r2.rating) AS p75
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    GROUP BY m2.year
)
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
JOIN year_percentiles yp ON yp.year = m.year
WHERE r.rating > yp.p75
ORDER BY m.year DESC, r.rating DESC

-- Q11 (unnested): Movies with above-average number of distinct directors
WITH director_counts AS (
    SELECT movie_id, COUNT(*) AS director_count
    FROM directors
    GROUP BY movie_id
)
SELECT m.id, m.title
FROM movies m
JOIN director_counts dc ON m.id = dc.movie_id
WHERE dc.director_count > (
    SELECT AVG(cnt)
    FROM (
        SELECT COUNT(*) AS cnt
        FROM directors
        GROUP BY movie_id
    ) sub
)

