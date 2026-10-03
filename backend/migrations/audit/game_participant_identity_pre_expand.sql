-- Read-only audit BEFORE the participant identity column is added.
-- Use only after confirming the intended database and the baseline schema.
-- Result sets include both information and findings; exit 0 is not approval to migrate.
-- Never repair data here. See backend/docs/mariadb-baseline.md for interpretation.

-- A04B-PRE-01: participant row count, also used for the post-expand comparison.
SELECT COUNT(*) AS total_participant_rows
FROM game_participant;

-- A04B-PRE-02: include active games with no participant rows.
SELECT
    g.game_id,
    COUNT(gp.id) AS participant_rows
FROM game AS g
LEFT JOIN game_participant AS gp ON gp.game_id = g.game_id
WHERE g.status = 'IN_PROGRESS'
GROUP BY g.game_id
ORDER BY g.game_id;

-- A04B-PRE-03: a member must not occur more than once in one game.
SELECT
    game_id,
    member_id,
    COUNT(*) AS duplicate_rows
FROM game_participant
WHERE member_id IS NOT NULL
GROUP BY game_id, member_id
HAVING COUNT(*) > 1
ORDER BY game_id, member_id;

-- A04B-PRE-04: a guest label must not occur more than once in one game.
SELECT
    game_id,
    guest_label,
    COUNT(*) AS duplicate_rows
FROM game_participant
WHERE guest_label IS NOT NULL
GROUP BY game_id, guest_label
HAVING COUNT(*) > 1
ORDER BY game_id, guest_label;

-- A04B-PRE-05: NULL, case variants and extra characters are not valid guest labels.
-- Length plus a binary pattern match avoids collation and trailing-newline acceptance.
SELECT
    id,
    game_id,
    member_id,
    is_guest,
    guest_label
FROM game_participant
WHERE NOT COALESCE(
    (is_guest = FALSE AND member_id IS NOT NULL AND guest_label IS NULL)
    OR
    (is_guest = TRUE AND member_id IS NULL
        AND OCTET_LENGTH(guest_label) = 10
        AND CAST(guest_label AS BINARY) REGEXP '^Guest-[0-9]{4}$'),
    FALSE
)
ORDER BY game_id, id;
