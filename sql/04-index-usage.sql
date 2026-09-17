-- =====================================================================
-- 04 - INDEX USAGE, SIZE, REDUNDANCY AND INDEX-ONLY-SCAN READINESS
-- =====================================================================
--
-- WHAT THIS SHOWS
--   Per index: how often it is scanned, how many heap rows those scans
--   pulled, how big it is, and whether it duplicates another index on the
--   same table. Plus the visibility-map coverage that decides whether an
--   index-only scan is even possible.
--
-- WHAT TO LOOK FOR
--   * Q4.1 - the full inventory. Read idx_scan against index size. A 4 GB
--     index with 3 scans is a candidate for deletion, not tuning.
--   * Q4.2 - duplicate and redundant indexes. This is the query most
--     people do not have. It finds indexes whose key columns are an exact
--     match, and indexes whose key columns are a strict PREFIX of another
--     index on the same table - the prefix case is the one that hides in
--     plain sight because the two indexes have different names and often
--     different column counts.
--   * Q4.3 - visibility map coverage. An index-only scan is only cheap
--     when the pages it needs are marked all-visible. If
--     relallvisible / relpages is low, your index-only scans are quietly
--     doing a heap fetch per row and you are paying for an index that
--     behaves like a plain index.
--   * Q4.4 - how many heap fetches each index scan performs. Low is good
--     (index-only behaviour); close to or above 1.0 means every scan
--     touches the heap.
--   * Q4.5 - write-heavy tables where an extra index is a net loss.
--
-- THE CAVEAT ABOUT COUNTERS (same as file 02, repeated because it matters)
--   pg_stat_user_indexes.idx_scan resets when statistics are reset, is
--   lost on crash recovery, and is ALWAYS ZERO ON A STANDBY. Never drop
--   an index based on a replica's counters, and never drop one inside a
--   business cycle shorter than your slowest-report cadence.
--
-- RISK OF RUNNING THIS
--   READ-ONLY. Q4.1-Q4.5 read pg_stat_* views, pg_index and pg_class.
--   The only heavy thing here is that pg_get_indexdef() is called once
--   per key column - on a database with tens of thousands of indexes this
--   is a visible amount of catalog work but nothing that blocks anyone.
--
-- VERSION NOTES
--   * `indnkeyatts` (key columns, excluding INCLUDE columns) is
--     PostgreSQL 11+. On 10 and older, replace `i.indnkeyatts` with
--     `i.indnatts` - INCLUDE did not exist, so the two are equal.
--   * `relallvisible` is available on all supported versions.
--   * REINDEX CONCURRENTLY is PostgreSQL 12+. On 11 and older, REINDEX
--     takes an exclusive lock and is NOT online - see INDEXING-PLAYBOOK.md.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q4.1  Complete index inventory, biggest first
-- ---------------------------------------------------------------------
-- Excludes the index for a primary key only in label, not in the listing,
-- because a primary key index is still worth sizing.
SELECT
    s.schemaname,
    s.relname                                             AS table_name,
    s.indexrelname                                        AS index_name,
    s.idx_scan,
    s.idx_tup_read,
    s.idx_tup_fetch,
    pg_size_pretty(pg_relation_size(s.indexrelid))         AS index_size,
    pg_relation_size(s.indexrelid)                         AS index_bytes,
    i.indisunique                                          AS is_unique,
    i.indisprimary                                         AS is_primary,
    i.indisvalid                                           AS is_valid,
    i.indnkeyatts                                          AS key_cols,
    i.indnatts                                             AS total_cols,  -- > key_cols means INCLUDE
    pg_get_expr(i.indpred, i.indrelid) IS NOT NULL         AS is_partial,
    pg_get_expr(i.indexprs, i.indrelid) IS NOT NULL        AS is_expression,
    pg_get_indexdef(s.indexrelid)                          AS definition
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE i.indisvalid
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 50;

-- Invalid indexes deserve their own line. An invalid index is ignored by
-- the planner but still maintained on every write: pure overhead. This
-- happens when CREATE INDEX CONCURRENTLY fails part-way, or a REINDEX
-- CONCURRENTLY is interrupted. Fix: DROP INDEX CONCURRENTLY then recreate.
SELECT
    n.nspname                                              AS schema,
    c.relname                                              AS index_name,
    t.relname                                              AS table_name,
    pg_size_pretty(pg_relation_size(c.oid))                AS index_size,
    i.indisready,
    i.indislive,
    pg_get_indexdef(c.oid)                                 AS definition
FROM pg_index i
JOIN pg_class c     ON c.oid = i.indexrelid
JOIN pg_class t     ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE NOT i.indisvalid
   OR NOT i.indisready
   OR NOT i.indislive;


-- ---------------------------------------------------------------------
-- Q4.2  DUPLICATE AND REDUNDANT INDEXES
-- ---------------------------------------------------------------------
-- HOW THIS WORKS
--   For each index we build an ordered text array of its key-column
--   definitions using pg_get_indexdef(index_oid, column_no, pretty),
--   which returns the column name plus any opclass, DESC or NULLS
--   ordering. Two indexes are duplicates when those arrays are equal and
--   their predicates are equal.
--
--   An index B is REDUNDANT when its key columns are an exact PREFIX of
--   index A's key columns on the same table. A btree on (a) is fully
--   served by a btree on (a, b): anything that can use (a) can use the
--   leading column of (a, b). Keeping both doubles the write cost of the
--   first column for no read benefit.
--
--   Three deliberate exclusions, because "redundant" would be wrong:
--     * unique and primary-key indexes are never reported as the
--       droppable side - they enforce a constraint, so dropping them
--       changes behaviour, not just performance.
--     * indexes with different predicates are never compared. A partial
--       index on (status) WHERE status = 'pending' is not a duplicate of
--       a full index on (status); it is a smaller, faster specialist.
--     * expression indexes are compared by their full expression text, so
--       an index on (lower(email)) never looks like a duplicate of one on
--       (email).
--
-- WHAT TO DO WITH A HIT
--   1. Copy pg_get_indexdef() of the index you plan to drop. Save it.
--   2. Confirm idx_scan of the droppable index is low, or that the
--      covering index already serves the same queries.
--   3. DROP INDEX CONCURRENTLY <name>;   -- cannot run inside a transaction
--   4. Watch for regression for one full business cycle.
WITH idx AS (
    SELECT
        i.indexrelid,
        i.indrelid,
        tn.nspname                                    AS schema_name,
        t.relname                                     AS table_name,
        ic.relname                                    AS index_name,
        i.indisunique,
        i.indisprimary,
        i.indnkeyatts,
        pg_relation_size(i.indexrelid)                AS index_bytes,
        pg_get_expr(i.indpred, i.indrelid)            AS predicate,
        pg_get_expr(i.indexprs, i.indrelid)           AS expression,
        -- ordered array of key-column definitions, e.g.
        -- {tenant_id, created_at DESC}
        (SELECT array_agg(pg_get_indexdef(i.indexrelid, g, true) ORDER BY g)
           FROM generate_series(1, i.indnkeyatts) AS g) AS key_cols
    FROM pg_index i
    JOIN pg_class ic     ON ic.oid = i.indexrelid
    JOIN pg_class t      ON t.oid = i.indrelid
    JOIN pg_namespace tn ON tn.oid = t.relnamespace
    WHERE tn.nspname NOT IN ('pg_catalog', 'information_schema')
      AND t.relkind IN ('r', 'm')
      AND i.indisvalid
)
SELECT
    a.table_name,
    a.index_name                                            AS keep_this_index,
    array_to_string(a.key_cols, ', ')                       AS keep_columns,
    pg_size_pretty(a.index_bytes)                           AS keep_size,
    b.index_name                                            AS droppable_index,
    array_to_string(b.key_cols, ', ')                       AS droppable_columns,
    pg_size_pretty(b.index_bytes)                           AS droppable_size,
    CASE
        WHEN a.key_cols = b.key_cols THEN 'EXACT DUPLICATE'
        ELSE 'REDUNDANT PREFIX'
    END                                                     AS reason,
    b.indisunique                                           AS droppable_is_unique,
    COALESCE(b.predicate, '(none)')                         AS droppable_predicate,
    pg_get_indexdef(b.indexrelid)                           AS droppable_definition
FROM idx a
JOIN idx b
  ON  a.indrelid  = b.indrelid
  AND a.indexrelid <> b.indexrelid
  -- only compare indexes of the same kind: both partial, or both full
  AND (a.predicate IS NOT DISTINCT FROM b.predicate)
  -- b's key columns are a prefix of a's key columns
  AND a.key_cols[1:array_length(b.key_cols, 1)] = b.key_cols
  AND array_length(b.key_cols, 1) <= array_length(a.key_cols, 1)
  -- never propose dropping a unique / primary-key index
  AND NOT b.indisunique
  AND NOT b.indisprimary
  -- do not report two mutually-covering indexes twice
  AND (array_length(a.key_cols, 1), a.indexrelid)
      > (array_length(b.key_cols, 1), b.indexrelid)
ORDER BY b.index_bytes DESC, a.table_name, a.index_name;


-- ---------------------------------------------------------------------
-- Q4.3  Visibility map coverage - is an index-only scan actually possible?
-- ---------------------------------------------------------------------
-- An index-only scan avoids the heap entirely ONLY for pages marked
-- all-visible in the visibility map. VACUUM sets those bits. So on a
-- read-mostly table that is rarely vacuumed, an index-only scan silently
-- degrades into "index scan plus one heap fetch per row".
--
-- vm_coverage_pct above ~95% is healthy. Below ~80% on a large table,
-- the fix is a VACUUM (not a REINDEX): VACUUM is what sets the bits.
-- Note that VACUUM FREEZE / autovacuum's freeze pass also sets them, and
-- that freshly written pages are never all-visible until a vacuum runs.
SELECT
    n.nspname                                   AS schema,
    c.relname                                   AS table_name,
    c.relpages                                  AS heap_pages,
    c.relallvisible                             AS all_visible_pages,
    round(100.0 * c.relallvisible / NULLIF(c.relpages, 0), 1) AS vm_coverage_pct,
    pg_size_pretty(pg_relation_size(c.oid))     AS heap_size,
    (SELECT count(*) FROM pg_index i
      WHERE i.indrelid = c.oid AND i.indisvalid) AS index_count,
    t.last_vacuum,
    t.last_autovacuum,
    t.n_dead_tup
FROM pg_class c
JOIN pg_namespace n       ON n.oid = c.relnamespace
JOIN pg_stat_user_tables t ON t.relid = c.oid
WHERE c.relkind = 'r'
  AND c.relpages > 1000
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 100.0 * c.relallvisible / NULLIF(c.relpages, 0) ASC NULLS FIRST,
         c.relpages DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q4.4  Heap fetches per index scan
-- ---------------------------------------------------------------------
-- tup_fetch_per_read = idx_tup_fetch / idx_tup_read.
--   near 0    the index is answering queries on its own. Either it is a
--             covering index used in index-only scans, or the queries
--             only ever touch dead/uncommitted tuples. Confirm which by
--             looking at Q4.3 for the same table.
--   near 1.0  every tuple the index returned required a heap fetch. If
--             the query only selects columns that are already in the
--             index, adding those columns as INCLUDE would turn this
--             into an index-only scan.
--   above 1.0 many heap tuples were fetched per index entry, which
--             happens when the index is non-unique and the scan follows a
--             long chain of duplicates.
--
-- NOTE ON THE RATIO ITSELF: on releases where index-only scans skip the
-- heap fetch entirely, idx_tup_fetch is not incremented for those rows,
-- which is exactly what makes this ratio diagnostic. Treat it as a
-- heuristic and confirm with EXPLAIN (ANALYZE, BUFFERS) before acting.
SELECT
    s.schemaname,
    s.relname                                       AS table_name,
    s.indexrelname                                  AS index_name,
    s.idx_scan,
    s.idx_tup_read,
    s.idx_tup_fetch,
    round(s.idx_tup_fetch::numeric / NULLIF(s.idx_tup_read, 0), 3) AS tup_fetch_per_read,
    round(s.idx_tup_read::numeric / GREATEST(s.idx_scan, 1), 1)    AS tup_read_per_scan,
    pg_size_pretty(pg_relation_size(s.indexrelid))  AS index_size,
    i.indnkeyatts                                   AS key_cols,
    i.indnatts - i.indnkeyatts                      AS include_cols,
    pg_get_indexdef(s.indexrelid)                   AS definition
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan > 100
  AND s.idx_tup_read > 0
  AND i.indisvalid
ORDER BY s.idx_tup_fetch::numeric / NULLIF(s.idx_tup_read, 0) DESC NULLS LAST,
         s.idx_tup_read DESC
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q4.5  Write-heavy tables where indexes are a net cost
-- ---------------------------------------------------------------------
-- For each non-constraint index: how many writes it has to absorb, and
-- how many scans it delivered. A index on a table with 10M updates and 5
-- scans has almost certainly cost more than it earned - but "almost
-- certainly" is why you confirm usage in pg_stat_statements first.
SELECT
    s.schemaname,
    s.relname                                        AS table_name,
    s.indexrelname                                   AS index_name,
    s.idx_scan,
    t.n_tup_ins,
    t.n_tup_upd,
    t.n_tup_del,
    t.n_tup_hot_upd,
    (t.n_tup_ins + t.n_tup_upd + t.n_tup_del)        AS total_writes,
    round((t.n_tup_ins + t.n_tup_upd + t.n_tup_del)::numeric
          / GREATEST(s.idx_scan, 1), 0)              AS writes_per_scan,
    pg_size_pretty(pg_relation_size(s.indexrelid))   AS index_size,
    pg_get_indexdef(s.indexrelid)                    AS definition
FROM pg_stat_user_indexes s
JOIN pg_stat_user_tables t ON t.relid = s.relid
JOIN pg_index i            ON i.indexrelid = s.indexrelid
WHERE i.indisvalid
  AND NOT i.indisprimary
  AND NOT i.indisunique
  AND t.n_tup_ins + t.n_tup_upd + t.n_tup_del > 50000
  AND (t.n_tup_ins + t.n_tup_upd + t.n_tup_del)::numeric
      / GREATEST(s.idx_scan, 1) > 1000
ORDER BY (t.n_tup_ins + t.n_tup_upd + t.n_tup_del)::numeric
         / GREATEST(s.idx_scan, 1) DESC
LIMIT 30;


-- ---------------------------------------------------------------------
-- Q4.6  OPTIONAL - real index bloat with the pgstattuple extension
-- ---------------------------------------------------------------------
-- Requires: CREATE EXTENSION IF NOT EXISTS pgstattuple;
-- avg_leaf_density below ~60 on a btree means half the leaf pages are
-- empty space. The fix is REINDEX (CONCURRENTLY on 12+), not a new index.
--
--   SELECT s.schemaname, s.relname AS table_name,
--          s.indexrelname AS index_name,
--          p.index_size, p.leaf_pages, p.empty_pages, p.deleted_pages,
--          p.avg_leaf_density, p.leaf_fragmentation
--   FROM pg_stat_user_indexes s
--   CROSS JOIN LATERAL pgstatindex(s.indexrelid) p
--   WHERE p.avg_leaf_density < 60
--     AND p.index_size > 10 * 1024 * 1024
--   ORDER BY p.index_size DESC;
