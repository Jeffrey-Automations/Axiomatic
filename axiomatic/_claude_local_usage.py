"""Read token counters from all local Claude Code transcripts, never their text.

The SQLite index stores only identifiers, timestamps and counters. Append offsets
avoid rescanning multi-GB history on every report. Source transcripts stay read-only.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "dorossi_claude_usage.sqlite3"
_LOCK = threading.Lock()
# USD per million tokens; checked 2026-10-07 against the official pricing table:
# https://platform.claude.com/docs/en/about-claude/pricing
# Exact versions only: a future/unknown model must never inherit a guessed price.
_RATES = {
    "claude-opus-5-5": (4, 20, .20),
    "claude-sonnet-5-5": (2, 10, .20),
    "claude-sonnet-5": (2, 10, .20),
    "claude-haiku-4-5": (1, 5, .10),
    "claude-haiku-3-5": (.8, 4, .08),
    "claude-opus-4": (15, 75, 1.5),
    "claude-opus-4-1": (15, 75, 1.5),
    "claude-sonnet-4": (3, 15, .30),
    "claude-sonnet-4-5": (3, 15, .30),
    "claude-sonnet-4-6": (3, 15, .30),
    **{f"claude-opus-{v}": (5, 25, .50)
       for v in ("4-5", "4-6", "4-7", "4-8", "5")},
    **{f"claude-{family}-{v}": (10, 50, read)
       for family in ("fable", "mythos") for v, read in (("5", 1), ("5-1", .25))},
}


def _count(value) -> int:
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        return int(value)
    return 0


def _entry(row):
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        return None
    usage, mid = message.get("usage"), message.get("id")
    if not isinstance(usage, dict) or not isinstance(mid, str) or not mid:
        return None
    try:
        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        ts = stamp.timestamp()
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        return None
    tokens = [_count(usage.get(k)) for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
        "output_tokens")]
    if not any(tokens):
        return None
    model = message.get("model")
    model = model if isinstance(model, str) else ""
    rate = _RATES.get(re.sub(r"-\d{8}$", "", model))
    cost = None
    if rate is not None:
        fresh, read, write, output = tokens
        cache = usage.get("cache_creation")
        hour = min(write, _count(cache.get("ephemeral_1h_input_tokens"))) \
            if isinstance(cache, dict) else 0
        base, out_rate, read_rate = rate
        multiplier = 1.1 if usage.get("inference_geo") == "us" else 1
        if usage.get("speed") == "fast":
            if model in ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"):
                multiplier *= 2
            else:
                rate = None
        if rate is not None:
            cost = (fresh * base + read * read_rate
                    + (write - hour) * base * 1.25 + hour * base * 2
                    + output * out_rate) * multiplier / 1_000_000
    # Content blocks repeat the same API response. Resumed/forked files may also
    # copy it: deduplicate across ALL files by message id and request id.
    request = row.get("requestId")
    key = json.dumps([mid, request if isinstance(request, str) else ""])
    return key, ts, *tokens, cost


def _refresh(conn, root: Path) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS files "
                 "(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, offset INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS calls "
                 "(path TEXT, key TEXT, ts REAL, fresh INTEGER, read INTEGER, "
                 "write INTEGER, output INTEGER, cost REAL, PRIMARY KEY(path,key))")
    known = {row[0]: row[1:] for row in conn.execute("SELECT * FROM files")}
    present = set()
    for path in root.rglob("*.jsonl"):
        name = str(path.resolve())
        present.add(name)
        try:
            stat = path.stat()
            old = known.get(name)
            if old and old[:2] == (stat.st_size, stat.st_mtime_ns):
                continue
            # A same-size rewrite or truncation invalidates that file's index.
            offset = old[2] if old and stat.st_size > old[0] else 0
            with conn:
                if not offset:
                    conn.execute("DELETE FROM calls WHERE path=?", (name,))
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        start = stream.tell()
                        raw = stream.readline()
                        if not raw or not raw.endswith(b"\n"):
                            offset = start  # a writer may finish this row later
                            break
                        try:
                            entry = _entry(json.loads(raw))
                        except (ValueError, UnicodeError):
                            continue
                        if entry is None:
                            continue
                        conn.execute(
                            "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(path,key) DO UPDATE SET "
                            "ts=min(ts,excluded.ts),fresh=max(fresh,excluded.fresh),"
                            "read=max(read,excluded.read),write=max(write,excluded.write),"
                            "output=max(output,excluded.output),"
                            "cost=CASE WHEN cost IS NULL AND excluded.cost IS NULL "
                            "THEN NULL ELSE max(coalesce(cost,0),coalesce(excluded.cost,0)) END",
                            (name, *entry))
                conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                             (name, stat.st_size, stat.st_mtime_ns, offset))
        except OSError:
            continue
    with conn:
        for stale in known.keys() - present:
            conn.execute("DELETE FROM calls WHERE path=?", (stale,))
            conn.execute("DELETE FROM files WHERE path=?", (stale,))


def claude_local_usage_totals(now: float, *, projects=None, cache=DEFAULT_CACHE) -> dict:
    """All available local sessions, including subagents; no bot-ledger addition.

    Return today, rolling seven days and available-history totals. An unreadable
    source/index is a reported failure, never a successful zero-usage report.
    """
    result = {key: {"tokens": 0, "usd": 0.0, "unpriced": 0}
              for key in ("today", "last7d", "lifetime")}
    root = (Path(projects) if projects is not None else
            Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects")
    if not root.is_dir():
        return {**result, "available": False}
    try:
        local = time.localtime(now)
        today = time.mktime(local[:3] + (0, 0, 0, 0, 0, -1))
        with _LOCK, sqlite3.connect(str(cache), timeout=30) as conn:
            _refresh(conn, root)
            # Group globally, not per project: copied/forked conversation history
            # and repeated content blocks belong to the original API call.
            for ts, tokens, cost in conn.execute(
                    "SELECT min(ts),max(fresh)+max(read)+max(write)+max(output),max(cost) "
                    "FROM calls GROUP BY key"):
                if ts > now:
                    continue
                buckets = ["lifetime"]
                if ts >= now - 7 * 86400:
                    buckets.append("last7d")
                if ts >= today:
                    buckets.append("today")
                for key in buckets:
                    result[key]["tokens"] += tokens
                    result[key]["usd"] += cost or 0
                    result[key]["unpriced"] += int(cost is None)
        result["available"] = True
        return result
    except (OSError, sqlite3.Error, ValueError, OverflowError):
        return {**result, "available": False}
