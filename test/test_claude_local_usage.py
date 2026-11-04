"""Local transcript accounting: repeated blocks, forks, appends and bad rows."""
import asyncio
from datetime import datetime, timezone
import json
import sqlite3
import time
import types

import pytest
import _claude_local_usage as usage
import discord_bot as b


NOW = time.time()


def _row(mid="msg-1", ts=NOW-1, **fields):
    return {"type": "assistant", "requestId": "req-" + mid,
            "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "message": {"id": mid, "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
                                  "cache_creation_input_tokens": 20,
                                  "output_tokens": 5, **fields}}}


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _totals(tmp_path):
    return usage.claude_local_usage_totals(NOW, projects=tmp_path / "projects",
                                          cache=tmp_path / "index.sqlite3")


def test_repeated_blocks_and_forked_history_are_counted_once(tmp_path):
    row = _row()
    _write(tmp_path / "projects" / "a" / "session.jsonl", [row, row])
    _write(tmp_path / "projects" / "b" / "subagents" / "agent.jsonl",
           [row, _row("msg-2")])
    totals = _totals(tmp_path)
    assert totals["today"]["tokens"] == 270
    # Opus 5.5: fresh 4, read .2, 5m write 5, output 20 / million.
    assert totals["today"]["usd"] == pytest.approx(.00052)
    assert totals["today"]["unpriced"] == 0
    with sqlite3.connect(tmp_path / "index.sqlite3") as conn:
        assert "content" not in {r[1] for r in conn.execute("PRAGMA table_info(calls)")}


def test_append_and_partial_line_are_read_on_the_next_query(tmp_path, monkeypatch):
    path = tmp_path / "projects" / "s.jsonl"
    _write(path, [_row()])
    assert _totals(tmp_path)["today"]["tokens"] == 135
    original = usage._entry
    seen = []
    monkeypatch.setattr(usage, "_entry", lambda row: (seen.append(row), original(row))[1])
    _totals(tmp_path)
    assert seen == []  # unchanged files aren't reparsed
    raw = json.dumps(_row("msg-2"))
    with path.open("a", encoding="utf-8") as f:
        f.write(raw[:50])
    assert _totals(tmp_path)["today"]["tokens"] == 135
    with path.open("a", encoding="utf-8") as f:
        f.write(raw[50:] + "\n")
    assert _totals(tmp_path)["today"]["tokens"] == 270
    assert len(seen) == 1


def test_truncation_invalidates_the_file_index(tmp_path):
    path = tmp_path / "projects" / "s.jsonl"
    _write(path, [_row(), _row("msg-2")])
    assert _totals(tmp_path)["today"]["tokens"] == 270
    _write(path, [_row("msg-3")])
    assert _totals(tmp_path)["today"]["tokens"] == 135


def test_windows_unknown_models_and_larger_final_output(tmp_path):
    unknown = _row("unknown", ts=NOW-10*86400)
    unknown["message"]["model"] = "claude-future-999"
    _write(tmp_path / "projects" / "s.jsonl", [
        _row(), _row(output_tokens=15), _row("week", ts=NOW-2*86400), unknown, unknown])
    totals = _totals(tmp_path)
    assert totals["today"]["tokens"] == 145
    assert totals["last7d"]["tokens"] == 280
    assert totals["lifetime"]["tokens"] == 415
    assert totals["lifetime"]["unpriced"] == 1


def test_hour_cache_and_fast_mode_prices():
    entry = usage._entry(_row(speed="fast", cache_creation={"ephemeral_1h_input_tokens": 20}))
    assert entry[-1] == pytest.approx((40+20+160+100)*2/1e6)


def test_bad_rows_are_skipped_and_missing_source_is_reported(tmp_path):
    assert _totals(tmp_path)["available"] is False
    path = tmp_path / "projects" / "s.jsonl"
    bad = _row()
    bad["timestamp"] = "bad"
    _write(path, [None, [], {"type": "user"}, bad, _row()])
    with path.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    assert _totals(tmp_path)["today"]["tokens"] == 135


def test_tokens_handler_includes_external_usage_without_bot_ledger_writes(monkeypatch):
    replies = []
    totals = {k: {"tokens": 1234567, "usd": 12.3, "unpriced": 0}
              for k in ("today", "last7d", "lifetime")}
    totals["available"] = True
    monkeypatch.setattr(b, "claude_local_usage_totals", lambda now: totals)
    async def query():
        return "額度正常"
    async def reply(_message, text):
        replies.append(text)
    monkeypatch.setattr(b, "_query_codex_account_usage", query)
    monkeypatch.setattr(b, "_query_agy_account_usage", query)
    monkeypatch.setattr(b, "_render_dorossi_local_usage", lambda: "bot 用量")
    monkeypatch.setattr(b, "safe_reply", reply)
    msg = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    asyncio.run(b.mcmd_tokens(msg, ""))
    assert "1,234,567" in "\n".join(replies)
    assert "勿與上方加總" in "\n".join(replies)
