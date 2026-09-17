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

**All 24 checks and all 7 SQL files were executed against a live PostgreSQL 17.11
server, and every statement ran cleanly.** That includes correct degradation when
`pg_stat_statements` is unavailable — the extension-dependent checks are reported
as *skipped* with the two-step install instructions, rather than failing or
silently reporting nothing.

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

The paid kit adds `INDEXING-PLAYBOOK.md` and the prioritised findings report in
`--json` form for CI.

→ **Postgres Performance Toolkit**: <!-- GUMROAD-LINK -->
