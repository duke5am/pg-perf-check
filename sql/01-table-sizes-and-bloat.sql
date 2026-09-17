-- =====================================================================
-- 01 - TABLE AND INDEX SIZES, DEAD TUPLES, BLOAT ESTIMATE
-- =====================================================================
--
-- WHAT THIS SHOWS
--   How much disk each table actually occupies, how much of it is dead
--   space, and a modelled estimate of how many pages the live rows
--   should need. That gap is "bloat" - pages the heap is holding but no
--   longer using for anything except slowing down every sequential scan.
--
-- WHAT TO LOOK FOR
--   * Q1.1 - which relations dominate the disk. Do this first; it tells
--     you where tuning effort can possibly pay off. An index larger than
--     its table is a red flag.
--   * Q1.2 - dead_pct. Above ~20% means autovacuum is not keeping up,
--     usually because of long-running transactions, a too-low
--     autovacuum_vacuum_cost_limit, or per-table scale factors that do
--     not fit the table's size. hot_update_pct below ~80% on an
--     update-heavy table usually means fillfactor 100 is hurting you.
--   * Q1.3 - modelled bloat. Treat it as an estimate with a wide error
--     bar, not a measurement. Use it to rank tables, not to quote
--     numbers.
--   * Q1.4/1.5 - exact measurement via the pgstattuple extension
--     (OPTIONAL, commented out - requires CREATE EXTENSION).
--
-- RISK OF RUNNING THIS
--   LOW / READ-ONLY. Q1.1, Q1.2 and Q1.5 read catalogs and statistics
--   views only - they take no locks and are safe on a busy primary.
--   Q1.3 reads pg_stats and pg_class; on a database with tens of
--   thousands of relations the pg_stats scan is measurable but not
--   dangerous. Q1.4 (pgstattuple) does a FULL PHYSICAL SCAN of each
--   table you point it at - do not run it on a large table during peak
--   hours; it reads every page and blocks nothing, but it adds real I/O.
--
-- VERSION NOTES
--   Works on PostgreSQL 9.6 - 17. Where a column only exists in a newer
--   release it is labelled inline and commented out.
--   pg_class.reltuples is -1 (meaning "never analysed / unknown") on
--   PostgreSQL 14+; queries below guard against that where it matters.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q1.1  Largest relations: heap + indexes + TOAST, in one row each
-- ---------------------------------------------------------------------
-- total_size  = heap + all indexes + TOAST table + TOAST index
-- main_size   = the heap file only
-- index_size  = all indexes attached to this relation
-- If index_size is close to or larger than main_size, you are paying
-- for indexes that a sequential scan would beat anyway. Go to file 04.
SELECT
    n.nspname                                     AS schema,
    c.relname                                     AS relation,
    c.relkind                                     AS kind,   -- r=table m=matview p=partitioned
    pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
    pg_size_pretty(pg_relation_size(c.oid))       AS heap_size,
    pg_size_pretty(pg_indexes_size(c.oid))        AS index_size,
    pg_total_relation_size(c.oid)                 AS total_bytes,
    c.reltuples::bigint                           AS est_rows,
    c.relpages                                    AS heap_pages
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp%'
  AND pg_total_relation_size(c.oid) > 0
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q1.2  Dead tuples and update pattern per table
-- ---------------------------------------------------------------------
-- dead_pct     : dead tuples as a share of all tuples. >20% = vacuum debt.
-- hot_update_pct: share of updates that avoided touching every index.
--                LOW (<80%) on an update-heavy table means either the
--                updated column is indexed, or there is no free space on
--                the page for HOT (fillfactor 100). Fixing this is often
--                worth more than any index change.
-- n_mod_since_analyze: rows changed since the planner last got fresh
--                statistics. Very high relative to n_live_tup = the
--                planner is choosing plans from stale numbers.
SELECT
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    CASE WHEN n_live_tup + n_dead_tup > 0
         THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1)
    END                                                     AS dead_pct,
    n_tup_ins,
    n_tup_upd,
    n_tup_del,
    n_tup_hot_upd,
    CASE WHEN n_tup_upd > 0
         THEN round(100.0 * n_tup_hot_upd / n_tup_upd, 1)
    END                                                     AS hot_update_pct,
    n_mod_since_analyze,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze,
    pg_size_pretty(pg_total_relation_size(relid))           AS total_size
FROM pg_stat_user_tables
WHERE n_dead_tup > 0
ORDER BY n_dead_tup DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q1.3  Modelled heap bloat
-- ---------------------------------------------------------------------
-- METHOD (so you can argue with it):
--   1. For every column with statistics, pg_stats.avg_width is the mean
--      stored width of NON-NULL values. Multiply by (1 - null_frac) to
--      get the expected bytes contributed per row.
--   2. Add 23 bytes of HeapTupleHeaderData + 4 bytes of ItemIdData per
--      tuple, plus a null bitmap of ceil(natts/8) bytes when the table
--      has any nullable column.
--   3. Usable bytes per page = (block_size - 24 byte PageHeaderData)
--      scaled by the table's fillfactor.
--   4. expected_pages = reltuples * bytes_per_tuple / usable_bytes.
--   5. bloat = (actual relpages - expected_pages) * block_size.
--
-- WHY THIS OVER-REPORTS BLOAT (read this before you act on the numbers):
--   * Column alignment padding is not modelled. A text column followed
--     by a bigint costs ~4-7 bytes of padding per row that this method
--     does not count, so bytes_per_tuple is slightly too small and the
--     table looks emptier than it is.
--   * Line-pointer fragmentation, page-level free space below the
--     fillfactor floor, and TOAST are not modelled.
--   * It is entirely dependent on ANALYZE being recent. If
--     n_mod_since_analyze is large (see Q1.2) these numbers are fiction.
-- Conclusion: use this to RANK tables, then confirm the top one or two
-- with pgstattuple_approx (Q1.4, exact-ish) before you rebuild anything.
WITH constants AS (
    SELECT
        current_setting('block_size')::numeric AS block_size,
        24::numeric                            AS page_header,   -- PageHeaderData
        23::numeric                            AS tuple_header,  -- HeapTupleHeaderData
        4::numeric                             AS item_pointer   -- ItemIdData
),
relations AS (
    SELECT
        c.oid       AS relid,
        n.nspname   AS schemaname,
        c.relname   AS tablename,
        c.reltuples::numeric AS reltuples,
        c.relpages::numeric  AS relpages,
        COALESCE(
            substring(array_to_string(c.reloptions, ',')
                      FROM 'fillfactor=([0-9]+)')::numeric,
            100
        ) AS fillfactor
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'                       -- ordinary tables only
      AND c.relpages > 0
      AND c.reltuples > 0
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
      AND n.nspname NOT LIKE 'pg_temp%'
),
column_stats AS (
    SELECT
        s.schemaname,
        s.tablename,
        sum(s.avg_width::numeric * (1 - COALESCE(s.null_frac::numeric, 0))) AS payload_bytes,
        count(*)::numeric                                                   AS n_attrs,
        count(*) FILTER (WHERE COALESCE(s.null_frac, 0) > 0)::numeric        AS n_nullable
    FROM pg_stats s
    WHERE s.schemaname NOT IN ('pg_catalog', 'information_schema')
      AND s.inherited = false      -- pg_stats has a row per column PER
                                   -- inheritance level; without this you
                                   -- double-count partitioned children
    GROUP BY s.schemaname, s.tablename
),
modelled AS (
    SELECT
        r.schemaname,
        r.tablename,
        r.relid,
        r.reltuples,
        r.relpages,
        r.fillfactor,
        c.payload_bytes,
        -- bytes of one tuple as this model sees it
        c.payload_bytes
          + CASE WHEN c.n_nullable > 0 THEN ceil(c.n_attrs / 8.0) ELSE 0 END
          + k.tuple_header
          + k.item_pointer                                  AS tuple_bytes,
        -- usable bytes in one page
        (k.block_size - k.page_header) * (r.fillfactor / 100.0) AS usable_page_bytes
    FROM relations r
    JOIN column_stats c
      ON c.schemaname = r.schemaname AND c.tablename = r.tablename
    CROSS JOIN constants k
)
SELECT
    m.schemaname,
    m.tablename,
    pg_size_pretty(pg_total_relation_size(m.relid))                        AS total_size,
    m.relpages::bigint                                                     AS actual_pages,
    ceil(m.reltuples * m.tuple_bytes / NULLIF(m.usable_page_bytes, 0))::bigint AS expected_pages,
    pg_size_pretty(
        (greatest(m.relpages - ceil(m.reltuples * m.tuple_bytes
                                    / NULLIF(m.usable_page_bytes, 0)), 0)
         * (SELECT block_size FROM constants))::bigint
    )                                                                      AS modelled_bloat,
    round(100.0 * greatest(m.relpages - ceil(m.reltuples * m.tuple_bytes
                                             / NULLIF(m.usable_page_bytes, 0)), 0)
          / NULLIF(m.relpages, 0), 1)                                      AS bloat_pct,
    m.reltuples::bigint                                                    AS est_rows,
    round(m.tuple_bytes, 1)                                                AS modelled_bytes_per_row
FROM modelled m
WHERE m.relpages > 128          -- ignore small tables: noise dominates
ORDER BY greatest(m.relpages - ceil(m.reltuples * m.tuple_bytes
                                    / NULLIF(m.usable_page_bytes, 0)), 0) DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q1.4  OPTIONAL - exact measurement with the pgstattuple extension
-- ---------------------------------------------------------------------
-- Requires a superuser (or a role granted EXECUTE) and:
--     CREATE EXTENSION IF NOT EXISTS pgstattuple;
-- pgstattuple_approx() samples the visibility map and is far cheaper than
-- pgstattuple() on a large table. Substitute your own table names.
--
--   SELECT *
--   FROM pgstattuple_approx('public.orders');
--
--   -- for several tables at once:
--   SELECT t.schemaname, t.relname,
--          p.table_len, p.dead_tuple_len, p.dead_tuple_percent,
--          p.approx_free_space, p.approx_free_percent, p.scanned_percent
--   FROM pg_stat_user_tables t
--   CROSS JOIN LATERAL pgstattuple_approx(t.relid) p   -- needs regclass
--   WHERE t.n_live_tup > 100000
--   ORDER BY p.dead_tuple_len DESC
--   LIMIT 20;
--
-- NOTE: pgstattuple_approx takes regclass; t.relid is oid, which casts
-- implicitly to regclass, so the LATERAL form above works as written.
-- scanned_percent tells you how much of the table was actually read.
--
-- Index bloat, same extension:
--   SELECT c.relname AS index_name, i.index_size, i.leaf_pages,
--          i.empty_pages, i.deleted_pages, i.avg_leaf_density,
--          i.leaf_fragmentation
--   FROM pg_stat_user_indexes s
--   JOIN pg_class c ON c.oid = s.indexrelid
--   CROSS JOIN LATERAL pgstatindex(s.indexrelid) i
--   WHERE i.avg_leaf_density < 60     -- a healthy btree is usually 70-90
--   ORDER BY i.index_size DESC;


-- ---------------------------------------------------------------------
-- Q1.5  Index size anomalies - non-optional, pure catalog reads
-- ---------------------------------------------------------------------
-- An index bigger than the heap it indexes is almost always one of:
--   a) badly bloated (needs REINDEX),
--   b) an over-wide composite index that nothing uses (see file 04),
--   c) a duplicate of another index on the same table (see file 04).
SELECT
    n.nspname                                      AS schema,
    t.relname                                      AS table_name,
    i.relname                                      AS index_name,
    pg_size_pretty(pg_relation_size(i.oid))        AS index_size,
    pg_size_pretty(pg_relation_size(t.oid))        AS heap_size,
    round(100.0 * pg_relation_size(i.oid)
          / NULLIF(pg_relation_size(t.oid), 0), 1) AS pct_of_heap,
    ix.indisunique                                 AS is_unique,
    ix.indisprimary                                AS is_primary,
    pg_get_indexdef(i.oid)                         AS definition
FROM pg_index ix
JOIN pg_class i     ON i.oid = ix.indexrelid
JOIN pg_class t     ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND pg_relation_size(t.oid) > 0
  AND pg_relation_size(i.oid) > pg_relation_size(t.oid) * 0.8
ORDER BY pg_relation_size(i.oid) DESC
LIMIT 30;
