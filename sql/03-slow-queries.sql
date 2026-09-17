-- =====================================================================
-- 03 - SLOW QUERIES VIA pg_stat_statements
-- =====================================================================
--
-- !!!! READ THIS FIRST - THIS FILE REQUIRES AN EXTENSION !!!!
--
-- Everything below Q3.0 needs the pg_stat_statements extension. It is
-- NOT installed by default and it CANNOT be created at runtime alone,
-- because it needs a shared library loaded into every backend at server
-- start. Two steps, both required, and step 1 needs a RESTART:
--
--   STEP 1 - edit postgresql.conf:
--
--       shared_preload_libraries = 'pg_stat_statements'
--       pg_stat_statements.max = 10000            # default; raise if you
--                                                 # see evictions
--       pg_stat_statements.track = 'top'          # 'top' (default) or
--                                                 # 'all' to include
--                                                 # nested statements in
--                                                 # PL/pgSQL functions
--       track_io_timing = on                      # needed for the
--                                                 # blk_read_time columns
--                                                 # to be non-zero
--
--       RESTART the server. `shared_preload_libraries` is PGC_POSTMASTER -
--       a reload is NOT enough. On a managed service (RDS, Cloud SQL,
--       Azure, Neon) set it in the parameter group / console instead; all
--       of them support this one.
--
--   STEP 2 - in each database you want to inspect:
--
--       CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
--
--   Verify with Q3.0. If it returns extension_installed = false, stop
--   and skip this file; file 02's seq-scan analysis still works without
--   it, and you can get per-query timing from log_min_duration_statement
--   plus pg_stat_statements later.
--
-- WHAT THIS SHOWS
--   Aggregate timing for every distinct query shape, normalised so that
--   literal values become $1, $2. This is the only view in PostgreSQL
--   that tells you where total database time is actually going.
--
-- WHAT TO LOOK FOR
--   * Q3.1 - total time. This is the budget question: which few query
--     shapes own most of your database's CPU. Optimising anything not in
--     this list is a hobby, not work.
--   * Q3.2 - mean time with a minimum call count. High mean + high calls
--     = the classic N+1 / missing-index-per-request problem.
--   * Q3.3 - coefficient of variation. A query whose stddev is bigger
--     than its mean is choosing different plans for different parameter
--     values. That is plan instability, not a missing index, and the fix
--     is usually `plan_cache_mode` or a rewritten predicate.
--   * Q3.4 - temp_blks_written. Non-zero here means work_mem is too
--     small for this sort or hash. This is often the single cheapest win
--     available, and it is a configuration change, not a schema change.
--   * Q3.6 - the 80/20 line. How many query shapes make up 80% of time.
--
-- RISK OF RUNNING THIS
--   READ-ONLY and cheap. pg_stat_statements is a plain view over a shared
--   memory hash table; scanning it costs microseconds. Q3.3 and Q3.4 read
--   the same table. Safe on a production primary, including during an
--   incident.
--   DO NOT run `pg_stat_statements_reset()` casually - it is the only
--   thing that destroys this data, and it is not recoverable. Note the
--   stats_since column (PostgreSQL 17+) or query it before resetting.
--
-- VERSION NOTES
--   * PostgreSQL 13 renamed `total_time` -> `total_exec_time` and
--     `mean_time` -> `mean_exec_time`, and added `wal_bytes` plus the
--     planning-time columns (`plans`, `total_plan_time`, `mean_plan_time`,
--     `min_plan_time`, `max_plan_time`, `stddev_plan_time`). Secondary
--     queries below use the 13+ names. Q3.9 gives a compatibility variant
--     that works on 12 and older by reading the row as JSONB.
--   * PostgreSQL 17 (pg_stat_statements 1.11) RENAMED the I/O timing
--     columns: `blk_read_time` -> `shared_blk_read_time`, and
--     `blk_write_time` -> `shared_blk_write_time`, adding per-context
--     siblings `local_blk_read_time`, `local_blk_write_time`,
--     `temp_blk_read_time`, `temp_blk_write_time`. Hardcoding either
--     spelling fails outright on the other side of that line, which is why
--     Q3.5 reads those two columns out of `to_jsonb(row)` instead.
--   * `stats_since` and `minmax_stats_since` are PostgreSQL 17+;
--     `pg_stat_statements_info` (with `dealloc`) is PostgreSQL 14+.
--   * `round(double precision, integer)` DOES NOT EXIST in PostgreSQL -
--     round(x, n) is numeric-only. Every rounding below casts to numeric
--     first. If you copy these queries, keep the casts.
--   * `shared_blks_*` counts are in 8 kB blocks by default; multiply by
--     current_setting('block_size') for bytes rather than assuming 8192.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q3.0  PREFLIGHT - is pg_stat_statements actually available?
-- ---------------------------------------------------------------------
-- Run this first. It needs no extension and always works.
SELECT
    current_setting('shared_preload_libraries')                        AS shared_preload_libraries,
    EXISTS (SELECT 1 FROM pg_extension
             WHERE extname = 'pg_stat_statements')                     AS extension_installed,
    (SELECT extversion FROM pg_extension
      WHERE extname = 'pg_stat_statements')                            AS extension_version,
    (SELECT count(*) FROM pg_available_extensions
      WHERE name = 'pg_stat_statements')                               AS available_to_install,
    current_setting('track_io_timing')                                 AS track_io_timing,
    current_setting('pg_stat_statements.track', true)                  AS pgss_track_setting;


-- ---------------------------------------------------------------------
-- Q3.1  Top queries by TOTAL time - where the database's time goes
-- ---------------------------------------------------------------------
-- pct_of_total is computed over the whole visible table, so with a large
-- pg_stat_statements.max and low eviction you are looking at the real
-- budget split. If top 10 rows do not add up to 50%, you have a flat,
-- diffuse workload and no single fix.
--
-- rows_per_call near 1.0 on a query that also has high calls is an N+1
-- access pattern: the application is issuing one round trip per row.
SELECT
    s.queryid,
    s.calls,
    round(s.total_exec_time::numeric, 1)                                AS total_ms,
    round((100.0 * s.total_exec_time
           / NULLIF(sum(s.total_exec_time) OVER (), 0))::numeric, 1)     AS pct_of_total,
    round(s.mean_exec_time::numeric, 3)                                 AS mean_ms,
    round(s.stddev_exec_time::numeric, 3)                               AS stddev_ms,
    round(s.max_exec_time::numeric, 1)                                  AS max_ms,
    s.rows,
    round(s.rows::numeric / GREATEST(s.calls, 1), 1)                    AS rows_per_call,
    s.shared_blks_hit,
    s.shared_blks_read,
    round((100.0 * s.shared_blks_hit
           / NULLIF(s.shared_blks_hit + s.shared_blks_read, 0))::numeric, 1) AS cache_hit_pct,
    left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)                 AS query
FROM pg_stat_statements s
WHERE s.calls > 5
  AND s.query NOT ILIKE '%pg_stat_statements%'    -- hide this file's own queries
ORDER BY s.total_exec_time DESC
LIMIT 25;


-- ---------------------------------------------------------------------
-- Q3.2  Top queries by MEAN time - the individually painful ones
-- ---------------------------------------------------------------------
-- The calls >= 20 floor keeps one-off DDL and a single 40-minute backfill
-- from dominating. A high mean here with modest call volume is still
-- worth fixing if the query sits on a user-facing request path.
SELECT
    s.queryid,
    s.calls,
    round(s.mean_exec_time::numeric, 2)                    AS mean_ms,
    round(s.total_exec_time::numeric, 0)                   AS total_ms,
    round(s.max_exec_time::numeric, 1)                     AS max_ms,
    s.rows,
    round(s.rows::numeric / GREATEST(s.calls, 1), 1)       AS rows_per_call,
    CASE WHEN s.temp_blks_written > 0 THEN 'writes temp files' END AS temp_spill,
    left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)    AS query
FROM pg_stat_statements s
WHERE s.calls >= 20
  AND s.query NOT ILIKE '%pg_stat_statements%'
ORDER BY s.mean_exec_time DESC
LIMIT 25;


-- ---------------------------------------------------------------------
-- Q3.3  PLAN INSTABILITY - same query, wildly different runtimes
-- ---------------------------------------------------------------------
-- coeff_variation = stddev_exec_time / mean_exec_time.
--   < 0.3  stable, one plan, predictable.
--   0.3-1  some parameter sensitivity.
--   > 1.0  the query is running materially different plans depending on
--          the parameter values. A single index will NOT fix this. Look
--          for: a predicate on a column with skewed distribution, a
--          generic plan cached for prepared statements, or a query that
--          sometimes matches 3 rows and sometimes 3 million.
-- Candidates to investigate: set plan_cache_mode = force_custom_plan for
-- the offending prepared statement, or split the query into a selective
-- branch and a bulk branch.
SELECT
    s.queryid,
    s.calls,
    round(s.mean_exec_time::numeric, 2)                     AS mean_ms,
    round(s.stddev_exec_time::numeric, 2)                   AS stddev_ms,
    round((s.stddev_exec_time / NULLIF(s.mean_exec_time, 0))::numeric, 2) AS coeff_variation,
    round(s.min_exec_time::numeric, 2)                      AS min_ms,
    round(s.max_exec_time::numeric, 2)                      AS max_ms,
    round((s.max_exec_time / NULLIF(s.min_exec_time, 0))::numeric, 1)     AS max_over_min,
    left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)     AS query
FROM pg_stat_statements s
WHERE s.calls >= 50
  AND s.mean_exec_time > 1
  AND s.query NOT ILIKE '%pg_stat_statements%'
ORDER BY (s.stddev_exec_time / NULLIF(s.mean_exec_time, 0)) DESC,
         s.total_exec_time DESC
LIMIT 25;


-- ---------------------------------------------------------------------
-- Q3.4  work_mem pressure - queries spilling to temporary files
-- ---------------------------------------------------------------------
-- Any non-zero temp_blks_written means this query's sort or hash did not
-- fit in work_mem and went to disk. On a busy system this is usually the
-- highest return-on-effort finding in the whole pack, because the fix is
-- `SET work_mem` or a per-role/per-database override - no schema change,
-- no downtime.
--
-- DO NOT raise work_mem globally to a large value. work_mem is per sort
-- node, per connection: 100 connections x 4 concurrent sorts x 256MB is
-- 100 GB of potential allocation and an OOM kill. Raise it for the
-- specific role or database that runs these queries, and confirm the
-- change on one connection first.
SELECT
    s.queryid,
    s.calls,
    s.temp_blks_written,
    pg_size_pretty((s.temp_blks_written
                    * current_setting('block_size')::bigint)::numeric::bigint) AS temp_written,
    round((s.temp_blks_written * current_setting('block_size')::numeric
           / 1024 / 1024 / GREATEST(s.calls, 1))::numeric, 2)        AS temp_mb_per_call,
    round(s.mean_exec_time::numeric, 1)                              AS mean_ms,
    round(s.total_exec_time::numeric, 0)                             AS total_ms,
    s.temp_blks_read,
    left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)              AS query
FROM pg_stat_statements s
WHERE s.temp_blks_written > 0
  AND s.query NOT ILIKE '%pg_stat_statements%'
ORDER BY s.temp_blks_written DESC
LIMIT 25;


-- ---------------------------------------------------------------------
-- Q3.5  Cache-miss heavy statements - the I/O hogs
-- ---------------------------------------------------------------------
-- shared_blks_read is real disk (or OS page cache) traffic, while
-- shared_blks_hit came from shared_buffers. A statement with a low hit
-- ratio AND a large read count is doing physical I/O on every execution.
-- Compare per-call reads against the number of rows returned: a query
-- reading thousands of blocks per call to return 5 rows is reading the
-- wrong way round (missing index, or an index whose heap fetches all
-- miss).
--
-- Requires track_io_timing = on (see the header) for the timing columns to
-- be non-zero; without it they stay 0 and are not evidence. The block
-- counts are meaningful either way.
--
-- WHY THE TIMING COLUMNS ARE READ AS JSONB:
--   PostgreSQL 17 renamed blk_read_time -> shared_blk_read_time. A query
--   hardcoding either name breaks on the other version. COALESCE over
--   to_jsonb(s) picks whichever key this server actually has, so the
--   statement runs unchanged from 9.4 through 18 - at the cost of one
--   row-to-JSON conversion per returned row, which is irrelevant against a
--   LIMIT 25 over an in-memory hash table.
SELECT
    s.queryid,
    s.calls,
    s.shared_blks_read,
    round((s.shared_blks_read::numeric / GREATEST(s.calls, 1)), 1)  AS blocks_read_per_call,
    s.shared_blks_hit,
    round((100.0 * s.shared_blks_hit
           / NULLIF(s.shared_blks_hit + s.shared_blks_read, 0))::numeric, 1) AS cache_hit_pct,
    round(COALESCE(to_jsonb(s) ->> 'shared_blk_read_time',
                   to_jsonb(s) ->> 'blk_read_time')::numeric, 1)    AS blk_read_ms_total,
    round(COALESCE(to_jsonb(s) ->> 'shared_blk_read_time',
                   to_jsonb(s) ->> 'blk_read_time')::numeric
          / GREATEST(s.calls, 1), 3)                                AS blk_read_ms_per_call,
    round(s.mean_exec_time::numeric, 2)                             AS mean_ms,
    left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)             AS query
FROM pg_stat_statements s
WHERE s.shared_blks_read > 1000
  AND s.calls > 10
  AND s.query NOT ILIKE '%pg_stat_statements%'
ORDER BY s.shared_blks_read DESC
LIMIT 25;


-- ---------------------------------------------------------------------
-- Q3.6  The 80/20 line - how concentrated is the pain?
-- ---------------------------------------------------------------------
-- Fewer than ~10 query shapes owning 80% of database time means you have
-- a small, tractable target list. A hundred shapes sharing it means the
-- problem is systemic (too many round trips, missing indexes everywhere,
-- or an undersized instance) and per-query tuning will not save you.
WITH ranked AS (
    SELECT
        s.queryid,
        s.calls,
        s.total_exec_time,
        row_number() OVER (ORDER BY s.total_exec_time DESC)     AS rank,
        sum(s.total_exec_time) OVER ()                          AS grand_total,
        sum(s.total_exec_time) OVER (ORDER BY s.total_exec_time DESC
                                     ROWS UNBOUNDED PRECEDING)  AS running_total
    FROM pg_stat_statements s
    WHERE s.query NOT ILIKE '%pg_stat_statements%'
)
SELECT
    count(*)                                                  AS distinct_query_shapes,
    sum(calls)                                                AS total_calls,
    round((sum(total_exec_time) / 1000)::numeric, 1)          AS total_seconds,
    (SELECT count(*) FROM ranked
      WHERE running_total <= 0.80 * grand_total)              AS shapes_for_80pct_of_time,
    round(max(running_total / NULLIF(grand_total, 0) * 100)::numeric, 1) AS top_shape_share_pct
FROM ranked;


-- ---------------------------------------------------------------------
-- Q3.7  Per-statement cumulative impact, sorted by "seconds per day"
-- ---------------------------------------------------------------------
-- Operations people think in wall-clock cost, not milliseconds. This
-- converts cumulative execution time into seconds of database time per
-- day, which is the number to take to a planning meeting.
-- Divide by uptime instead of a day if you want a different window.
SELECT
    s.queryid,
    s.calls,
    round((s.total_exec_time / 1000)::numeric, 1)                AS total_seconds,
    round((s.total_exec_time / 1000
           / GREATEST(EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))
                      / 86400.0, 0.0001))::numeric, 1)           AS seconds_per_day,
    round(s.mean_exec_time::numeric, 2)                          AS mean_ms,
    left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)          AS query
FROM pg_stat_statements s
WHERE s.query NOT ILIKE '%pg_stat_statements%'
ORDER BY s.total_exec_time DESC
LIMIT 20;

-- Companion metric: how much of total database time is spent executing
-- statements at all versus doing other work. Read committed/idle time is
-- not here - that lives in pg_stat_database (see file 06) and in the
-- difference between pg_stat_statements total and pg_stat_database
-- session_time on PostgreSQL 14+.


-- ---------------------------------------------------------------------
-- Q3.8  PostgreSQL 14+ - planning time vs execution time
-- ---------------------------------------------------------------------
-- If total_plan_time is a large share of (plan + exec), the planner is
-- the bottleneck, not the data. Symptoms: thousands of tables/partitions
-- making planning expensive, or very high call counts on trivial
-- statements. Fixes are different from index fixes.
--
--   SELECT s.queryid, s.calls,
--          round(s.mean_plan_time::numeric, 3)  AS mean_plan_ms,
--          round(s.mean_exec_time::numeric, 3)  AS mean_exec_ms,
--          round((100.0 * s.total_plan_time
--                 / NULLIF(s.total_plan_time + s.total_exec_time, 0))::numeric, 1)
--                                               AS pct_time_planning,
--          left(regexp_replace(s.query, '\s+', ' ', 'g'), 200) AS query
--   FROM pg_stat_statements s
--   WHERE s.calls > 100
--     AND s.query NOT ILIKE '%pg_stat_statements%'
--   ORDER BY s.total_plan_time DESC
--   LIMIT 25;


-- ---------------------------------------------------------------------
-- Q3.9  PostgreSQL 12 and older - compatibility variant of Q3.1
-- ---------------------------------------------------------------------
-- On 12 and older the timing columns are named total_time / mean_time /
-- stddev_time / min_time / max_time instead of *_exec_time, and wal_bytes
-- does not exist at all. Rather than branching, read the row as JSONB and
-- pull whichever key exists. This is slower than a direct column read but
-- runs unchanged on 9.4 through 17.
--
--   SELECT s.queryid, s.calls,
--          COALESCE((to_jsonb(s) ->> 'total_exec_time'),
--                   (to_jsonb(s) ->> 'total_time'))::numeric       AS total_ms,
--          COALESCE((to_jsonb(s) ->> 'mean_exec_time'),
--                   (to_jsonb(s) ->> 'mean_time'))::numeric        AS mean_ms,
--          COALESCE((to_jsonb(s) ->> 'stddev_exec_time'),
--                   (to_jsonb(s) ->> 'stddev_time'))::numeric      AS stddev_ms,
--          s.rows,
--          left(regexp_replace(s.query, '\s+', ' ', 'g'), 220)     AS query
--   FROM pg_stat_statements s
--   WHERE s.calls > 5
--     AND s.query NOT ILIKE '%pg_stat_statements%'
--   ORDER BY 3 DESC
--   LIMIT 25;
