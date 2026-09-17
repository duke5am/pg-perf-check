-- =====================================================================
-- 02 - UNUSED INDEXES AND CANDIDATE MISSING INDEXES
-- =====================================================================
--
-- WHAT THIS SHOWS
--   Two sides of the same problem. Q2.1-Q2.4 find indexes you are paying
--   for on every write and never reading. Q2.5-Q2.7 find tables that are
--   being sequentially scanned in a way that suggests a selective index
--   is missing.
--
-- THE CAVEAT THAT MAKES OR BREAKS THIS FILE
--   pg_stat_user_indexes.idx_scan counts scans since the statistics were
--   last reset - NOT since the server started. Statistics are also lost
--   on a crash or an immediate shutdown, and they are NEVER copied to a
--   standby (a replica's counters are always zero). Every query below
--   prints the stats_reset timestamp for exactly this reason.
--
--   RULE: never drop an index because idx_scan = 0 unless
--     (a) stats_reset is at least one full business cycle in the past
--         (a monthly report index shows zero for 29 days), AND
--     (b) the index is not a primary key or unique constraint backing a
--         real constraint, AND
--     (c) you have confirmed with the application team, or by checking
--         pg_stat_statements for queries whose text mentions the column.
--   There is no "make this index invisible for a week and see what
--   happens" feature in PostgreSQL (that exists in MySQL, not here). So
--   your low-risk option is: DROP INDEX CONCURRENTLY, then watch for
--   regressions for a full cycle. Keep the exact `CREATE INDEX`
--   statement from pg_get_indexdef BEFORE you drop, so you can put it
--   back with CREATE INDEX CONCURRENTLY. Reindexing is not a substitute
--   for dropping - it neither proves nor disproves usefulness.
--
-- RISK OF RUNNING THIS
--   READ-ONLY. Catalog and statistics views only, no locks beyond
--   AccessShareLock on catalogs. Safe on a production primary.
--   The "suggested fix" comments reference DROP INDEX CONCURRENTLY and
--   CREATE INDEX CONCURRENTLY - those DO take locks and DO cost I/O.
--   Read them, do not paste them blindly.
--
-- VERSION NOTES
--   Works on 9.6+. `last_idx_scan` / `last_seq_scan` (used in Q2.4/Q2.5)
--   only exist on PostgreSQL 16+ and are commented out.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q2.0  How much do these numbers mean? Check the reset time first.
-- ---------------------------------------------------------------------
SELECT
    datname,
    stats_reset,
    now() - stats_reset                       AS stats_age,
    xact_commit + xact_rollback               AS transactions_counted,
    blks_read,
    blks_hit
FROM pg_stat_database
WHERE datname = current_database();


-- ---------------------------------------------------------------------
-- Q2.1  Never-scanned non-constraint indexes
-- ---------------------------------------------------------------------
-- Excludes primary keys and unique indexes, because those usually back a
-- constraint the application depends on and cannot be dropped silently.
SELECT
    s.schemaname,
    s.relname                                     AS table_name,
    s.indexrelname                                AS index_name,
    s.idx_scan,
    pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
    pg_relation_size(s.indexrelid)                AS index_bytes,
    pg_get_indexdef(s.indexrelid)                 AS definition,
    (SELECT stats_reset FROM pg_stat_database
      WHERE datname = current_database())         AS stats_reset
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique
  AND NOT i.indisprimary
  AND i.indisvalid
  AND pg_relation_size(s.indexrelid) > 1024 * 1024   -- > 1 MB only
ORDER BY pg_relation_size(s.indexrelid) DESC;


-- ---------------------------------------------------------------------
-- Q2.2  Barely-used indexes: what each scan costs you in write traffic
-- ---------------------------------------------------------------------
-- write_ops_per_scan is the honest trade-off number. An index on a table
-- with 50M updates and 12 scans has cost far more than it returned.
-- Every INSERT and every UPDATE that changes an indexed column must
-- maintain this index; HOT updates avoid it only when no indexed column
-- changed and there is free space on the page.
SELECT
    s.schemaname,
    s.relname                                       AS table_name,
    s.indexrelname                                  AS index_name,
    s.idx_scan,
    s.idx_tup_read,
    t.n_tup_ins + t.n_tup_upd + t.n_tup_del         AS table_write_ops,
    round((t.n_tup_ins + t.n_tup_upd + t.n_tup_del)::numeric
          / GREATEST(s.idx_scan, 1), 0)             AS write_ops_per_scan,
    pg_size_pretty(pg_relation_size(s.indexrelid))  AS index_size,
    pg_get_indexdef(s.indexrelid)                   AS definition
FROM pg_stat_user_indexes s
JOIN pg_stat_user_tables t ON t.relid = s.relid
JOIN pg_index i            ON i.indexrelid = s.indexrelid
WHERE s.idx_scan < 50
  AND NOT i.indisprimary
  AND i.indisvalid
  AND t.n_tup_ins + t.n_tup_upd + t.n_tup_del > 10000
ORDER BY (t.n_tup_ins + t.n_tup_upd + t.n_tup_del)::numeric
         / GREATEST(s.idx_scan, 1) DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q2.3  Index write amplification per table - total cost of all indexes
-- ---------------------------------------------------------------------
-- indexes_per_table above ~6 on a write-heavy table is a smell. Each
-- extra index multiplies the cost of every non-HOT update.
SELECT
    t.schemaname,
    t.relname                                        AS table_name,
    count(*)                                         AS index_count,
    sum(s.idx_scan)                                  AS total_index_scans,
    pg_size_pretty(sum(pg_relation_size(s.indexrelid))) AS total_index_size,
    pg_size_pretty(pg_relation_size(t.relid))        AS heap_size,
    t.n_tup_ins + t.n_tup_upd + t.n_tup_del          AS table_write_ops,
    CASE WHEN t.n_tup_upd > 0
         THEN round(100.0 * t.n_tup_hot_upd / t.n_tup_upd, 1) END AS hot_update_pct
FROM pg_stat_user_tables t
JOIN pg_stat_user_indexes s ON s.relid = t.relid
JOIN pg_index i             ON i.indexrelid = s.indexrelid
WHERE i.indisvalid
GROUP BY t.schemaname, t.relname, t.relid, t.n_tup_ins, t.n_tup_upd,
         t.n_tup_del, t.n_tup_hot_upd
HAVING count(*) >= 4
ORDER BY count(*) DESC, pg_relation_size(t.relid) DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q2.4  Dead index candidates: table is written constantly, index read
--       almost never, and the index is not tiny
-- ---------------------------------------------------------------------
-- This is the sharpest form of the "is this index worth it" question.
SELECT
    s.schemaname,
    s.relname                                        AS table_name,
    s.indexrelname                                   AS index_name,
    s.idx_scan,
    t.n_tup_ins + t.n_tup_upd + t.n_tup_del          AS table_write_ops,
    t.n_live_tup,
    pg_size_pretty(pg_relation_size(s.indexrelid))   AS index_size,
    pg_get_indexdef(s.indexrelid)                    AS definition
FROM pg_stat_user_indexes s
JOIN pg_stat_user_tables t ON t.relid = s.relid
JOIN pg_index i            ON i.indexrelid = s.indexrelid
WHERE i.indisvalid
  AND NOT i.indisprimary
  AND NOT i.indisunique
  AND s.idx_scan = 0
  AND t.n_tup_ins + t.n_tup_upd + t.n_tup_del > 100000
  AND pg_relation_size(s.indexrelid) > 8 * 1024 * 1024
ORDER BY pg_relation_size(s.indexrelid) DESC;

-- PostgreSQL 16+ only: the same idea with timestamps, which is far more
-- trustworthy than a counter because it survives "when did it last get
-- used at all?" questions the counter cannot answer.
--
--   SELECT s.schemaname, s.relname, s.indexrelname, s.idx_scan,
--          s.last_idx_scan, now() - s.last_idx_scan AS since_last_scan,
--          pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size
--   FROM pg_stat_user_indexes s
--   WHERE s.last_idx_scan IS NOT NULL
--     AND s.last_idx_scan < now() - interval '30 days'
--   ORDER BY pg_relation_size(s.indexrelid) DESC;


-- ---------------------------------------------------------------------
-- Q2.5  CANDIDATE MISSING INDEXES - sequential scans on large tables
-- ---------------------------------------------------------------------
-- HOW TO READ THIS (this is the part people get wrong):
--   pct_of_table_per_scan is seq_tup_read / seq_scan divided by
--   n_live_tup - the average fraction of the table a single sequential
--   scan had to walk through.
--
--     * LOW pct (say < 5%) with a HIGH seq_scan count is the signature of
--       a missing selective index. The query only wants a few hundred
--       rows out of millions and we are reading all of them.
--
--     * HIGH pct (> 30%) means the sequential scan is CORRECT. If a query
--       genuinely needs a fifth of the table, an index would require a
--       bitmap of thousands of pages plus a heap fetch for each - the
--       planner is right and adding an index would make it slower. Do not
--       "fix" these.
--
--     * On a table with n_live_tup under ~10k, a sequential scan is
--       always correct (one or two pages) and is excluded below.
--
--   n_live_tup is an estimate maintained by ANALYZE and VACUUM, and it
--   drifts. Read it as an order of magnitude, not a count.
SELECT
    t.schemaname,
    t.relname                                            AS table_name,
    t.seq_scan,
    t.seq_tup_read,
    round(t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1), 0) AS avg_rows_read_per_scan,
    t.n_live_tup,
    round(100.0 * (t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1))
          / NULLIF(t.n_live_tup, 0), 1)                  AS pct_of_table_per_scan,
    t.idx_scan,
    pg_size_pretty(pg_relation_size(t.relid))            AS heap_size,
    t.n_tup_ins + t.n_tup_upd + t.n_tup_del              AS write_ops
FROM pg_stat_user_tables t
WHERE t.seq_scan > 0
  AND t.n_live_tup > 10000
  AND t.seq_tup_read > 100000
ORDER BY
    -- rank by "wasted rows read": scans that walked a big table for a
    -- small result, weighted by how many times it happened
    (t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1)) * t.seq_scan
        / GREATEST(t.n_live_tup, 1) DESC,
    t.seq_scan DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q2.6  Same signal, but only where an index is affordable
-- ---------------------------------------------------------------------
-- An index is worth creating when the table is large, the scan is
-- frequent, and the average scan is a small slice. This narrows Q2.5 to
-- the tables where the fix is most likely to be a clear win.
SELECT
    t.schemaname,
    t.relname                                       AS table_name,
    t.seq_scan,
    round(t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1), 0) AS avg_rows_per_scan,
    t.n_live_tup,
    pg_size_pretty(pg_relation_size(t.relid))       AS heap_size,
    pg_size_pretty(COALESCE((
        SELECT sum(pg_relation_size(s.indexrelid))
        FROM pg_stat_user_indexes s WHERE s.relid = t.relid), 0)) AS existing_index_size
FROM pg_stat_user_tables t
WHERE t.n_live_tup > 100000
  AND t.seq_scan > 100
  AND (t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1))
      < 0.05 * t.n_live_tup          -- average scan touches <5% of the table
ORDER BY t.seq_tup_read DESC
LIMIT 30;

-- What to do with the table name you get: do NOT guess the index. Find
-- the actual query with file 03 or your logs, then read
-- EXPLAIN-GUIDE.md and INDEXING-PLAYBOOK.md before writing DDL. An index
-- guessed from counters is a coin flip; an index derived from the WHERE
-- clause and ORDER BY of a real query is not.


-- ---------------------------------------------------------------------
-- Q2.7  OPTIONAL - correlate the seq-scan tables with pg_stat_statements
-- ---------------------------------------------------------------------
-- Requires pg_stat_statements (see file 03). This finds the actual SQL
-- text responsible, which is what you need before choosing columns.
-- The ILIKE match on the table name is a HEURISTIC: it will miss
-- queries that use aliases only without the table name, and it can
-- produce false positives on names that are substrings of each other
-- (e.g. `orders` matching `order_items`).
--
--   WITH hot_tables AS (
--       SELECT relname
--       FROM pg_stat_user_tables
--       WHERE n_live_tup > 100000
--         AND seq_scan > 100
--         AND (seq_tup_read::numeric / GREATEST(seq_scan, 1))
--             < 0.05 * n_live_tup
--   )
--   SELECT h.relname AS scanned_table,
--          s.calls,
--          round(s.mean_exec_time::numeric, 2) AS mean_ms,
--          round(s.total_exec_time::numeric, 0) AS total_ms,
--          left(regexp_replace(s.query, '\s+', ' ', 'g'), 300) AS query
--   FROM hot_tables h
--   JOIN pg_stat_statements s
--     ON s.query ILIKE '%' || h.relname || '%'
--   WHERE s.calls > 20
--   ORDER BY s.total_exec_time DESC
--   LIMIT 40;
