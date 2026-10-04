-- Read-only audit AFTER the participant identity column is added.
-- Run before any backfill or constraint migration, never against the column-free baseline.
-- Result sets include both information and findings; exit 0 is not approval to migrate.
-- Never repair data here. See backend/docs/mariadb-baseline.md for interpretation.

-- A04B-01: baseline row counts and the size of the required backfill.
SELECT
    COUNT(*) AS total_participant_rows,
    COALESCE(SUM(participant_id IS NULL), 0) AS null_participant_id_rows
FROM game_participant;

-- A04B-02: include active games with no participants; absent rows are not NULL identities.
SELECT
    g.game_id,
    COUNT(gp.id) AS participant_rows,
    SUM(CASE WHEN gp.id IS NOT NULL AND gp.participant_id IS NULL THEN 1 ELSE 0 END)
        AS null_participant_id_rows
FROM game AS g
LEFT JOIN game_participant AS gp ON gp.game_id = g.game_id
WHERE g.status = 'IN_PROGRESS'
GROUP BY g.game_id
ORDER BY g.game_id;

-- A04B-03: a member must not occur more than once in one game.
SELECT
    game_id,
    member_id,
    COUNT(*) AS duplicate_rows
FROM game_participant
WHERE member_id IS NOT NULL
GROUP BY game_id, member_id
HAVING COUNT(*) > 1
ORDER BY game_id, member_id;

-- A04B-04: a guest label must not occur more than once in one game.
SELECT
    game_id,
    guest_label,
    COUNT(*) AS duplicate_rows
FROM game_participant
WHERE guest_label IS NOT NULL
GROUP BY game_id, guest_label
HAVING COUNT(*) > 1
ORDER BY game_id, guest_label;

-- A04B-05: NULL, case variants and extra characters are not valid guest labels.
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

-- A04B-06: every populated identity must be a canonical lowercase, hyphenated UUIDv4.
SELECT
    id,
    game_id,
    participant_id
FROM game_participant
WHERE participant_id IS NOT NULL
  AND (
      OCTET_LENGTH(participant_id) <> 36
      OR CAST(participant_id AS BINARY) NOT REGEXP
          '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  )
ORDER BY game_id, id;

-- A04B-07: a populated participant identity must be unique inside its game.
SELECT
    game_id,
    participant_id,
    COUNT(*) AS duplicate_rows
FROM game_participant
WHERE participant_id IS NOT NULL
GROUP BY game_id, participant_id
HAVING COUNT(*) > 1
ORDER BY game_id, participant_id;
