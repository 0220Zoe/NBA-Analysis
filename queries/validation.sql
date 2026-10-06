-- These examples demonstrate that the schema directly supports every target
-- question. Replace the named parameters in an application or SQLite client.

-- 1. All games where Player X scored at least 30 points.
SELECT
    p.full_name,
    g.game_id,
    g.game_date,
    t.abbreviation AS team,
    pgs.points
FROM player_game_stats AS pgs
JOIN players AS p ON p.player_id = pgs.player_id
JOIN games AS g ON g.game_id = pgs.game_id
JOIN teams AS t ON t.team_id = pgs.team_id
WHERE p.normalized_name = :normalized_player_name
  AND pgs.points >= 30
ORDER BY g.game_date DESC;

-- 2. Player Y's averages over their last 10 appearances (not merely their
-- team's last 10 games). The inner LIMIT occurs before AVG.
SELECT
    full_name,
    COUNT(*) AS games_in_sample,
    ROUND(AVG(points), 2) AS avg_points,
    ROUND(AVG(rebounds), 2) AS avg_rebounds,
    ROUND(AVG(assists), 2) AS avg_assists
FROM (
    SELECT
        p.full_name,
        pgs.points,
        pgs.rebounds,
        pgs.assists
    FROM player_game_stats AS pgs
    JOIN players AS p ON p.player_id = pgs.player_id
    JOIN games AS g ON g.game_id = pgs.game_id
    WHERE p.normalized_name = :normalized_player_name
    ORDER BY g.game_date DESC, g.game_id DESC
    LIMIT 10
);

-- 3. Team Z's current roster and roster-specific positions.
SELECT
    p.full_name,
    rm.jersey_number,
    rm.position,
    rm.roster_status,
    p.is_placeholder AS missing_from_canonical_player_feed
FROM roster_memberships AS rm
JOIN players AS p ON p.player_id = rm.player_id
JOIN teams AS t ON t.team_id = rm.team_id
WHERE t.abbreviation = :team_abbreviation
  AND rm.is_current = 1
ORDER BY rm.jersey_number, p.full_name;

-- 4. Highest average team points per game in a season. team_game_stats provides
-- one row per team-game, eliminating home/away UNION logic.
SELECT
    t.city || ' ' || t.name AS team,
    COUNT(*) AS games_played,
    ROUND(AVG(tgs.points), 2) AS points_per_game
FROM team_game_stats AS tgs
JOIN teams AS t ON t.team_id = tgs.team_id
JOIN games AS g ON g.game_id = tgs.game_id
WHERE g.season = :season
  AND LOWER(g.status) = 'final'
GROUP BY t.team_id
ORDER BY points_per_game DESC
LIMIT 1;

-- 5. Points before/after each matched injury report. Games on the report date
-- are excluded because the source provides no report time, so their temporal
-- relationship to the report cannot be established safely.
SELECT
    p.full_name,
    ir.injury_type,
    ir.status,
    ir.date_reported,
    SUM(CASE WHEN g.game_date < ir.date_reported THEN 1 ELSE 0 END) AS games_before,
    ROUND(AVG(CASE WHEN g.game_date < ir.date_reported THEN pgs.points END), 2)
        AS avg_points_before,
    SUM(CASE WHEN g.game_date > ir.date_reported THEN 1 ELSE 0 END) AS games_after,
    ROUND(AVG(CASE WHEN g.game_date > ir.date_reported THEN pgs.points END), 2)
        AS avg_points_after,
    ROUND(
        AVG(CASE WHEN g.game_date > ir.date_reported THEN pgs.points END)
        - AVG(CASE WHEN g.game_date < ir.date_reported THEN pgs.points END),
        2
    ) AS points_change
FROM injury_reports AS ir
JOIN players AS p ON p.player_id = ir.player_id
JOIN player_game_stats AS pgs ON pgs.player_id = ir.player_id
JOIN games AS g ON g.game_id = pgs.game_id
WHERE g.game_date <> ir.date_reported
GROUP BY ir.injury_report_id
ORDER BY ir.date_reported, p.full_name;

