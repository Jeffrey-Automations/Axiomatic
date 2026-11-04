"""je 變體的 Chrome 開機路徑——`webrunner_je_only.start_driver`。

`/run` 的**預設**變體就是 je，所以這支是正式批次每一次開機（以及每一次記憶體回收重啟）
真正走的那條路。39 行敘述裡 38 行從來沒被跑過；既有的 `test_je_facade.py` 只對它做
AST 檢查——確認 `service=` 真的傳進 `wr.set_driver(...)`，也就是**形狀**，而不是「重試
的時候會發生什麼」。

這條路最貴的失效是**重試把自己弄壞**。三次嘗試之間要做四件事，少任何一件，重試都會
用一個保證失敗的狀態再試一次，然後把「Chrome 起不來」報成一個與真正原因無關的訊息：

| 步驟 | 少了它會怎樣 |
|---|---|
| `_dump_chromedriver_log_tail()` | chromedriver 每次啟動會**清空** `--log-path` 指的檔，所以這一次失敗的原因在下一次建立 service 時就沒了——傾印必須在下一次嘗試**之前** |
| `_kill_orphan_chrome()` | 半途死掉的 chrome.exe 還握著記憶體，低記憶體機器上重試會跟著 OOM（「一路重試到崩潰」） |
| 重新 `_snapshot_chrome_profile()` ＋ `_clear_snapshot_locks()` | 舊快照裡的 lockfile 還在，重試必定再失敗一次 |
| `cli_args[-1] = f"--user-data-dir={snapshot}"` | 快照重做了卻沒告訴 Chrome，等於整段白做——**這一步 2026-09-07 之前在 je 這一側是漏的**，selenium 變體一直都有 |

另外兩件事是這個 repo 付過學費的：

* **`log_output=`，不是 `log_path=`。** selenium 的 `Service` 沒有 `log_path` 這個參數，
  傳進去會一路掉進 `**kwargs` 被最底層安靜丟掉——不警告、不報錯，記錄檔從此不存在。
  selenium 變體踩了好幾個月。
* **診斷走 `full_error_detail`，而且連 `__cause__` 一起印。** je_web_runner 的包裝層會
  把底層訊息包起來，`__cause__` 是最可能放著真正 selenium 例外的位置；而
  `WebDriverException` 的 `args` 是空的，`!r` 印出來只剩一對空括號。
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest
import selenium.webdriver.chrome.service as chrome_service

import os

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import webrunner_je_only as je  # noqa: E402
import _webrunner_shared as ws  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


class _WrapperError(Exception):
    """je_web_runner 的包裝層例外：訊息在 `msg`，`args` 是空的（跟 selenium 一樣）。"""

    def __init__(self, msg: str):
        super().__init__()
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


@pytest.fixture
def boot(monkeypatch, tmp_path):
    env = types.SimpleNamespace(
        # 腳本：每次嘗試要丟的例外，`None` ＝ 成功。
        script=[None],
        order=[], set_driver_calls=[], services=[], snapshots=[],
        cleared=[], kills=0, dumps=0, sleeps=[], warmups=[],
        versions=[], versions_raise=None, stealth=[], stealth_raise=None,
        # CDP 回的 script identifier；None ＝ 函式庫沒拿到像樣的回應。
        stealth_identifier="1", quits=0,
        caps={"browserVersion": "153.0.0.0"},
    )

    def _snapshot():
        target = tmp_path / f"snap{len(env.snapshots)}"
        target.mkdir()
        env.snapshots.append(target)
        env.order.append("snapshot")
        return target

    def _set_driver(kind, **kwargs):
        index = len(env.set_driver_calls)
        # ⚠️ `options` 是**同一個 list 物件**，重試時就地被改（`cli_args[-1] = …`）。
        # 直接存參考的話，每一次嘗試都會讀到**最後**那個值，於是「重試有沒有換
        # 快照」這支測試永遠通過。要存當下的複本。
        snapshot_of = {k: (list(v) if isinstance(v, list) else
                           dict(v) if isinstance(v, dict) else v)
                       for k, v in kwargs.items()}
        env.set_driver_calls.append((kind, snapshot_of))
        env.order.append("set_driver")
        failure = env.script[index] if index < len(env.script) else None
        if failure is not None:
            raise failure

    class _FakeService:
        def __init__(self, **kwargs):
            env.services.append(kwargs)
            env.order.append("service")

    monkeypatch.setattr(chrome_service, "Service", _FakeService)
    # 函式庫的契約另有自己的測試（本檔第六節）；這裡只記下它被呼叫、排在哪裡。真的跑的話
    # 會去讀**這台機器**載入的 je_web_runner，並改掉整個測試行程的失敗政策。
    env.contract_raise = None

    def _contract():
        env.order.append("contract")
        if env.contract_raise is not None:
            raise env.contract_raise

    monkeypatch.setattr(je, "_require_wrapper_contract", _contract)
    # ※ **這一行不能省。** 沒有它，`start_driver()` 會真的去讀這台機器的 Selenium
    # Manager 快取，到期就真的跑 selenium-manager（連網、改寫正式快取）。暖機本身的
    # 行為由 `test_driver_warmup.py` 測；這裡只記下它被呼叫、排在哪裡。
    monkeypatch.setattr(ws, "warm_up_selenium_manager",
                        lambda locate: (env.warmups.append(locate),
                                        env.order.append("warmup")))
    monkeypatch.setattr(je, "_trim_chromedriver_log",
                        lambda: env.order.append("trim"))
    monkeypatch.setattr(je, "_rotate_chromedriver_log",
                        lambda: env.order.append("rotate"))
    monkeypatch.setattr(je, "_snapshot_chrome_profile", _snapshot)
    monkeypatch.setattr(je, "_clear_snapshot_locks",
                        lambda snap: (env.cleared.append(Path(snap)),
                                      env.order.append("clear")))
    monkeypatch.setattr(je, "_dump_chromedriver_log_tail",
                        lambda: (setattr(env, "dumps", env.dumps + 1),
                                 env.order.append("dump")))

    def _kill():
        env.kills += 1
        env.order.append("kill")
        return 0

    monkeypatch.setattr(je, "_kill_orphan_chrome", _kill)
    monkeypatch.setattr(je.time, "sleep",
                        lambda secs: (env.sleeps.append(secs),
                                      env.order.append("sleep")))

    def _add_script(script):
        env.stealth.append(script)
        env.order.append("stealth")
        if env.stealth_raise is not None:
            raise env.stealth_raise
        return env.stealth_identifier

    def _quit():
        env.quits += 1
        env.order.append("quit")

    monkeypatch.setattr(je, "wr", types.SimpleNamespace(
        set_driver=_set_driver,
        current_webdriver=types.SimpleNamespace(capabilities=env.caps),
        add_script_to_evaluate_on_new_document=_add_script,
        quit=_quit))

    def _log_versions(caps):
        env.versions.append(caps)
        if env.versions_raise is not None:
            raise env.versions_raise

    monkeypatch.setattr(ws, "log_driver_versions", _log_versions)
    monkeypatch.setattr(je, "_CURRENT_SNAPSHOT_PROFILE", None)
    return env


def _options_of(env, index: int) -> list[str]:
    return env.set_driver_calls[index][1]["options"]


def _user_data_dir(env, index: int) -> str:
    return [a for a in _options_of(env, index)
            if a.startswith("--user-data-dir=")][0]


# ---------------------------------------------------------------------------
# 一、順利開機的那一次
# ---------------------------------------------------------------------------

def test_a_clean_boot_spawns_once_and_touches_nothing_else(boot):
    je.start_driver()
    assert len(boot.set_driver_calls) == 1
    assert (boot.kills, boot.dumps, boot.sleeps) == (0, 0, [])


def test_the_log_is_capped_and_rotated_before_the_service_is_built(boot):
    """封頂與改名都必須在 `ChromeService(...)` **之前**。

    那之後那個檔就有 chromedriver 握著，中途截斷會被作業系統補零——檔案看起來還在、
    大小也對，內容卻是一整段 `\\x00`。兩個變體的順序相同、理由相同。
    """
    je.start_driver()
    assert boot.order.index("trim") < boot.order.index("service")
    assert boot.order.index("rotate") < boot.order.index("service")
    assert boot.order.index("trim") < boot.order.index("rotate")


def test_the_driver_warmup_runs_first_and_once_even_when_the_spawn_retries(boot):
    """Selenium Manager 暖機排在一切之前，整次開機只做一次——重試迴圈裡不再做。

    je 這一側也要暖機：`wr.set_driver` 丟掉 `install()` 的回傳值，driver 跟 selenium
    變體一樣由 Selenium Manager 解析（`architecture.md` §8.40）。暖機本身的
    行為在 `test_driver_warmup.py`。
    """
    boot.script = [_WrapperError("first attempt fails"), None]
    je.start_driver()
    assert len(boot.set_driver_calls) == 2, "前提：真的重試了一次"
    assert boot.order[:2] == ["contract", "warmup"], boot.order
    assert boot.order.count("warmup") == 1, boot.order
    assert boot.order.count("contract") == 1, "契約檢查跑進了重試迴圈"
    assert boot.warmups == [je._selenium_manager_binary]


def test_the_snapshot_is_taken_and_unlocked_before_chrome_is_told_about_it(boot):
    """快照要先做、鎖要先清，然後那個路徑才會變成 `--user-data-dir=`。"""
    je.start_driver()
    assert boot.order.index("snapshot") < boot.order.index("set_driver")
    assert boot.order.index("clear") < boot.order.index("set_driver")
    assert boot.cleared == [boot.snapshots[0]]
    assert _user_data_dir(boot, 0) == f"--user-data-dir={boot.snapshots[0]}"


def test_the_service_names_log_output_not_log_path(boot):
    """**`log_output=`，不是 `log_path=`。**

    selenium 的 `Service` 沒有 `log_path` 這個參數；傳進去會一路掉進 `**kwargs`
    被最底層安靜丟掉——不警告、不報錯，記錄檔從此不存在。selenium 變體踩了好幾個
    月，而 je 這一側在 2026-09-09 以前根本沒有記錄檔，於是「je 為什麼起不來」在正式
    環境是查不到的（`_watch_webrunner` 又會在 5 分鐘內靜靜轉跑 selenium 變體）。
    """
    je.start_driver()
    kwargs = boot.services[0]
    assert "log_output" in kwargs, f"ChromeService 的關鍵字變了：{kwargs}"
    assert "log_path" not in kwargs
    assert str(ws._CHROMEDRIVER_LOG) == kwargs["log_output"]


def test_the_service_asks_for_info_level_logging(boot):
    """`--log-level=INFO` 而不是 `--verbose`：實測 root cause 一字不差，但平時的
    雜訊少九成（4370 → 484 bytes/命令）。"""
    je.start_driver()
    assert boot.services[0]["service_args"] == ["--log-level=INFO"]


def test_the_memory_conserving_flags_are_passed(boot):
    """長時間執行時的 OOM（「Aw, Snap!」）是這台機器真的發生過的失效。

    這幾個旗標是當時的緩解措施；靜靜掉了不會有任何立即症狀，只會讓幾十小時後的
    批次又開始崩。
    """
    je.start_driver()
    args = _options_of(boot, 0)
    for flag in ("--disk-cache-size=", "--media-cache-size=",
                 "--disable-renderer-backgrounding",
                 "--disable-blink-features=AutomationControlled"):
        assert any(a.startswith(flag) for a in args), f"少了 {flag}"


def test_the_automation_switches_are_excluded(boot):
    je.start_driver()
    experimental = boot.set_driver_calls[0][1]["experimental_options"]
    assert experimental["excludeSwitches"] == ["enable-automation"]
    assert experimental["useAutomationExtension"] is False


def test_the_webdriver_manager_download_is_switched_off_on_every_attempt(boot):
    """`install_driver=False`，每一次嘗試都要帶（§8.222 G10）。

    `set_driver` 預設先跑 webdriver-manager 的 `install()`、再把結果丟掉——真正選 driver
    的是 Selenium Manager。下載來源連不上的那一天，開機就失敗、而且計入快速失敗，原因卻
    是一個從來沒被用到的檔案。只有第一次帶、重試沒帶的話，偶發失敗之後的重試反而多一個
    網路相依。
    """
    boot.script = [_WrapperError("一"), None]
    je.start_driver()
    assert len(boot.set_driver_calls) == 2, "前提：真的重試了一次"
    assert [call[1].get("install_driver") for call in boot.set_driver_calls] == [
        False, False], boot.set_driver_calls


def test_the_wrapper_contract_is_checked_before_anything_touches_the_machine(boot):
    """函式庫缺 API 就在這裡失敗：還沒暖機、還沒快照、還沒開瀏覽器。

    暖機一次可能要 1～3 分鐘、快照會先刪掉再重建 `.chrome_profile_snap/`——在一個注定
    開不了機的函式庫上先做這兩件事，只會讓失敗晚好幾分鐘才出現。
    """
    boot.contract_raise = RuntimeError("沒有 set_raise_wrapper_errors")
    with pytest.raises(RuntimeError, match="set_raise_wrapper_errors"):
        je.start_driver()
    assert boot.order == ["contract"], boot.order
    assert boot.set_driver_calls == [] and boot.snapshots == []


# ---------------------------------------------------------------------------
# 二、重試：四件事都要做，而且順序有意義
# ---------------------------------------------------------------------------

def test_a_failed_attempt_is_retried_up_to_three_times(boot):
    boot.script = [_WrapperError("一"), _WrapperError("二"), None]
    je.start_driver()
    assert len(boot.set_driver_calls) == 3
    assert (boot.kills, boot.dumps) == (2, 2)
    assert boot.sleeps == [2.0, 2.0]


def test_the_log_tail_is_dumped_before_the_next_attempt(boot):
    """**順序在這裡是內容的一部分。**

    chromedriver 每次啟動會清空 `--log-path` 指的那個檔，所以這一次失敗的原因在
    attempt+1 建立 service 的瞬間就沒了。傾印晚一步，看到的就是下一次的（或空的）。
    """
    boot.script = [_WrapperError("一"), None]
    je.start_driver()
    first_dump = boot.order.index("dump")
    second_service = [i for i, step in enumerate(boot.order)
                      if step == "service"][1]
    assert first_dump < second_service, boot.order


def test_each_retry_gets_a_fresh_unlocked_snapshot(boot):
    """舊快照裡的 lockfile 還在，重試必定再失敗一次。"""
    boot.script = [_WrapperError("一"), None]
    je.start_driver()
    assert len(boot.snapshots) == 2
    assert boot.cleared == boot.snapshots


def test_the_retry_actually_tells_chrome_about_the_new_snapshot(boot):
    """**2026-09-07 之前 je 這一側漏的就是這一行。**

    快照重做了、鎖也清了，卻沒有更新 `--user-data-dir=`，於是 Chrome 還是被指向
    那個鎖著的舊目錄——整段補救白做，而且看起來「重試邏輯都在」。selenium 變體
    一直都有這一步。
    """
    boot.script = [_WrapperError("一"), None]
    je.start_driver()
    assert _user_data_dir(boot, 1) == f"--user-data-dir={boot.snapshots[1]}"
    assert _user_data_dir(boot, 0) != _user_data_dir(boot, 1)


def test_orphan_chrome_is_reaped_between_attempts(boot):
    """半途死掉的 chrome.exe 還握著記憶體；低記憶體機器上重試會跟著 OOM。"""
    boot.script = [_WrapperError("一"), None]
    je.start_driver()
    kill_at = boot.order.index("kill")
    second_attempt = [i for i, s in enumerate(boot.order) if s == "set_driver"][1]
    assert kill_at < second_attempt
    assert boot.order.index("dump") < kill_at, "傾印要在殺掉之前"


def test_nothing_is_prepared_after_the_last_attempt_fails(boot):
    """最後一次失敗之後不要再做準備工作——沒有下一次了，那些動作只是延後報錯。"""
    boot.script = [_WrapperError("一"), _WrapperError("二"), _WrapperError("三")]
    with pytest.raises(RuntimeError):
        je.start_driver()
    assert boot.kills == 2, "最後一次失敗後又去掃了一輪孤兒行程"
    assert len(boot.snapshots) == 3
    assert boot.sleeps == [2.0, 2.0]


# ---------------------------------------------------------------------------
# 三、三次都失敗
# ---------------------------------------------------------------------------

def test_three_failures_raise_and_keep_the_last_error_as_the_cause(boot):
    """`raise ... from last_err`：鏈子斷掉的話，traceback 只剩一句我們自己寫的
    猜測，真正的 selenium／webdriver-manager 錯誤就完全不見了。"""
    last = _WrapperError("session not created: 版本對不上")
    boot.script = [_WrapperError("一"), _WrapperError("二"), last]
    with pytest.raises(RuntimeError) as caught:
        je.start_driver()
    assert caught.value.__cause__ is last


def test_the_final_message_carries_the_last_error_text(boot):
    """訊息走 `full_error_detail`，不是 `!r`。

    `WebDriverException` 把訊息放在 `self.msg`、`args` 是空的，所以 `repr()` 印出來
    只剩一對空括號——而這句話是無人值守的批次失敗時唯一留下的東西。
    """
    boot.script = [_WrapperError("x")] * 2 + [_WrapperError("版本對不上")]
    with pytest.raises(RuntimeError) as caught:
        je.start_driver()
    assert "版本對不上" in str(caught.value)
    assert "3" in str(caught.value), "沒說總共試了幾次"


def test_a_wrapped_cause_is_printed_with_the_attempt(boot, capsys):
    """`__cause__` 是**最可能**放著真正 selenium 例外的位置：je_web_runner 的包裝層
    在外面又包了一層，只印外層等於什麼都沒印。"""
    inner = _WrapperError("chromedriver 版本 152，Chrome 是 153")
    outer = _WrapperError("set_driver failed")
    outer.__cause__ = inner
    boot.script = [outer, None]
    je.start_driver()
    err = capsys.readouterr().err
    assert "chromedriver 版本 152" in err, f"包在裡面的真正原因不見了：{err}"
    assert "set_driver failed" in err


def test_without_a_cause_the_line_stays_clean(boot, capsys):
    """沒有 `__cause__` 時不要印一個空的 `(cause: )`——那會讓人以為原因被吞掉了。"""
    boot.script = [_WrapperError("單純失敗"), None]
    je.start_driver()
    err = capsys.readouterr().err
    assert "cause:" not in err, err


def test_every_failed_attempt_is_numbered(boot, capsys):
    boot.script = [_WrapperError("一"), _WrapperError("二"), None]
    je.start_driver()
    err = capsys.readouterr().err
    assert "1/3" in err and "2/3" in err


# ---------------------------------------------------------------------------
# 四、開機成功之後
# ---------------------------------------------------------------------------

def test_the_snapshot_that_actually_worked_is_the_one_recorded(boot):
    """`_CURRENT_SNAPSHOT_PROFILE` 是 `main()` 收尾時拿來 sync back 的路徑。

    記成第一次那個（而不是重試後真正用的那個）會有兩個後果：這一輪的登入態／
    cookie 全部丟掉，而且同步回去的是一個從來沒被 Chrome 寫過的空快照。
    """
    boot.script = [_WrapperError("一"), None]
    je.start_driver()
    assert je._CURRENT_SNAPSHOT_PROFILE == boot.snapshots[1]


def test_a_failed_boot_does_not_record_a_snapshot(boot):
    """三次都失敗時不能留下一個路徑給收尾去 sync back——那會把一個沒用過的快照
    寫回正式 profile。"""
    boot.script = [_WrapperError("x")] * 3
    with pytest.raises(RuntimeError):
        je.start_driver()
    assert je._CURRENT_SNAPSHOT_PROFILE is None


def test_the_driver_versions_are_logged_once_per_spawn(boot):
    """每次 spawn 記一行版本——事後判讀「那次是哪個 chromedriver 配哪個 Chrome」
    唯一的來源，而且與 selenium 變體同步。"""
    je.start_driver()
    assert boot.versions == [boot.caps]


def test_a_failure_to_log_versions_does_not_kill_the_boot(boot, capsys):
    """記錄失敗是「少一條線索」，不是「不能用」。讓它翻掉整次開機，等於因為記錄
    壞了而中止一個要跑幾十小時的批次。"""
    boot.versions_raise = RuntimeError("capabilities 讀不到")
    je.start_driver()
    assert "driver version log failed" in capsys.readouterr().err
    assert boot.stealth, "版本記錄失敗之後就不做 stealth 注入了"


def test_the_stealth_script_is_installed_for_every_new_document(boot):
    je.start_driver()
    assert boot.stealth == [je.STEALTH_JS]


def test_a_failure_to_install_the_stealth_script_fails_the_boot(boot):
    """隱身腳本注入失敗＝這次開機失敗（**擁有者 2026-10-01 裁定**，§8.222 G2）。

    這支原本允許注入失敗繼續啟動，
    釘的是相反的行為：記一行、不帶隱身照跑。那會讓 `navigator.webdriver` 這類站方看得到
    的特徵整輪露在外面，而唯一的線索是一行會被捲走的警告。selenium 變體一直是硬失敗，
    裁定之後兩邊一樣。

    三件事一起釘：例外往上丟而且帶著原因（`raise … from`）、剛開的瀏覽器被收掉（不收的話
    `main()` 的失敗路徑直接 return、要等下一輪的孤兒掃描）、snapshot **不**記下來（收尾
    不該把一次失敗開機的 profile 寫回正式那份）。
    """
    cause = RuntimeError("CDP 不支援")
    boot.stealth_raise = cause
    with pytest.raises(RuntimeError, match="stealth script injection failed") as caught:
        je.start_driver()
    assert caught.value.__cause__ is cause, "原因沒有接在例外鏈上"
    assert "CDP 不支援" in str(caught.value)
    assert boot.quits == 1, "注入失敗之後沒有收掉剛開的瀏覽器"
    assert boot.order.index("stealth") < boot.order.index("quit")
    assert je._CURRENT_SNAPSHOT_PROFILE is None, "失敗的開機留下了一個要 sync back 的 snapshot"


def test_an_unconfirmed_stealth_script_also_fails_the_boot(boot):
    """沒丟例外、但 CDP 沒給 script identifier → 一樣不算注入成功。

    在函式庫「吞例外、回 None」的失敗政策下，None 就是失敗唯一的樣子；只接例外的話，
    那條路會安靜地不帶隱身照跑——正是這個裁定要擋的事。
    """
    boot.stealth_identifier = None
    with pytest.raises(RuntimeError, match="not confirmed"):
        je.start_driver()
    assert boot.quits == 1
    assert je._CURRENT_SNAPSHOT_PROFILE is None


def test_a_clean_boot_never_quits_the_browser_it_just_opened(boot):
    """必須放行的那一面：注入成功就不收瀏覽器。少了這一支，把「收掉」寫成無條件也會讓
    上面兩支通過。"""
    je.start_driver()
    assert boot.quits == 0
    assert je._CURRENT_SNAPSHOT_PROFILE == boot.snapshots[0]


# ---------------------------------------------------------------------------
# 五、靜態釘：兩個變體的重試步驟必須一致
# ---------------------------------------------------------------------------

_RETRY_STEPS = ("_kill_orphan_chrome", "_snapshot_chrome_profile",
                "_clear_snapshot_locks")


def _retry_body_calls(source: str, func_name: str) -> set[str]:
    """函式裡「`if attempt < max_attempts:` 那一段」呼叫到的名字。"""
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            text = ast.unparse(inner.test)
            if "attempt" in text and "max_attempts" in text:
                names = set()
                for call in ast.walk(ast.Module(body=inner.body,
                                                type_ignores=[])):
                    if isinstance(call, ast.Call):
                        fn = call.func
                        if isinstance(fn, ast.Name):
                            names.add(fn.id)
                        elif isinstance(fn, ast.Attribute):
                            names.add(fn.attr)
                return names
    return set()


def test_the_retry_preparation_is_the_same_in_both_variants():
    """兩個變體的重試準備動作必須一致——**這一條是量出來才加的**。

    `_clear_snapshot_locks` 在 je 這一側漏了一段時間（2026-09-07 補上），而漏掉的
    症狀是「偶爾重試也失敗」，看起來就像機器狀況不好。行為測試守得住今天的 je，
    守不住「selenium 那側新增了第四個步驟而 je 沒跟上」。
    """
    je_src = (PKG_ROOT / "webrunner_je_only.py").read_text(encoding="utf-8")
    je_steps = _retry_body_calls(je_src, "start_driver")
    assert set(_RETRY_STEPS) <= je_steps


def test_the_retry_step_extractor_is_live():
    """先證明抽取器真的抓得到東西——抓不到的掃描讀起來跟一致一模一樣。"""
    synthetic = (
        "def start_driver():\n"
        "    for attempt in range(1, 4):\n"
        "        try:\n"
        "            go()\n"
        "        except Exception:\n"
        "            if attempt < max_attempts:\n"
        "                _kill_orphan_chrome()\n"
        "                snapshot = _snapshot_chrome_profile()\n")
    assert _retry_body_calls(synthetic, "start_driver") == {
        "_kill_orphan_chrome", "_snapshot_chrome_profile"}


# ---------------------------------------------------------------------------
# 六、函式庫的契約：`_require_wrapper_contract`
# ---------------------------------------------------------------------------
# 兩個 API 都比 `requirements.txt` 的下限新（`je_web_runner>=0.0.88` 兩個都沒有），所以
# 開機時當場查、缺了就失敗。真的函式庫有沒有它們由 `test_je_facade.py` 在子行程裡問；
# 這裡用替身釘住**判斷本身**：缺哪一個都不開機、兩個都在才把政策設下去。

@pytest.fixture
def wrapper_logger():
    """函式庫的 logger 會被調高門檻；測完照原樣放回去，免得影響同一個行程的其他測試。

    用 `setLevel` 還原而不是 monkeypatch 屬性：`Logger.setLevel` 會清掉判斷快取，直接
    改 `level` 屬性不會，快取裡留著的舊答案會讓還原看起來沒有生效。
    """
    import logging
    logger = logging.getLogger(je._WRAPPER_LOGGER_NAME)
    before = logger.level
    try:
        yield logger
    finally:
        logger.setLevel(before)


def _contract_world(monkeypatch, *, setter=True, install_driver=True):
    """替身函式庫：`executor.set_raise_wrapper_errors` 在不在、`set_driver` 收不收
    `install_driver`，各自可以拿掉。回傳政策被設成什麼的紀錄。"""
    policy: list = []
    executor = (types.SimpleNamespace(set_raise_wrapper_errors=policy.append)
                if setter else types.SimpleNamespace())
    monkeypatch.setattr(je, "je_web_runner", types.SimpleNamespace(executor=executor))

    if install_driver:
        def set_driver(webdriver_name, options=None, install_driver=True, **kwargs):
            raise AssertionError("契約檢查不該真的開瀏覽器")
    else:
        def set_driver(webdriver_name, options=None, **kwargs):
            raise AssertionError("契約檢查不該真的開瀏覽器")
    monkeypatch.setattr(je, "wr", types.SimpleNamespace(set_driver=set_driver))
    return policy


def test_the_contract_pins_the_raise_policy_and_quiets_the_logger(
        monkeypatch, wrapper_logger):
    """兩個 API 都在：失敗政策設成丟例外、函式庫的 logger 只留 CRITICAL（G8、G9）。"""
    import logging
    wrapper_logger.setLevel(logging.WARNING)            # 函式庫自己的預設值
    policy = _contract_world(monkeypatch)
    je._require_wrapper_contract()
    assert policy == [True], policy
    assert wrapper_logger.level == logging.CRITICAL
    assert not wrapper_logger.isEnabledFor(logging.ERROR), (
        "包裝層吞下來的失敗還是會往 WEBRunner.log 寫 ERROR")


def test_a_library_that_swallows_failures_refuses_to_boot(monkeypatch, wrapper_logger):
    """沒有 `set_raise_wrapper_errors` 的版本：不開機，也不碰 logger。

    退回「照舊吞例外」是最不能接受的那條路：瀏覽器死掉時 `login_if_needed` 會讀成
    session 還在、`has_blocking_dialog` 的 None 被讀成已經關掉——全部安靜地發生。
    """
    import logging
    wrapper_logger.setLevel(logging.WARNING)
    _contract_world(monkeypatch, setter=False)
    with pytest.raises(RuntimeError, match="set_raise_wrapper_errors"):
        je._require_wrapper_contract()
    assert wrapper_logger.level == logging.WARNING, "失敗的檢查也去調了 logger"


def test_a_set_driver_without_install_driver_refuses_to_boot(monkeypatch, wrapper_logger):
    """`set_driver` 不認得 `install_driver` 的版本：不開機，**政策也不設**。

    舊版的 `set_driver` 有 `**kwargs`，不認得的參數會一路轉給瀏覽器類別，三次嘗試全部
    以一個看不出原因的 `TypeError` 失敗。先查完兩項再動手，所以這裡政策一次都沒被設——
    檢查失敗不該留下半套設定。
    """
    policy = _contract_world(monkeypatch, install_driver=False)
    with pytest.raises(RuntimeError, match="install_driver"):
        je._require_wrapper_contract()
    assert policy == [], "檢查還沒過就先改了函式庫的失敗政策"


def test_a_non_callable_policy_attribute_is_not_mistaken_for_the_api(
        monkeypatch, wrapper_logger):
    """名字在、但不是函式（例如被設成 None）一樣算缺。只看 `hasattr` 會把它放行，然後在
    呼叫那一刻以 `TypeError` 炸在開機中途。"""
    monkeypatch.setattr(je, "je_web_runner", types.SimpleNamespace(
        executor=types.SimpleNamespace(set_raise_wrapper_errors=None)))
    with pytest.raises(RuntimeError, match="set_raise_wrapper_errors"):
        je._require_wrapper_contract()


def test_main_reports_a_failed_start_without_starting_the_batch(monkeypatch, capsys):
    """`start_driver` 失敗（包含契約不符）→ `main()` 回 1、印一行、不進 `run_batch`。

    訊息寫「start_driver failed」而不是「set_driver failed」：失敗可能發生在 `set_driver`
    之前，那句話會把人送去查一個根本沒被呼叫的函式。
    """
    monkeypatch.setattr(ws, "log_code_fingerprint", lambda: None)
    monkeypatch.setattr(je, "read_credentials", lambda _p: ("someone@example.invalid", "pw"))
    monkeypatch.setattr(ws, "run_preflight", lambda _server: True)
    monkeypatch.setattr(je, "_kill_orphan_chrome", lambda: 0)

    def _boom():
        raise RuntimeError("載入的 je_web_runner 沒有 executor.set_raise_wrapper_errors")

    monkeypatch.setattr(je, "start_driver", _boom)
    monkeypatch.setattr(ws, "run_batch", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("開機失敗了還跑批次")))
    monkeypatch.setattr(je.sys, "argv", ["webrunner_je_only.py"])
    assert je.main() == 1
    err = capsys.readouterr().err
    assert "start_driver failed" in err and "set_raise_wrapper_errors" in err, err
