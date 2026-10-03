-- Workload: imdb_join
-- Join-heavy queries over the IMDb schema. There is no correlation to remove
-- here, so the wins come from aggregating before joining, filtering the large
-- relation before it is joined, and keeping every join on an indexed key.
--
-- Tables: movies(id, title, year), ratings(movie_id, rating, votes),
--         people(id, name, birth), stars(movie_id, person_id),
--         directors(movie_id, person_id)

-- J01: Average rating per decade, with the count of rated movies
SELECT
    (m.year / 10) * 10 AS decade,
    AVG(r.rating) AS avg_rating,
    COUNT(*) AS rated_movies
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE m.year IS NOT NULL
GROUP BY (m.year / 10) * 10
ORDER BY decade;

-- J02: The ten best rated movies, with their vote counts
SELECT
    m.title,
    m.year,
    r.rating,
    r.votes
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE r.votes > 1000
ORDER BY r.rating DESC
LIMIT 10;

-- J03: How many ratings each movie received, for movies with more than 5000
SELECT
    m.title,
    m.year,
    COUNT(r.movie_id) AS rating_count
FROM movies m
JOIN ratings r ON r.movie_id = m.id
GROUP BY m.id, m.title, m.year
HAVING COUNT(r.movie_id) > 5000
ORDER BY rating_count DESC;

-- J04: Actors who directed at least one of the movies they appeared in
SELECT DISTINCT
    p.name AS actor_director,
    m.title,
    m.year
FROM people p
JOIN stars s ON s.person_id = p.id
JOIN movies m ON m.id = s.movie_id
JOIN directors d ON d.movie_id = m.id
WHERE d.person_id = p.id
ORDER BY actor_director, m.year;

-- J05: Movies that have both a star and a director, with how many of each
SELECT
    m.title,
    COUNT(DISTINCT s.person_id) AS actors,
    COUNT(DISTINCT d.person_id) AS directors
FROM movies m
JOIN stars s ON s.movie_id = m.id
JOIN directors d ON d.movie_id = m.id
GROUP BY m.id, m.title
ORDER BY actors DESC, directors DESC;

-- J06: The highest rated movie for each year that has ratings
SELECT
    m.year,
    m.title,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
WHERE m.year BETWEEN 1990 AND 2000
  AND r.rating = (
    SELECT MAX(r2.rating)
    FROM ratings r2
    JOIN movies m2 ON m2.id = r2.movie_id
    WHERE m2.year = m.year
  )
ORDER BY m.year;

-- J07: People ranked by how many movies they appeared in, top 25
SELECT
    p.name,
    COUNT(*) AS appearances
FROM people p
JOIN stars s ON s.person_id = p.id
GROUP BY p.id, p.name
ORDER BY appearances DESC
LIMIT 25;

-- J08: Average rating by year, only for years with at least 100 rated movies
WITH yearly AS (
    SELECT
        m2.year AS year,
        r2.rating AS rating
    FROM movies m2
    JOIN ratings r2 ON r2.movie_id = m2.id
    WHERE m2.year IS NOT NULL
)
SELECT
    year,
    AVG(rating) AS avg_rating,
    COUNT(*) AS n
FROM yearly
GROUP BY year
HAVING COUNT(*) >= 100
ORDER BY year;

-- J09: Directors who have worked with an actor more than once
SELECT
    p1.name AS director,
    p2.name AS actor,
    COUNT(*) AS collaborations
FROM directors d
JOIN movies m ON m.id = d.movie_id
JOIN stars s ON s.movie_id = m.id
JOIN people p1 ON p1.id = d.person_id
JOIN people p2 ON p2.id = s.person_id
WHERE d.person_id <> s.person_id
GROUP BY p1.name, p2.name
HAVING COUNT(*) > 3
ORDER BY collaborations DESC;

-- J10: Every movie rated below the overall average, with its rating
SELECT
    m.title,
    m.year,
    r.rating
FROM movies m
JOIN ratings r ON r.movie_id = m.id
CROSS JOIN (
    SELECT AVG(rating) AS overall FROM ratings
) a
WHERE r.rating < a.overall
ORDER BY r.rating ASC
LIMIT 100;

-- J11: Rating distribution per year
SELECT
    m.year,
    COUNT(*) AS total,
    SUM(CASE WHEN r.rating >= 8 THEN 1 ELSE 0 END) AS high,
    SUM(CASE WHEN r.rating < 5 THEN 1 ELSE 0 END) AS low
FROM movies m
JOIN ratings r ON r.movie_id = m.id
GROUP BY m.year
ORDER BY m.year;

-- J12: Actors who have never been a director
SELECT
    p.id,
    p.name
FROM people p
JOIN stars s ON s.person_id = p.id
WHERE p.id NOT IN (
    SELECT d.person_id FROM directors d
)
ORDER BY p.name;