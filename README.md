# pg-perf-check

Answer **"why is this database slow?"** with a scripted diagnostic report instead
of guesswork. 24 threshold checks plus 7 read-only SQL files, each answering one
question.

```bash
pip install psycopg2-binary        # or: apt install python3-psycopg2
python3 run_diagnostics.py --database-url "$DATABASE_URL"
python3 run_diagnostics.py --list-checks
python3 run_diagnostics.py --self-test      # no database needed
```

```
Connected to mydb on 127.0.0.1/5433 (PostgreSQL 17.11)
Running 24 checks and the sql/ files...

  connections              CONNECTIONS
    84 of 100 connection slots in use, 12 idle in transaction
    ...
    Suggested action:
      Put a connection pooler in transaction mode in front of PostgreSQL and cap
      the pool at roughly (2 x CPU cores) + effective_spindle_count. Do not raise
      max_connections as the first move: it multiplies memory use per backend.
    Source: sql/05-connections-and-locks.sql (Q5.0)
```

## Everything here is read-only

No check writes, alters or drops anything, and none holds a lock beyond a normal
`SELECT`. Each SQL file says so in its header so you can confirm before pointing
it at production.

## What it checks

**24 threshold checks** across connections, idle-in-transaction sessions, lock
blocking chains, long-running queries, the oldest `xmin` horizon, replication
slots pinning `xmin`, table and index bloat, unused and duplicate indexes, cache
hit ratios, autovacuum health, and more — each with the evidence, the likely
impact and a concrete action.

**7 SQL files**, each answering one question and safe to run alone:

| File | Question |
|---|---|
| `01-table-sizes-and-bloat` | what is big, and what is dead weight? |
| `02-unused-and-missing-indexes` | which indexes earn their keep, and what is missing? |
| `03-slow-queries` | where is the time actually going? |
| `04-index-usage` | scan counts, index sizes, redundancy |
| `05-connections-and-locks` | who is waiting on whom? |
| `06-cache-hit-and-io` | is it reading from cache or from disk? |
| `07-vacuum-and-autovacuum` | is bloat accumulating, and why? |

## Verified against a real PostgreSQL 17.11

**All 24 checks were executed against a live PostgreSQL 17.11 server, and the
whole report was produced end to end from that server.** Where
`pg_stat_statements` is unavailable, the four extension-dependent checks are
reported as *skipped* with the two-step install instructions, rather than failing
or silently reporting nothing.

The 7 SQL files are executed by the runner too, which prints a per-file result
line so you can see the state of your own server version. Exactly what that
showed, and what was not verified:

* `01`, `02`, `04`, `05`, `06` and `07` ran every statement cleanly on 17.11.
  `07-vacuum-and-autovacuum.sql` reports **10/10 statements OK**.
* Two statements in `07-vacuum-and-autovacuum.sql` did **not**, and are fixed:
  Q7.6 named `pg_stat_progress_vacuum` columns that PostgreSQL 17 replaced —
  `max_dead_tuples` / `num_dead_tuples` became `max_dead_tuple_bytes`,
  `dead_tuple_bytes` and `num_dead_item_ids`, a rename *and* a change of unit, so
  naming either set breaks the other branch. Q7.6 now reads those fields out of
  the row's JSON form (`to_jsonb(p) ->> '…'`), so it parses on every supported
  version and reports which branch you are on in a `dead_store_units` column.
  Q7.7 selected `datname` from `pg_stat_user_tables`, a column that has never
  existed on **any** release — that view is already scoped to the current
  database — and now selects `schemaname` / `relname`. Q7.6's 17+ branch was
  verified by catching a live throttled `VACUUM` (`dead_store_units = bytes`,
  `dead_store_capacity = 67108864`, i.e. the default 64 MiB
  `maintenance_work_mem`); Q7.7 was verified directly. **Not verified:** the
  pre-17 branch of Q7.6, because no PostgreSQL 16-or-older server was available.
* Two check-output defects were found during that same verification and are also
  fixed: `shared_buffers_low` reported `approximately 0 MB` for a stock 128 MB
  `shared_buffers` because the parser ignored `pg_settings.unit` (`setting = 16384,
  unit = 8kB`) — it now prints `approximately 128 MB` — and `autovacuum_throttled`
  printed `vacuum_cost_page_miss = None, vacuum_cost_page_dirty = None` because the
  settings query selected neither; it now prints `2` and `20`.
* `03-slow-queries.sql` needs the `pg_stat_statements` extension. On a server
  without it, seven of its eight statements fail with *relation
  "pg_stat_statements" does not exist*, and the runner reports exactly that
  alongside the install instructions. That is a missing extension on the server,
  not a broken statement, and it is the only remaining failure the runner prints
  on a stock 17.11 server without that extension.

## The advice it gives about its own output

Worth quoting, because it is the part most tooling gets wrong:

> Change ONE thing, then re-run this script and compare. Two changes at once means
> you cannot tell which one helped. […] Changing a setting is not the same as
> fixing a query.

Counters are cumulative since the last statistics reset, so the counter-based
findings (unused indexes, sequential scans, cache ratios) only mean something
after a full business cycle.

## What it is not

It diagnoses; it does not tune for you. It cannot know your workload, so it cannot
tell you whether a missing index is worth the write cost. `EXPLAIN-GUIDE.md`
covers reading plans; designing an index is your call.

Results also depend on your statistics being current — on a server where `ANALYZE`
has not run recently, estimated row counts will mislead you.

## Requirements

PostgreSQL 12+, Python 3.8+, and a Postgres driver (`psycopg2`).

## The full pack

The paid kit adds the two playbooks this repository does not ship —
`INDEXING-PLAYBOOK.md` (how to design an index the planner actually uses, with
page counts and WAL volumes measured on a live PostgreSQL 17.11 server rather than
asserted) and `REMEDIATION-PLAYBOOK.md` (one section per check: the threshold that
fired, what the counter really counts, the false positives to rule out, the SQL
with the guards that stop it being an outage, and how to verify the fix worked) —
plus the CI harness that turns the `--json` report into a baseline comparison,
failing a build only on findings that are new or worse.

→ **[Postgres Performance Toolkit](https://duke5am.gumroad.com/l/05-postgres-perf-toolkit)** — $29 on Gumroad <!-- GUMROAD-LINK -->
