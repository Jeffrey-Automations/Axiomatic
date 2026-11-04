"""`dorossi_local_usage_totals` —— `/dorossi tokens` 本機用量段的純計算。

這支函式是 owner-only `/dorossi tokens` 第一段（「這支 bot 自己的 Dorossi 用量
＋美元估算」）唯一的計算來源，讀的是 append-only 帳本 `dorossi_usage.ndjson`。它的
合約有三條硬性要求，全部在這裡釘住：

* **永不 raise**：檔案缺席／某幾列是壞 JSON、不是 dict、缺 `ts` 或 `ts` 不是有限
  數——一律跳過那一列，不得讓整則指令回覆炸掉。
* **三個時窗都對**：今日＝同一個本地日曆日、近 7 日＝滾動 7×86400 秒、累計＝全部。
  每一格都要同時算對 token（`in`＋`out`）與美元（`cost_usd` 直接加總）。
* **壞列不進帳**：壞的一列不能污染任何一格的小計（否則報表會悄悄多算或少算）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import dorossi_backend as db  # noqa: E402


# 固定一個「現在」：2026-01-15 12:00:00 當地時間，換成 epoch 供全檔共用。
_NOW = time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, -1))
_DAY = 86400.0


def _write(path: Path, records) -> None:
    """把 record（dict）或原始字串逐行寫進帳本；字串原樣寫（用來塞壞列）。"""
    lines = []
    for rec in records:
        lines.append(rec if isinstance(rec, str)
                     else json.dumps(rec, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_three_windows_token_and_usd():
    """今日／近 7 日／累計各自的 token（in＋out）與美元加總都要對。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [
            {"ts": _NOW - 1, "in": 100, "out": 10, "cost_usd": 1.0},      # 今日
            {"ts": _NOW - 2 * _DAY, "in": 200, "out": 20, "cost_usd": 2.5},  # 近 7 日
            {"ts": _NOW - 10 * _DAY, "in": 300, "out": 30, "cost_usd": 4.0},  # 只進累計
        ])
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    assert out["today"] == {"tokens": 110, "usd": 1.0, "unpriced": 0}
    assert out["last7d"] == {"tokens": 330, "usd": 3.5, "unpriced": 0}
    assert out["lifetime"] == {"tokens": 660, "usd": 7.5, "unpriced": 0}


def test_today_is_same_local_calendar_day_not_rolling_24h():
    """今日以**本地日曆日**為界：今天稍早（同一天但超過 24 小時前也算今天）算今日；
    昨天深夜（不到 24 小時前但已跨日）不算今日。"""
    import tempfile
    day_start = time.mktime(time.localtime(_NOW)[:3] + (0, 0, 0, 0, 0, -1))
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [
            {"ts": day_start + 5, "in": 10, "out": 0, "cost_usd": 0.1},     # 今天 00:00 剛過
            {"ts": day_start - 5, "in": 99, "out": 0, "cost_usd": 9.9},     # 昨天 23:59
        ])
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    assert out["today"] == {"tokens": 10, "usd": 0.1, "unpriced": 0}
    # 昨晚那一列仍在近 7 日與累計裡。
    assert out["last7d"]["tokens"] == 109
    assert out["lifetime"]["tokens"] == 109


def test_seven_day_window_boundary_is_inclusive_rolling():
    """近 7 日窗是 `ts >= now - 7*86400`：正好 7 天前算進、7 天又 1 秒前不算。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [
            {"ts": _NOW - 7 * _DAY, "in": 1, "out": 0, "cost_usd": 0.0},      # 剛好 7 天
            {"ts": _NOW - 7 * _DAY - 1, "in": 500, "out": 0, "cost_usd": 0.0},  # 超過一秒
        ])
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    assert out["last7d"]["tokens"] == 1
    assert out["lifetime"]["tokens"] == 501


def test_malformed_lines_are_skipped_not_raised():
    """壞 JSON、不是 dict、缺 `ts`、`ts` 非有限數、空白列——全部跳過且不 raise，
    好的列照算。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [
            "{ this is not json",
            "[1, 2, 3]",                                     # 是 JSON 但不是 dict
            "   ",                                            # 空白
            {"in": 5, "out": 5, "cost_usd": 1.0},            # 缺 ts
            {"ts": "nope", "in": 5, "out": 5, "cost_usd": 1.0},  # ts 不是數字
            {"ts": float("nan"), "in": 9, "out": 9, "cost_usd": 9.0},  # ts 是 NaN
            {"ts": _NOW - 1, "in": 40, "out": 2, "cost_usd": 3.0},     # 唯一好列
        ])
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    assert out["today"] == {"tokens": 42, "usd": 3.0, "unpriced": 0}
    assert out["lifetime"] == {"tokens": 42, "usd": 3.0, "unpriced": 0}


def test_missing_fields_and_bad_values_count_as_zero():
    """缺 `in`／`out`／`cost_usd`、或值是 bool／非有限數，都當 0，不得 raise。
    有效的 `ts` 讓這一列仍被納入時窗（只是加 0）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [
            {"ts": _NOW - 1},                                            # 只有 ts
            {"ts": _NOW - 1, "in": True, "out": 3, "cost_usd": True},    # bool 不算
            {"ts": _NOW - 1, "in": 1, "out": 1, "cost_usd": float("inf")},  # inf 當 0
        ])
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    assert out["today"] == {"tokens": 5, "usd": 0.0, "unpriced": 0}


def test_missing_file_returns_zeros():
    """檔案根本不存在：三格全 0，不 raise。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "does_not_exist.ndjson"
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    blank = {"tokens": 0, "usd": 0.0, "unpriced": 0}
    assert out == {"today": blank, "last7d": blank, "lifetime": blank}


def test_empty_file_returns_zeros():
    """空檔（含只有空白列）：三格全 0。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        p.write_text("\n  \n\n", encoding="utf-8")
        out = db.dorossi_local_usage_totals(_NOW, path=p)

    assert out["today"]["tokens"] == 0
    assert out["lifetime"]["usd"] == 0.0


def test_path_accepts_string_not_only_pathlib():
    """`path` 給字串也要能讀（渲染端傳的是預設的 Path，但介面別挑型別）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [{"ts": _NOW - 1, "in": 7, "out": 3, "cost_usd": 0.5}])
        out = db.dorossi_local_usage_totals(_NOW, path=str(p))

    assert out["today"] == {"tokens": 10, "usd": 0.5, "unpriced": 0}


def test_gemini_estimate_keeps_actual_charge_unknown():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dorossi_usage.ndjson"
        _write(p, [{"ts": _NOW - 1, "backend": "gemini", "in": 100,
                    "out": 20, "cost_usd": 0.01, "cost_estimated": True,
                    "cost_unknown": True}])
        out = db.dorossi_local_usage_totals(_NOW, path=p)
    assert out["today"] == {"tokens": 120, "usd": 0.01, "unpriced": 1}


def test_cached_tokens_are_included(tmp_path):
    p = tmp_path / "usage.ndjson"
    _write(p, [{"ts": _NOW - 1, "in": 2, "cr": 1000, "cc": 300, "out": 20}])
    assert db.dorossi_local_usage_totals(_NOW, path=p)["today"]["tokens"] == 1322
