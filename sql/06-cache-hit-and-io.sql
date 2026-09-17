-- =====================================================================
-- 06 - CACHE HIT RATIOS AND I/O STATISTICS
-- =====================================================================
--
-- WHAT THIS SHOWS
--   Where reads are being served from shared_buffers versus where they
--   are going to the operating system or to disk, per database, per
--   table, per index and per TOAST table. Plus temporary-file usage, the
--   background writer, and replication lag.
--
-- WHAT TO LOOK FOR
--   * Q6.1 - the database-wide hit ratio. It is the least useful number
--     in this file and the one people quote most, because 99% is normal
--     even on a badly tuned server. Read the CAVEAT below before you use
--     it in a meeting.
--   * Q6.2 - per-table hit ratios. These ARE useful: a single table at
--     85% while everything else is at 99.9% is a specific problem with a
--     specific fix, and it tells you which query to go find in file 03.
--   * Q6.5 - temporary file volume. On a database that reads almost
--     entirely from cache, temp files are often where the real I/O is.
--   * Q6.6 - the background writer and whether backends are forced to
--     write out their own dirty buffers (buffers_backend on 16 and
--     older). High buffers_backend means checkpoint pacing is wrong.
--
-- THE CENTRAL CAVEAT ABOUT CACHE HIT RATIO
--   1. blks_hit / (blks_hit + blks_read) counts BUFFER accesses, not
--      queries and not rows. A query that scans the same 200-page index
--      10,000 times contributes 2,000,000 hits and can push the ratio to
--      99.99% while the query is still slow. A high ratio does not mean
--      the system is fast; it means the pages it did touch were in
--      memory. It says nothing about pages it never had to touch because
--      it was doing a sequential scan.
--   2. A "miss" counted here is a miss in shared_buffers. If the
--      operating system page cache still holds the page, the "read" is a
--      memory copy from the OS, not a disk seek. PostgreSQL cannot see
--      that distinction, so a low ratio is not automatically disk-bound.
--      Compare against actual disk latency from pg_stat_io (PostgreSQL
--      16+) or your host metrics before concluding anything.
--   3. `blks_read` on a standby is not comparable to a primary.
--   4. On PostgreSQL 17+ some counters that used to live in
--      pg_stat_bgwriter moved to pg_stat_checkpointer. Q6.6 only selects
--      columns that exist on both sides of that move.
--
-- RISK OF RUNNING THIS
--   READ-ONLY and cheap. Statistics views only. Safe during an incident.
--   The one thing to avoid: reading these views on a replica and then
--   making decisions about the primary. A standby's buffers are warm from
--   replaying WAL and its hit ratios do not reflect the primary's.
--
-- VERSION NOTES
--   * `track_io_timing` must be `on` for blk_read_time / blk_write_time
--     to be populated (Q6.1, Q6.4). It is off by default because it calls
--     the OS clock on every I/O. Turn it on and watch the overhead before
--     leaving it on in a very high-IOPS environment.
--   * `pg_stat_io` is PostgreSQL 16+ (Q6.5, commented out).
--   * `session_time`, `active_time`, `idle_in_transaction_time` are
--     PostgreSQL 14+ and are not used below so the file runs everywhere.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q6.1  Database-wide cache hit ratio (with the caveat, restated)
-- ---------------------------------------------------------------------
-- A hit ratio above 99% is the normal case and tells you almost nothing.
-- What IS worth watching here:
--   * xact_rollback / (xact_commit + xact_rollback): above ~1% means the
--     application is provoking errors, and every rollback wastes the work
--     already done plus a WAL flush.
--   * deadlocks > 0: the application takes locks in inconsistent orders.
--     No database setting fixes this.
--   * temp_bytes: sorted/hashed data that did not fit in work_mem.
--   * blk_read_time / blk_write_time: real I/O latency, only meaningful
--     when track_io_timing = on.
SELECT
    datname,
    numbackends,
    xact_commit,
    xact_rollback,
    round(100.0 * xact_rollback
          / NULLIF(xact_commit + xact_rollback, 0), 3)              AS rollback_pct,
    blks_read,
    blks_hit,
    round(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2)    AS cache_hit_pct,
    round(100.0 * tup_returned
          / NULLIF(tup_returned + tup_fetched, 0), 1)               AS pct_tuples_from_seqscan,
    temp_files,
    pg_size_pretty(temp_bytes)                                      AS temp_bytes,
    deadlocks,
    round(blk_read_time::numeric, 0)                                AS blk_read_ms,
    round(blk_write_time::numeric, 0)                               AS blk_write_ms,
    stats_reset
FROM pg_stat_database
WHERE datname = current_database();

-- Note on `pct_tuples_from_seqscan`: tup_returned counts rows returned by
-- sequential scans, tup_fetched counts rows fetched by index scans. If
-- tup_returned dominates, most of your row traffic never touched an
-- index at all. That is the database-level version of the per-table
-- signal in file 02, and it is a much better indicator of "we are missing
-- indexes" than the cache hit ratio.


-- ---------------------------------------------------------------------
-- Q6.2  Per-table cache hit ratio - this is the useful one
-- ---------------------------------------------------------------------
-- A table that is read constantly and sits below ~95% is either too big
-- for shared_buffers or is being scanned in an order that defeats the
-- cache. Both are specific, and both are worth chasing.
--
-- heap_hit + heap_read = 0 means the table was never read at all since
-- stats reset - it is write-only, which is fine.
SELECT
    s.schemaname,
    s.relname,
    s.heap_blks_read,
    s.heap_blks_hit,
    round(100.0 * s.heap_blks_hit
          / NULLIF(s.heap_blks_hit + s.heap_blks_read, 0), 2)  AS heap_hit_pct,
    s.idx_blks_read,
    s.idx_blks_hit,
    round(100.0 * s.idx_blks_hit
          / NULLIF(s.idx_blks_hit + s.idx_blks_read, 0), 2)    AS index_hit_pct,
    s.tidx_blks_read                                          AS toast_idx_reads,
    t.n_live_tup,
    pg_size_pretty(pg_relation_size(s.relid))                 AS heap_size,
    pg_size_pretty(pg_total_relation_size(s.relid))           AS total_size
FROM pg_statio_user_tables s
JOIN pg_stat_user_tables t ON t.relid = s.relid
WHERE s.heap_blks_hit + s.heap_blks_read + s.idx_blks_hit + s.idx_blks_read > 10000
ORDER BY (s.heap_blks_read + s.idx_blks_read) DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q6.3  Tables whose reads MISS the cache most
-- ---------------------------------------------------------------------
-- Sorted by absolute reads rather than ratio, so a huge table at 97% can
-- outrank a small one at 80%. Absolute misses are what cost you disk.
SELECT
    s.schemaname,
    s.relname,
    s.heap_blks_read                                       AS heap_misses,
    s.idx_blks_read                                        AS index_misses,
    s.heap_blks_read + s.idx_blks_read                     AS total_misses,
    pg_size_pretty(((s.heap_blks_read + s.idx_blks_read)::bigint
                    * current_setting('block_size')::bigint))  AS approx_bytes_read,
    round(100.0 * s.heap_blks_hit
          / NULLIF(s.heap_blks_hit + s.heap_blks_read, 0), 2) AS heap_hit_pct,
    pg_size_pretty(pg_total_relation_size(s.relid))        AS total_size,
    round(pg_total_relation_size(s.relid)::numeric
          / NULLIF((SELECT setting::bigint FROM pg_settings
                     WHERE name = 'shared_buffers'), 0), 2) AS size_in_shared_buffers
FROM pg_statio_user_tables s
ORDER BY s.heap_blks_read + s.idx_blks_read DESC
LIMIT 30;

-- size_in_shared_buffers is the ratio that explains most cache-miss
-- cases: a table occupying 8x shared_buffers cannot stay resident under
-- mixed access. Either the query needs an index that returns fewer pages,
-- or the table needs partitioning, or the instance needs more memory.
-- Do not reach for `shared_buffers = 25% of RAM` reflexively - that rule
-- of thumb is for dedicated database hosts, and on a host with a large
-- OS page cache, raising shared_buffers beyond ~8-16 GB often gains
-- nothing because the OS cache was doing the job already.


-- ---------------------------------------------------------------------
-- Q6.4  Per-index I/O - which indexes actually cost you reads
-- ---------------------------------------------------------------------
SELECT
    s.schemaname,
    s.relname                                        AS table_name,
    s.indexrelname                                   AS index_name,
    s.idx_blks_read,
    s.idx_blks_hit,
    round(100.0 * s.idx_blks_hit
          / NULLIF(s.idx_blks_hit + s.idx_blks_read, 0), 2) AS index_hit_pct,
    us.idx_scan,
    pg_size_pretty(pg_relation_size(s.indexrelid))   AS index_size,
    round(s.idx_blks_read::numeric
          / GREATEST(us.idx_scan, 1), 1)             AS misses_per_scan
FROM pg_statio_user_indexes s
JOIN pg_stat_user_indexes us ON us.indexrelid = s.indexrelid
WHERE s.idx_blks_read + s.idx_blks_hit > 1000
ORDER BY s.idx_blks_read DESC
LIMIT 30;

-- misses_per_scan above a few hundred on a large index means each scan is
-- walking a lot of pages it has to fetch: either the index has poor
-- correlation with the heap (an index scan returning scattered rows),
-- which a BRIN or a different key order would help, or the index is
-- bloated and only a fraction of its pages hold live entries (file 01).


-- ---------------------------------------------------------------------
-- Q6.5  OPTIONAL - PostgreSQL 16+ pg_stat_io, the real I/O picture
-- ---------------------------------------------------------------------
-- pg_stat_io is the first view in PostgreSQL that breaks I/O down by
-- backend type and by context, which is what you need to answer "is it
-- WAL, is it vacuum, is it the client backends, or is it checkpoints?"
--
--   SELECT backend_type, object, context,
--          reads, round(read_time::numeric, 0)     AS read_ms,
--          writes, round(write_time::numeric, 0)   AS write_ms,
--          extends, hits, evictions
--   FROM pg_stat_io
--   WHERE reads + writes + extends > 1000
--   ORDER BY reads DESC;
--
-- What to look for:
--   * context = 'vacuum' with a large share of reads: vacuum is doing a
--     lot of I/O. Usually a sign that autovacuum is running too often
--     because the scale factor is too low, or too rarely so each run is
--     enormous.
--   * backend_type = 'client backend' with high read_time: your queries
--     are doing physical I/O. Go to file 03.
--   * evictions high relative to hits: shared_buffers is too small for
--     the working set, or one query is thrashing it.
--   * object = 'relation' vs 'wal': separates data I/O from WAL I/O.
--
-- Column note: fsyncs / fsync_time existed on 16 and 17 and are gone on
-- 18, which is why they are not selected above.


-- ---------------------------------------------------------------------
-- Q6.6  Background writer and allocator - portable across 9.6 to 18
-- ---------------------------------------------------------------------
-- Only four columns are selected on purpose: PostgreSQL 17 moved the
-- checkpoint counters out of pg_stat_bgwriter into pg_stat_checkpointer,
-- and removed buffers_backend (it is now pg_stat_io with
-- backend_type='client backend' and context='normal').
--
-- How to read it:
--   * buffers_clean: pages written by the background writer ahead of
--     demand. You want this to be doing work.
--   * maxwritten_clean: how many times the bgwriter stopped early because
--     it hit bgwriter_lru_maxpages in one round. A large number means the
--     bgwriter is being throttled while dirty pages pile up - raise
--     bgwriter_lru_maxpages or lower bgwriter_lru_multiplier's inverse...
--     more precisely, raise bgwriter_lru_maxpages so each round can flush
--     the pages it estimated were needed.
--   * buffers_alloc: total buffer allocations. Useful as the denominator
--     for the other two.
SELECT
    buffers_clean,
    maxwritten_clean,
    buffers_alloc,
    stats_reset,
    now() - stats_reset                                AS since_reset
FROM pg_stat_bgwriter;

-- PostgreSQL 16 and older only: backend writes are a checkpoint-pacing
-- symptom. If buffers_backend is a large fraction of buffers_alloc, the
-- checkpointer is not spreading writes out and client backends are being
-- forced to write dirty pages synchronously (a stall, not just I/O).
--
--   SELECT buffers_clean, maxwritten_clean, buffers_alloc,
--          buffers_backend, buffers_backend_fsync,
--          checkpoints_timed, checkpoints_req,
--          round(100.0 * checkpoints_req
--                / NULLIF(checkpoints_timed + checkpoints_req, 0), 1)
--              AS pct_checkpoints_forced
--   FROM pg_stat_bgwriter;
--
-- checkpoints_req (forced) should be a small fraction of the total. If
-- most checkpoints are forced, max_wal_size is too small for your write
-- rate and every checkpoint is a spike. Raise max_wal_size (it is a soft
-- limit, cheap to raise) before touching checkpoint_completion_target.


-- ---------------------------------------------------------------------
-- Q6.7  Temporary file usage - the I/O that hides from the cache ratio
-- ---------------------------------------------------------------------
-- Sorted by data written, because writes are what hurt. Temp files are
-- created for sorts and hashes that exceed work_mem, and for some
-- materialisation steps. Each one is thrown away, so this is pure waste.
SELECT
    datname,
    temp_files,
    pg_size_pretty(temp_bytes)                                AS temp_bytes,
    round(temp_bytes::numeric
          / GREATEST(temp_files, 1) / 1024 / 1024, 2)         AS avg_mb_per_temp_file,
    round(100.0 * blks_read / NULLIF(blks_read + blks_hit, 0), 2) AS cache_miss_pct,
    stats_reset
FROM pg_stat_database
WHERE temp_files > 0
ORDER BY temp_bytes DESC
LIMIT 20;

-- If temp_files is large but you cannot tell which query, use file 03
-- Q3.4, which attributes temp blocks to individual statements. The fix
-- order is:
--   1. SET work_mem higher for the specific role/database that runs the
--      query - never globally, see the note in file 03.
--   2. Check whether the query needs to sort at all: an index matching
--      the ORDER BY removes the sort entirely, which is better than
--      giving the sort more memory.
--   3. Check for a hash join on a badly estimated row count - the planner
--      chooses a hash join because it thinks the input is small. Fix the
--      statistics (ANALYZE, or ALTER TABLE ... ALTER COLUMN ... SET
--      STATISTICS) and the planner may choose a nested loop instead.


-- ---------------------------------------------------------------------
-- Q6.8  Replication lag - because a lagging replica changes your reads
-- ---------------------------------------------------------------------
-- read_lag (PostgreSQL 10+) is a time-based estimate of how far behind a
-- standby is. write_lag and flush_lag isolate the network from the local
-- I/O; if write_lag is tiny but replay_lag is large, the standby's disk
-- or its single-threaded replay is the bottleneck, not the network.
--
-- On a primary with no standbys this returns zero rows, which is normal.
SELECT
    pid,
    usename,
    application_name,
    COALESCE(client_addr::text, 'local')   AS client,
    state,
    sync_state,
    sent_lsn,
    write_lsn,
    flush_lsn,
    replay_lsn,
    (sent_lsn - replay_lsn)                AS lsn_behind_bytes,
    write_lag,
    flush_lag,
    replay_lag
FROM pg_stat_replication
ORDER BY replay_lag DESC NULLS LAST;
