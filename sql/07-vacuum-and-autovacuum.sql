-- =====================================================================
-- 07 - VACUUM AND AUTOVACUUM HEALTH
-- =====================================================================
--
-- WHAT THIS SHOWS
--   Whether autovacuum is actually keeping up, computed rather than
--   guessed: for every table, the exact dead-tuple count at which
--   autovacuum will fire, how far past that line the table currently is,
--   how stale the planner's statistics are, and how close each table is
--   to a transaction-ID wraparound forced vacuum.
--
-- WHAT TO LOOK FOR
--   * Q7.2 - tables already PAST their autovacuum trigger point. If
--     autovacuum is enabled and the table is over the threshold, then
--     autovacuum is either running and losing (Q7.6, Q7.7) or it cannot
--     run at all because an old transaction pins the xmin horizon
--     (file 05, Q5.7). Check that first - tuning scale factors on a
--     database with a pinned horizon changes nothing.
--   * Q7.3 - a table last autovacuumed days ago while being written
--     constantly is the same signal, seen from the other end.
--   * Q7.4 - stale statistics. The planner cannot choose a good plan from
--     numbers that do not describe the data. This is the cheapest fix in
--     the entire pack: ANALYZE (or just wait for autoanalyze).
--   * Q7.5 - wraparound proximity. This is the only item in this pack
--     that can take a database down permanently. Wraparound at age 2^31
--     refuses all new writes. If any table is above 75% of
--     autovacuum_freeze_max_age, stop reading and deal with it.
--   * Q7.8 - tables with autovacuum disabled. Someone turned it off
--     because a vacuum was disruptive. Something has to replace it.
--
-- THE #1 MISDIAGNOSIS, STATED PLAINLY
--   "Autovacuum is not running." Autovacuum almost always IS running. It
--   is either (a) throttled by cost limits, (b) unable to remove anything
--   because an old snapshot pins the horizon, or (c) firing so rarely on
--   a huge table that each run has to do enormous work. Those three have
--   three different fixes. Q7.6 and file 05 Q5.7 tell you which one you
--   have; do not skip them.
--
-- RISK OF RUNNING THIS
--   READ-ONLY. Catalog and statistics views. `pg_stat_progress_vacuum`
--   is a live view and is free to read.
--   The recommended ACTIONS in the comments are not read-only:
--     * VACUUM (without FULL) takes a SHARE UPDATE EXCLUSIVE lock on the
--       table. It does NOT block reads or writes. It is safe to run under
--       load, but it competes for I/O - run it with a cost limit.
--     * VACUUM FULL takes an ACCESS EXCLUSIVE lock and BLOCKS EVERYTHING
--       on that table for the duration. It also rewrites the table and
--       needs free disk equal to the table's size. Never run it on a live
--       primary during business hours.
--     * ALTER TABLE ... SET (autovacuum_...) takes ACCESS EXCLUSIVE
--       briefly. Use LOCK_TIMEOUT so it does not queue behind a long
--       transaction and stall the table behind it.
--
-- VERSION NOTES
--   * `pg_stat_progress_vacuum` is 9.6+.
--   * Q7.6's SECOND statement reads the dead-tuple-store columns of
--     `pg_stat_progress_vacuum`, which PostgreSQL 17 RENAMED and RE-UNIT-ED.
--     This is not a pure rename:
--
--         PostgreSQL 16 and older : max_dead_tuples       - a TUPLE count
--                                   num_dead_tuples       - a TUPLE count
--         PostgreSQL 17 and newer : max_dead_tuple_bytes  - BYTES
--                                   dead_tuple_bytes      - BYTES
--                                   num_dead_item_ids     - a count of item IDs
--
--     Naming either branch's columns directly makes the statement fail to
--     parse on the other branch, so the version-specific fields are read out
--     of the row's JSON form (`to_jsonb(p) ->> '...'`). That parses on every
--     supported version and yields NULL for a key your version does not have.
--     The result column `dead_store_units` says which branch you are on -
--     'tuples' on 16 and older, 'bytes' on 17 and newer - and Q7.6 reports
--     capacity and usage in that same unit, so `pct_dead_store_full` is a
--     like-for-like ratio on both branches (on 17+ the byte pair is the
--     comparable one; `num_dead_item_ids` is NOT byte-comparable).
--
--     VERIFIED on PostgreSQL 17.11 (this pack's build server): the statement
--     parses, executes and returns without error, and `dead_store_units`
--     reports 'bytes'. It returns no rows when no VACUUM is running, which is
--     the normal state; the branch itself was exercised by shape, not by
--     catching a live vacuum mid-flight. The 'tuples' branch taken on 16 and
--     older is **UNVERIFIED** - no 16-or-older server was available here.
--   * Q7.7 reads `pg_stat_user_tables`, which is already scoped to the
--     current database and has never had a `datname` column on any release.
--     Naming one there fails with 42703 on EVERY version, so this file uses
--     `schemaname` and `relname`. VERIFIED on PostgreSQL 17.11.
--   * `pg_stat_activity.backend_type` is PostgreSQL 10+. On 9.6, Q7.6's
--     first query still works via its `query ILIKE 'autovacuum:%'` terms
--     but will not see a worker that is between statements; drop the
--     backend_type line if the column does not exist.
--   * `n_ins_since_vacuum` (PostgreSQL 13+) is a useful extra column to
--     add to Q7.3: it is the insert counter that
--     autovacuum_vacuum_insert_threshold acts on, and it matters for
--     insert-only tables that never accumulate dead tuples and therefore
--     never get vacuumed by the dead-tuple rule at all.
--   * `relfrozenxid` / `relminmxid` exist on all supported versions.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q7.1  The current autovacuum settings - what is actually in effect
-- ---------------------------------------------------------------------
-- Read this before changing anything. `source` tells you whether a value
-- comes from postgresql.conf, a per-database ALTER DATABASE, a per-role
-- ALTER ROLE, or the compiled-in default. Values set in more than one
-- place are the usual reason a change "did not take effect".
SELECT
    name,
    setting,
    unit,
    boot_val,
    reset_val,
    source,
    short_desc
FROM pg_settings
WHERE name IN (
        'autovacuum',
        'autovacuum_naptime',
        'autovacuum_max_workers',
        'autovacuum_vacuum_threshold',
        'autovacuum_vacuum_scale_factor',
        'autovacuum_vacuum_insert_threshold',
        'autovacuum_vacuum_insert_scale_factor',
        'autovacuum_analyze_threshold',
        'autovacuum_analyze_scale_factor',
        'autovacuum_vacuum_cost_delay',
        'autovacuum_vacuum_cost_limit',
        'vacuum_cost_page_hit',
        'vacuum_cost_page_miss',
        'vacuum_cost_page_dirty',
        'autovacuum_freeze_max_age',
        'autovacuum_multixact_freeze_max_age',
        'autovacuum_freeze_min_age',
        'autovacuum_freeze_table_age',
        'vacuum_failsafe_age',
        'vacuum_multixact_failsafe_age',
        'max_connections',
        'maintenance_work_mem',
        'track_counts',
        'track_cost_delay_timing'
      )
ORDER BY name;

-- `autovacuum_vacuum_insert_threshold` / `..._insert_scale_factor` are
-- PostgreSQL 13+. On 12 and older they do not exist and the IN list above
-- simply matches nothing for them - the query still runs.
-- `vacuum_failsafe_age` is PostgreSQL 14+.
-- `track_counts` MUST be on, or every pg_stat_user_tables column this
-- file relies on stays at zero and autovacuum will not fire at all.


-- ---------------------------------------------------------------------
-- Q7.2  Computed autovacuum trigger point, per table
-- ---------------------------------------------------------------------
-- THE FORMULA AUTOVACUUM ACTUALLY USES (from the PostgreSQL source):
--
--     threshold = autovacuum_vacuum_threshold
--               + autovacuum_vacuum_scale_factor * reltuples
--
-- where reltuples is pg_class.reltuples, i.e. the tuple count as of the
-- last VACUUM or ANALYZE - not n_live_tup. Then autovacuum fires when
-- n_dead_tup exceeds that threshold. Per-table reloptions override both
-- terms; the COALESCEs below implement exactly that.
--
-- dead_over_threshold > 0 means the table is past the line. If autovacuum
-- is enabled and has not fired, look for a pinned xmin horizon or a
-- throttled worker, not for the threshold.
--
-- WHY LARGE TABLES ARE ALWAYS "BEHIND"
--   With the default scale factor of 0.20, a 500 GB table needs 100 GB of
--   dead tuples before autovacuum starts. That is the design, and it is
--   why big tables need a LOWER scale factor and a high
--   autovacuum_vacuum_cost_limit - not a higher threshold.
SELECT
    t.schemaname,
    t.relname                                            AS table_name,
    t.n_live_tup,
    t.n_dead_tup,
    c.reltuples::bigint                                  AS reltuples_used_by_the_formula,
    COALESCE(
        substring(array_to_string(c.reloptions, ',')
                  FROM 'autovacuum_vacuum_threshold=([0-9]+)')::numeric,
        (SELECT setting::numeric FROM pg_settings
          WHERE name = 'autovacuum_vacuum_threshold')
    )                                                    AS eff_threshold,
    COALESCE(
        substring(array_to_string(c.reloptions, ',')
                  FROM 'autovacuum_vacuum_scale_factor=([0-9.]+)')::numeric,
        (SELECT setting::numeric FROM pg_settings
          WHERE name = 'autovacuum_vacuum_scale_factor')
    )                                                    AS eff_scale_factor,
    ceil(
        COALESCE(
            substring(array_to_string(c.reloptions, ',')
                      FROM 'autovacuum_vacuum_threshold=([0-9]+)')::numeric,
            (SELECT setting::numeric FROM pg_settings
              WHERE name = 'autovacuum_vacuum_threshold')
        )
        +
        COALESCE(
            substring(array_to_string(c.reloptions, ',')
                      FROM 'autovacuum_vacuum_scale_factor=([0-9.]+)')::numeric,
            (SELECT setting::numeric FROM pg_settings
              WHERE name = 'autovacuum_vacuum_scale_factor')
        ) * c.reltuples::numeric
    )                                                    AS dead_tuples_needed_to_trigger,
    t.n_dead_tup - ceil(
        COALESCE(
            substring(array_to_string(c.reloptions, ',')
                      FROM 'autovacuum_vacuum_threshold=([0-9]+)')::numeric,
            (SELECT setting::numeric FROM pg_settings
              WHERE name = 'autovacuum_vacuum_threshold')
        )
        +
        COALESCE(
            substring(array_to_string(c.reloptions, ',')
                      FROM 'autovacuum_vacuum_scale_factor=([0-9.]+)')::numeric,
            (SELECT setting::numeric FROM pg_settings
              WHERE name = 'autovacuum_vacuum_scale_factor')
        ) * c.reltuples::numeric
    )                                                    AS dead_over_threshold,
    CASE WHEN array_to_string(c.reloptions, ',') LIKE '%autovacuum_enabled=false%'
         THEN 'DISABLED' ELSE 'enabled' END              AS autovacuum_state,
    t.last_autovacuum,
    t.last_vacuum,
    pg_size_pretty(pg_relation_size(t.relid))             AS heap_size
FROM pg_stat_user_tables t
JOIN pg_class c ON c.oid = t.relid
WHERE c.reltuples > 0
ORDER BY dead_over_threshold DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q7.3  Vacuum / analyze staleness - the simple version
-- ---------------------------------------------------------------------
-- Nothing here needs a formula: how long since anything looked at this
-- table, and how much has changed since.
-- Thresholds worth acting on:
--   * last_autovacuum is NULL and n_dead_tup > 100000 -> never vacuumed.
--   * last_autoanalyze older than a few hours on a table taking thousands
--     of writes -> the planner is flying blind. Run ANALYZE now.
--   * n_mod_since_analyze > 10% of n_live_tup -> same conclusion.
SELECT
    schemaname,
    relname                                              AS table_name,
    n_live_tup,
    n_dead_tup,
    n_mod_since_analyze,
    round(100.0 * n_mod_since_analyze
          / NULLIF(n_live_tup, 0), 1)                     AS pct_modified_since_analyze,
    last_vacuum,
    last_autovacuum,
    now() - last_autovacuum                              AS since_autovacuum,
    last_analyze,
    last_autoanalyze,
    now() - last_autoanalyze                             AS since_autoanalyze,
    vacuum_count,
    autovacuum_count,
    analyze_count,
    autoanalyze_count
FROM pg_stat_user_tables
WHERE n_live_tup > 10000
   OR n_dead_tup > 10000
   OR n_mod_since_analyze > 10000
ORDER BY COALESCE(now() - last_autovacuum, interval '1000 years') DESC,
         n_dead_tup DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q7.4  Tables never vacuumed or never analyzed at all
-- ---------------------------------------------------------------------
-- A table with rows and no autovacuum/autoanalyze timestamp has either
-- just been created or is genuinely being skipped. Cross-check
-- `autovacuum_state` from Q7.2 before assuming a bug.
SELECT
    schemaname,
    relname                                AS table_name,
    n_live_tup,
    n_dead_tup,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze,
    pg_size_pretty(pg_relation_size(relid)) AS heap_size
FROM pg_stat_user_tables
WHERE (last_vacuum IS NULL AND last_autovacuum IS NULL AND n_live_tup > 1000)
   OR (last_analyze IS NULL AND last_autoanalyze IS NULL AND n_live_tup > 1000)
ORDER BY n_live_tup DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q7.5  Transaction ID wraparound proximity - THE ONE THAT KILLS SYSTEMS
-- ---------------------------------------------------------------------
-- PostgreSQL transaction IDs are 32-bit. There are ~2.1 billion of them
-- available at any time. When a table's relfrozenxid falls more than 2^31
-- transactions behind, the server REFUSES NEW WRITES with:
--     "database is not accepting commands to avoid wraparound data loss"
--
-- autovacuum_freeze_max_age (default 200,000,000) is when an aggressive
-- anti-wraparound vacuum is forced, whatever your per-table settings say.
-- vacuum_failsafe_age (14+, default 1.6 billion) makes vacuum skip its
-- cost throttling entirely and run as fast as it can, because running out
-- of transaction IDs is fatal.
--
-- pct_to_forced_freeze above 50% deserves attention; above 75% is an
-- incident; above 90% is a page-someone-now situation.
--
-- ACTION WHEN YOU SEE THIS: `VACUUM (FREEZE, VERBOSE) <table>;` or, for a
-- whole database, `VACUUM (FREEZE);`. This is expensive and I/O heavy.
-- The better fix is to find out WHY autovacuum cannot freeze: an old
-- backend_xmin (file 05 Q5.7), a stuck replication slot, or autovacuum
-- workers being throttled into uselessness (Q7.6/Q7.7).
--
-- ALSO CHECK pg_database.datfrozenxid - the database-level horizon is
-- what actually triggers the shutdown message, and it is the oldest table
-- in the database that sets it.
SELECT
    (SELECT setting::numeric FROM pg_settings
      WHERE name = 'autovacuum_freeze_max_age')          AS autovacuum_freeze_max_age,
    (SELECT setting::numeric FROM pg_settings
      WHERE name = 'autovacuum_multixact_freeze_max_age') AS autovacuum_mxid_freeze_max_age,
    d.datname,
    d.datfrozenxid,
    age(d.datfrozenxid)                                  AS database_xid_age,
    round(100.0 * age(d.datfrozenxid)
          / NULLIF((SELECT setting::numeric FROM pg_settings
                     WHERE name = 'autovacuum_freeze_max_age'), 0), 1) AS database_pct_to_forced_freeze,
    age(d.datminmxid)                                    AS database_mxid_age,
    pg_size_pretty(pg_database_size(d.datname))          AS database_size
FROM pg_database d
WHERE d.datallowconn
ORDER BY age(d.datfrozenxid) DESC;

-- The worst tables, which are what drive the database number above.
SELECT
    n.nspname                                            AS schema,
    c.relname                                            AS table_name,
    c.relfrozenxid,
    age(c.relfrozenxid)                                  AS xid_age,
    round(100.0 * age(c.relfrozenxid)
          / NULLIF((SELECT setting::numeric FROM pg_settings
                     WHERE name = 'autovacuum_freeze_max_age'), 0), 1) AS pct_to_forced_freeze,
    c.relminmxid,
    age(c.relminmxid)                                    AS mxid_age,
    round(100.0 * age(c.relminmxid)
          / NULLIF((SELECT setting::numeric FROM pg_settings
                     WHERE name = 'autovacuum_multixact_freeze_max_age'), 0), 1) AS mxid_pct_to_forced_freeze,
    pg_size_pretty(pg_relation_size(c.oid))              AS heap_size,
    t.n_dead_tup,
    t.last_autovacuum
FROM pg_class c
JOIN pg_namespace n        ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables t ON t.relid = c.oid
WHERE c.relkind IN ('r', 'm', 't')            -- tables, matviews, TOAST
  AND n.nspname NOT IN ('information_schema')
  AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY age(c.relfrozenxid) DESC
LIMIT 30;

-- Multixact IDs wrap too, and they have their own much smaller budget
-- (autovacuum_multixact_freeze_max_age defaults to 400,000,000). A table
-- low on xid_age but high on mxid_age is being hit by something that
-- takes FOR KEY SHARE / FOR SHARE row locks - typically a foreign key
-- check or SELECT ... FOR UPDATE on a hot row. Nothing in this pack can
-- fix that; the fix is usually to stop holding row locks on hot rows.


-- ---------------------------------------------------------------------
-- Q7.6  What autovacuum is doing right now (and how fast)
-- ---------------------------------------------------------------------
-- Two views. pg_stat_activity tells you which workers exist and on which
-- tables; pg_stat_progress_vacuum (9.6+) gives phase-by-phase progress for
-- a running VACUUM, including whether it is in the index-vacuuming phases
-- that usually dominate the runtime on a table with many indexes.
-- (`backend_type` is 10+; on 9.6 the ILIKE terms carry the query alone.)
SELECT
    a.pid,
    a.datname,
    now() - a.xact_start                          AS running_for,
    now() - a.query_start                         AS current_phase_for,
    a.wait_event_type,
    a.wait_event,
    left(regexp_replace(a.query, '\s+', ' ', 'g'), 120) AS query
FROM pg_stat_activity a
WHERE a.backend_type = 'autovacuum worker'
   OR a.query ILIKE 'autovacuum:%'
   OR a.query ILIKE 'VACUUM%'
ORDER BY a.xact_start ASC;

-- Phase-level detail for the ones currently running.
--
-- The dead-tuple-store columns differ by version (see VERSION NOTES at the top
-- of this file), so they are read out of the row's JSON form instead of being
-- named directly: naming either branch's columns makes this statement fail to
-- PARSE on the other branch, not merely return nothing.
SELECT
    p.pid,
    p.datname,
    p.relid::regclass                                   AS table_name,
    p.phase,
    p.heap_blks_total,
    p.heap_blks_scanned,
    round(100.0 * p.heap_blks_scanned
          / NULLIF(p.heap_blks_total, 0), 1)             AS pct_scanned,
    p.heap_blks_vacuumed,
    round(100.0 * p.heap_blks_vacuumed
          / NULLIF(p.heap_blks_total, 0), 1)             AS pct_vacuumed,
    p.index_vacuum_count,
    -- Which unit the two columns below are in: 'tuples' on 16 and older,
    -- 'bytes' on 17 and newer.
    CASE WHEN rowjson ? 'max_dead_tuples' THEN 'tuples'
         ELSE 'bytes' END                               AS dead_store_units,
    -- How much dead-tuple data the maintenance_work_mem store can hold.
    -- 16-: max_dead_tuples (a tuple count). 17+: max_dead_tuple_bytes.
    COALESCE((rowjson ->> 'max_dead_tuples')::bigint,
             (rowjson ->> 'max_dead_tuple_bytes')::bigint)
                                                        AS dead_store_capacity,
    -- How much of that store is in use, in the SAME unit as the capacity
    -- above. 16-: num_dead_tuples (a tuple count). 17+: dead_tuple_bytes -
    -- deliberately NOT num_dead_item_ids, which counts item identifiers and
    -- is therefore not comparable with a byte capacity.
    COALESCE((rowjson ->> 'num_dead_tuples')::bigint,
             (rowjson ->> 'dead_tuple_bytes')::bigint)  AS dead_store_used,
    -- The ratio that matters: both branches compare like with like, so this
    -- is meaningful on 16- and on 17+ even though the units differ.
    round(100.0 * COALESCE((rowjson ->> 'num_dead_tuples')::numeric,
                           (rowjson ->> 'dead_tuple_bytes')::numeric)
          / NULLIF(COALESCE((rowjson ->> 'max_dead_tuples')::numeric,
                            (rowjson ->> 'max_dead_tuple_bytes')::numeric), 0), 1)
                                                        AS pct_dead_store_full,
    (SELECT count(*) FROM pg_index i WHERE i.indrelid = p.relid) AS table_index_count
FROM pg_stat_progress_vacuum p
CROSS JOIN LATERAL (SELECT to_jsonb(p) AS rowjson) AS rowjson_source
ORDER BY p.heap_blks_total DESC;

-- Reading this:
--   * Stuck in 'vacuuming indexes' with index_vacuum_count climbing: the
--     table has many indexes and each one is being swept. On a table with
--     10+ indexes this is where most of the wall time goes. Dropping
--     redundant indexes (file 04 Q4.2) makes every future vacuum faster -
--     that is a vacuum fix disguised as an index fix.
--   * Phase flickering between 'scanning heap' and 'vacuuming heap' with
--     pct_scanned not advancing: the worker is being throttled by
--     autovacuum_vacuum_cost_delay. See Q7.7.
--   * pct_dead_store_full near 100: the maintenance_work_mem dead-tuple
--     store filled up, so vacuum had to stop and run an index vacuum cycle,
--     and on a large table it then restarts its heap pass. Raise
--     maintenance_work_mem for the autovacuum workers, or drop the indexes
--     that no query uses so each cycle is cheaper.


-- ---------------------------------------------------------------------
-- Q7.7  Is autovacuum being throttled into uselessness?
-- ---------------------------------------------------------------------
-- Autovacuum workers share a single cost budget (autovacuum_vacuum_cost_limit,
-- default 200) divided among however many are running, and sleep
-- autovacuum_vacuum_cost_delay (default 2 ms) each time they exceed it.
-- On modern NVMe and SSD storage this default is absurdly conservative: it
-- was chosen for spinning disks.
--
-- The ramp that is safe on almost any storage, applied one step at a time
-- and measured:
--
--   ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 2000;
--   ALTER SYSTEM SET autovacuum_vacuum_cost_delay = 1;      -- ms
--   ALTER SYSTEM SET autovacuum_max_workers = 5;
--   SELECT pg_reload_conf();     -- all three are reloadable, no restart
--
-- Then watch Q6.6 (maxwritten_clean) and your disk latency for ten
-- minutes. If latency is flat, raise cost_limit again. Do not jump
-- straight to 10000 on a shared or network-attached volume.
--
-- Per-table override for the two or three biggest, hottest tables, which
-- is usually better than a global change:
--
--   SET lock_timeout = '3s';
--   ALTER TABLE public.orders SET (
--       autovacuum_vacuum_scale_factor = 0.02,   -- fire at 2% dead, not 20%
--       autovacuum_vacuum_cost_limit   = 1000,   -- per-table, does not
--                                                -- consume the shared budget
--       autovacuum_analyze_scale_factor = 0.01
--   );
--
-- Note that a per-table autovacuum_vacuum_cost_limit makes that table's
-- workers use their own budget instead of the shared one, so a couple of
-- very large tables can be tuned aggressively without changing the
-- behaviour of the other few thousand small tables. That is the single
-- most useful autovacuum trick in this file.
-- `pg_stat_user_tables` is already scoped to the database you are connected
-- to, and has never had a `datname` column on any PostgreSQL release - naming
-- one here fails with 42703 "column datname does not exist" on every version,
-- not only on 17. Identify the row by schema and table instead.
SELECT
    schemaname,
    relname,
    n_dead_tup,
    n_live_tup,
    seq_scan,
    idx_scan,
    autovacuum_count,
    vacuum_count,
    last_autovacuum,
    CASE
        WHEN last_autovacuum IS NULL AND n_dead_tup > 0
            THEN 'never autovacuumed with dead tuples present - check Q7.2 autovacuum_state, then file 05 Q5.7'
        WHEN autovacuum_count = 0 AND n_dead_tup > 100000
            THEN 'has dead tuples but has never been autovacuumed - likely blocked, not misconfigured'
        WHEN last_autovacuum < now() - interval '1 day' AND n_dead_tup > 100000
            THEN 'last autovacuum over a day ago with 100k+ dead tuples - check worker throttling and horizon'
        ELSE 'ok'
    END                                                   AS verdict
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000 OR autovacuum_count > 0
ORDER BY n_dead_tup DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q7.8  Tables with autovacuum explicitly disabled
-- ---------------------------------------------------------------------
-- Someone disabled it, usually because a vacuum caused a latency spike.
-- That spike was almost always caused by an unbounded vacuum on a huge
-- table, and the correct fix is a cost limit and a lower scale factor,
-- not an off switch. Every table in this list needs a manual VACUUM
-- schedule or a very good reason.
SELECT
    n.nspname                                   AS schema,
    c.relname                                   AS table_name,
    c.reloptions,
    t.n_live_tup,
    t.n_dead_tup,
    t.last_vacuum,
    t.last_autovacuum,
    pg_size_pretty(pg_relation_size(c.oid))     AS heap_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_stat_user_tables t ON t.relid = c.oid
WHERE array_to_string(c.reloptions, ',') LIKE '%autovacuum_enabled=false%'
ORDER BY t.n_dead_tup DESC;

-- To re-enable without a full rewrite:
--   SET lock_timeout = '3s';
--   ALTER TABLE public.big_table RESET (autovacuum_enabled);
-- It takes ACCESS EXCLUSIVE for a moment. Run it in a low-traffic window
-- and keep the lock_timeout so it fails fast rather than queueing and
-- blocking everything behind it.
