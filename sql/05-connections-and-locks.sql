-- =====================================================================
-- 05 - CONNECTIONS, TRANSACTIONS AND BLOCKING CHAINS
-- =====================================================================
--
-- WHAT THIS SHOWS
--   Who is connected, what they are doing, what they are waiting on, and
--   - most importantly - who is blocking whom and what the root of the
--   chain is. Q5.7 shows the other, quieter kind of blocker: an old
--   transaction or an abandoned replication slot holding back VACUUM
--   across the whole database without ever showing up as a lock wait.
--
-- WHAT TO LOOK FOR
--   * Q5.1 - anything in state 'idle in transaction' for more than a few
--     seconds. That is an application bug (a transaction left open across
--     a network call, a missing commit/rollback in an error path), not a
--     database problem. It holds locks and, worse, holds back the xmin
--     horizon so VACUUM cannot remove dead tuples anywhere.
--   * Q5.4/Q5.5 - the blocking chain and its root. Kill the root, not the
--     leaves. Cancelling a leaf just makes the next waiter the victim.
--   * Q5.7 - the oldest xmin horizon. If the oldest backend_xmin is hours
--     old, every table in the database is accumulating dead tuples that
--     cannot be reclaimed. This is the most common cause of "autovacuum
--     is running but bloat keeps growing".
--   * Q5.8 - prepared transactions. If max_prepared_transactions is
--     non-zero and something crashed mid-two-phase-commit, an orphaned
--     prepared transaction holds locks and an xmin forever.
--
-- RISK OF RUNNING THIS
--   READ-ONLY, and deliberately so: every query here is a SELECT over
--   pg_stat_activity, pg_locks and friends. None of them take a lock that
--   can block user work.
--   THE FIXES ARE NOT READ-ONLY. `pg_cancel_backend(pid)` and
--   `pg_terminate_backend(pid)` are destructive to the victim's session:
--     * pg_cancel_backend sends SIGINT - cancels the running query, the
--       transaction aborts, the connection survives. Prefer this.
--     * pg_terminate_backend sends SIGTERM - kills the whole backend,
--       rolls back its transaction, and the client sees a dropped
--       connection. Use it only for an unresponsive or abandoned session.
--   Neither is a substitute for fixing the application. The queries in
--   this file will not run either one for you.
--
-- VERSION NOTES
--   * `pg_blocking_pids()` is PostgreSQL 9.6+. It is the only correct way
--     to find blockers and it works across parallel workers and prepared
--     transactions where the old pg_locks self-join does not.
--   * `wait_event_type` / `wait_event` are 9.6+.
--   * `backend_type` is 10+. `leader_pid` and `query_id` are 14+ and are
--     not used below.
--   * `pg_locks.waitstart` is 14+ and is not used below; the lock wait
--     duration is derived from pg_stat_activity.query_start instead.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q5.0  Connection budget and per-application usage
-- ---------------------------------------------------------------------
-- Interpretation guide:
--   * total close to max_connections means new connections will be
--     refused. Every connection costs a backend process and roughly
--     5-10 MB of memory even when idle, so max_connections is not a
--     number to raise lightly - a connection pooler in transaction mode
--     is usually the right answer.
--   * a high share of 'idle' connections is normal for a pool that keeps
--     warm connections. A high share of 'idle in transaction' is not.
SELECT
    (SELECT setting::int FROM pg_settings
      WHERE name = 'max_connections')                          AS max_connections,
    (SELECT setting::int FROM pg_settings
      WHERE name = 'superuser_reserved_connections')           AS reserved_for_superuser,
    count(*)                                                   AS connections,
    round(100.0 * count(*) / NULLIF((SELECT setting::int FROM pg_settings
                                     WHERE name = 'max_connections'), 0), 1) AS pct_of_max,
    count(*) FILTER (WHERE state = 'active')                   AS active,
    count(*) FILTER (WHERE state = 'idle')                     AS idle,
    count(*) FILTER (WHERE state = 'idle in transaction')      AS idle_in_transaction,
    count(*) FILTER (WHERE state = 'idle in transaction (aborted)') AS idle_in_txn_aborted,
    count(*) FILTER (WHERE state = 'disabled')                 AS disabled,
    count(*) FILTER (WHERE wait_event_type = 'Lock')           AS waiting_on_locks,
    count(*) FILTER (WHERE backend_type = 'autovacuum worker')  AS autovacuum_workers,
    count(*) FILTER (WHERE backend_type = 'walsender')          AS walsenders
FROM pg_stat_activity
WHERE backend_type = 'client backend'
   OR backend_type IN ('autovacuum worker', 'walsender');   -- backend_type is 10+

-- Grouped by application, which is where you find the leaking service.
SELECT
    application_name,
    COALESCE(client_addr::text, 'local')      AS client,
    count(*)                                  AS connections,
    count(*) FILTER (WHERE state = 'active')  AS active,
    count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction,
    max(now() - backend_start)                AS oldest_connection_age
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY application_name, client_addr
ORDER BY count(*) DESC
LIMIT 30;

-- Per-role and per-database limits, in case the cap is not global.
SELECT rolname, rolconnlimit,
       CASE WHEN rolconnlimit = -1 THEN 'unlimited' ELSE rolconnlimit::text END AS limit
FROM pg_roles
WHERE rolconnlimit <> -1
ORDER BY rolname;


-- ---------------------------------------------------------------------
-- Q5.1  Everything that is doing something, or squatting on a transaction
-- ---------------------------------------------------------------------
-- ORDER BY puts the worst squatters first: idle-in-transaction sessions
-- are sorted by how long the TRANSACTION (not the query) has been open,
-- because that is what holds back VACUUM.
SELECT
    a.pid,
    a.datname,
    a.usename,
    a.application_name,
    COALESCE(a.client_addr::text, 'local')                     AS client,
    a.state,
    a.wait_event_type,
    a.wait_event,
    now() - a.xact_start                                       AS xact_age,
    now() - a.query_start                                      AS query_age,
    now() - a.state_change                                     AS state_age,
    a.backend_xid,
    a.backend_xmin,
    left(regexp_replace(a.query, '\s+', ' ', 'g'), 160)        AS query
FROM pg_stat_activity a
WHERE a.backend_type = 'client backend'
  AND a.pid <> pg_backend_pid()
  AND (
        a.state = 'active'
     OR a.state IN ('idle in transaction', 'idle in transaction (aborted)')
     OR a.xact_start IS NOT NULL
      )
ORDER BY
    CASE a.state
        WHEN 'idle in transaction (aborted)' THEN 0
        WHEN 'idle in transaction'           THEN 1
        WHEN 'active'                        THEN 2
        ELSE 3
    END,
    COALESCE(a.xact_start, a.query_start) ASC
LIMIT 50;


-- ---------------------------------------------------------------------
-- Q5.2  Idle-in-transaction sessions - the vacuum blocker and lock leaker
-- ---------------------------------------------------------------------
-- This is the single highest-value output in this file. A session in
-- 'idle in transaction' has an open transaction but is doing nothing.
-- It holds every lock it has taken, blocks DDL, and pins backend_xmin so
-- that no table in the database can have its dead tuples removed.
--
-- Thresholds worth acting on:
--   > 60s   investigate; find the code path
--   > 5min  fix this week; it is measurably stalling vacuum
--   > 1h    treat as an incident
--
-- The usual causes: an ORM session opened and never closed, a
-- `BEGIN` in a connection pool check-out without a matching commit, a
-- try/except that catches the exception and forgets to roll back, or an
-- application waiting on an external HTTP call inside a transaction.
--
-- The database-side mitigation, if you cannot fix the code today:
--   idle_in_transaction_session_timeout = '5min'   (PostgreSQL 9.6+)
--   -- per role:  ALTER ROLE app_user SET idle_in_transaction_session_timeout = '5min';
-- Setting it globally is a blunt instrument: it will terminate legitimate
-- long interactive transactions too. Per-role is almost always better.
SELECT
    a.pid,
    a.datname,
    a.usename,
    a.application_name,
    COALESCE(a.client_addr::text, 'local')              AS client,
    a.state,
    now() - a.xact_start                                AS transaction_open_for,
    now() - a.state_change                              AS idle_for,
    a.backend_xmin,
    age(a.backend_xmin)                                 AS xmin_horizon_age,
    left(regexp_replace(a.query, '\s+', ' ', 'g'), 200) AS last_query
FROM pg_stat_activity a
WHERE a.state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND a.pid <> pg_backend_pid()
ORDER BY a.xact_start ASC NULLS LAST;


-- ---------------------------------------------------------------------
-- Q5.3  What everyone is waiting on, aggregated
-- ---------------------------------------------------------------------
-- Lightweight waits (ClientRead, ClientWrite, WalWriter) are the session
-- waiting on the network or the WAL writer - usually not your problem.
-- Lock, IO, LWLock and BufferPin waits are.
SELECT
    a.wait_event_type,
    a.wait_event,
    count(*)                                       AS sessions,
    max(now() - a.query_start)                      AS longest_wait
FROM pg_stat_activity a
WHERE a.state = 'active'
  AND a.pid <> pg_backend_pid()
  AND a.wait_event IS NOT NULL
GROUP BY a.wait_event_type, a.wait_event
ORDER BY count(*) DESC, max(now() - a.query_start) DESC;


-- ---------------------------------------------------------------------
-- Q5.4  Blocking pairs - blocked session and the session blocking it
-- ---------------------------------------------------------------------
-- Uses pg_blocking_pids(), which returns an ARRAY because one waiter can
-- be blocked by several holders at once. Hence the LATERAL unnest.
-- Read the blocked_query and blocker_query side by side: usually the
-- blocker is a long UPDATE/DELETE/DDL and the victim is something
-- unrelated that just wants a lock on the same row or table.
SELECT
    blocked.pid                                              AS blocked_pid,
    blocked.usename                                          AS blocked_user,
    blocked.application_name                                 AS blocked_app,
    blocked.state                                            AS blocked_state,
    now() - blocked.query_start                              AS blocked_for,
    blocked.wait_event_type,
    blocked.wait_event,
    blocker.pid                                              AS blocker_pid,
    blocker.usename                                          AS blocker_user,
    blocker.application_name                                 AS blocker_app,
    blocker.state                                            AS blocker_state,
    now() - blocker.xact_start                               AS blocker_txn_age,
    now() - blocker.state_change                             AS blocker_state_age,
    left(regexp_replace(blocked.query, '\s+', ' ', 'g'), 120) AS blocked_query,
    left(regexp_replace(blocker.query, '\s+', ' ', 'g'), 120) AS blocker_query
FROM pg_stat_activity blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(blocker_pid)
JOIN pg_stat_activity blocker ON blocker.pid = bp.blocker_pid
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
ORDER BY (now() - blocked.query_start) DESC;


-- ---------------------------------------------------------------------
-- Q5.5  Full blocking chains, with the ROOT blocker identified
-- ---------------------------------------------------------------------
-- A blocks B blocks C blocks D. Killing C does nothing useful; the root
-- is A. This walks the chain from every blocked session up to the session
-- that is waiting for nothing.
--
-- The `NOT e.blocker_pid = ANY(c.path)` guard stops the recursion if a
-- cycle ever appears in the graph. PostgreSQL's deadlock detector kills
-- one participant of a true cycle within deadlock_timeout, so you should
-- not normally see one - but a recursive CTE without a cycle guard is an
-- infinite loop waiting for a bad day.
--
-- CONCURRENCY WARNING: pg_stat_activity is a live view. A chain can
-- change between the recursive CTE and the joins below, so a result can
-- be internally inconsistent on a busy server. Re-run it before acting on
-- it, and prefer the root_pid you see twice over one you see once.
WITH RECURSIVE
blocked_edges AS (
    SELECT
        a.pid                                  AS blocked_pid,
        unnest(pg_blocking_pids(a.pid))        AS blocker_pid
    FROM pg_stat_activity a
    WHERE cardinality(pg_blocking_pids(a.pid)) > 0
),
chains AS (
    SELECT
        e.blocked_pid,
        e.blocker_pid,
        1                          AS depth,
        ARRAY[e.blocked_pid, e.blocker_pid] AS path
    FROM blocked_edges e

    UNION ALL

    SELECT
        c.blocked_pid,
        e.blocker_pid,
        c.depth + 1,
        c.path || e.blocker_pid
    FROM chains c
    JOIN blocked_edges e ON e.blocked_pid = c.blocker_pid
    WHERE NOT e.blocker_pid = ANY (c.path)
      AND c.depth < 20                       -- hard stop, belt and braces
),
deepest AS (
    SELECT DISTINCT ON (c.blocked_pid)
        c.blocked_pid,
        c.blocker_pid       AS root_pid,
        c.depth             AS blockers_ahead,
        c.path
    FROM chains c
    ORDER BY c.blocked_pid, c.depth DESC
)
SELECT
    d.blocked_pid,
    blocked.usename                                  AS blocked_user,
    blocked.state                                    AS blocked_state,
    now() - blocked.query_start                      AS blocked_for,
    blocked.wait_event,
    d.blockers_ahead,
    d.root_pid,
    root.usename                                     AS root_user,
    root.application_name                            AS root_app,
    root.state                                       AS root_state,
    now() - root.xact_start                          AS root_txn_age,
    now() - root.state_change                        AS root_state_age,
    array_to_string(d.path, ' -> ')                  AS chain,
    left(regexp_replace(blocked.query, '\s+', ' ', 'g'), 100) AS blocked_query,
    left(regexp_replace(root.query, '\s+', ' ', 'g'), 100)    AS root_query
FROM deepest d
JOIN pg_stat_activity blocked ON blocked.pid = d.blocked_pid
JOIN pg_stat_activity root    ON root.pid    = d.root_pid
WHERE d.blockers_ahead > 1
ORDER BY d.blockers_ahead DESC, blocked_for DESC;


-- ---------------------------------------------------------------------
-- Q5.6  Root blockers only - the sessions to act on
-- ---------------------------------------------------------------------
-- Sessions that block at least one other session and are not themselves
-- blocked. If you must terminate something under load, it is one of
-- these, and you should still prefer pg_cancel_backend() first.
-- `blocked_sessions` tells you how many people a single decision unblocks.
SELECT
    root.pid,
    root.datname,
    root.usename,
    root.application_name,
    root.state,
    root.wait_event_type,
    root.wait_event,
    now() - root.xact_start                          AS txn_age,
    now() - root.query_start                         AS query_age,
    cardinality(pg_blocking_pids(root.pid))          AS own_blockers,
    cardinality(ARRAY(
        SELECT p.pid FROM pg_stat_activity p
        WHERE root.pid = ANY (pg_blocking_pids(p.pid))
    ))                                               AS blocked_sessions,
    left(regexp_replace(root.query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_activity root
WHERE root.pid <> pg_backend_pid()
  AND cardinality(pg_blocking_pids(root.pid)) = 0        -- not itself blocked
  AND EXISTS (
        SELECT 1 FROM pg_stat_activity w
        WHERE root.pid = ANY (pg_blocking_pids(w.pid))
      )
ORDER BY cardinality(ARRAY(
            SELECT p.pid FROM pg_stat_activity p
            WHERE root.pid = ANY (pg_blocking_pids(p.pid))
         )) DESC,
         txn_age DESC NULLS LAST;


-- ---------------------------------------------------------------------
-- Q5.7  Long-running transactions, and the OLDEST XMIN HORIZON
-- ---------------------------------------------------------------------
-- Part 1: transactions that have been open a long time. A transaction
-- open for an hour, even if idle, prevents VACUUM from cleaning up any
-- row version created after it started - in EVERY table, not just the
-- ones it touched.
--
-- Part 2: a single row telling you the age of the oldest xmin held by any
-- backend. If xmin_age is in the millions, nothing can be vacuumed away
-- and your tables will bloat no matter how the autovacuum settings look.
SELECT
    a.pid,
    a.datname,
    a.usename,
    a.application_name,
    a.state,
    a.xact_start,
    now() - a.xact_start        AS xact_age,
    a.backend_xid,
    age(a.backend_xid)          AS xid_age,
    a.backend_xmin,
    age(a.backend_xmin)         AS xmin_age,
    left(regexp_replace(a.query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_activity a
WHERE a.xact_start IS NOT NULL
  AND a.pid <> pg_backend_pid()
  AND now() - a.xact_start > interval '1 minute'
ORDER BY a.xact_start ASC;

-- The horizon itself, in one number.
SELECT
    max(age(backend_xmin))                     AS oldest_xmin_age_ticks,
    count(*) FILTER (WHERE backend_xmin IS NOT NULL) AS sessions_holding_xmin,
    -- how far the database as a whole could freeze right now
    (SELECT max(age(datfrozenxid)) FROM pg_database) AS oldest_database_frozenxid_age
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL;

-- Replication slots that are inactive or far behind pin xmin too, and
-- they survive the client disappearing. An abandoned slot is one of the
-- classic causes of runaway bloat on a primary.
-- Note: on a standby these columns are mostly NULL - check the PRIMARY.
SELECT
    slot_name,
    slot_type,
    database,
    active,
    xmin,
    age(xmin)             AS xmin_age,
    catalog_xmin,
    age(catalog_xmin)     AS catalog_xmin_age,
    restart_lsn,
    confirmed_flush_lsn
FROM pg_replication_slots
ORDER BY age(xmin) DESC NULLS LAST,
         age(catalog_xmin) DESC NULLS LAST;


-- ---------------------------------------------------------------------
-- Q5.8  Prepared transactions (two-phase commit leftovers)
-- ---------------------------------------------------------------------
-- Only relevant if max_prepared_transactions > 0 (the default is 0,
-- which disables the feature entirely). An orphaned prepared transaction
-- - a coordinator that crashed between PREPARE and COMMIT PREPARED -
-- holds its locks and its xmin until someone resolves it. It is invisible
-- to the application, holds no connection, and blocks vacuum forever.
--
-- To resolve: COMMIT PREPARED '<gid>';  or  ROLLBACK PREPARED '<gid>';
-- Decide which by consulting whoever ran the distributed transaction.
-- Do not guess on a production system.
SELECT
    (SELECT setting::int FROM pg_settings
      WHERE name = 'max_prepared_transactions')      AS max_prepared_transactions,
    p.gid,
    p.prepared,
    now() - p.prepared                               AS prepared_for,
    p.owner,
    p.database,
    p.transaction                                    AS xid
FROM pg_prepared_xacts p
ORDER BY p.prepared ASC;


-- ---------------------------------------------------------------------
-- Q5.9  Lock inventory by type and mode
-- ---------------------------------------------------------------------
-- `granted = false` rows are waiters. A crowd of AccessExclusiveLock
-- waiters on one relation is the signature of a blocked DDL statement
-- (ALTER TABLE, TRUNCATE, DROP) - and worse, once an AccessExclusiveLock
-- is WAITING, it queues ahead of later readers, so a single ALTER TABLE
-- can stall the whole table behind it. That is why DDL belongs in a
-- maintenance window or behind a lock_timeout:
--     SET lock_timeout = '3s';   then run the ALTER.
SELECT
    l.locktype,
    l.mode,
    l.granted,
    count(*)                                              AS lock_count,
    count(DISTINCT l.pid)                                 AS sessions,
    count(*) FILTER (WHERE l.relation IS NOT NULL)        AS on_relations
FROM pg_locks l
GROUP BY l.locktype, l.mode, l.granted
ORDER BY l.granted ASC, count(*) DESC;

-- Waits on user relations, with the relation named. This is the query to
-- keep open during a migration.
SELECT
    l.pid,
    a.usename,
    a.application_name,
    a.state,
    n.nspname                                   AS schema,
    c.relname                                   AS relation,
    l.mode,
    l.granted,
    now() - a.query_start                       AS waiting_for,
    left(regexp_replace(a.query, '\s+', ' ', 'g'), 160) AS query
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
LEFT JOIN pg_class c    ON c.oid = l.relation
LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE l.locktype = 'relation'
  AND l.relation IS NOT NULL
  AND c.relkind IN ('r', 'm', 'p', 'i', 'S')     -- tables, matviews, indexes, sequences
  AND c.relnamespace NOT IN (
        SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog', 'information_schema'))
ORDER BY l.granted ASC, (now() - a.query_start) DESC NULLS LAST;
