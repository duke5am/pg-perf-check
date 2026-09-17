# Reading `EXPLAIN (ANALYZE, BUFFERS)`

Most people run `EXPLAIN`, see `Seq Scan`, and conclude "missing index". That conclusion
is wrong often enough to be dangerous — adding an index to a query that *should* be doing a
sequential scan makes the database slower and you will not understand why.

This guide is about reading the actual numbers so you can tell the difference between a
plan that is wrong and a plan that is right but slow for a reason an index cannot fix.

---

## 0. The single most important caveat

**Plans are not properties of queries. They are properties of queries *plus* the current
statistics *plus* the cost model *plus* the parameter values.**

The same SQL text can produce five different plans in an hour on the same server: as the
table grows, as `ANALYZE` refreshes the row estimates, as `random_page_cost` differs
between your laptop and production, as a prepared statement switches from a custom plan to
a generic plan, and as the literal value in the `WHERE` clause changes from a common one to
a rare one.

Consequences worth internalising:

- A plan you copy out of a blog post (including this one) may be irrelevant to your data.
  The *shape* of the reasoning transfers; the numbers do not.
- `EXPLAIN` on an empty or freshly-restored table tells you almost nothing. Row estimates
  come from `pg_statistic`, which `ANALYZE` fills in. No `ANALYZE`, no meaningful plan.
- If you change a setting (`random_page_cost`, `work_mem`, `enable_seqscan`) to "make the
  planner pick the index", you have changed the *estimate*, not the *reality*. Sometimes
  that is the right fix. Often it just hides a statistics problem.
- "The plan changed and got slower" is normal and expected. Plans are supposed to change
  when the data changes.

Everything below is about interpreting evidence, not about memorising rules.

---

## 1. `EXPLAIN` vs `EXPLAIN ANALYZE` vs `EXPLAIN (ANALYZE, BUFFERS)`

| Command | Runs the query? | What you get |
|---|---|---|
| `EXPLAIN q` | No | The planner's *guess*: chosen plan, estimated cost and estimated rows |
| `EXPLAIN ANALYZE q` | **Yes** | The guess *plus* real time, real rows, real loop counts |
| `EXPLAIN (ANALYZE, BUFFERS) q` | **Yes** | The above plus page-level I/O counters per node |
| `EXPLAIN (ANALYZE, BUFFERS, VERBOSE) q` | **Yes** | The above plus output column lists per node — usually noise |
| `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) q` | **Yes** | Machine-readable; use this for diffing plans |

The rule that matters: **`ANALYZE` executes the statement.** The deadlock between wanting
real numbers and not wanting to run a 40-minute query is solved with:

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS) <your query>;
ROLLBACK;
```

`ROLLBACK` is only meaningful if the statement writes. For a plain `SELECT`, use a
`statement_timeout` instead so a bad plan cannot run away:

```sql
SET statement_timeout = '10s';
EXPLAIN (ANALYZE, BUFFERS) SELECT ...;
RESET statement_timeout;
```

**Never run `EXPLAIN ANALYZE` on an `UPDATE` or `DELETE` outside a transaction you intend
to roll back.** It performs the write. This is the most common way people destroy data
while "just looking at a plan". `EXPLAIN` without `ANALYZE` is safe for anything.

---

## 2. The node, field by field

Take one line from a real plan and read it left to right.

```
Limit  (cost=0.43..2841.55 rows=20 width=44) (actual time=1842.117..1842.121 rows=0 loops=1)
```

**`Limit`** — the node type. The plan is a tree; this is the root, so it is the last thing
to run and its output is what the client receives.

**`cost=0.43..2841.55`** — the planner's cost estimate. These are **arbitrary units**, not
milliseconds, not bytes, and they are not comparable across servers with different
settings. `0.43` is the startup cost — the work needed before the first row can be
returned (important for `LIMIT`, cursors and `EXISTS`). `2841.55` is the total cost to
return every row. A large gap between them means the node can stream output early.

**`rows=20`** — the *estimated* number of rows this node will emit. Note: for the `Limit`
node this is the `LIMIT` value, so it is trivially right and tells you nothing. Estimates
only become informative at scan and join nodes.

**`width=44`** — the estimated average row width in bytes. Used for cost maths and for
deciding whether a sort fits in `work_mem`. A wildly wrong `width` is a common reason
`work_mem` spills happen when the planner "should" have known better.

**`actual time=1842.117..1842.121`** — real elapsed milliseconds, measured only when
`ANALYZE` is used. First number = time to the first row, second = time to the last row.
The difference is the time spent *producing* rows.

**`rows=0`** — the *actual* row count. This is the number you compare against the estimate.

**`loops=1`** — how many times this node was executed. **Critical and constantly
misread:** when `loops > 1`, `actual time` and `rows` are **per-loop averages**, not
totals. The real totals are:

```
total rows   = rows   * loops
total time   = actual time (last number) * loops
```

A nested-loop inner node showing `rows=1 loops=2,000,000` did 2,000,000 row lookups, not 1.

The indentation is the tree structure. `->` marks a child. Read a plan **bottom-up**: the
deepest, most-indented nodes execute first and feed their parents.

---

## 3. `BUFFERS` — why it is not optional

```
Buffers: shared hit=402118 read=88124
```

**`shared`** = the `shared_buffers` pool. **`hit`** = the page was already in memory.
**`read`** = PostgreSQL had to ask the operating system for it (which may still be an OS
page-cache hit — PostgreSQL cannot tell the difference, which is why a `read` is not
automatically a disk seek).

Counts are in **8 kB blocks** by default. So:

```
shared hit=402118 read=88124
→ 490,242 blocks × 8 kB = 4,016,062,464 bytes ≈ 3.7 GB of page traffic
```

Three reasons this matters more than the timings:

1. **Timings are noisy; buffer counts are not.** A query that reads 4 GB of buffers is
   doing 4 GB of work regardless of whether the machine was busy. Buffer counts are the
   most reproducible evidence in a plan.
2. **They show where the work is.** If the top node reports 500,000 buffers and its child
   reports 4, that is where the I/O is. Timings alone often cannot tell you.
3. **They separate "slow because it read too much" from "slow because it read the right
   amount slowly."** A plan that touches 12 buffers and takes 900 ms is waiting on
   something else — a lock, the client, a function, or WAL.

Other `BUFFERS` fields you will see:

- **`dirtied`** — pages this query modified. Useful for spotting a `SELECT` that is
  unexpectedly writing (a function with side effects, or a `SELECT ... FOR UPDATE`).
- **`written`** — pages this backend had to evict to disk itself. A large number here
  during a read query is a sign of buffer pressure.
- **`local`** — temporary tables and temp files for the session.
- **`temp read` / `temp written`** — sort and hash spills. Any non-zero value means
  `work_mem` was too small for this node.
- **`I/O Timings: read=...`** — only present when `track_io_timing = on`. Necessary if you
  want to argue about latency rather than volume.

Version note: on PostgreSQL 18, `BUFFERS` is enabled by default for `EXPLAIN ANALYZE`, and
buffer counts are reported per node more consistently. On earlier versions you must ask for
it explicitly, and older releases aggregate some counters differently. If two plans look
different between versions, check this before concluding the planner changed.

---

## 4. Scan types and what each one means

| Node | What it does | When it is the right choice |
|---|---|---|
| `Seq Scan` | Reads every page of the heap, filters rows | The query needs a large fraction of the table (roughly >5–10%, but it depends on correlation and `random_page_cost`), or the table is small (a few pages) |
| `Index Scan` | Walks the index, then fetches each matching heap row | Few rows needed **and** the heap rows are physically clustered (good correlation) so fetches are sequential-ish |
| `Index Only Scan` | Answers entirely from the index, never touching the heap | All needed columns are in the index **and** the pages are marked all-visible in the visibility map |
| `Bitmap Index Scan` + `Bitmap Heap Scan` | Builds a bitmap of matching pages, then visits each page once in physical order | A medium number of rows, especially when they are scattered. Turns many random fetches into one ordered pass |
| `Tid Scan` | Fetches heap rows by physical location | Almost always from a query rewritten against `ctid` |

### `Seq Scan` is frequently correct

`Seq Scan` on a table where the filter keeps 60% of rows is not a bug. Forcing it into an
index scan there is worse, because the index scan has to do a random heap fetch per row
and the pages are not in order.

The signal that a `Seq Scan` is *wrong* is not the node name. It is the combination:

- **`Rows Removed by Filter` much larger than `rows`** — it walked a lot of data to keep
  almost none.
- **`Buffers` far out of proportion to `rows`** — it read 300,000 pages to return 12 rows.
- **It happens at high frequency** — a `Seq Scan` on a lookup query called 4 million times
  a day is a catastrophe; the same scan on a nightly report is fine.

That last point is why the counters in `sql/02-unused-and-missing-indexes.sql` matter:
frequency is invisible in a single plan.

### `Index Scan` can be slow for a reason nobody expects

An `Index Scan` returning 200,000 rows does 200,000 heap fetches. If those rows are spread
across the whole table, every fetch is a different page, and you have converted one
sequential read into 200,000 random reads. The planner estimates this with **correlation**
— `pg_stats.correlation`, where 1.0 means the column's physical order matches the index
order and 0 means it is random.

```sql
-- Correlation near -1 or 1 means the index scan will be cheap.
-- Correlation near 0 means expect random I/O.
SELECT attname, correlation, n_distinct
FROM pg_stats
WHERE schemaname = 'public' AND tablename = 'events'
ORDER BY abs(correlation) DESC;
```

A `created_at` column on an append-only table typically has correlation near 1.0, and its
index scans are cheap. A `status` column on the same table has correlation near 0, and its
index scans are expensive. This is also why `random_page_cost` matters so much: it is the
price the planner assigns to exactly that random fetch.

### `Index Only Scan` and the `Heap Fetches` field

```
Index Only Scan using orders_customer_created_idx on orders
  Heap Fetches: 184203
```

`Heap Fetches` is the number of times the "index-only" scan still had to visit the heap. It
is non-zero for any page not marked all-visible in the visibility map, which is a bitmap
`VACUUM` maintains. Two consequences:

- An index-only scan on a table written constantly will always show heap fetches for the
  recent pages. That is expected, not a bug.
- If `Heap Fetches` is close to the row count on an old, stable table, you have a vacuum
  problem, not an index problem. `VACUUM` fixes it; `REINDEX` does not.

`sql/04-index-usage.sql` Q4.3 shows visibility-map coverage per table. Check it
before you build a covering index expecting index-only scans.

---

## 5. Estimated vs actual rows — the single most diagnostic signal

Compare `rows=` in the estimate with `rows=` in the actual, at every scan and join node.

| Divergence | What it usually means |
|---|---|
| Within ~2–3x | Fine. This is normal estimation error. |
| ~10x off | The planner may still choose a good plan, but it is on the edge. Order-of-magnitude errors change join strategy. |
| 100x+ off, in either direction | The plan is being chosen from fiction. **This is the root cause**, and no index will fix it until the estimate does. |

Estimate **too low** is the expensive direction, because it makes the planner choose a
nested loop it cannot afford — it thinks it will do 50 lookups and actually does 500,000.

Estimate **too high** makes the planner choose a hash join or a sequential scan it did not
need, which wastes memory and I/O but is rarely catastrophic.

Why estimates go wrong, in rough order of frequency:

1. **Stale statistics.** `n_mod_since_analyze` is high; the estimate describes last week's
   data. Fix: `ANALYZE`, then lower `autovacuum_analyze_scale_factor` for that table.
2. **Correlated predicates.** `WHERE city = 'Paris' AND country = 'France'` — the planner
   multiplies two independent selectivities and gets a number far too small, because it
   does not know those values always occur together. Modern PostgreSQL (10+) collects
   multivariate statistics for explicitly created *extended statistics*:
   ```sql
   CREATE STATISTICS orders_city_country (dependencies, ndistinct)
     ON city, country FROM public.orders;
   ANALYZE public.orders;
   ```
   This is the real fix for a correlated-predicate misestimate, and it is one of the
   highest-leverage things in this guide.
3. **Skew.** A column where one value is 90% of rows and the rest are 1% each. The default
   `default_statistics_target` of 100 builds 100 buckets, which cannot represent that
   shape. Fix: raise the target for that column only.
   ```sql
   ALTER TABLE public.orders ALTER COLUMN status SET STATISTICS 1000;
   ANALYZE public.orders;
   ```
4. **A function on the column.** `WHERE date_trunc('day', created_at) = '2025-01-01'` — the
   planner has no statistics for `date_trunc(created_at)`, so it falls back to a default
   guess. Fix: rewrite as a range, or create an expression index.
   ```sql
   -- rewritten as a sargable range
   WHERE created_at >= '2025-01-01' AND created_at < '2025-01-02'
   ```
5. **Very large tables with an out-of-date `reltuples`.** `pg_class.reltuples` is `-1` on
   PostgreSQL 14+ when the relation has never been vacuumed or analyzed.

### How to *see* estimates in a plan

```
->  Nested Loop  (cost=0.86..4211.02 rows=50 width=88) (actual time=0.038..18402.7 rows=498213 loops=1)
      ->  Index Scan using customers_pkey on customers c  (cost=0.43..8.45 rows=1 width=52) (actual time=0.02..0.03 rows=1 loops=1)
      ->  Index Scan using orders_customer_id_idx on orders o  (cost=0.43..79.90 rows=50 width=36) (actual time=0.041..0.028 rows=9964 loops=199)
```

Read it as: the planner expected 50 orders for a customer and planned a nested loop
accordingly. The inner node actually returned `rows=9964` per loop. Over 199 loops that is
~1.98 million rows, and the join took 18 seconds. The error is 200x, at the node that
matters. **The fix here is statistics, not an index** — an index is already being used.

---

## 6. `Rows Removed by Filter` — what it actually tells you

```
Filter: (customer_id = 48213)
Rows Removed by Filter: 4103241
```

This counts rows that the node read, tested against the filter, and discarded. Those rows
cost the full price of being read and then produced nothing. Interpretations:

- **`Rows Removed by Filter` ≫ `rows` returned, at a scan node** → the access path is not
  selective enough. This is the strongest single indicator that a better index exists.
- **The filter references a column an existing index does not cover** → you have an index
  that gets you into the neighbourhood and then a filter that throws most of it away. This
  is exactly the "equality before range" case in `INDEXING-PLAYBOOK.md`.
- **The filter is on a function of an indexed column** → the index cannot be used for that
  predicate at all. See the expression-index section of the playbook.
- **The rows removed are a small fraction of rows returned** → the filter is doing its job
  and there is nothing to fix.
- **`Rows Removed by Join Filter`** is the same idea for a join predicate: the join read
  pairs and rejected most of them, usually because it is a nested loop over a
  non-selective condition.
- **Rows removed by `Index Recheck`** (under a Bitmap Heap Scan) means the bitmap was lossy
  — the index returned page-level rather than row-level matches, so the condition is
  re-tested on the heap. Not an error; a sign the bitmap was large.

Do not confuse `Rows Removed by Filter` with rows never read. A `Seq Scan` on a 12M-row
table with `Rows Removed by Filter: 200` read all 12M rows and removed only 200 — the
filter is not the problem there, the scan is. The two numbers only mean something
together, with the `rows` output.

---

## 7. Join strategies: nested loop, hash join, merge join

There is no "best" join. Each is optimal in a different regime, and the planner picks by
cost using its row estimates — which is why a misestimate so often produces a catastrophic
join.

### Nested Loop

```
->  Nested Loop  (cost=... rows=...) (actual time=... rows=... loops=1)
      ->  Seq Scan on customers c
      ->  Index Scan using orders_customer_id_idx on orders o
            Index Cond: (o.customer_id = c.id)
```

For each row from the outer side, look up matches on the inner side.

- **Chosen when:** the outer side is small and the inner side has an index on the join key.
- **Cost:** `outer_rows × inner_lookup_cost`. Excellent at 100 outer rows. Catastrophic at
  500,000.
- **In a plan, look for:** `loops=` on the inner node. `loops=2000000` on an inner index
  scan is the classic "nested loop over a misestimate" signature and it is where 90% of
  "the query suddenly got slow" incidents live.
- **Correct fix:** fix the outer row estimate (statistics) so the planner stops choosing
  it. Adding an index to the *inner* side does not help — there already is one.
- **Legitimate exception:** nested loops are the right answer for a `LIMIT` query, because
  they can stop early. Do not force them off globally.

### Hash Join

```
->  Hash Join  (cost=... rows=...) (actual time=... rows=... loops=1)
      Hash Cond: (o.customer_id = c.id)
      ->  Seq Scan on orders o
      ->  Hash  (cost=... rows=...) (actual time=... rows=... loops=1)
            Buckets: 65536  Batches: 4  Memory Usage: 8192kB
            ->  Seq Scan on customers c
```

Build a hash table from the smaller side, then probe it once per row of the larger side.

- **Chosen when:** both sides are large, the join is an equality join, and there is no
  useful ordering to exploit.
- **Cost:** roughly linear in total rows. Very robust — a hash join is rarely catastrophic,
  which is why misestimates that produce nested loops are so much worse.
- **In a plan, look for:** `Batches` and `Memory Usage`. `Batches: 1` means it fit in
  `work_mem`. `Batches: 4` means it spilled and had to make four passes over the data, and
  each extra batch is real I/O.
- **`Batches > 1` fixes, in order:** (a) fix the row estimate so the planner sizes it
  correctly, (b) give more `work_mem` to the role running this query, (c) reduce the data
  before the join.
- **Variant:** `Parallel Hash Join` with `Workers Planned: 4  Workers Launched: 4`. Workers
  launched below workers planned is normal under concurrency; it usually means
  `max_parallel_workers` or `max_worker_processes` is saturated.

### Merge Join

```
->  Merge Join  (cost=... rows=...) (actual time=... rows=... loops=1)
      Merge Cond: (o.created_at = e.occurred_at)
      ->  Index Scan using orders_created_at_idx on orders o
      ->  Sort  (cost=... rows=...) (actual time=... rows=... loops=1)
            Sort Key: e.occurred_at
            Sort Method: external merge  Disk: 84512kB
            ->  Seq Scan on events e
```

Both inputs are sorted on the join key; then a single pass over both finds matches.

- **Chosen when:** both sides already arrive sorted (from an index), or the output needs to
  be sorted anyway, or the join is on a range/inequality rather than equality. It is also
  the only strategy that can be used for a non-equality join.
- **Cost:** `sort + sort + linear pass`. If both sides come from indexes in the right order
  the sorts are free and it is very efficient.
- **In a plan, look for:** `Sort Method`. `quicksort` means it fit in memory. `external
  merge  Disk: ...` means it spilled — see the `work_mem` guidance. A merge join whose sort
  spilled is usually worse than the hash join the planner rejected.

### The decision table

| Situation | Expected strategy |
|---|---|
| Small result set, index on the join key, outer side small | Nested Loop |
| Large × large, equality join | Hash Join |
| Either side already sorted by the join key, or ordering is needed downstream | Merge Join |
| Non-equality join (`<`, `>`, `BETWEEN`) | Merge Join (hash cannot do it) |
| `LIMIT n` on a query with a matching index | Nested Loop — it can stop after `n` |
| Small outer, inner side **without** an index | Hash Join (the planner has no choice) |

If the plan shows a nested loop and you expected a hash join, **the problem is almost
always the row estimate on the outer side, not the join method.** Go back to section 5.

---

## 8. Worked example

A fictional but realistic case, with the reasoning laid out the way you would actually do it.

### The report

> "The customer detail page takes 1.8 seconds. Sometimes 2 seconds. It used to be fast."

### The query

```sql
SELECT id, customer_id, total_cents, created_at
FROM orders
WHERE customer_id = 48213
  AND created_at >= now() - interval '30 days'
ORDER BY created_at DESC
LIMIT 20;
```

### What exists already

```sql
orders                      -- 12,000,000 rows, ~7.5 GB
orders_pkey                 -- PRIMARY KEY (id)
orders_created_at_idx       -- btree (created_at)
orders_customer_id_idx      -- btree (customer_id)
```

`ANALYZE` ran an hour ago. `n_mod_since_analyze` is small. Statistics are not the problem
here — established before reading the plan, which is the right order of operations.

### The plan

```
Limit  (cost=0.43..3312.10 rows=20 width=44) (actual time=1837.902..1837.906 rows=0 loops=1)
  Buffers: shared hit=401884 read=88112
  ->  Index Scan Backward using orders_created_at_idx on orders
        (cost=0.43..1648802.55 rows=9954 width=44) (actual time=1837.900..1837.900 rows=0 loops=1)
        Index Cond: (created_at >= (now() - '30 days'::interval))
        Filter: (customer_id = 48213)
        Rows Removed by Filter: 4103241
        Buffers: shared hit=401884 read=88112
Planning Time: 0.418 ms
Execution Time: 1838.204 ms
```

### The reasoning, step by step

**1. `Index Scan Backward using orders_created_at_idx`** — it is walking the `created_at`
index backwards to satisfy `ORDER BY created_at DESC`, and never touching the
`customer_id` index. That is not obviously stupid: the planner hopes to walk recent orders
newest-first and stop as soon as it has 20 for this customer.

**2. `Index Cond: (created_at >= ...)` and `Filter: (customer_id = 48213)`** — the index
only enforces the date range. The customer filter is applied *after* the heap fetch. So
every one of the 4.1 million orders in the last 30 days is fetched from the heap and then
discarded.

**3. `Rows Removed by Filter: 4103241` vs `rows=0`** — it read 4,103,241 rows and returned
zero. This is the diagnosis. Not a slow plan in general: a plan whose entire cost was
wasted on rows the index could not exclude.

**4. `Buffers: shared hit=401884 read=88112`** — 490,242 blocks × 8 kB ≈ **3.7 GB of page
traffic** for a query that returns nothing. 401,884 of those pages came from
`shared_buffers` and 88,112 from the OS, so this was not even a disk problem. It was pure
CPU and memory-bandwidth cost of reading 3.7 GB.

**5. `rows=0` actual vs `rows=9954` estimated** — a 9,954-row error, and worth understanding.
The planner's reasoning was: "30 days of orders out of the whole table is about 12% of
rows; `customer_id` has many distinct values, so about 9,954 of them belong to this one
customer." The distribution is skewed: customer 48213 has *no* orders in the last 30 days.
The estimate is not absurd — it is wrong in a way statistics cannot see, because it assumes
`created_at` and `customer_id` are independent. **You cannot fix this with `ANALYZE`.** The
useful part of the estimate is the 4.1M rows the index was going to scan, and that number
is right.

**6. Why the `customer_id` index was not used** — `Index Scan using
orders_customer_id_idx` would find every order that customer has ever placed (say 40,000 of
them across years), then filter by date and sort by date. That is 40,000 heap fetches plus a
sort, and the planner correctly costed it as worse than 4.1M sequential-ish index entries.
Neither single-column index can serve this query. That is the real finding.

**7. Why it is "sometimes 2 seconds"** — the buffers are mostly `hit`, so the timing depends
on how much of the index and heap is in cache, which depends on what else is running. The
variation is a cache-pressure symptom, not evidence of a second plan.

### The fix

The query's predicate is `customer_id = <equality>` AND `created_at >= <range>`, and it
orders by `created_at DESC`. From the playbook: **equality columns first, then range
columns, then the sort direction.**

```sql
CREATE INDEX CONCURRENTLY idx_orders_customer_created
  ON public.orders (customer_id, created_at DESC);
```

One index serves all three parts: it seeks straight to the customer, the second column is
already in `created_at DESC` order so the `ORDER BY` needs no sort, and the `LIMIT 20` can
stop after the first 20 index entries.

### The new plan

```
Limit  (cost=0.43..8.94 rows=20 width=44) (actual time=0.041..0.044 rows=0 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using idx_orders_customer_created on orders
        (cost=0.43..8.94 rows=20 width=44) (actual time=0.040..0.040 rows=0 loops=1)
        Index Cond: ((customer_id = 48213) AND (created_at >= (now() - '30 days'::interval)))
        Buffers: shared hit=4
Planning Time: 0.212 ms
Execution Time: 0.068 ms
```

**1838 ms → 0.068 ms.** 490,242 buffers → 4 buffers (**3.7 GB → 32 kB**). And note there is
no `Filter:` line at all — both conditions are in `Index Cond`, meaning the index excluded
everything without a single wasted heap fetch.

The `rows=0` result is unchanged. The query was always returning nothing; the 1.8 seconds
was entirely the cost of proving that. This is worth pausing on: **a query that returns no
rows can be the most expensive query in your system**, and no amount of "but it's not
returning anything" reasoning will show up in a per-row metric.

### What to verify before shipping that index

- Cost: `created_at DESC` matches the `ORDER BY`, but a `DESC` index is not usable for a
  plain `ORDER BY created_at ASC`. Check whether any query needs the ascending order; if
  one does, an ascending composite still serves the equality+range seek and only costs a
  sort for the ascending case.
- Write cost: one more index on `orders` means every insert and every update touching
  `customer_id` or `created_at` maintains it. `sql/04-index-usage.sql` Q4.5 shows whether
  that matters for this table.
- Redundancy: does `orders_customer_id_idx` now become a redundant prefix of the new index?
  Run `sql/04-index-usage.sql` Q4.2. If so, drop it *after* confirming the new index gets
  used.

---

## 9. Reading `Sort`, `Aggregate`, `Materialize` and friends

- **`Sort`** — `Sort Method: quicksort` = in memory. `top-N heapsort` = a `LIMIT`-bounded
  sort, cheap. `external merge  Disk: NkB` = it spilled to a temp file. Spilling is the
  signal to look at `work_mem` **or** at whether an index could provide the order for free.
  An index that supplies `ORDER BY` is usually better than more memory.
- **`Aggregate`** — `GroupAggregate` requires sorted input (often fed by a sort or an
  index). `HashAggregate` builds a hash table; watch `Batches` and `Memory Usage` for
  spills — a high-cardinality `GROUP BY` that spills is a common hidden cost.
- **`Materialize`** — the inner side of a nested loop being buffered. Harmless, and a sign
  the planner is avoiding repeated work.
- **`Memoize`** (PostgreSQL 14+) — a cache in front of a nested-loop inner side. Good sign:
  it means repeated lookups are being served from memory. Watch `Hits` vs `Misses`; a very
  low hit ratio means the cache is not helping.
- **`Gather` / `Gather Merge`** — the parallel executor collecting worker output. `Gather
  Merge` preserves order and implies each worker sorted its share, which is often a hidden
  cost. Lines beginning `Workers Planned: N` tell you how much parallelism was available;
  `Workers Launched: M < N` means workers were not available at runtime.
- **`JIT`** — if you see a `JIT:` section with high `Timing` or `Inlining` cost on a
  *fast* query, JIT compilation is costing more than it saves. That is common on short
  queries; `jit = off` for the role, or raising `jit_above_cost`, fixes it.
- **`Planning Time`** — if planning is a large share of total time, the problem is
  planning, not execution. Many partitions or thousands of joins make planning expensive,
  and no index helps. See `sql/03-slow-queries.sql` Q3.8.
- **`Execution Time`** — excludes network transfer to the client and client-side
  processing. If the plan says 5 ms and the user waits 800 ms, the database is not the
  bottleneck. Measure the real end-to-end time before continuing to tune.

---

## 10. A short checklist

When you look at a plan, in this order:

1. **Are the estimates close to the actuals** at each scan and join node? If not, stop and
   fix statistics (`ANALYZE`, extended statistics, `SET STATISTICS`) — an index will not
   help.
2. **Which node holds the buffers?** That is where the work is, regardless of where the
   time appears to be.
3. **Is there a `Rows Removed by Filter` far larger than the rows returned?** That is a
   missing or non-selective access path.
4. **Are the loops high on an inner node?** Nested loop over a misestimate.
5. **Is anything spilling?** `Sort Method: external merge`, `Batches > 1`,
   `temp read/written` — that is `work_mem`, or an index that removes the sort.
6. **Does the timing actually explain the user-visible slowness?** If execution is 5 ms and
   the request is 900 ms, you are optimising the wrong layer.
7. **Verify the fix against this plan.** Re-run it. Compare buffer counts, not just
   timings. Buffer counts are the honest measurement.

And the meta-rule: **change one thing at a time, and re-read the plan.** Two simultaneous
changes produce a plan you cannot interpret and a lesson you cannot reuse.
