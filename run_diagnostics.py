#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_diagnostics.py - PostgreSQL Performance Toolkit diagnostic runner.

Connects to a PostgreSQL database using DATABASE_URL, runs the read-only
queries from the sql/ directory plus a set of threshold-based checks, and
prints a PRIORITISED findings report: for every problem it found, the
evidence, the likely impact, and the concrete suggested action.

Everything this script executes is read-only. That is not a promise in a
comment - the session is switched to `default_transaction_read_only = on`
before any diagnostic runs, every statement is given a statement_timeout,
and any statement in sql/ that is not a SELECT/WITH/VALUES/SHOW/EXPLAIN is
refused rather than executed. Nothing here writes to your database.

Exit codes:
    0   report produced, no priority-1 findings
    1   report produced, at least one priority-1 finding
    2   could not run (no DATABASE_URL, no driver, could not connect)

Usage:
    export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
    python3 run_diagnostics.py
    python3 run_diagnostics.py --help
    python3 run_diagnostics.py --list-checks
    python3 run_diagnostics.py --self-test        # no database needed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Set, Tuple)

PROGRAM = "run_diagnostics.py"
VERSION = "1.0"
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_CONNECT_TIMEOUT_S = 10
MAX_EVIDENCE_LINES = 6

# Directory containing the .sql files, relative to this script.
SQL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql")


# =====================================================================
# Small helpers
# =====================================================================

def num(value: Any, default: float = 0.0) -> float:
    """Coerce a database value to float, tolerating None / Decimal / str."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def opt_num(value: Any) -> Optional[float]:
    """Like num(), but returns None for NULL so callers can distinguish."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "?"
    step = 1024.0
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < step or unit == "TB":
            if unit == "B":
                return "%d %s" % (int(n), unit)
            return "%.1f %s" % (n, unit)
        n /= step
    return "%.1f TB" % n


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = float(seconds)
    if seconds < 1:
        return "%.0f ms" % (seconds * 1000)
    if seconds < 120:
        return "%.0f s" % seconds
    if seconds < 7200:
        return "%.1f min" % (seconds / 60.0)
    if seconds < 172800:
        return "%.1f h" % (seconds / 3600.0)
    return "%.1f days" % (seconds / 86400.0)


def jsonable(value: Any) -> Any:
    """Convert a database value into something json.dumps can serialise."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:
            return repr(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


# =====================================================================
# Readable failures instead of raw tracebacks
# =====================================================================

SQLSTATE_HINTS: Dict[str, str] = {
    "28P01": "Authentication failed. Check the user and password inside DATABASE_URL, "
             "and remember that special characters in a password must be "
             "percent-encoded (for example @ becomes %40).",
    "28000": "The server refused the connection based on pg_hba.conf. Check which "
             "host and database this user is allowed to connect from.",
    "3D000": "That database does not exist on this server. Run \\l in psql, or fix "
             "the path component of DATABASE_URL.",
    "42P01": "A relation does not exist. On older PostgreSQL a view or catalog used by "
             "this query is missing; see the version notes in the relevant sql/ file.",
    "42703": "A column does not exist. This is almost always a PostgreSQL version "
             "difference - the column was added or renamed in a later release.",
    "42883": "A function does not exist. Either the PostgreSQL version is older than the "
             "function, or an extension that provides it is not installed.",
    "42501": "Permission denied. The role needs read access to the pg_stat_* views. On "
             "PostgreSQL 10+ grant pg_monitor:  GRANT pg_monitor TO <role>;",
    "55000": "The server is in recovery or shutting down, so this statistics view is "
             "not available right now.",
    "57014": "The statement was cancelled because it exceeded the statement_timeout. "
             "Re-run with --timeout 120000, or run it during a quieter period.",
    "53300": "Too many connections - the server refused a new one. Set max_connections or "
             "put a pooler in front. Note that this is itself evidence for the "
             "'connection pressure' finding.",
    "53400": "A configuration limit was exceeded (for example max_connections).",
    "08P01": "Protocol-level failure. Usually a mismatched driver version or a pooler in "
             "front of PostgreSQL that does not speak the protocol the driver expects.",
    "08006": "The connection failed or dropped mid-query. Check network reachability and "
             "whether a firewall or pooler closed idle connections.",
    "08001": "Could not establish a connection at all. Check host, port and that the "
             "server is listening on that interface.",
    "25P02": "The current transaction is aborted. This script wraps each statement "
             "individually, so this should not happen - please report it.",
    "57P03": "The server is starting up and not accepting connections yet.",
    "XX000": "An internal error. This is a PostgreSQL bug or a corrupted catalog - "
             "nothing this script did.",
}


def sqlstate_of(exc: BaseException) -> Optional[str]:
    """Pull the SQLSTATE out of either psycopg (v3) or psycopg2 exceptions."""
    for attr in ("sqlstate", "pgcode"):
        code = getattr(exc, attr, None)
        if code:
            return str(code)
    # psycopg2 sometimes nests the real error
    diag = getattr(exc, "diag", None)
    code = getattr(diag, "sqlstate", None) if diag is not None else None
    return str(code) if code else None


def friendly_error(exc: BaseException, context: str = "") -> str:
    """Turn a driver exception into two readable lines with a hint."""
    code = sqlstate_of(exc)
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    lines = []
    prefix = ("%s: " % context) if context else ""
    lines.append("%s%s%s" % (prefix, ("[%s] " % code) if code else "", message))
    hint = SQLSTATE_HINTS.get(code or "")
    if hint:
        lines.append("  hint: " + hint)
    elif "could not connect" in message.lower() or "connection refused" in message.lower():
        lines.append("  hint: nothing is listening on that host:port, or a firewall dropped "
                     "the connection. Verify with `pg_isready -d \"$DATABASE_URL\"`.")
    return "\n".join(lines)


class SetupError(Exception):
    """Raised when the script cannot start at all (no DSN, no driver, no connection)."""


class SplitError(Exception):
    """Raised when a .sql file cannot be split into statements."""


# =====================================================================
# Driver loading and a thin database wrapper
# =====================================================================

def load_driver() -> Tuple[Optional[str], Any]:
    """Return (name, module) for psycopg 3 if present, else psycopg2, else (None, None)."""
    try:
        import psycopg  # type: ignore
        return "psycopg", psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore
        return "psycopg2", psycopg2
    except ImportError:
        return None, None


class Db:
    """Minimal query wrapper. Identical API over psycopg 3 and psycopg2."""

    def __init__(self, conn: Any, driver: str, dsn: str):
        self.conn = conn
        self.driver = driver
        self.dsn = dsn
        self.notes: List[str] = []

    # -- lifecycle -----------------------------------------------------

    @classmethod
    def connect(cls, dsn: str, timeout_ms: int, connect_timeout_s: int) -> "Db":
        name, module = load_driver()
        if module is None:
            raise SetupError(
                "No PostgreSQL driver found.\n"
                "  This script needs one of: psycopg (v3) or psycopg2.\n"
                "  Install one of them:\n"
                "      pip install 'psycopg[binary]'\n"
                "      # or\n"
                "      pip install psycopg2-binary\n"
                "  Debian/Ubuntu system packages:\n"
                "      apt install python3-psycopg2\n"
                "  You can still see the findings engine work with no database:\n"
                "      python3 %s --self-test" % PROGRAM
            )

        try:
            conn = module.connect(dsn, connect_timeout=connect_timeout_s)
        except TypeError:
            # Very old drivers do not accept connect_timeout as a keyword.
            conn = module.connect(dsn)
        except Exception as exc:  # noqa: BLE001 - we re-raise as SetupError
            raise SetupError("Could not connect to PostgreSQL.\n  " +
                             friendly_error(exc).replace("\n", "\n  "))

        db = cls(conn, name, dsn)
        db._harden(timeout_ms)
        return db

    def _harden(self, timeout_ms: int) -> None:
        """Put the session into a state where it physically cannot mutate data."""
        try:
            self.conn.autocommit = True
        except Exception:
            pass
        for statement, label in (
            ("SET default_transaction_read_only = on", "read-only mode"),
            ("SET statement_timeout = %d" % int(timeout_ms), "statement timeout"),
            ("SET lock_timeout = '5s'", "lock timeout"),
            ("SET idle_in_transaction_session_timeout = '30s'", "idle timeout"),
            ("SET application_name = 'pg_perf_toolkit_diagnostics'", "application name"),
        ):
            try:
                self._raw(statement)
            except Exception as exc:  # noqa: BLE001
                self.notes.append("could not set %s: %s" % (label, friendly_error(exc)))

    # -- querying ------------------------------------------------------

    def _raw(self, sql: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def query(self, sql: str) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            if cur.description is None:
                return []
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def scalar(self, sql: str) -> Any:
        rows = self.query(sql)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # -- identity (for the report header) ------------------------------

    def describe_target(self) -> Dict[str, str]:
        info = {"database": "?", "server": "?", "version_num": "0",
                "host": "?", "port": "?", "user": "?", "uptime": "?"}
        try:
            row = self.query(
                "SELECT current_database() AS database, version() AS server, "
                "       current_setting('server_version_num') AS version_num, "
                "       COALESCE(inet_server_addr()::text, 'local') AS host, "
                "       COALESCE(inet_server_port()::text, '-') AS port, "
                "       current_user AS \"user\", "
                "       (now() - pg_postmaster_start_time())::text AS uptime"
            )[0]
            for key in info:
                if row.get(key) is not None:
                    info[key] = str(row[key])
        except Exception as exc:  # noqa: BLE001
            info["server"] = "unavailable (%s)" % friendly_error(exc).splitlines()[0]
        return info


# =====================================================================
# SQL statement splitting (used to run the shipped sql/ files safely)
# =====================================================================

_DOLLAR_RE = re.compile(r"\$[A-Za-z_0-9]*\$")
_READ_ONLY_START = re.compile(r"^\s*\(?\s*(select|with|values|table|show|explain)\b", re.IGNORECASE)


def split_statements(text: str) -> List[Tuple[int, str]]:
    """Split a SQL script into (starting_line, statement) pairs.

    Handles line comments, nested block comments, single-quoted strings with
    doubled quotes, double-quoted identifiers and dollar-quoted bodies. This
    is a small parser on purpose: it must not be fooled by the contents of
    the strings inside the toolkit's own queries.
    """
    out: List[Tuple[int, str]] = []
    buf: List[str] = []
    line = 1
    start_line: Optional[int] = None
    i = 0
    n = len(text)

    while i < n:
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue

        if text.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    if text[i] == "\n":
                        line += 1
                    i += 1
            buf.append(" ")
            continue

        m = _DOLLAR_RE.match(text, i)
        if m:
            tag = m.group(0)
            end = text.find(tag, i + len(tag))
            if end < 0:
                raise SplitError("unterminated dollar-quoted string at line %d" % line)
            if start_line is None:
                start_line = line
            chunk = text[i:end + len(tag)]
            buf.append(chunk)
            line += chunk.count("\n")
            i = end + len(tag)
            continue

        ch = text[i]

        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        j += 2
                        continue
                    j += 1
                    break
                if text[j] == "\n":
                    line += 1
                j += 1
            if start_line is None:
                start_line = line
            buf.append(text[i:j])
            i = j
            continue

        if ch == ";":
            statement = "".join(buf).strip()
            if statement and start_line is not None:
                out.append((start_line, statement))
            buf = []
            start_line = None
            i += 1
            continue

        if ch == "\n":
            line += 1
        if start_line is None and not ch.isspace():
            start_line = line
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail and start_line is not None:
        out.append((start_line, tail))
    return out


def is_read_only(statement: str) -> bool:
    """True if the statement is one this script is willing to execute."""
    return bool(_READ_ONLY_START.match(statement))


# =====================================================================
# Findings
# =====================================================================

PRIORITY_LABELS = {
    1: "P1 - ACT NOW",
    2: "P2 - FIX THIS WEEK",
    3: "P3 - HYGIENE / WORTH DOING",
}


@dataclass
class Finding:
    priority: int
    key: str
    title: str
    evidence: List[str]
    impact: str
    action: str
    source: str


@dataclass
class Check:
    key: str
    title: str
    sql: str
    fn: Callable[[List[Dict[str, Any]]], List[Finding]]
    source: str
    requires_pgss: bool = False
    min_version_num: int = 0
    note: str = ""


@dataclass
class CheckResult:
    check: Check
    rows: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    skipped: Optional[str] = None
    error: Optional[str] = None


@dataclass
class FileResult:
    path: str
    statements_total: int = 0
    statements_ok: int = 0
    rows_returned: int = 0
    errors: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    raw_output: List[Tuple[int, str, List[Dict[str, Any]]]] = field(default_factory=list)


# =====================================================================
# The checks. Each one answers exactly one question.
# =====================================================================

def _bullet(rows: Sequence[str]) -> List[str]:
    return list(rows)[:MAX_EVIDENCE_LINES] + (
        ["... and %d more" % (len(rows) - MAX_EVIDENCE_LINES)]
        if len(rows) > MAX_EVIDENCE_LINES else []
    )


def check_connections(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    r = rows[0]
    total = int(num(r.get("total")))
    maximum = int(num(r.get("max_connections"), 1)) or 1
    pct = 100.0 * total / maximum
    idle_txn = int(num(r.get("idle_in_transaction")))
    if pct < 70 and idle_txn == 0:
        return []
    evidence = [
        "%d of %d connection slots in use (%.1f%%)" % (total, maximum, pct),
        "active=%d idle=%d idle-in-transaction=%d waiting-on-locks=%d" % (
            int(num(r.get("active"))), int(num(r.get("idle"))), idle_txn,
            int(num(r.get("waiting_on_locks")))),
    ]
    priority = 1 if pct >= 90 else 2
    impact = (
        "Connection slots are a hard limit. At 100% new connections are refused "
        "outright, which surfaces as 'FATAL: sorry, too many clients already'. "
        "Each backend also costs 5-10 MB of memory even when idle, so a high "
        "connection count raises memory pressure long before it hits the limit."
    )
    action = (
        "Put a connection pooler in transaction mode (PgBouncer or the pooler your "
        "platform provides) in front of PostgreSQL and cap the pool at roughly "
        "(2 x CPU cores) + effective_spindle_count. Do not raise max_connections as "
        "the first move: it multiplies memory use per backend and makes every "
        "autovacuum worker's shared cost budget smaller. If idle-in-transaction "
        "sessions are a large share of these connections, treat that as its own "
        "finding - see the idle-in-transaction check."
    )
    return [Finding(priority, "connection_pressure",
                    "Connection pool is %.0f%% full" % pct,
                    evidence, impact, action,
                    "sql/05-connections-and-locks.sql (Q5.0)")]


def check_idle_in_transaction(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    worst = max(num(r.get("xact_seconds")) for r in rows)
    evidence = []
    for r in rows:
        evidence.append(
            "pid %s user=%s app=%s open for %s, idle for %s%s" % (
                r.get("pid"), r.get("usename"), r.get("application_name") or "-",
                human_duration(opt_num(r.get("xact_seconds"))),
                human_duration(opt_num(r.get("idle_seconds"))),
                (", xmin age %s" % r.get("xmin_age")) if r.get("xmin_age") is not None else ""))
    priority = 1 if worst >= 300 else 2
    impact = (
        "An open transaction pins backend_xmin. VACUUM cannot remove any row version "
        "created after that snapshot, in ANY table - not just the ones this session "
        "touched. So a single idle-in-transaction session leaks dead tuples across the "
        "whole database, and it holds every lock it has taken, which blocks DDL and can "
        "block unrelated writers."
    )
    action = (
        "1. Find the code path: the last_query column is what the session did before it "
        "went idle. A connection checked out of a pool with an implicit BEGIN and never "
        "committed, or an external HTTP call made inside a transaction, is the usual "
        "cause.\n"
        "2. Fix the code: commit or roll back, and never hold a transaction open across "
        "a network round trip.\n"
        "3. Interim database-side guard (PostgreSQL 9.6+), per role so you do not kill "
        "legitimate long interactive transactions:\n"
        "     ALTER ROLE app_user SET idle_in_transaction_session_timeout = '5min';\n"
        "   That makes the server terminate the session, which is a blunt but effective "
        "backstop. It does not fix the bug."
    )
    return [Finding(priority, "idle_in_transaction",
                    "%d session(s) idle in transaction, worst %s" % (
                        len(rows), human_duration(worst)),
                    _bullet(evidence), impact, action,
                    "sql/05-connections-and-locks.sql (Q5.2)")]


def check_blocking(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    blockers: Dict[Any, int] = {}
    for r in rows:
        blockers[r.get("blocker_pid")] = blockers.get(r.get("blocker_pid"), 0) + 1
    root_pid, root_count = max(blockers.items(), key=lambda kv: kv[1])
    worst = max(num(r.get("blocked_seconds")) for r in rows)
    evidence = ["%d blocked session(s) across %d blocker(s)" % (len(rows), len(blockers)),
                "worst wait: %s" % human_duration(worst),
                "biggest blocker: pid %s blocks %d session(s)" % (root_pid, root_count)]
    for r in rows[:3]:
        evidence.append("pid %s waits on pid %s (%s) - blocked query: %s" % (
            r.get("blocked_pid"), r.get("blocker_pid"),
            r.get("wait_event") or "lock", (r.get("blocked_query") or "")[:90]))
    return [Finding(
        1, "blocking",
        "Lock contention: %d session(s) blocked right now" % len(rows),
        _bullet(evidence),
        "Blocked sessions hold connections and do no work, so a lock pile-up "
        "multiplies load exactly when the system is least able to absorb it. If the "
        "blocker is a DDL statement waiting for an AccessExclusiveLock, every later "
        "reader queues behind it - one ALTER TABLE can stall an entire table.",
        "Act on the ROOT blocker, not a victim: cancelling a leaf just promotes the "
        "next waiter. First choice is always:\n"
        "     SELECT pg_cancel_backend(%s);   -- cancels the query, keeps the session\n"
        "Use pg_terminate_backend() only if the session ignores cancellation or is "
        "abandoned. Then remove the cause: run DDL with `SET lock_timeout = '3s'` so it "
        "fails fast instead of queueing, make long UPDATEs batched, and check that "
        "nothing is holding a transaction open (see the idle-in-transaction finding)."
        % root_pid,
        "sql/05-connections-and-locks.sql (Q5.4/Q5.5/Q5.6)")]


def check_long_running_queries(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    worst = max(num(r.get("query_seconds")) for r in rows)
    evidence = []
    for r in rows:
        evidence.append("pid %s runs for %s (user=%s, app=%s, wait=%s) %s" % (
            r.get("pid"), human_duration(opt_num(r.get("query_seconds"))),
            r.get("usename"), r.get("application_name") or "-",
            r.get("wait_event") or "none", (r.get("query") or "")[:80]))
    priority = 1 if worst >= 300 else 2
    return [Finding(
        priority, "long_running_queries",
        "%d active query(ies) running longer than 10s, worst %s" % (
            len(rows), human_duration(worst)),
        _bullet(evidence),
        "A long-running statement holds its snapshot, so vacuum cannot clean up behind "
        "it, and it occupies a connection for its whole duration. If it is a read, it "
        "may also be blocking DDL indirectly by holding an AccessShareLock.",
        "Get the plan before you do anything else:\n"
        "     EXPLAIN (ANALYZE, BUFFERS) <the query>;\n"
        "Then read EXPLAIN-GUIDE.md in this pack. If the query must run long, do not "
        "kill it - set a per-statement bound instead:\n"
        "     SET statement_timeout = '30s';\n"
        "applied to the role that issues it, so a stuck query fails fast rather than "
        "pinning a connection for an hour. Adding an index is only the right answer "
        "when the plan shows a sequential scan or a bad row estimate; see "
        "INDEXING-PLAYBOOK.md.",
        "sql/05-connections-and-locks.sql (Q5.1)")]


def check_xmin_horizon(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    r = rows[0]
    oldest = int(num(r.get("oldest_xmin_age")))
    holders = int(num(r.get("holders")))
    if not holders or oldest == 0:
        return []
    priority = 1 if oldest >= 10_000_000 else (2 if oldest >= 1_000_000 else 3)
    evidence = [
        "oldest backend_xmin is %s transactions old" % "{:,}".format(oldest),
        "%d session(s) currently hold a backend_xmin" % holders,
    ]
    if r.get("oldest_xact_seconds") is not None:
        evidence.append("oldest open transaction: %s" % human_duration(opt_num(r.get("oldest_xact_seconds"))))
    return [Finding(
        priority, "xmin_horizon",
        "VACUUM horizon pinned %s transactions back" % "{:,}".format(oldest),
        evidence,
        "VACUUM can only remove row versions that are invisible to EVERY open "
        "snapshot. While an old backend_xmin exists, dead tuples accumulate in every "
        "table in the database, autovacuum runs and reclaims almost nothing, and tables "
        "grow without bound. This is the most common reason for 'autovacuum is running "
        "but bloat keeps growing'.",
        "1. Identify the holder: sql/05-connections-and-locks.sql Q5.7 lists sessions "
        "with their transaction age, and the replication-slot query in the same file "
        "lists abandoned slots (a slot that is inactive:false with an old xmin pins the "
        "horizon just as hard and survives the client disappearing).\n"
        "2. An idle-in-transaction session -> see the idle-in-transaction finding.\n"
        "3. An INACTIVE replication slot -> resolve it now:\n"
        "     SELECT pg_drop_replication_slot('slot_name');\n"
        "   Confirm with whoever owns the replica first: dropping a slot that a "
        "standby still needs breaks its ability to reconnect without a rebuild.\n"
        "4. Do not tune autovacuum scale factors until this is resolved. It will not help.",
        "sql/05-connections-and-locks.sql (Q5.7)")]


def check_replication_slots(rows: List[Dict[str, Any]]) -> List[Finding]:
    bad = []
    for r in rows:
        xmin_age = num(r.get("xmin_age"))
        cat_age = num(r.get("catalog_xmin_age"))
        if (r.get("active") in (False, "f", "false") and max(xmin_age, cat_age) > 1_000_000) \
                or max(xmin_age, cat_age) > 50_000_000:
            bad.append(r)
    if not bad:
        return []
    evidence = []
    for r in bad:
        evidence.append("slot %s type=%s active=%s xmin_age=%s catalog_xmin_age=%s" % (
            r.get("slot_name"), r.get("slot_type"), r.get("active"),
            r.get("xmin_age"), r.get("catalog_xmin_age")))
    return [Finding(
        1 if any(r.get("active") in (False, "f", "false") for r in bad) else 2,
        "replication_slots",
        "%d replication slot(s) holding back the vacuum horizon" % len(bad),
        _bullet(evidence),
        "An inactive slot still pins xmin, so it blocks dead-tuple removal database-wide "
        "even though nothing is connected to it. Slots are also not removed "
        "automatically - they persist until dropped, which is why an abandoned slot from "
        "a decommissioned replica is a classic cause of runaway bloat on a primary.",
        "For each slot: confirm whether a consumer still exists.\n"
        "  * Consumer gone -> SELECT pg_drop_replication_slot('slot_name');\n"
        "  * Consumer exists but is far behind -> restore it and let it catch up, or "
        "rebuild it (pg_basebackup) and drop the old slot.\n"
        "Also set a safety net so this cannot recur silently:\n"
        "     ALTER SYSTEM SET max_slot_wal_keep_size = '10GB';   -- PostgreSQL 13+\n"
        "     SELECT pg_reload_conf();\n"
        "That limits how much WAL a slot may retain, so a dead consumer costs you disk "
        "rather than the whole database.",
        "sql/05-connections-and-locks.sql (Q5.7)")]


def check_wraparound(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    r = rows[0]
    pct = num(r.get("pct_to_forced_freeze"))
    age_ticks = int(num(r.get("xid_age")))
    if pct < 25:
        return []
    priority = 1 if pct >= 60 else 2
    return [Finding(
        priority, "wraparound",
        "Transaction ID wraparound is %.1f%% of the way to a forced freeze" % pct,
        ["database %s: datfrozenxid age %s transactions" % (
            r.get("datname"), "{:,}".format(age_ticks)),
         "autovacuum_freeze_max_age = %s" % r.get("freeze_max_age"),
         "at 100% of that value an aggressive anti-wraparound vacuum is forced "
         "regardless of per-table autovacuum settings",
         "at 2^31 (~2.1 billion) the server REFUSES ALL NEW WRITES"],
        "This is the only finding in this report that can take a database offline "
        "permanently. Long before that, an unexpected forced freeze vacuum starts "
        "reading every page of the database, which is a multi-hour I/O event you did "
        "not schedule.",
        "Do not wait for the automatic freeze. Investigate why normal autovacuum has "
        "not been freezing:\n"
        "1. Check the vacuum horizon first (see the xmin horizon finding). If a session "
        "or an inactive replication slot pins xmin, freezing cannot advance and nothing "
        "else you do will help.\n"
        "2. Then run the freeze manually in a maintenance window:\n"
        "     VACUUM (FREEZE, VERBOSE) <the oldest table>;\n"
        "   VACUUM FREEZE takes an aggressive scan of the whole table. It does not block "
        "reads or writes, but it is I/O heavy.\n"
        "3. Lower autovacuum_freeze_max_age so it happens earlier and more often, when "
        "the tables are smaller, rather than as one giant event later:\n"
        "     ALTER SYSTEM SET autovacuum_freeze_max_age = 150000000;\n"
        "   (Default 200000000. Reloadable, no restart.)",
        "sql/07-vacuum-and-autovacuum.sql (Q7.5)")]


def check_autovacuum_disabled(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["%s.%s (dead tuples: %s, live rows: %s, reloptions: %s)" % (
        r.get("nspname"), r.get("relname"), r.get("n_dead_tup"),
        r.get("n_live_tup"), r.get("reloptions")) for r in rows]
    return [Finding(
        2, "autovacuum_disabled",
        "Autovacuum is disabled on %d table(s)" % len(rows),
        _bullet(evidence),
        "Nothing reclaims dead tuples on these tables, so they grow monotonically and "
        "every sequential scan gets slower forever. Statistics also stop being "
        "refreshed by autoanalyze, so the planner degrades too. Note that "
        "autovacuum_enabled=false does NOT stop the anti-wraparound freeze vacuum: "
        "that one ignores the setting, so you get the I/O spike without the routine "
        "cleanup.",
        "Someone disabled it for a reason - usually a latency spike during a large "
        "vacuum. Reproduce the benefit without switching it off: keep autovacuum on and "
        "bound the cost instead.\n"
        "     SET lock_timeout = '3s';\n"
        "     ALTER TABLE public.<table> RESET (autovacuum_enabled);\n"
        "     ALTER TABLE public.<table> SET (\n"
        "         autovacuum_vacuum_scale_factor = 0.05,\n"
        "         autovacuum_vacuum_cost_delay   = 10,     -- ms, gentler than default\n"
        "         autovacuum_vacuum_cost_limit   = 200);\n"
        "The ALTER takes ACCESS EXCLUSIVE for a moment; keep the lock_timeout so it "
        "fails fast rather than queueing behind a long transaction. Until then, run "
        "VACUUM manually on a schedule.",
        "sql/07-vacuum-and-autovacuum.sql (Q7.8)")]


def check_dead_tuples(rows: List[Dict[str, Any]]) -> List[Finding]:
    bad = [r for r in rows
           if num(r.get("dead_pct")) >= 15 and num(r.get("n_dead_tup")) >= 10_000]
    if not bad:
        return []
    evidence = []
    for r in bad:
        line = "%s.%s: %s dead / %s live (%.1f%% dead), %s" % (
            r.get("schemaname"), r.get("relname"), r.get("n_dead_tup"),
            r.get("n_live_tup"), num(r.get("dead_pct")), r.get("total_size"))
        if opt_num(r.get("hot_update_pct")) is not None and num(r.get("hot_update_pct")) < 70:
            line += ", HOT updates only %.0f%%" % num(r.get("hot_update_pct"))
        if r.get("last_autovacuum") is None:
            line += ", never autovacuumed"
        evidence.append(line)
    worst = max(num(r.get("dead_pct")) for r in bad)
    return [Finding(
        2 if worst < 40 else 1, "dead_tuples",
        "%d table(s) carry 15%% or more dead tuples" % len(bad),
        _bullet(evidence),
        "Dead tuples are row versions that are still on disk but invisible. They are "
        "not free: they occupy pages, so every sequential scan reads more pages than "
        "the live data needs, and every index scan touches heap pages that are mostly "
        "dead. A 30% dead table does roughly 40% more I/O than the same data freshly "
        "vacuumed.",
        "1. Establish WHY they are accumulating before changing any setting. The two "
        "causes have different fixes: a pinned vacuum horizon (see the xmin horizon "
        "finding - fix this first or nothing else works) or autovacuum misfiring/throttled.\n"
        "2. Fix the horizon or the throttling, then run one manual pass to catch up:\n"
        "     VACUUM (VERBOSE, ANALYZE) public.<table>;\n"
        "   This does NOT block reads or writes and is safe under load; it does compete "
        "for I/O, so prefer a quiet period on a busy primary.\n"
        "3. For the biggest, hottest tables, lower the scale factor so autovacuum fires "
        "sooner and each run is smaller:\n"
        "     SET lock_timeout = '3s';\n"
        "     ALTER TABLE public.<table> SET (\n"
        "         autovacuum_vacuum_scale_factor = 0.02,   -- fire at 2% dead, not 20%\n"
        "         autovacuum_vacuum_cost_limit   = 1000);\n"
        "   A per-table cost limit uses its own budget instead of the shared one, so "
        "this does not slow autovacuum down for every other table.\n"
        "4. If HOT updates are below ~70% on an update-heavy table, low fillfactor is "
        "forcing full index maintenance on every update:\n"
        "     ALTER TABLE public.<table> SET (fillfactor = 85);\n"
        "   That only affects newly written pages - existing pages keep their layout "
        "until rewritten.",
        "sql/01-table-sizes-and-bloat.sql (Q1.2)")]


def check_hot_updates(rows: List[Dict[str, Any]]) -> List[Finding]:
    bad = [r for r in rows if num(r.get("n_tup_upd")) >= 100_000
           and num(r.get("hot_update_pct")) < 50]
    if not bad:
        return []
    evidence = ["%s.%s: %s updates, only %.1f%% HOT, %s rows, fillfactor %s" % (
        r.get("schemaname"), r.get("relname"), r.get("n_tup_upd"),
        num(r.get("hot_update_pct")), r.get("n_live_tup"), r.get("fillfactor"))
        for r in bad]
    return [Finding(
        3, "hot_updates",
        "%d update-heavy table(s) get few HOT updates" % len(bad),
        _bullet(evidence),
        "A HOT update keeps the new row version on the same page and skips index "
        "maintenance entirely. When HOT cannot be used, every update has to insert a "
        "new entry into every index on the table, which multiplies write amplification "
        "by the number of indexes and creates index bloat as well as heap bloat.",
        "HOT requires two things: no indexed column changed, and room on the same page. "
        "You can only control the second.\n"
        "1. Give pages room for a second version:\n"
        "     SET lock_timeout = '3s';\n"
        "     ALTER TABLE public.<table> SET (fillfactor = 85);\n"
        "   Applies to pages written after the change; a VACUUM will not rewrite "
        "existing pages, so expect the benefit to phase in.\n"
        "2. Check whether you are indexing a column that gets updated constantly. The "
        "classic offender is an index on `updated_at` or on the mutable `status` of a "
        "work queue. Run sql/04-index-usage.sql Q4.2 and Q4.5 on this table - dropping "
        "an index on a frequently-updated column is often worth more than the "
        "fillfactor change.\n"
        "3. If neither helps, confirm the table is not being updated by a statement "
        "that rewrites every column (for example an ORM saving the whole row on every "
        "change). That forces a new tuple regardless of free space.",
        "sql/01-table-sizes-and-bloat.sql (Q1.2)")]


def check_stale_statistics(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["%s.%s: %s rows changed since last analyze (%.1f%% of live rows), "
                "last autoanalyze: %s" % (
                    r.get("schemaname"), r.get("relname"), r.get("n_mod_since_analyze"),
                    num(r.get("pct_modified")), r.get("last_autoanalyze") or "never")
                for r in rows]
    return [Finding(
        2, "stale_statistics",
        "%d table(s) have significantly stale planner statistics" % len(rows),
        _bullet(evidence),
        "The planner chooses between a sequential scan and an index scan from row-count "
        "estimates. When those estimates are stale, it can pick a nested loop over 5 "
        "rows when the real answer is 500,000 rows - a plan that is orders of magnitude "
        "slower and that no index change will fix, because the index was never the "
        "problem.",
        "1. Refresh now, which is cheap and non-blocking (ANALYZE takes only a "
        "SHARE UPDATE EXCLUSIVE lock, so it does not block reads or writes):\n"
        "     ANALYZE public.<table>;\n"
        "   For a large table where a plain ANALYZE is too coarse:\n"
        "     ALTER TABLE public.<table> ALTER COLUMN <skewed_column> SET STATISTICS 1000;\n"
        "     ANALYZE public.<table>;\n"
        "   (Default is 100.) Raise it only for columns used in WHERE clauses whose "
        "values are unevenly distributed - a status column, a tenant_id, a nullable "
        "foreign key. Raising it on every column just slows down ANALYZE.\n"
        "2. Then make it stay fresh: the autoanalyze threshold is\n"
        "     autovacuum_analyze_threshold + autovacuum_analyze_scale_factor * reltuples\n"
        "   which is 50 + 10% of the table. On a 100M-row table that is 10M changes "
        "before the planner gets fresh numbers. Lower it for that table:\n"
        "     SET lock_timeout = '3s';\n"
        "     ALTER TABLE public.<table> SET (autovacuum_analyze_scale_factor = 0.02);\n"
        "3. Verify the effect with EXPLAIN (ANALYZE, BUFFERS) - see EXPLAIN-GUIDE.md.",
        "sql/07-vacuum-and-autovacuum.sql (Q7.3)")]


def check_seq_scan_candidates(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["%s.%s: %s sequential scans, %.0f rows read per scan, %.1f%% of the "
                "table per scan, %s, %s index scans" % (
                    r.get("schemaname"), r.get("relname"), r.get("seq_scan"),
                    num(r.get("avg_rows_per_scan")), num(r.get("pct_of_table_per_scan")),
                    r.get("heap_size"), r.get("idx_scan"))
                for r in rows]
    return [Finding(
        2, "seq_scan_candidates",
        "%d large table(s) sequentially scanned for a small fraction of their rows" % len(rows),
        _bullet(evidence),
        "Reading (say) 2% of a large table by sequential scan costs you 50x the pages a "
        "matching index would. This is the signature of a missing selective index - but "
        "it is only a signature, not proof: the next section explains what to verify "
        "before you write any DDL.",
        "DO NOT create an index from this list alone. The counters tell you where to "
        "look, not what to build.\n"
        "1. Find the actual query. Either read sql/03-slow-queries.sql (if "
        "pg_stat_statements is installed) or turn on slow-query logging temporarily:\n"
        "     ALTER SYSTEM SET log_min_duration_statement = '250ms';\n"
        "     SELECT pg_reload_conf();\n"
        "   (Reloadable, no restart. Remember to turn it back to -1.)\n"
        "2. Get the plan and confirm the scan is the problem rather than a symptom:\n"
        "     EXPLAIN (ANALYZE, BUFFERS) <the query>;\n"
        "   A sequential scan is the CORRECT plan when the query genuinely needs a "
        "large fraction of the table. If the plan shows a small `rows removed by "
        "filter`, you have a real missing index (see EXPLAIN-GUIDE.md).\n"
        "3. Then design the index from the query's WHERE and ORDER BY, following "
        "INDEXING-PLAYBOOK.md: equality columns first, then range columns, then INCLUDE "
        "the selected columns if the heap fetches dominate.\n"
        "4. Build it without blocking writes:\n"
        "     CREATE INDEX CONCURRENTLY idx_<table>_<cols> ON public.<table> (<cols>);\n"
        "   CONCURRENTLY cannot run inside a transaction block, and if it fails it "
        "leaves an INVALID index behind that still costs you writes - check with "
        "sql/04-index-usage.sql and drop it before retrying.",
        "sql/02-unused-and-missing-indexes.sql (Q2.5/Q2.6)")]


def check_unused_indexes(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    total_bytes = sum(num(r.get("index_bytes")) for r in rows)
    resets = {str(r.get("stats_reset")) for r in rows}
    evidence = ["%s.%s: %s scans, %s, table has %s write ops" % (
        r.get("schemaname"), r.get("relname"), r.get("idx_scan"),
        r.get("index_size"), r.get("table_writes")) for r in rows]
    evidence.append("total reclaimable: %s" % human_bytes(total_bytes))
    evidence.append("statistics last reset: %s" % ", ".join(sorted(resets)))
    return [Finding(
        3, "unused_indexes",
        "%d index(es) over 8 MB have never been scanned" % len(rows),
        _bullet(evidence),
        "Every index must be maintained on every INSERT and on every UPDATE that "
        "changes one of its columns. An index that is never read is pure write "
        "amplification plus disk, and it also makes VACUUM slower because vacuum has to "
        "sweep it too.",
        "Verify the numbers mean something before you drop anything:\n"
        "  * idx_scan counts scans since `stats_reset` above, NOT since the server "
        "started. If the reset was recent, or the server restarted, zero means nothing. "
        "One full business cycle is the minimum - a monthly report index reads zero for "
        "29 days.\n"
        "  * On a STANDBY these counters are always zero. Never drop an index based on "
        "a replica.\n"
        "  * Save the definition first so you can restore it:\n"
        "        SELECT pg_get_indexdef('<index>');\n"
        "Then, in a low-traffic window and OUTSIDE any transaction block:\n"
        "     DROP INDEX CONCURRENTLY public.<index_name>;\n"
        "DROP INDEX CONCURRENTLY does not block reads or writes. It cannot run inside a "
        "transaction, and it can leave an invalid index behind if it fails - check "
        "afterwards and clean up. Watch for one full cycle before dropping the next one.",
        "sql/02-unused-and-missing-indexes.sql (Q2.1/Q2.4)")]


def check_invalid_indexes(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["%s.%s on %s (valid=%s ready=%s live=%s)" % (
        r.get("nspname"), r.get("index_name"), r.get("table_name"),
        r.get("indisvalid"), r.get("indisready"), r.get("indislive")) for r in rows]
    return [Finding(
        2, "invalid_indexes",
        "%d invalid index(es) present - maintained on write, ignored by the planner" % len(rows),
        _bullet(evidence),
        "An index that is not `indisvalid` is invisible to the query planner, so it "
        "delivers zero read benefit - but it is still recorded in the catalog and kept "
        "up to date on writes in the cases where it is `indisready`. You are paying "
        "100% of the cost for 0% of the benefit. This normally happens when CREATE "
        "INDEX CONCURRENTLY fails part-way (a lock timeout, a cancelled statement, a "
        "deadlock) or REINDEX CONCURRENTLY is interrupted.",
        "1. Confirm it is genuinely invalid and not mid-build. On PostgreSQL 12+ you can "
        "see a build in progress in pg_stat_progress_create_index.\n"
        "2. If genuinely dead, remove it and rebuild, outside any transaction:\n"
        "     DROP INDEX CONCURRENTLY public.<index_name>;\n"
        "     CREATE INDEX CONCURRENTLY <same definition as before>;\n"
        "   Keep the definition from pg_get_indexdef() before dropping.\n"
        "3. Prevent the recurrence: CONCURRENTLY builds take longer than plain builds "
        "and can be interrupted. Give the session room - do not run them through a "
        "pooler with a short client timeout, and do not wrap them in a transaction.",
        "sql/04-index-usage.sql (Q4.1)")]


def check_duplicate_indexes(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    total = sum(num(r.get("index_bytes")) for r in rows)
    evidence = ["%s: drop %s (%s on %s) - %s, covered by %s on %s" % (
        r.get("table_name"), r.get("droppable_index"), r.get("droppable_size"),
        r.get("droppable_columns"), r.get("reason"), r.get("covering_index"),
        r.get("covering_columns")) for r in rows]
    evidence.append("reclaimable index space: %s" % human_bytes(total))
    n_exact = sum(1 for r in rows if r.get("reason") == "exact duplicate")
    return [Finding(
        2, "duplicate_indexes",
        "%d redundant index(es) found (%d exact duplicates)" % (len(rows), n_exact),
        _bullet(evidence),
        "A btree on (a) is fully served by a btree on (a, b) for any query that can use "
        "the leading column, so keeping both doubles the write cost of the first column "
        "and doubles the space, for no read benefit. Exact duplicates are worse: they "
        "cost the same on every write and there is no case where both are needed. They "
        "also make VACUUM slower on every run, permanently.",
        "1. Save both definitions before touching anything:\n"
        "     SELECT pg_get_indexdef('public.<index>');\n"
        "2. Confirm the droppable index really is unused - check idx_scan in "
        "sql/04-index-usage.sql Q4.1, and remember the stats-reset caveat.\n"
        "3. Drop outside any transaction block:\n"
        "     DROP INDEX CONCURRENTLY public.<droppable_index>;\n"
        "4. If it was a UNIQUE index, this query would not have reported it - uniqueness "
        "is a constraint, not an optimisation, and dropping it changes behaviour. If you "
        "believe a unique index is genuinely redundant, verify the same uniqueness is "
        "enforced by another constraint before doing anything.\n"
        "5. Prevent recurrence: ORM migration tools create these constantly, because "
        "each migration adds an index without checking for an equivalent existing one. "
        "Keep this query in CI against a staging copy of the schema.",
        "sql/04-index-usage.sql (Q4.2)")]


def check_cache_hit_tables(rows: List[Dict[str, Any]]) -> List[Finding]:
    bad = [r for r in rows
           if opt_num(r.get("heap_hit_pct")) is not None
           and num(r.get("heap_hit_pct")) < 95
           and num(r.get("total_misses")) > 20_000]
    if not bad:
        return []
    evidence = ["%s.%s: heap hit %.1f%%, index hit %.1f%%, %s read, %s" % (
        r.get("schemaname"), r.get("relname"), num(r.get("heap_hit_pct")),
        num(r.get("index_hit_pct")), r.get("total_misses"), r.get("total_size"))
        for r in bad]
    return [Finding(
        2, "cache_hit_tables",
        "%d table(s) read with a heap cache hit ratio below 95%%" % len(bad),
        _bullet(evidence),
        "Misses here mean shared_buffers did not hold the page. Whether that is slow "
        "depends on the OS page cache - a 'miss' PostgreSQL sees may still be a memory "
        "copy from the operating system, not a disk seek. So treat this as 'these "
        "tables do not fit in shared_buffers', which is a real signal about the working "
        "set, not automatically proof of a bottleneck.",
        "1. First check WHICH queries read these tables - the fix is usually fewer pages "
        "read, not more memory. Use sql/03-slow-queries.sql Q3.5 (cache-miss heavy "
        "statements) if pg_stat_statements is installed.\n"
        "2. Check the size ratio. sql/06-cache-hit-and-io.sql Q6.3 computes each table's "
        "size in units of shared_buffers. A table many times larger than shared_buffers "
        "cannot stay resident under mixed access, whatever you set.\n"
        "3. If the query reads the same small slice repeatedly, the answer is an index "
        "returning fewer pages (see INDEXING-PLAYBOOK.md).\n"
        "4. If the whole working set genuinely does not fit, the options in order of "
        "cheapness are: raise shared_buffers (restart required, and beyond roughly 8-16 "
        "GB on a large machine the OS page cache was already doing much of the work), "
        "partition the table so hot partitions stay resident, or move the hot data to a "
        "faster volume. Confirm with pg_stat_io on PostgreSQL 16+ or host-level disk "
        "latency before spending money.",
        "sql/06-cache-hit-and-io.sql (Q6.2/Q6.3)")]


def check_temp_files(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    r = rows[0]
    files = int(num(r.get("temp_files")))
    if files == 0:
        return []
    gb = num(r.get("temp_bytes")) / 1024.0 / 1024.0 / 1024.0
    priority = 2 if gb >= 10 or num(r.get("temp_bytes")) >= 1_000_000_000 else 3
    return [Finding(
        priority, "temp_files",
        "Database has written %s across %s temporary file(s)" % (
            r.get("temp_bytes"), "{:,}".format(files)),
        ["temp_files = %s, temp_bytes = %s (%.2f GB)" % (
            "{:,}".format(files), r.get("temp_bytes"), gb),
         "average %.2f MB per temp file" % num(r.get("avg_mb_per_temp_file")),
         "cache miss ratio on this database: %s%%" % r.get("cache_miss_pct")],
        "Temp files are the sort and hash spills that did not fit in work_mem. They are "
        "written to disk and thrown away, so the I/O is pure waste - and it is invisible "
        "in the cache hit ratio, which is why a database reading 99.9% from cache can "
        "still be I/O bound. On a container or cloud volume, temp file I/O is often the "
        "dominant cost.",
        "1. Find the statements responsible: sql/03-slow-queries.sql Q3.4 attributes "
        "temp_blks_written to individual queries (requires pg_stat_statements).\n"
        "2. Raise work_mem for the specific role or database that runs them - never "
        "globally:\n"
        "     ALTER ROLE app_user SET work_mem = '64MB';\n"
        "   work_mem is per sort/hash node PER CONNECTION. 100 connections x 4 "
        "concurrent sorts x 256 MB is 100 GB of potential allocation, which is how a "
        "work_mem increase turns into an OOM kill.\n"
        "3. Better than more memory: remove the sort. If the query has an ORDER BY or a "
        "hash join that an index could satisfy, an index removes the temp file entirely "
        "- see INDEXING-PLAYBOOK.md.\n"
        "4. If a hash join is spilling because the planner underestimated the row count, "
        "the fix is statistics, not memory - see the stale-statistics finding.",
        "sql/06-cache-hit-and-io.sql (Q6.7)")]


def check_database_stats(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    findings: List[Finding] = []
    r = rows[0]
    rollback = num(r.get("rollback_pct"))
    deadlocks = int(num(r.get("deadlocks")))
    if rollback >= 0.5:
        findings.append(Finding(
            3, "rollback_rate",
            "%.2f%% of transactions are rolling back" % rollback,
            ["xact_commit=%s xact_rollback=%s (%.3f%%)" % (
                r.get("xact_commit"), r.get("xact_rollback"), rollback)],
            "Every rollback throws away work already done and still writes the WAL for "
            "it. A high rollback rate is usually the application using exceptions for "
            "control flow - for example an INSERT that is expected to violate a unique "
            "constraint, then caught and retried.",
            "Look for the cause in the application, not the database: "
            "log_min_error_statement or the application error log will show the class of "
            "error. Common patterns worth changing:\n"
            "  * INSERT ... ON CONFLICT DO NOTHING instead of try/except on a unique "
            "violation.\n"
            "  * A pooler in transaction mode that recycles a connection mid-transaction.\n"
            "  * statement_timeout set too aggressively for legitimate long queries.",
            "sql/06-cache-hit-and-io.sql (Q6.1)"))
    if deadlocks > 0:
        findings.append(Finding(
            2, "deadlocks",
            "%s deadlock(s) recorded since stats reset" % "{:,}".format(deadlocks),
            ["deadlocks=%s since %s" % ("{:,}".format(deadlocks), r.get("stats_reset"))],
            "A deadlock costs one transaction plus the work it had already done, and it "
            "is a symptom of the application taking locks in an inconsistent order. "
            "No database setting prevents it - PostgreSQL cannot reorder your UPDATEs.",
            "1. Log them so you can see the pair of statements involved:\n"
            "     ALTER SYSTEM SET log_lock_waits = on;\n"
            "     ALTER SYSTEM SET deadlock_timeout = '1s';   -- default 1s, lower shows more\n"
            "     SELECT pg_reload_conf();\n"
            "2. Fix the ordering in the application: when a request must update several "
            "rows, always touch them in a deterministic order (for example ORDER BY id "
            "before updating), and shorten transactions so they hold fewer locks.\n"
            "3. If two statements must update the same rows in different orders, "
            "serialise them explicitly with `SELECT ... FOR UPDATE` in a consistent "
            "order at the start of the transaction.",
            "sql/06-cache-hit-and-io.sql (Q6.1)"))
    return findings


def check_pgss_top_total(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["queryid %s: %s calls, %.1f%% of database time, mean %.1f ms, "
                "max %.1f ms, %s rows/call" % (
                    r.get("queryid"), "{:,}".format(int(num(r.get("calls")))),
                    num(r.get("pct_of_total")), num(r.get("mean_exec_time")),
                    num(r.get("max_exec_time")),
                    ("%.1f" % num(r.get("rows_per_call"))) if r.get("rows_per_call") is not None else "?")
                for r in rows[:MAX_EVIDENCE_LINES]]
    top = rows[0]
    top_share = num(top.get("pct_of_total"))
    return [Finding(
        2 if top_share >= 20 else 3, "pgss_top_total",
        "Top query is %.1f%% of all database time" % top_share,
        evidence + ["queries: " + (str(top.get("query")) or "")[:200]],
        "This is the time budget. Cumulative execution time is what your users wait "
        "for in aggregate, so a query at 30% of database time is worth more attention "
        "than one that is individually slow but runs twice a day.",
        "1. Take the top one or two queries only, and get the real plan:\n"
        "     EXPLAIN (ANALYZE, BUFFERS) <the query>;\n"
        "   Read EXPLAIN-GUIDE.md before interpreting it - estimated versus actual rows "
        "is where the answer usually is.\n"
        "2. If rows_per_call is close to 1.0 and calls is huge, the application is "
        "issuing one query per row (an N+1 access pattern). No index fixes that: batch "
        "or join in the application.\n"
        "3. If mean_exec_time is low but calls is enormous, consider caching in the "
        "application or the pooler, or a materialised view, before touching the schema.\n"
        "4. Look up the exact statement text in your logs or repository by queryid to "
        "find the code path.",
        "sql/03-slow-queries.sql (Q3.1)")]


def check_pgss_temp_spill(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["queryid %s: %s calls, %s temp blocks written, mean %.1f ms - %s" % (
        r.get("queryid"), "{:,}".format(int(num(r.get("calls")))),
        "{:,}".format(int(num(r.get("temp_blks_written")))),
        num(r.get("mean_exec_time")), (str(r.get("query")) or "")[:110])
        for r in rows]
    return [Finding(
        2, "pgss_temp_spill",
        "%d statement(s) spill sorts or hashes to temporary files" % len(rows),
        _bullet(evidence),
        "Each temp block is 8 kB of real disk write and a later read, for data that is "
        "discarded immediately. On a cloud volume this is often the largest single "
        "source of I/O in a read-mostly database, and it never shows up in the cache hit "
        "ratio.",
        "1. Raise work_mem for the role that runs these, not globally:\n"
        "     ALTER ROLE app_user SET work_mem = '64MB';\n"
        "   Confirm with one session first:\n"
        "     SET work_mem = '64MB';  EXPLAIN (ANALYZE, BUFFERS) <query>;\n"
        "   Watch for 'Sort Method: quicksort' replacing 'external merge Disk'.\n"
        "2. Then check whether the sort is needed at all. If the ORDER BY, GROUP BY or "
        "join key can be served by an index, the sort disappears and no amount of "
        "work_mem is required - see INDEXING-PLAYBOOK.md.\n"
        "3. If a hash join is spilling because the estimated row count is far below the "
        "actual, fix the statistics first (ANALYZE, or per-column SET STATISTICS). More "
        "memory treats the symptom; better estimates remove the spill.",
        "sql/03-slow-queries.sql (Q3.4)")]


def check_pgss_instability(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    evidence = ["queryid %s: mean %.1f ms but stddev %.1f ms (CV %.2f), range %.1f-%.1f ms, "
                "%s calls - %s" % (
                    r.get("queryid"), num(r.get("mean_exec_time")),
                    num(r.get("stddev_exec_time")), num(r.get("coeff_variation")),
                    num(r.get("min_exec_time")), num(r.get("max_exec_time")),
                    "{:,}".format(int(num(r.get("calls")))), (str(r.get("query")) or "")[:100])
                for r in rows]
    return [Finding(
        2, "pgss_instability",
        "%d statement(s) have unstable runtimes - the same query gets different plans" % len(rows),
        _bullet(evidence),
        "A standard deviation larger than the mean means the query is not slow, it is "
        "UNPREDICTABLE. Optimising the average will not help: some parameter values get a "
        "good plan and others get a catastrophic one. This is usually a skewed column "
        "where the planner's row estimate is wrong for specific values, or a generic plan "
        "cached for a prepared statement.",
        "1. Find the parameter values for the slow executions. Turn on logging for this "
        "statement only, temporarily:\n"
        "     ALTER SYSTEM SET log_min_duration_statement = '500ms';\n"
        "     SELECT pg_reload_conf();\n"
        "   Then compare the fast and slow parameter sets.\n"
        "2. If the predicate column is heavily skewed, give the planner better "
        "information about the skew:\n"
        "     ALTER TABLE public.<table> ALTER COLUMN <col> SET STATISTICS 1000;\n"
        "     ANALYZE public.<table>;\n"
        "   For a very common value with a handful of rare ones, also consider a partial "
        "index for the rare case - see INDEXING-PLAYBOOK.md.\n"
        "3. If the application uses prepared statements and the plan flips between good "
        "and bad, force a fresh plan per execution for that statement:\n"
        "     SET plan_cache_mode = force_custom_plan;\n"
        "   (PostgreSQL 12+.) It costs a little planning time per call and usually fixes "
        "the instability. Set it per role, or in the code path that prepares the "
        "statement, not globally.\n"
        "4. Last resort for a genuinely bimodal query: split it into a selective branch "
        "and a bulk branch and let the application choose.",
        "sql/03-slow-queries.sql (Q3.3)")]


def check_pgss_evictions(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    dealloc = int(num(rows[0].get("dealloc")))
    if dealloc == 0:
        return []
    return [Finding(
        2, "pgss_evictions",
        "%s statement(s) have been evicted from pg_stat_statements" % "{:,}".format(dealloc),
        ["dealloc = %s, stats_reset = %s" % ("{:,}".format(dealloc), rows[0].get("stats_reset"))],
        "The pg_stat_statements hash table is full, so older entries were discarded. "
        "That means the total-time ranking in this report is incomplete and may be "
        "missing your worst offender entirely - an evicted query is exactly the one "
        "nobody has looked at.",
        "Raise the limit in postgresql.conf and RESTART (it is not reloadable, because "
        "the shared memory segment is sized at server start):\n"
        "     pg_stat_statements.max = 20000\n"
        "Then, to keep the memory cost bounded, also restrict what is tracked:\n"
        "     pg_stat_statements.track = top     -- not 'all'\n"
        "Each slot costs roughly 200 bytes plus the query text, so 20000 entries is on "
        "the order of a few MB. On PostgreSQL 14+ you can watch this counter directly "
        "via the pg_stat_statements_info view, which is where this check read it from.",
        "sql/03-slow-queries.sql (Q3.1)")]


def check_vm_coverage(rows: List[Dict[str, Any]]) -> List[Finding]:
    bad = [r for r in rows
           if opt_num(r.get("vm_coverage_pct")) is not None
           and num(r.get("vm_coverage_pct")) < 80
           and num(r.get("relpages")) > 5000]
    if not bad:
        return []
    evidence = ["%s.%s: %.1f%% all-visible (%s of %s pages), %s, %s dead tuples, last "
                "autovacuum %s" % (
                    r.get("schemaname"), r.get("relname"), num(r.get("vm_coverage_pct")),
                    r.get("relallvisible"), r.get("relpages"), r.get("heap_size"),
                    r.get("n_dead_tup"), r.get("last_autovacuum") or "never")
                for r in bad]
    return [Finding(
        3, "vm_coverage",
        "%d large table(s) have poor visibility-map coverage" % len(bad),
        _bullet(evidence),
        "The visibility map is what makes index-only scans possible. For pages not "
        "marked all-visible, an index-only scan has to visit the heap to check tuple "
        "visibility, so an index that LOOKS covering behaves like a plain index and your "
        "heap fetches stay high. VACUUM is what sets these bits - REINDEX does not.",
        "1. Run VACUUM on the affected tables. It does not block reads or writes:\n"
        "     VACUUM (ANALYZE, VERBOSE) public.<table>;\n"
        "2. Check again - coverage should jump sharply. If it does not, the table is "
        "being written continuously (new pages start out not-all-visible), which is "
        "expected, and index-only scans are simply not available for the freshest data.\n"
        "3. If you built a covering index expecting index-only scans and coverage stays "
        "low because of constant writes, that index is buying you less than you planned. "
        "Confirm the real behaviour with:\n"
        "     EXPLAIN (ANALYZE, BUFFERS) <query>;\n"
        "   and look for `Heap Fetches` under the Index Only Scan node. A high number "
        "there means the index-only scan is not saving you heap access.",
        "sql/04-index-usage.sql (Q4.3)")]


def check_settings(rows: List[Dict[str, Any]]) -> List[Finding]:
    if not rows:
        return []
    current = {str(r.get("name")): r for r in rows}
    findings: List[Finding] = []

    def setting(name: str) -> Optional[str]:
        r = current.get(name)
        return None if r is None else str(r.get("setting"))

    def numeric(name: str) -> Optional[float]:
        v = setting(name)
        return opt_num(v)

    def memory_mb(name: str) -> Optional[float]:
        """A memory setting's value in MB, converted using its `unit` column.

        `pg_settings.setting` is a bare number expressed in whatever unit the
        sibling `unit` column names - it is NOT self-describing. A default
        shared_buffers arrives as `setting = '16384', unit = '8kB'`, so reading
        `setting` alone and assuming bytes reported a real 128 MB as
        "approximately 0 MB". Returns None when the row is missing or its unit
        is not a known memory unit, so callers never act on a guessed number.
        """
        row = current.get(name)
        if row is None or row.get("setting") is None:
            return None
        text = str(row.get("setting")).strip()
        unit = str(row.get("unit") or "").strip().lower()
        m = re.match(r"^([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([A-Za-z]*)$", text)
        if not m:
            return None
        try:
            value = float(m.group(1))
        except ValueError:
            return None
        if m.group(2):
            # Some settings spell the unit in the value itself ('128MB'); the
            # `unit` column then repeats it or is empty. Trust the value.
            unit = m.group(2).lower()
        factors = {"b": 1.0, "kb": 1024.0, "mb": 1024.0 ** 2,
                   "gb": 1024.0 ** 3, "tb": 1024.0 ** 4}
        factor = factors.get(unit)
        if factor is None and unit.endswith("b") and unit[:-2].isdigit():
            # pg_settings reports block-sized units as '8kB', '16kB', '32kB'...
            factor = float(unit[:-2]) * 1024.0
        if factor is None:
            return None
        return value * factor / (1024.0 * 1024.0)

    if setting("autovacuum") in ("off", "false", "0"):
        findings.append(Finding(
            1, "autovacuum_off",
            "autovacuum is switched OFF globally",
            ["autovacuum = off (set from: %s)" % current["autovacuum"].get("source")],
            "Nothing reclaims dead tuples anywhere, no table is ever analyzed "
            "automatically, and transaction ID freezing stops progressing. Table bloat "
            "and planner misestimation will both grow without bound, and the "
            "anti-wraparound freeze - which ignores this setting - will eventually fire "
            "as one enormous unplanned I/O event.",
            "Turn it back on. It is reloadable, no restart:\n"
            "     ALTER SYSTEM SET autovacuum = on;\n"
            "     SELECT pg_reload_conf();\n"
            "If autovacuum was disabled because of latency spikes, do not just re-enable "
            "it blind - lower autovacuum_vacuum_cost_limit and reduce "
            "autovacuum_max_workers first, then re-enable and watch. The spikes come "
            "from unbounded work, not from autovacuum existing.",
            "sql/07-vacuum-and-autovacuum.sql (Q7.1)"))
    if setting("track_counts") in ("off", "false", "0"):
        findings.append(Finding(
            1, "track_counts_off",
            "track_counts is OFF - all statistics views are empty and autovacuum cannot run",
            ["track_counts = off"],
            "pg_stat_user_tables, pg_stat_user_indexes and every other cumulative "
            "statistics view will report zeros, and autovacuum will not fire at all "
            "because it has no dead-tuple counts to act on. Every counter-based query in "
            "this toolkit is meaningless until this is on.",
            "Turn it on and restart (it is not reloadable in all cases - check the "
            "source column, and if it comes from postgresql.conf, restart):\n"
            "     ALTER SYSTEM SET track_counts = on;\n"
            "Counters start from zero, so let the system run for a full business cycle "
            "before trusting any of the counter-based findings.",
            "sql/06-cache-hit-and-io.sql (Q6.1)"))
    if setting("track_io_timing") in ("off", "false", "0"):
        findings.append(Finding(
            3, "track_io_timing_off",
            "track_io_timing is off - I/O timing evidence is unavailable",
            ["track_io_timing = off"],
            "blk_read_time and blk_write_time stay at zero, so neither this report nor "
            "pg_stat_database can tell you how much wall-clock time your queries spend "
            "waiting on I/O. You can still see I/O VOLUME, just not its cost.",
            "Enable it while you are diagnosing, then decide whether to keep it:\n"
            "     ALTER SYSTEM SET track_io_timing = on;\n"
            "     SELECT pg_reload_conf();\n"
            "It calls the OS clock on every I/O operation, so there is measurable "
            "overhead on a very high-IOPS system. Leave it on for a day to gather "
            "evidence, then decide.",
            "sql/06-cache-hit-and-io.sql (Q6.1)"))
    cost_delay = numeric("autovacuum_vacuum_cost_delay")
    if cost_delay is not None and cost_delay >= 10:
        findings.append(Finding(
            2, "autovacuum_throttled",
            "autovacuum_vacuum_cost_delay is %s ms - autovacuum is heavily throttled" % setting("autovacuum_vacuum_cost_delay"),
            ["autovacuum_vacuum_cost_delay = %s ms" % setting("autovacuum_vacuum_cost_delay"),
             "autovacuum_vacuum_cost_limit = %s" % setting("autovacuum_vacuum_cost_limit"),
             "vacuum_cost_page_miss = %s, vacuum_cost_page_dirty = %s" % (
                 setting("vacuum_cost_page_miss"), setting("vacuum_cost_page_dirty"))],
            "With a delay that large, autovacuum can spend more time asleep than "
            "working. On a table large enough to need many rounds, it "
            "may never finish a pass before the next one is due - so dead tuples "
            "accumulate faster than they are removed, which is exactly the "
            "'autovacuum runs but bloat grows' symptom.",
            "Bring the delay down and the limit up, on modern storage. Both are "
            "reloadable:\n"
            "     ALTER SYSTEM SET autovacuum_vacuum_cost_delay = 1;    -- ms\n"
            "     ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 2000;\n"
            "     SELECT pg_reload_conf();\n"
            "Watch disk latency for ten minutes and raise the limit again if it stays "
            "flat. Note that the cost limit is SHARED between all autovacuum workers, so "
            "raising autovacuum_max_workers without raising the limit does not make "
            "vacuum faster - it splits the same budget more ways. For the two or three "
            "largest tables, set a per-table limit instead, which uses its own budget:\n"
            "     ALTER TABLE public.<table> SET (autovacuum_vacuum_cost_limit = 1000);",
            "sql/07-vacuum-and-autovacuum.sql (Q7.1/Q7.7)"))
    buff = setting("shared_buffers")
    buff_mb = memory_mb("shared_buffers")
    if buff is not None and buff_mb is not None and buff_mb < 256:
        ecs = setting("effective_cache_size")
        ecs_mb = memory_mb("effective_cache_size")
        ecs_evidence = "effective_cache_size = %s" % ecs
        if ecs_mb is not None:
            ecs_evidence += " (approximately %.0f MB)" % ecs_mb
        findings.append(Finding(
            3, "shared_buffers_low",
            "shared_buffers is small at %s (approximately %.0f MB)" % (buff, buff_mb),
            ["shared_buffers = %s (approximately %.0f MB)" % (buff, buff_mb),
             ecs_evidence],
            "A very small shared_buffers makes PostgreSQL depend on the operating "
            "system page cache for almost everything. That works - the OS cache is "
            "usually large - but the planner's cost model behaves differently and "
            "some workloads (high write rates, many concurrent writers) suffer.",
            "If raising it, do it deliberately: it requires a RESTART and the memory "
            "is allocated up front. A reasonable target is 25% of RAM on a dedicated "
            "database host, and no more than about 8-16 GB on a large machine, "
            "because beyond that the OS page cache was already serving those reads. "
            "Also keep effective_cache_size honest (roughly 50-75% of total RAM on a "
            "dedicated host) - it is only a planner hint and costs no memory, but a "
            "wrong value produces wrong plan choices.",
            "sql/06-cache-hit-and-io.sql (Q6.1)"))
    rpc = numeric("random_page_cost")
    if rpc is not None and rpc >= 4.0 and setting("effective_io_concurrency") in ("1", "0"):
        findings.append(Finding(
            3, "storage_settings_dialled_for_spinning_disks",
            "random_page_cost=%s and effective_io_concurrency=%s look like defaults for spinning disks" % (
                setting("random_page_cost"), setting("effective_io_concurrency")),
            ["random_page_cost = %s (default 4.0)" % setting("random_page_cost"),
             "effective_io_concurrency = %s (default 1)" % setting("effective_io_concurrency"),
             "seq_page_cost = %s" % setting("seq_page_cost")],
            "These defaults assume a spinning disk where a random read costs four times a "
            "sequential one. On SSD or cloud block storage a random read is nearly as "
            "cheap as a sequential one, so the high value makes the planner avoid index "
            "scans it should be choosing - you get sequential scans on selective queries "
            "and blame the missing index you already have.",
            "If your storage is SSD or better (not a spinning disk or a very high-latency "
            "network volume), adjust both, per role or per database rather than globally "
            "so you do not affect other tenants:\n"
            "     ALTER DATABASE <db> SET random_page_cost = 1.1;\n"
            "     ALTER DATABASE <db> SET effective_io_concurrency = 200;\n"
            "Both are reloadable/per-session. Change one at a time and confirm the effect "
            "with EXPLAIN (ANALYZE, BUFFERS) on a query you know is choosing badly. Do "
            "NOT lower random_page_cost on a spinning-disk-backed volume - you will get "
            "more index scans and worse performance.",
            "sql/06-cache-hit-and-io.sql (Q6.1)"))
    return findings


# =====================================================================
# The check registry
# =====================================================================

CHECKS: List[Check] = [
    Check(
        "connections",
        "Connection slot usage",
        """
        SELECT (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections,
               count(*)                                                   AS total,
               count(*) FILTER (WHERE state = 'active')                   AS active,
               count(*) FILTER (WHERE state = 'idle')                     AS idle,
               count(*) FILTER (WHERE state = 'idle in transaction')      AS idle_in_transaction,
               count(*) FILTER (WHERE wait_event_type = 'Lock')           AS waiting_on_locks
        FROM pg_stat_activity
        WHERE backend_type = 'client backend'
        """,
        check_connections,
        "sql/05-connections-and-locks.sql (Q5.0)",
    ),
    Check(
        "idle_in_transaction",
        "Sessions idle in transaction",
        """
        SELECT pid,
               datname,
               usename,
               application_name,
               state,
               EXTRACT(EPOCH FROM (now() - xact_start))::bigint   AS xact_seconds,
               EXTRACT(EPOCH FROM (now() - state_change))::bigint AS idle_seconds,
               age(backend_xmin)                                  AS xmin_age,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 160) AS query
        FROM pg_stat_activity
        WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
          AND pid <> pg_backend_pid()
        ORDER BY xact_start ASC NULLS LAST
        LIMIT 20
        """,
        check_idle_in_transaction,
        "sql/05-connections-and-locks.sql (Q5.2)",
    ),
    Check(
        "blocking",
        "Lock blocking chains",
        """
        SELECT blocked.pid                                          AS blocked_pid,
               blocked.usename                                      AS blocked_user,
               blocked.application_name                             AS blocked_app,
               blocker.pid                                          AS blocker_pid,
               blocker.usename                                      AS blocker_user,
               blocker.state                                        AS blocker_state,
               blocked.wait_event                                   AS wait_event,
               EXTRACT(EPOCH FROM (now() - blocked.query_start))::bigint AS blocked_seconds,
               EXTRACT(EPOCH FROM (now() - blocker.xact_start))::bigint  AS blocker_xact_seconds,
               left(regexp_replace(blocked.query, '\\s+', ' ', 'g'), 150) AS blocked_query,
               left(regexp_replace(blocker.query, '\\s+', ' ', 'g'), 150) AS blocker_query
        FROM pg_stat_activity blocked
        CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(blocker_pid)
        JOIN pg_stat_activity blocker ON blocker.pid = bp.blocker_pid
        WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
        ORDER BY blocked_seconds DESC
        """,
        check_blocking,
        "sql/05-connections-and-locks.sql (Q5.4/Q5.5/Q5.6)",
    ),
    Check(
        "long_running_queries",
        "Long-running active queries",
        """
        SELECT pid, datname, usename, application_name, state,
               wait_event_type, wait_event,
               EXTRACT(EPOCH FROM (now() - query_start))::bigint AS query_seconds,
               EXTRACT(EPOCH FROM (now() - xact_start))::bigint  AS xact_seconds,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 180) AS query
        FROM pg_stat_activity
        WHERE state = 'active'
          AND pid <> pg_backend_pid()
          AND now() - query_start > interval '10 seconds'
        ORDER BY query_start ASC
        LIMIT 20
        """,
        check_long_running_queries,
        "sql/05-connections-and-locks.sql (Q5.1)",
    ),
    Check(
        "xmin_horizon",
        "Oldest xmin horizon held by any session",
        """
        SELECT max(age(backend_xmin))::bigint                     AS oldest_xmin_age,
               count(*) FILTER (WHERE backend_xmin IS NOT NULL)   AS holders,
               EXTRACT(EPOCH FROM max(now() - xact_start))::bigint AS oldest_xact_seconds
        FROM pg_stat_activity
        """,
        check_xmin_horizon,
        "sql/05-connections-and-locks.sql (Q5.7)",
    ),
    Check(
        "replication_slots",
        "Replication slots pinning xmin",
        """
        SELECT slot_name,
               slot_type,
               active,
               database,
               age(xmin)::bigint         AS xmin_age,
               age(catalog_xmin)::bigint AS catalog_xmin_age
        FROM pg_replication_slots
        ORDER BY age(xmin) DESC NULLS LAST
        """,
        check_replication_slots,
        "sql/05-connections-and-locks.sql (Q5.7)",
    ),
    Check(
        "wraparound",
        "Transaction ID wraparound proximity",
        """
        SELECT datname,
               age(datfrozenxid)::bigint AS xid_age,
               (SELECT setting::numeric FROM pg_settings
                 WHERE name = 'autovacuum_freeze_max_age') AS freeze_max_age,
               round(100.0 * age(datfrozenxid)
                     / NULLIF((SELECT setting::numeric FROM pg_settings
                                WHERE name = 'autovacuum_freeze_max_age'), 0), 1)
                                          AS pct_to_forced_freeze
        FROM pg_database
        WHERE datallowconn
        ORDER BY age(datfrozenxid) DESC
        LIMIT 5
        """,
        check_wraparound,
        "sql/07-vacuum-and-autovacuum.sql (Q7.5)",
    ),
    Check(
        "autovacuum_disabled",
        "Tables with autovacuum disabled",
        """
        SELECT n.nspname, c.relname, c.reloptions,
               t.n_dead_tup, t.n_live_tup
        FROM pg_class c
        JOIN pg_namespace n        ON n.oid = c.relnamespace
        JOIN pg_stat_user_tables t ON t.relid = c.oid
        WHERE array_to_string(c.reloptions, ',') LIKE '%autovacuum_enabled=false%'
        ORDER BY t.n_dead_tup DESC
        """,
        check_autovacuum_disabled,
        "sql/07-vacuum-and-autovacuum.sql (Q7.8)",
    ),
    Check(
        "dead_tuples",
        "Tables carrying dead tuples",
        """
        SELECT t.schemaname, t.relname, t.n_live_tup, t.n_dead_tup,
               CASE WHEN t.n_live_tup + t.n_dead_tup > 0
                    THEN round(100.0 * t.n_dead_tup
                               / (t.n_live_tup + t.n_dead_tup), 1) END AS dead_pct,
               CASE WHEN t.n_tup_upd > 0
                    THEN round(100.0 * t.n_tup_hot_upd / t.n_tup_upd, 1) END AS hot_update_pct,
               pg_total_relation_size(t.relid)                       AS total_bytes,
               pg_size_pretty(pg_total_relation_size(t.relid))        AS total_size,
               t.last_autovacuum,
               c.reltuples::bigint                                    AS reltuples
        FROM pg_stat_user_tables t
        JOIN pg_class c ON c.oid = t.relid
        WHERE t.n_dead_tup > 1000
        ORDER BY t.n_dead_tup DESC
        LIMIT 30
        """,
        check_dead_tuples,
        "sql/01-table-sizes-and-bloat.sql (Q1.2)",
    ),
    Check(
        "hot_updates",
        "HOT update ratio on update-heavy tables",
        """
        SELECT t.schemaname, t.relname, t.n_live_tup, t.n_tup_upd, t.n_tup_hot_upd,
               CASE WHEN t.n_tup_upd > 0
                    THEN round(100.0 * t.n_tup_hot_upd / t.n_tup_upd, 1) END AS hot_update_pct,
               COALESCE(substring(array_to_string(c.reloptions, ',')
                                  FROM 'fillfactor=([0-9]+)'), '100') AS fillfactor
        FROM pg_stat_user_tables t
        JOIN pg_class c ON c.oid = t.relid
        WHERE t.n_tup_upd > 100000
          AND (t.n_tup_hot_upd::numeric / t.n_tup_upd) < 0.5
        ORDER BY t.n_tup_upd DESC
        LIMIT 20
        """,
        check_hot_updates,
        "sql/01-table-sizes-and-bloat.sql (Q1.2)",
    ),
    Check(
        "stale_statistics",
        "Tables with stale planner statistics",
        """
        SELECT t.schemaname, t.relname, t.n_live_tup, t.n_mod_since_analyze,
               round(100.0 * t.n_mod_since_analyze / NULLIF(t.n_live_tup, 0), 1) AS pct_modified,
               t.last_analyze, t.last_autoanalyze
        FROM pg_stat_user_tables t
        WHERE t.n_live_tup > 10000
          AND t.n_mod_since_analyze > 0.10 * t.n_live_tup
        ORDER BY t.n_mod_since_analyze DESC
        LIMIT 20
        """,
        check_stale_statistics,
        "sql/07-vacuum-and-autovacuum.sql (Q7.3)",
    ),
    Check(
        "seq_scan_candidates",
        "Sequential scans over a small fraction of large tables",
        """
        SELECT t.schemaname, t.relname, t.seq_scan, t.seq_tup_read, t.n_live_tup,
               round(t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1), 0) AS avg_rows_per_scan,
               round(100.0 * (t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1))
                     / NULLIF(t.n_live_tup, 0), 1)                       AS pct_of_table_per_scan,
               t.idx_scan,
               pg_relation_size(t.relid)                                 AS heap_bytes,
               pg_size_pretty(pg_relation_size(t.relid))                  AS heap_size
        FROM pg_stat_user_tables t
        WHERE t.n_live_tup > 100000
          AND t.seq_scan > 100
          AND (t.seq_tup_read::numeric / GREATEST(t.seq_scan, 1)) < 0.05 * t.n_live_tup
        ORDER BY t.seq_tup_read DESC
        LIMIT 20
        """,
        check_seq_scan_candidates,
        "sql/02-unused-and-missing-indexes.sql (Q2.5/Q2.6)",
    ),
    Check(
        "unused_indexes",
        "Never-scanned indexes over 8 MB",
        """
        SELECT s.schemaname, s.relname, s.indexrelname, s.idx_scan,
               pg_relation_size(s.indexrelid)                            AS index_bytes,
               pg_size_pretty(pg_relation_size(s.indexrelid))             AS index_size,
               (t.n_tup_ins + t.n_tup_upd + t.n_tup_del)                  AS table_writes,
               (SELECT stats_reset FROM pg_stat_database
                 WHERE datname = current_database())                      AS stats_reset
        FROM pg_stat_user_indexes s
        JOIN pg_stat_user_tables t ON t.relid = s.relid
        JOIN pg_index i            ON i.indexrelid = s.indexrelid
        WHERE s.idx_scan = 0
          AND NOT i.indisunique
          AND NOT i.indisprimary
          AND i.indisvalid
          AND pg_relation_size(s.indexrelid) > 8 * 1024 * 1024
        ORDER BY pg_relation_size(s.indexrelid) DESC
        LIMIT 20
        """,
        check_unused_indexes,
        "sql/02-unused-and-missing-indexes.sql (Q2.1/Q2.4)",
    ),
    Check(
        "invalid_indexes",
        "Invalid indexes",
        """
        SELECT n.nspname, c.relname AS index_name, t.relname AS table_name,
               i.indisvalid, i.indisready, i.indislive,
               pg_size_pretty(pg_relation_size(c.oid)) AS index_size
        FROM pg_index i
        JOIN pg_class c     ON c.oid = i.indexrelid
        JOIN pg_class t     ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE NOT i.indisvalid OR NOT i.indisready OR NOT i.indislive
        """,
        check_invalid_indexes,
        "sql/04-index-usage.sql (Q4.1)",
    ),
    Check(
        "duplicate_indexes",
        "Duplicate and redundant indexes",
        """
        WITH idx AS (
            SELECT i.indexrelid, i.indrelid, t.relname AS table_name,
                   ic.relname AS index_name, i.indisunique, i.indisprimary, i.indnkeyatts,
                   pg_relation_size(i.indexrelid) AS index_bytes,
                   pg_get_expr(i.indpred, i.indrelid) AS predicate,
                   (SELECT array_agg(pg_get_indexdef(i.indexrelid, g, true) ORDER BY g)
                      FROM generate_series(1, i.indnkeyatts) AS g) AS key_cols
            FROM pg_index i
            JOIN pg_class ic     ON ic.oid = i.indexrelid
            JOIN pg_class t      ON t.oid = i.indrelid
            JOIN pg_namespace n  ON n.oid = t.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND t.relkind IN ('r', 'm')
              AND i.indisvalid
        )
        SELECT DISTINCT ON (b.indexrelid)
               b.table_name,
               b.index_name                            AS droppable_index,
               array_to_string(b.key_cols, ', ')        AS droppable_columns,
               b.index_bytes,
               pg_size_pretty(b.index_bytes)            AS droppable_size,
               a.index_name                            AS covering_index,
               array_to_string(a.key_cols, ', ')        AS covering_columns,
               CASE WHEN a.key_cols = b.key_cols
                    THEN 'exact duplicate' ELSE 'redundant prefix' END AS reason
        FROM idx a
        JOIN idx b
          ON  a.indrelid = b.indrelid
          AND a.indexrelid <> b.indexrelid
          AND (a.predicate IS NOT DISTINCT FROM b.predicate)
          AND a.key_cols[1:array_length(b.key_cols, 1)] = b.key_cols
          AND array_length(b.key_cols, 1) <= array_length(a.key_cols, 1)
          AND NOT b.indisunique
          AND NOT b.indisprimary
          AND (array_length(a.key_cols, 1), a.indexrelid)
              > (array_length(b.key_cols, 1), b.indexrelid)
        ORDER BY b.indexrelid, b.index_bytes DESC
        LIMIT 25
        """,
        check_duplicate_indexes,
        "sql/04-index-usage.sql (Q4.2)",
        note="PostgreSQL 11+ (uses pg_index.indnkeyatts).",
    ),
    Check(
        "cache_hit_tables",
        "Per-table heap cache hit ratio",
        """
        SELECT s.schemaname, s.relname,
               s.heap_blks_read, s.heap_blks_hit,
               round(100.0 * s.heap_blks_hit
                     / NULLIF(s.heap_blks_hit + s.heap_blks_read, 0), 2) AS heap_hit_pct,
               round(100.0 * s.idx_blks_hit
                     / NULLIF(s.idx_blks_hit + s.idx_blks_read, 0), 2)   AS index_hit_pct,
               (s.heap_blks_read + s.idx_blks_read)                      AS total_misses,
               pg_size_pretty(pg_total_relation_size(s.relid))            AS total_size,
               pg_total_relation_size(s.relid)                            AS total_bytes
        FROM pg_statio_user_tables s
        WHERE s.heap_blks_hit + s.heap_blks_read > 20000
        ORDER BY (s.heap_blks_read + s.idx_blks_read) DESC
        LIMIT 20
        """,
        check_cache_hit_tables,
        "sql/06-cache-hit-and-io.sql (Q6.2/Q6.3)",
    ),
    Check(
        "temp_files",
        "Temporary file usage for the current database",
        """
        SELECT datname,
               temp_files,
               temp_bytes,
               pg_size_pretty(temp_bytes) AS temp_size,
               round(temp_bytes::numeric / GREATEST(temp_files, 1) / 1024 / 1024, 2)
                                          AS avg_mb_per_temp_file,
               round(100.0 * blks_read / NULLIF(blks_read + blks_hit, 0), 2) AS cache_miss_pct,
               stats_reset
        FROM pg_stat_database
        WHERE datname = current_database()
        """,
        check_temp_files,
        "sql/06-cache-hit-and-io.sql (Q6.7)",
    ),
    Check(
        "database_stats",
        "Database-wide transaction, temp and deadlock counters",
        """
        SELECT datname, xact_commit, xact_rollback,
               round(100.0 * xact_rollback
                     / NULLIF(xact_commit + xact_rollback, 0), 3) AS rollback_pct,
               temp_files, temp_bytes, deadlocks,
               round(blk_read_time::numeric, 0)  AS blk_read_ms,
               round(blk_write_time::numeric, 0) AS blk_write_ms,
               stats_reset
        FROM pg_stat_database
        WHERE datname = current_database()
        """,
        check_database_stats,
        "sql/06-cache-hit-and-io.sql (Q6.1)",
    ),
    Check(
        "vm_coverage",
        "Visibility map coverage (index-only scan readiness)",
        """
        SELECT n.nspname AS schemaname, c.relname,
               c.relpages, c.relallvisible,
               round(100.0 * c.relallvisible / NULLIF(c.relpages, 0), 1) AS vm_coverage_pct,
               pg_size_pretty(pg_relation_size(c.oid))                   AS heap_size,
               t.n_dead_tup, t.last_autovacuum
        FROM pg_class c
        JOIN pg_namespace n        ON n.oid = c.relnamespace
        JOIN pg_stat_user_tables t ON t.relid = c.oid
        WHERE c.relkind = 'r'
          AND c.relpages > 1000
        ORDER BY 100.0 * c.relallvisible / NULLIF(c.relpages, 0) ASC NULLS FIRST
        LIMIT 15
        """,
        check_vm_coverage,
        "sql/04-index-usage.sql (Q4.3)",
    ),
    Check(
        "pgss_top_total",
        "Top statements by cumulative execution time (pg_stat_statements)",
        """
        SELECT queryid, calls, rows,
               total_exec_time, mean_exec_time, stddev_exec_time,
               min_exec_time, max_exec_time,
               shared_blks_hit, shared_blks_read, temp_blks_written,
               100.0 * total_exec_time
                     / NULLIF(sum(total_exec_time) OVER (), 0)       AS pct_of_total,
               rows::numeric / GREATEST(calls, 1)                    AS rows_per_call,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 200)    AS query
        FROM pg_stat_statements
        WHERE calls > 5
          AND query NOT ILIKE '%pg_stat_statements%'
        ORDER BY total_exec_time DESC
        LIMIT 15
        """,
        check_pgss_top_total,
        "sql/03-slow-queries.sql (Q3.1)",
        requires_pgss=True,
    ),
    Check(
        "pgss_temp_spill",
        "Statements spilling to temporary files (pg_stat_statements)",
        """
        SELECT queryid, calls, temp_blks_written, temp_blks_read,
               mean_exec_time, total_exec_time,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 200) AS query
        FROM pg_stat_statements
        WHERE temp_blks_written > 0
          AND query NOT ILIKE '%pg_stat_statements%'
        ORDER BY temp_blks_written DESC
        LIMIT 10
        """,
        check_pgss_temp_spill,
        "sql/03-slow-queries.sql (Q3.4)",
        requires_pgss=True,
    ),
    Check(
        "pgss_instability",
        "Statement runtime instability (pg_stat_statements)",
        """
        SELECT queryid, calls, mean_exec_time, stddev_exec_time,
               min_exec_time, max_exec_time,
               (stddev_exec_time / NULLIF(mean_exec_time, 0))     AS coeff_variation,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 200) AS query
        FROM pg_stat_statements
        WHERE calls >= 50
          AND mean_exec_time > 1
          AND query NOT ILIKE '%pg_stat_statements%'
          AND (stddev_exec_time / NULLIF(mean_exec_time, 0)) > 1
        ORDER BY (stddev_exec_time / NULLIF(mean_exec_time, 0)) DESC
        LIMIT 10
        """,
        check_pgss_instability,
        "sql/03-slow-queries.sql (Q3.3)",
        requires_pgss=True,
    ),
    Check(
        "pgss_evictions",
        "pg_stat_statements evictions",
        "SELECT dealloc, stats_reset FROM pg_stat_statements_info",
        check_pgss_evictions,
        "sql/03-slow-queries.sql (Q3.1)",
        requires_pgss=True,
        min_version_num=140000,
        note="PostgreSQL 14+ (pg_stat_statements_info).",
    ),
    Check(
        "settings",
        "Configuration review",
        """
        SELECT name, setting, unit, source
        FROM pg_settings
        WHERE name IN (
                'autovacuum', 'autovacuum_vacuum_cost_delay', 'autovacuum_vacuum_cost_limit',
                'autovacuum_max_workers', 'autovacuum_vacuum_threshold',
                'autovacuum_vacuum_scale_factor', 'autovacuum_analyze_threshold',
                'autovacuum_analyze_scale_factor', 'autovacuum_freeze_max_age',
                'track_counts', 'track_io_timing', 'shared_buffers', 'work_mem',
                'maintenance_work_mem', 'effective_cache_size', 'random_page_cost',
                'seq_page_cost', 'effective_io_concurrency', 'max_wal_size',
                'checkpoint_completion_target', 'max_connections', 'jit',
                'vacuum_cost_page_miss', 'vacuum_cost_page_dirty')
        ORDER BY name
        """,
        check_settings,
        "sql/07-vacuum-and-autovacuum.sql (Q7.1)",
        note="Checks a fixed list of settings against known-bad defaults.",
    ),
]

CHECKS_BY_KEY = {c.key: c for c in CHECKS}


# =====================================================================
# Running the checks
# =====================================================================

def pgss_status(db: Db) -> Tuple[bool, str]:
    """Return (usable, human_readable_status) for pg_stat_statements."""
    try:
        row = db.query(
            "SELECT EXISTS (SELECT 1 FROM pg_extension "
            "               WHERE extname = 'pg_stat_statements') AS installed, "
            "       (SELECT extversion FROM pg_extension "
            "         WHERE extname = 'pg_stat_statements') AS version, "
            "       current_setting('shared_preload_libraries') AS preload"
        )[0]
    except Exception as exc:  # noqa: BLE001
        return False, "unknown (%s)" % friendly_error(exc).splitlines()[0]

    installed = bool(row.get("installed"))
    preload = str(row.get("preload") or "")
    if not installed:
        hint = ""
        if "pg_stat_statements" not in preload:
            hint = (" - shared_preload_libraries does not list it, so it must be added "
                    "there and the server RESTARTED before CREATE EXTENSION will work")
        return False, "not installed%s" % hint
    try:
        db.query("SELECT 1 FROM pg_stat_statements LIMIT 1")
    except Exception as exc:  # noqa: BLE001
        return False, "extension present but unreadable (%s)" % friendly_error(exc).splitlines()[0]
    return True, "available (extension %s)" % (row.get("version") or "?")


def run_checks(db: Db, selected: Sequence[Check], version_num: int,
               pgss_ok: bool, verbose: bool) -> List[CheckResult]:
    results: List[CheckResult] = []
    for check in selected:
        result = CheckResult(check=check)

        if check.requires_pgss and not pgss_ok:
            result.skipped = ("skipped - pg_stat_statements is not usable on this server. "
                              "See the header of sql/03-slow-queries.sql for the two-step "
                              "install (shared_preload_libraries + RESTART, then CREATE "
                              "EXTENSION).")
            results.append(result)
            if verbose:
                print("  - %-24s %s" % (check.key, "SKIPPED"), file=sys.stderr)
            continue

        if check.min_version_num and version_num and version_num < check.min_version_num:
            result.skipped = ("skipped - requires PostgreSQL %d or newer (%s)" % (
                check.min_version_num // 10000, check.note or "version-gated"))
            results.append(result)
            if verbose:
                print("  - %-24s %s" % (check.key, "SKIPPED"), file=sys.stderr)
            continue

        started = time.time()
        try:
            result.rows = db.query(check.sql)
        except Exception as exc:  # noqa: BLE001
            result.error = friendly_error(exc, context="check '%s'" % check.key)
            results.append(result)
            if verbose:
                print("  - %-24s ERROR" % check.key, file=sys.stderr)
            continue

        try:
            result.findings = check.fn(result.rows)
        except Exception as exc:  # noqa: BLE001
            result.error = ("check '%s' returned data this version of the script could "
                            "not interpret: %s. The raw rows are available with --json."
                            % (check.key, exc))
        results.append(result)
        if verbose:
            print("  - %-24s %d row(s), %d finding(s) in %.2fs" % (
                check.key, len(result.rows), len(result.findings),
                time.time() - started), file=sys.stderr)
    return results


def run_sql_files(db: Db, sql_dir: str, only: Optional[Set[str]],
                  raw: bool, verbose: bool) -> List[FileResult]:
    """Execute every statement in every sql/ file, read-only, and report what happened.

    This is how the report proves the pack actually runs against YOUR server
    version: each statement is executed and any version incompatibility shows
    up as an error against a file and line number instead of being hidden.
    """
    results: List[FileResult] = []
    if not os.path.isdir(sql_dir):
        return results

    names = sorted(f for f in os.listdir(sql_dir) if f.endswith(".sql"))
    if only:
        names = [n for n in names if n[:2] in only]

    for name in names:
        path = os.path.join(sql_dir, name)
        fr = FileResult(path="sql/" + name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            fr.errors.append("could not read file: %s" % exc)
            results.append(fr)
            continue

        try:
            statements = split_statements(text)
        except SplitError as exc:
            fr.errors.append("could not parse file: %s" % exc)
            results.append(fr)
            continue

        for line_no, statement in statements:
            fr.statements_total += 1
            if not is_read_only(statement):
                fr.refused.append("line %d: refused (not a read-only statement)" % line_no)
                continue
            try:
                rows = db.query(statement)
                fr.statements_ok += 1
                fr.rows_returned += len(rows)
                if raw:
                    fr.raw_output.append((line_no, statement, rows[:10]))
            except Exception as exc:  # noqa: BLE001
                fr.errors.append("line %d: %s" % (line_no, friendly_error(exc)))
            if verbose:
                print("    %s line %-5d ok" % (name, line_no), file=sys.stderr)
        results.append(fr)
    return results


# =====================================================================
# Report rendering
# =====================================================================

WIDTH = 78


def rule(char: str = "-") -> str:
    return char * WIDTH


def wrap(text: str, indent: str = "  ", width: int = WIDTH) -> str:
    """Wrap prose but keep explicit newlines and relative indentation.

    Action text contains numbered steps and SQL snippets on their own lines.
    Re-wrapping those with one flat indent destroys the layout, so each line's
    own leading whitespace is kept and only its continuation lines are
    indented further.
    """
    out: List[str] = []
    for paragraph in str(text).split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        extra = len(paragraph) - len(paragraph.lstrip())
        first = indent + " " * extra
        # Prose (no leading whitespace of its own) gets a flat indent; only a
        # deliberately indented line gets a hanging indent for its runs-over.
        rest = indent + " " * (extra + 3) if extra else indent
        out.extend(textwrap.wrap(paragraph.strip(), width=width,
                                 initial_indent=first,
                                 subsequent_indent=rest,
                                 break_long_words=False,
                                 break_on_hyphens=False) or [first.rstrip()])
    return "\n".join(out)


def render_finding(finding: Finding, index: int) -> str:
    lines: List[str] = []
    lines.append("[%s-%d] %s" % ("P%d" % finding.priority, index, finding.title))
    lines.append("")
    lines.append("  Evidence:")
    for item in finding.evidence:
        for i, chunk in enumerate(textwrap.wrap(str(item), width=WIDTH - 8,
                                                break_long_words=False,
                                                break_on_hyphens=False) or [""]):
            lines.append("    - " + chunk if i == 0 else "      " + chunk)
    lines.append("")
    lines.append("  Likely impact:")
    lines.append(wrap(finding.impact))
    lines.append("")
    lines.append("  Suggested action:")
    lines.append(wrap(finding.action, indent="    "))
    lines.append("")
    lines.append("  Source: %s" % finding.source)
    return "\n".join(lines)


def render_report(target: Dict[str, str], findings: List[Finding],
                  check_results: List[CheckResult], file_results: List[FileResult],
                  pgss_text: str, started: float, raw: bool) -> str:
    out: List[str] = []
    findings_sorted = sorted(findings, key=lambda f: (f.priority, f.key))

    out.append(rule("="))
    out.append("POSTGRES PERFORMANCE TOOLKIT - PRIORITISED DIAGNOSTIC REPORT")
    out.append(rule("="))
    out.append("Database    : %s on %s:%s" % (target.get("database"), target.get("host"), target.get("port")))
    out.append("Server      : %s" % (target.get("server") or "?"))
    out.append("Connected as: %s" % target.get("user"))
    out.append("Uptime      : %s" % target.get("uptime"))
    out.append("Generated   : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out.append("Took        : %.2fs" % (time.time() - started))
    out.append("Read-only   : enforced (default_transaction_read_only = on for this session,")
    out.append("              statement_timeout applied, non-read-only statements refused)")
    out.append("")

    # ---- preflight -------------------------------------------------
    out.append(rule("-"))
    out.append("PREFLIGHT")
    out.append(rule("-"))
    out.append("pg_stat_statements      : %s" % pgss_text)
    timed_out = [r for r in check_results if r.error and "57014" in (r.error or "")]
    errored = [r for r in check_results if r.error]
    skipped = [r for r in check_results if r.skipped]
    out.append("checks run              : %d" % (len(check_results) - len(skipped)))
    if skipped:
        out.append("checks skipped          : %d" % len(skipped))
    if errored:
        out.append("checks that errored     : %d (listed at the end of this report)" % len(errored))

    # ---- sql file health ------------------------------------------
    if file_results:
        out.append("")
        out.append(rule("-"))
        out.append("SQL FILE CHECK - does this pack run on your server version?")
        out.append(rule("-"))
        for fr in file_results:
            status = "%d/%d statements OK" % (fr.statements_ok, fr.statements_total)
            if fr.errors:
                status += ", %d failed" % len(fr.errors)
            if fr.refused:
                status += ", %d refused" % len(fr.refused)
            out.append("  %-40s %s" % (fr.path, status))
            for err in fr.errors[:3]:
                out.append("      ! %s" % err.replace("\n", "\n        "))

    # ---- findings by priority -------------------------------------
    for priority in (1, 2, 3):
        group = [f for f in findings_sorted if f.priority == priority]
        if not group:
            continue
        out.append("")
        out.append(rule("="))
        out.append("%s  (%d finding%s)" % (PRIORITY_LABELS[priority], len(group),
                                           "" if len(group) == 1 else "s"))
        out.append(rule("="))
        for i, finding in enumerate(group, start=1):
            out.append("")
            out.append(render_finding(finding, i))

    # ---- nothing found --------------------------------------------
    if not findings_sorted:
        out.append("")
        out.append(rule("="))
        out.append("NO FINDINGS ABOVE THE BUILT-IN THRESHOLDS")
        out.append(rule("="))
        out.append(wrap(
            "Every check ran and none crossed its threshold. That does not mean the "
            "database is fast - it means none of the common problems this toolkit looks "
            "for are present at the severity it looks for. If users still report slow "
            "responses, the next step is a specific query, not a general sweep:\n"
            "  1. Take the one query that is slowest for users.\n"
            "  2. EXPLAIN (ANALYZE, BUFFERS) it, and read EXPLAIN-GUIDE.md.\n"
            "  3. Check whether the time is in the database at all - if the query runs in "
            "1 ms but the request takes 900 ms, the problem is in the application, the "
            "network, or the connection pool, and no database tuning will help."))

    # ---- raw output -----------------------------------------------
    if raw:
        for fr in file_results:
            if not fr.raw_output:
                continue
            out.append("")
            out.append(rule("="))
            out.append("RAW OUTPUT: %s (first 10 rows per statement)" % fr.path)
            out.append(rule("="))
            for line_no, statement, rows in fr.raw_output:
                out.append("")
                out.append("-- line %d: %s" % (
                    line_no, " ".join(statement.split())[:120]))
                if not rows:
                    out.append("   (no rows)")
                    continue
                columns = list(rows[0].keys())
                out.append("   " + " | ".join(columns))
                for row in rows:
                    out.append("   " + " | ".join(
                        "-" if row.get(c) is None else str(row.get(c))[:28] for c in columns))

    # ---- errors and skips -----------------------------------------
    if errored or skipped:
        out.append("")
        out.append(rule("-"))
        out.append("CHECKS THAT DID NOT RUN")
        out.append(rule("-"))
        for r in skipped:
            out.append("  %s: %s" % (r.check.key, r.skipped))
        for r in errored:
            out.append("  %s:" % r.check.key)
            out.append("    " + (r.error or "").replace("\n", "\n    "))

    # ---- closing --------------------------------------------------
    out.append("")
    out.append(rule("="))
    out.append("HOW TO USE THIS REPORT")
    out.append(rule("="))
    p1 = len([f for f in findings_sorted if f.priority == 1])
    p2 = len([f for f in findings_sorted if f.priority == 2])
    p3 = len([f for f in findings_sorted if f.priority == 3])
    out.append(wrap(
        "Findings: %d act-now, %d fix-this-week, %d hygiene. Work them in that order - "
        "a priority-2 fix applied while a priority-1 problem is live usually produces no "
        "measurable improvement, which is why so much database tuning feels random." % (p1, p2, p3)))
    out.append("")
    out.append(wrap(
        "Before you change anything: change ONE thing, then re-run this script and "
        "compare. Two changes at once means you cannot tell which one helped. Every "
        "counter in this report is cumulative since the last statistics reset shown "
        "against it, so re-run after a full business cycle for the counter-based "
        "findings (unused indexes, sequential scans, cache ratios) to mean anything."))
    out.append("")
    out.append(wrap(
        "Changing a setting is not the same as fixing a query. This report tells you "
        "where to look and what to try; it cannot know your workload. The SQL in sql/ "
        "shows the raw evidence, EXPLAIN-GUIDE.md explains how to read plans, and "
        "INDEXING-PLAYBOOK.md covers how to design an index that is actually used."))
    out.append("")
    out.append(rule("="))
    return "\n".join(out)


# =====================================================================
# --self-test: exercise the findings engine with no database
# =====================================================================

SELF_TEST_FIXTURES: Dict[str, List[Dict[str, Any]]] = {
    "connections": [{"max_connections": 100, "total": 94, "active": 40, "idle": 48,
                     "idle_in_transaction": 6, "waiting_on_locks": 3}],
    "idle_in_transaction": [
        {"pid": 1234, "datname": "appdb", "usename": "app", "application_name": "web-1",
         "state": "idle in transaction", "xact_seconds": 2530, "idle_seconds": 2520,
         "xmin_age": 1450000, "query": "SELECT * FROM orders WHERE id = $1"}],
    "blocking": [
        {"blocked_pid": 200, "blocked_user": "app", "blocked_app": "web",
         "blocker_pid": 100, "blocker_user": "batch", "blocker_state": "active",
         "wait_event": "transactionid", "blocked_seconds": 42, "blocker_xact_seconds": 380,
         "blocked_query": "UPDATE accounts SET balance = balance - 10 WHERE id = 7",
         "blocker_query": "UPDATE accounts SET balance = balance + 10 WHERE id = 3"},
        {"blocked_pid": 201, "blocked_user": "app", "blocked_app": "web",
         "blocker_pid": 100, "blocker_user": "batch", "blocker_state": "active",
         "wait_event": "transactionid", "blocked_seconds": 31, "blocker_xact_seconds": 380,
         "blocked_query": "UPDATE accounts SET n = 1 WHERE id = 9",
         "blocker_query": "UPDATE accounts SET balance = balance + 10 WHERE id = 3"}],
    "long_running_queries": [
        {"pid": 555, "datname": "appdb", "usename": "reporting", "application_name": "metabase",
         "state": "active", "wait_event_type": "IO", "wait_event": "DataFileRead",
         "query_seconds": 900, "xact_seconds": 901,
         "query": "SELECT date_trunc('day', created_at), count(*) FROM events GROUP BY 1"}],
    "xmin_horizon": [{"oldest_xmin_age": 24_500_000, "holders": 2,
                      "oldest_xact_seconds": 2530}],
    "replication_slots": [
        {"slot_name": "replica_old", "slot_type": "physical", "active": False,
         "database": None, "xmin_age": 80_000_000, "catalog_xmin_age": None}],
    "wraparound": [{"datname": "appdb", "xid_age": 130_000_000, "freeze_max_age": 200_000_000,
                    "pct_to_forced_freeze": 65.0}],
    "autovacuum_disabled": [
        {"nspname": "public", "relname": "events", "reloptions": ["autovacuum_enabled=false"],
         "n_dead_tup": 900000, "n_live_tup": 12000000}],
    "dead_tuples": [
        {"schemaname": "public", "relname": "events", "n_live_tup": 12_000_000,
         "n_dead_tup": 4_100_000, "dead_pct": 25.5, "hot_update_pct": 41.0,
         "total_bytes": 8_000_000_000, "total_size": "7.5 GB",
         "last_autovacuum": None, "reltuples": 15_900_000}],
    "hot_updates": [
        {"schemaname": "public", "relname": "jobs", "n_live_tup": 3_000_000,
         "n_tup_upd": 9_500_000, "n_tup_hot_upd": 1_200_000, "hot_update_pct": 12.6,
         "fillfactor": "100"}],
    "stale_statistics": [
        {"schemaname": "public", "relname": "events", "n_live_tup": 12_000_000,
         "n_mod_since_analyze": 3_400_000, "pct_modified": 28.3,
         "last_analyze": None, "last_autoanalyze": None}],
    "seq_scan_candidates": [
        {"schemaname": "public", "relname": "events", "seq_scan": 8400,
         "seq_tup_read": 25_000_000, "n_live_tup": 12_000_000,
         "avg_rows_per_scan": 2976, "pct_of_table_per_scan": 0.0, "idx_scan": 120,
         "heap_bytes": 8_000_000_000, "heap_size": "7.5 GB"}],
    "unused_indexes": [
        {"schemaname": "public", "relname": "orders", "indexrelname": "idx_orders_legacy_ref",
         "idx_scan": 0, "index_bytes": 740_000_000, "index_size": "706 MB",
         "table_writes": 4_200_000, "stats_reset": "2025-01-01 00:00:00+00"}],
    "invalid_indexes": [
        {"nspname": "public", "index_name": "idx_orders_created_at", "table_name": "orders",
         "indisvalid": False, "indisready": True, "indislive": True, "index_size": "212 MB"}],
    "duplicate_indexes": [
        {"table_name": "orders", "droppable_index": "idx_orders_user", "droppable_columns": "user_id",
         "index_bytes": 320_000_000, "droppable_size": "305 MB",
         "covering_index": "idx_orders_user_created", "covering_columns": "user_id, created_at",
         "reason": "redundant prefix"},
        {"table_name": "orders", "droppable_index": "idx_orders_user_id", "droppable_columns": "user_id",
         "index_bytes": 318_000_000, "droppable_size": "303 MB",
         "covering_index": "idx_orders_user_created", "covering_columns": "user_id, created_at",
         "reason": "exact duplicate"}],
    "cache_hit_tables": [
        {"schemaname": "public", "relname": "events", "heap_blks_read": 2_400_000,
         "heap_blks_hit": 3_100_000, "heap_hit_pct": 56.4, "index_hit_pct": 91.2,
         "total_misses": 2_600_000, "total_size": "7.5 GB", "total_bytes": 8_000_000_000}],
    "temp_files": [
        {"datname": "appdb", "temp_files": 41_000, "temp_bytes": 92_000_000_000,
         "temp_size": "86 GB", "avg_mb_per_temp_file": 2.14, "cache_miss_pct": 0.7,
         "stats_reset": "2025-01-01 00:00:00+00"}],
    "database_stats": [
        {"datname": "appdb", "xact_commit": 90_000_000, "xact_rollback": 1_800_000,
         "rollback_pct": 1.961, "temp_files": 41_000, "temp_bytes": 92_000_000_000,
         "deadlocks": 42, "blk_read_ms": 120_000, "blk_write_ms": 44_000,
         "stats_reset": "2025-01-01 00:00:00+00"}],
    "vm_coverage": [
        {"schemaname": "public", "relname": "orders", "relpages": 5_000_000,
         "relallvisible": 500_000, "vm_coverage_pct": 10.0, "heap_size": "38 GB",
         "n_dead_tup": 200_000, "last_autovacuum": "2025-01-02 03:00:00+00"}],
    "pgss_top_total": [
        {"queryid": 9918273, "calls": 4_200_000, "rows": 4_100_000,
         "total_exec_time": 5_400_000.0, "mean_exec_time": 1.286,
         "stddev_exec_time": 0.4, "min_exec_time": 0.2, "max_exec_time": 210.0,
         "shared_blks_hit": 900_000_000, "shared_blks_read": 4_000_000,
         "temp_blks_written": 0, "pct_of_total": 38.4, "rows_per_call": 0.98,
         "query": "SELECT id, status FROM orders WHERE user_id = $1"}],
    "pgss_temp_spill": [
        {"queryid": 77812, "calls": 900, "temp_blks_written": 5_600_000,
         "temp_blks_read": 5_600_000, "mean_exec_time": 8400.0,
         "total_exec_time": 7_560_000.0,
         "query": "SELECT tenant_id, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC"}],
    "pgss_instability": [
        {"queryid": 4412, "calls": 30_000, "mean_exec_time": 42.0, "stddev_exec_time": 380.0,
         "min_exec_time": 0.4, "max_exec_time": 9800.0, "coeff_variation": 9.05,
         "query": "SELECT * FROM events WHERE tenant_id = $1 AND created_at > $2"}],
    "pgss_evictions": [{"dealloc": 18_400, "stats_reset": "2025-01-01 00:00:00+00"}],
    "settings": [
        {"name": "autovacuum", "setting": "on", "unit": None, "source": "default"},
        {"name": "autovacuum_vacuum_cost_delay", "setting": "20", "unit": "ms", "source": "configuration file"},
        {"name": "autovacuum_vacuum_cost_limit", "setting": "200", "unit": None, "source": "default"},
        {"name": "vacuum_cost_page_miss", "setting": "2", "unit": None, "source": "default"},
        {"name": "vacuum_cost_page_dirty", "setting": "20", "unit": None, "source": "default"},
        {"name": "track_counts", "setting": "on", "unit": None, "source": "default"},
        {"name": "track_io_timing", "setting": "off", "unit": None, "source": "default"},
        {"name": "shared_buffers", "setting": "16384", "unit": "8kB", "source": "configuration file"},
        {"name": "random_page_cost", "setting": "4", "unit": None, "source": "default"},
        {"name": "effective_io_concurrency", "setting": "1", "unit": None, "source": "default"},
        {"name": "effective_cache_size", "setting": "524288", "unit": "8kB", "source": "default"},
        {"name": "seq_page_cost", "setting": "1", "unit": None, "source": "default"},
    ],
}


def self_test(as_json: bool = False) -> int:
    """Run every check's finding logic against fixture rows. No database needed."""
    problems: List[str] = []
    all_findings: List[Finding] = []
    summary: List[Tuple[str, int, int]] = []

    for check in CHECKS:
        rows = SELF_TEST_FIXTURES.get(check.key)
        if rows is None:
            problems.append("no fixture for check '%s' - the self-test does not cover it"
                            % check.key)
            continue
        try:
            findings = check.fn(rows)
        except Exception as exc:  # noqa: BLE001
            problems.append("check '%s' raised %s: %s" % (check.key, type(exc).__name__, exc))
            continue
        for finding in findings:
            if not finding.evidence or not finding.impact or not finding.action:
                problems.append("check '%s' produced a finding with empty fields" % check.key)
            if finding.priority not in (1, 2, 3):
                problems.append("check '%s' produced priority %r" % (check.key, finding.priority))
            all_findings.append(finding)
        summary.append((check.key, len(rows), len(findings)))

    # Empty input must never raise, for any check.
    for check in CHECKS:
        try:
            check.fn([])
        except Exception as exc:  # noqa: BLE001
            problems.append("check '%s' raised on empty input: %s" % (check.key, exc))

    # Silent-on-clean-data: the connection check must stay quiet when healthy.
    quiet = check_connections([{"max_connections": 100, "total": 10, "active": 2,
                                "idle": 8, "idle_in_transaction": 0,
                                "waiting_on_locks": 0}])
    if quiet:
        problems.append("check_connections reported a finding for a healthy database")

    if as_json:
        print(json.dumps({
            "self_test": "pass" if not problems else "fail",
            "checks": len(CHECKS),
            "findings": [jsonable(f.__dict__) for f in all_findings],
            "problems": problems,
        }, indent=2))
        return 0 if not problems else 1

    print(rule("="))
    print("SELF-TEST - findings engine, no database required")
    print(rule("="))
    print("This exercises every check function against built-in fixture rows so you can")
    print("verify the report logic without pointing it at a database.")
    print("")
    for key, n_rows, n_find in summary:
        print("  %-24s %2d fixture row(s) -> %d finding(s)" % (key, n_rows, n_find))
    print("")
    print("Findings produced: %d (P1=%d P2=%d P3=%d)" % (
        len(all_findings),
        len([f for f in all_findings if f.priority == 1]),
        len([f for f in all_findings if f.priority == 2]),
        len([f for f in all_findings if f.priority == 3])))
    print("")
    if problems:
        print("PROBLEMS FOUND:")
        for p in problems:
            print("  ! " + p)
        print("")
        print("SELF-TEST: FAIL")
        return 1
    print("SELF-TEST: PASS")
    print("")
    print("Sample rendered finding:")
    print(rule("-"))
    print(render_finding(all_findings[0], 1))
    return 0


# =====================================================================
# CLI
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROGRAM,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "PostgreSQL Performance Toolkit - diagnostic runner.\n\n"
            "Connects read-only, runs the SQL in sql/ plus a set of threshold checks,\n"
            "and prints a prioritised findings report: evidence, likely impact, and the\n"
            "concrete action to take for each problem found."
        ),
        epilog=textwrap.dedent("""\
            examples:
              # the normal case
              export DATABASE_URL='postgresql://user:pass@localhost:5432/appdb'
              python3 run_diagnostics.py

              # machine-readable, for CI or a ticket
              python3 run_diagnostics.py --json > findings.json

              # dump the raw rows behind every query in sql/
              python3 run_diagnostics.py --raw > full-evidence.txt

              # only the connection and lock sections
              python3 run_diagnostics.py --only 05

              # see what the tool can detect, with no database at all
              python3 run_diagnostics.py --self-test

              # list the individual checks
              python3 run_diagnostics.py --list-checks

            exit codes:
              0  report produced, no priority-1 findings
              1  report produced, at least one priority-1 finding
              2  could not run (no DATABASE_URL, no driver, could not connect)

            database url:
              Read from the DATABASE_URL environment variable, or --database-url.
              Anything libpq accepts works, for example:
                postgresql://user:password@host:5432/dbname?sslmode=require
              Percent-encode special characters in the password (@ becomes %40).

            safety:
              This script executes SELECT statements only. It sets
              default_transaction_read_only = on for its session, applies a
              statement_timeout, and refuses to execute any statement in sql/ that is
              not a SELECT/WITH/VALUES/SHOW/EXPLAIN. It never runs VACUUM, ANALYZE,
              CREATE INDEX, ALTER or DROP - the report tells you those statements, it
              does not run them.
            """),
    )
    p.add_argument("--database-url", metavar="DSN",
                   help="PostgreSQL connection string. Defaults to $DATABASE_URL.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS, metavar="MS",
                   help="statement_timeout per query in milliseconds (default: %(default)s).")
    p.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT_S,
                   metavar="SECONDS",
                   help="connection timeout in seconds (default: %(default)s).")
    p.add_argument("--only", metavar="NN[,NN]",
                   help="run only the checks and sql/ files for these two-digit numbers "
                        "(for example --only 01,04). Check keys also work.")
    p.add_argument("--skip", metavar="NN[,NN]",
                   help="skip these two-digit numbers or check keys.")
    p.add_argument("--raw", action="store_true",
                   help="also print the first 10 rows returned by every statement in sql/.")
    p.add_argument("--json", action="store_true",
                   help="emit the whole report as JSON instead of formatted text.")
    p.add_argument("--output", metavar="FILE",
                   help="write the report to FILE as well as printing it.")
    p.add_argument("--no-sql-files", action="store_true",
                   help="skip executing the files in sql/ and run only the built-in checks.")
    p.add_argument("--sql-dir", metavar="DIR", default=SQL_DIR,
                   help="directory containing the .sql files (default: ./sql next to this script).")
    p.add_argument("--min-priority", type=int, choices=(1, 2, 3), default=3,
                   help="only report findings at this priority or more severe "
                        "(1 = act now only, 3 = everything; default: 3).")
    p.add_argument("--self-test", action="store_true",
                   help="run the findings engine against built-in fixtures. No database.")
    p.add_argument("--list-checks", action="store_true",
                   help="list every check and exit.")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-check progress on stderr.")
    p.add_argument("--debug", action="store_true",
                   help="print full Python tracebacks on failure instead of readable errors.")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p


def select_checks(only: Optional[str], skip: Optional[str]) -> List[Check]:
    def tokens(value: Optional[str]) -> Set[str]:
        if not value:
            return set()
        return {t.strip() for t in value.split(",") if t.strip()}

    only_t = tokens(only)
    skip_t = tokens(skip)
    selected = []
    for check in CHECKS:
        if only_t:
            if check.key not in only_t and not any(check.source.startswith("sql/%s" % t)
                                                   for t in only_t if t.isdigit()):
                continue
        if skip_t and (check.key in skip_t or any(check.source.startswith("sql/%s" % t)
                                                  for t in skip_t if t.isdigit())):
            continue
        selected.append(check)
    return selected


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started = time.time()

    if args.list_checks:
        print("Available checks (%d):" % len(CHECKS))
        print("")
        for check in CHECKS:
            flags = []
            if check.requires_pgss:
                flags.append("needs pg_stat_statements")
            if check.min_version_num:
                flags.append("PostgreSQL %d+" % (check.min_version_num // 10000))
            print("  %-24s %s" % (check.key, check.title))
            print("  %-24s source: %s%s" % ("", check.source,
                                            ("  [" + "; ".join(flags) + "]") if flags else ""))
            if check.note:
                print("  %-24s note: %s" % ("", check.note))
            print("")
        return 0

    if args.self_test:
        return self_test(as_json=args.json)

    dsn = args.database_url or os.environ.get("DATABASE_URL") or os.environ.get("PGURL")
    if not dsn:
        print("error: no database connection string.\n", file=sys.stderr)
        print("Set DATABASE_URL in the environment, or pass --database-url:\n"
              "    export DATABASE_URL='postgresql://user:password@host:5432/dbname'\n"
              "    python3 %s\n" % PROGRAM, file=sys.stderr)
        print("Special characters in the password must be percent-encoded "
              "(@ becomes %40).\n", file=sys.stderr)
        print("If you only want to see what this tool detects, run:\n"
              "    python3 %s --self-test\n" % PROGRAM, file=sys.stderr)
        return 2

    try:
        db = Db.connect(dsn, args.timeout, args.connect_timeout)
    except SetupError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            raise
        print("error: %s" % friendly_error(exc), file=sys.stderr)
        return 2

    try:
        target = db.describe_target()
        version_num = 0
        try:
            version_num = int(float(target.get("version_num") or 0))
        except (TypeError, ValueError):
            version_num = 0

        pgss_ok, pgss_text = pgss_status(db)
        selected = select_checks(args.only, args.skip)

        if not args.quiet:
            server_short = " ".join((target.get("server") or "?").split(" ")[:2])
            print("Connected to %s on %s:%s (%s)" % (
                target.get("database"), target.get("host"), target.get("port"),
                server_short), file=sys.stderr)
            print("pg_stat_statements: %s" % pgss_text, file=sys.stderr)
            print("Running %d checks%s..." % (
                len(selected), "" if args.no_sql_files else " and the sql/ files"),
                file=sys.stderr)

        file_results: List[FileResult] = []
        if not args.no_sql_files:
            only_digits = None
            if args.only:
                only_digits = {t.strip() for t in args.only.split(",")
                               if t.strip().isdigit()}
                if not only_digits:
                    only_digits = None
            file_results = run_sql_files(db, args.sql_dir, only_digits, args.raw,
                                         not args.quiet)

        check_results = run_checks(db, selected, version_num, pgss_ok, not args.quiet)

        findings: List[Finding] = []
        for result in check_results:
            findings.extend(result.findings)
        findings = [f for f in findings if f.priority <= args.min_priority]

        if args.json:
            payload = {
                "toolkit": "Postgres Performance Toolkit",
                "version": VERSION,
                "generated": datetime.now().isoformat(),
                "duration_seconds": round(time.time() - started, 3),
                "read_only": True,
                "target": {k: jsonable(v) for k, v in target.items()},
                "pg_stat_statements": pgss_text,
                "sql_files": [{
                    "path": fr.path,
                    "statements_total": fr.statements_total,
                    "statements_ok": fr.statements_ok,
                    "rows_returned": fr.rows_returned,
                    "errors": fr.errors,
                    "refused": fr.refused,
                } for fr in file_results],
                "checks": [{
                    "key": r.check.key,
                    "title": r.check.title,
                    "rows": jsonable(r.rows),
                    "skipped": r.skipped,
                    "error": r.error,
                } for r in check_results],
                "findings": [jsonable({
                    "priority": f.priority,
                    "priority_label": PRIORITY_LABELS[f.priority],
                    "key": f.key,
                    "title": f.title,
                    "evidence": f.evidence,
                    "impact": f.impact,
                    "action": f.action,
                    "source": f.source,
                }) for f in sorted(findings, key=lambda x: (x.priority, x.key))],
                "summary": {
                    "p1": len([f for f in findings if f.priority == 1]),
                    "p2": len([f for f in findings if f.priority == 2]),
                    "p3": len([f for f in findings if f.priority == 3]),
                },
            }
            text = json.dumps(payload, indent=2)
        else:
            text = render_report(target, findings, check_results, file_results,
                                 pgss_text, started, args.raw)

        if args.output:
            try:
                with open(args.output, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    if not text.endswith("\n"):
                        fh.write("\n")
                if not args.json:
                    print("\nReport written to %s" % args.output, file=sys.stderr)
            except OSError as exc:
                print("error: could not write %s: %s" % (args.output, exc), file=sys.stderr)

        print(text)

        if args.quiet:
            p1 = [f for f in findings if f.priority == 1]
            print("\n%d priority-1 finding(s), %d priority-2, %d priority-3." % (
                len(p1), len([f for f in findings if f.priority == 2]),
                len([f for f in findings if f.priority == 3])), file=sys.stderr)
        return 1 if any(f.priority == 1 for f in findings) else 0

    except KeyboardInterrupt:
        print("\ninterrupted - nothing was written to the database.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            raise
        print("error: %s" % friendly_error(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
