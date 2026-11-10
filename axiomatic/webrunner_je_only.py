"""唯一的背景產圖程式，所有瀏覽器操作透過函式庫門面執行。

批次與單張伺服器共用磁碟協定，主迴圈在 `_webrunner_shared`。
Windows 啟動前必須通過工作物件閘門，父行程負責收尾自己的瀏覽器子孫。
最低函式庫版本與開機契約見 requirements.txt、`_require_wrapper_contract`。
從專案根目錄執行：`py -3 axiomatic/webrunner_je_only.py`。
"""
from __future__ import annotations

from _process_job import wait_start_gate, popen as _job_popen
_JOB_GUARDED = wait_start_gate()

import inspect
import logging
import os
import random
import sys
import time
from pathlib import Path

from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402 — constants only
from urllib3.exceptions import (  # noqa: E402
    ConnectTimeoutError as Urllib3ConnectTimeoutError,
    MaxRetryError as Urllib3MaxRetryError,
    NewConnectionError as Urllib3NewConnectionError,
    ProtocolError as Urllib3ProtocolError,
    ReadTimeoutError as Urllib3ReadTimeoutError,
)

# Driver-agnostic helpers shared by both webrunner variants (P6). Imported by
# name so existing call sites stay byte-identical; `ws.*` reaches the rest
# (the bulk now lives in the shared module after C1–C6).
import _webrunner_shared as ws  # noqa: E402
from _generation_config import GENERATION_PROFILE  # noqa: E402

# `je_web_runner` 從哪裡載入：環境變數 `WEBRUNNER_PATH` → `bot_config.json` 的
# `webrunner_path` → repo 旁邊的同層資料夾 → 已安裝的套件（規則只有一份，在
# `_webrunner_location.resolve_webrunner_path`；與 selenium 變體同一行）。必須排在下面
# `import je_web_runner` 之前——本變體依賴的兩個介面比 PyPI 上的發行版新，載入哪一份
# 決定開不開得了機（`_require_wrapper_contract`）。同一行也把函式庫的記錄檔
# （`WEBRUNNER_LOG_PATH`，沒設才設）指向 `webrunner.log`，函式庫只在第一次匯入時讀它。
WEBRUNNER_LIBRARY = ws.put_webrunner_tree_first()

from _webrunner_shared import (  # noqa: E402
    # 裸名字匯入是必要的，不是風格：接合守門認不得 `ws.` 前綴的呼叫。理由與實測
    # 寫在 `webrunner_je_only.py` 同一個匯入區塊。
    _is_single_path_component,
    human_pause,
    human_type,
    hide_browser_windows,
    read_credentials,
    with_retry,
)
# chromedriver 記錄檔的處理，與 selenium 變體共用同一份（實測度量與「為什麼是一份
# 不是兩份」寫在共用模組那個區段的抬頭）。**只匯入函式，不匯入 `_CHROMEDRIVER_LOG*`
# 常數**——理由同 `webrunner_je_only.py` 那一側：常數只留在共用模組，測試 patch 的
# 目標對不上就會立刻 AttributeError，而不是安靜地讓修剪打到 repo 根目錄那個真檔。
from _webrunner_shared import (  # noqa: E402
    _dump_chromedriver_log_tail,
    _rotate_chromedriver_log,
    _trim_chromedriver_log,
)
# spawn 失敗時的例外格式，與 selenium 變體共用同一份——`{err!r}` 會把 selenium
# 的訊息整段丟掉（`args` 是空的）。完整理由與實測寫在那支的 docstring。
from _webrunner_shared import full_error_detail  # noqa: E402

# Transport-level exceptions when the chromedriver conversation itself fails
# (Chrome page hung, GPU stuck, in-page fetch blocking past selenium's 120s
# HTTP timeout — or chromedriver.exe simply gone). Selenium does NOT wrap
# urllib3's exceptions; they propagate raw and would crash the whole webrunner
# mid-batch. Hot-path helpers wrap their script calls in this tuple so caller
# poll / retry loops can recover.
#
# **必須與 `webrunner_je_only.py` 的那一份逐項相同**（`test_variant_parity.py`
# 釘住）。完整的理由、實測資料與「新加進來的每一個類別都同時列在
# `_webrunner_shared._SESSION_GONE_EXC_NAMES` 裡」這條不變式寫在那一份的註解裡，
# 這裡不重抄一遍，免得兩份說明各自漂移。一句話版本：`MaxRetryError` /
# `NewConnectionError` 的 MRO **不經過 `OSError`**，所以它們雖然一直列在 gone
# 名單裡，卻永遠到不了 `_note_transport_error` 手上（2026-09-09 查出，正式環境
# 有 09-07 三次未分類的 `critical_error` 為證）。
_DRIVER_TRANSPORT_ERRORS = (
    WebDriverException,
    Urllib3ReadTimeoutError,        # 連上了但沒回話 → 卡頓（**不**算 gone）
    Urllib3MaxRetryError,           # 重試用盡（實測就是這一個）
    Urllib3NewConnectionError,      # 連不上（ConnectTimeoutError 的子類）
    Urllib3ConnectTimeoutError,     # 連線階段逾時
    Urllib3ProtocolError,           # 連線中途被對方斷掉
    OSError,                        # socket 層（ConnectionRefusedError…）
)

import je_web_runner  # noqa: E402 — `_require_wrapper_contract` 讀它的 `executor`
from je_web_runner import (  # noqa: E402
    TestObject,
    webdriver_wrapper_instance,
)

# Short alias — every "driver.X" in webrunner_je_only.py becomes "wr.X" here.
wr = webdriver_wrapper_instance


# ---------- BrowserPort adapter (P6 C2) -------------------------------------
# DOM/driver indirection layer. Every helper reaches the browser through this
# single surface instead of touching `wr` / the old `jss`/`xp`/`xps` shims
# directly, so C3+ can lift the helpers into the shared module unchanged.
# Wraps the module-global `wr` singleton — its underlying driver is (re)bound
# by `wr.set_driver` / `wr.quit` in the lifecycle code, so the port always
# follows the live driver with no re-pointing needed (unlike the selenium
# variant, whose raw driver object is replaced on restart). Mirrors
# `webrunner_je_only.BrowserPort` method-for-method, including the human-like
# input (2026-10-01, §8.222 G3/G4/G5): `click` hovers, pauses, then clicks with
# a JS fallback; `mouse_wiggle` really wiggles; `press_escape` sends an
# ActionChains Escape before the activeElement fallback. The only delta is the
# call style — the ActionChains go through the wrapper's queue (`wr.move_to_element`
# / `wr.pause` / `wr.left_click` / `wr.move_by_offset` / `wr.send_keys` /
# `wr.perform`) instead of a fresh selenium `ActionChains(driver)`. Lifecycle
# methods (restart/quit/sync_back) are deliberately NOT on the port yet — those
# stay raw `wr.*` for now (deferred to C6).


def _drop_queued_actions() -> None:
    """ActionChains 失敗之後清掉包裝層的動作佇列（本機與瀏覽器那一側），永不 raise。

    包裝層的 ActionChains 是**一個共用、會累積的佇列**（selenium 變體每次都 new 一個，
    沒有這個問題）。`perform()` 會先把佇列清空再送出，所以送出失敗不會留下東西；但只要
    排隊排到一半就丟例外，殘留的動作會被**下一次**不相干的 `perform()` 一起送出去——例如
    一次沒送出的 hover 跑進下一次 Escape 裡。順手的 `reset_actions` 也會請瀏覽器放開所有
    按著的鍵與滑鼠鍵（W3C 的 release actions）：一個在送到一半時斷掉的點選，可能留下
    「滑鼠鍵還按著」的狀態。

    多一次往返只發生在失敗那條路上；瀏覽器已經不在的話這裡會失敗，接著的 JS 退路會把
    真正的原因往上丟。
    """
    try:
        wr.reset_actions()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


class BrowserPort:
    """je_web_runner-backed DOM/driver adapter. See module comment above."""

    # Transport-level exception tuple, exposed so shared hot-path readers can
    # `except port.TRANSPORT_ERRORS` without importing selenium / urllib3.
    TRANSPORT_ERRORS = _DRIVER_TRANSPORT_ERRORS

    def restart(self, email, password) -> None:
        """Mid-run Chrome restart (memory flush). Delegates to the per-variant
        `_restart_chrome_session`, which drives the module-global `wr` singleton
        (quit → sync_back → kill_orphan → start_driver → _setup_session). This
        port wraps `wr`, so it automatically follows the new driver."""
        _restart_chrome_session(email, password)

    # ---- JS ----
    def execute_script(self, script, *args):
        return wr.execute_script(script, *args)

    def execute_async_script(self, script, *args):
        return wr.execute_async_script(script, *args)

    # ---- element lookup (lowercase 'xpath' = W3C By.XPATH) ----
    def find_elements_xpath(self, xpath):
        return wr.find_elements(TestObject(xpath, "xpath")) or []

    def find_element_xpath(self, xpath):
        """第一個符合的元素，沒有就回 None——「沒找到」不是錯誤。

        刻意走 `find_elements` 取第一個，不走 `wr.find_element`：後者在沒找到時丟
        `NoSuchElementException`，包裝層的失敗政策釘成丟例外之後（見
        `_require_wrapper_contract`），每一次輪詢（`_do_login` 等 email 欄位出現就是
        一秒三次）都會變成一個例外；釘之前則是每一次都往 `WEBRunner.log` 寫一筆
        ERROR。與 selenium 變體的實作同一個形狀。
        """
        elements = wr.find_elements(TestObject(xpath, "xpath")) or []
        return elements[0] if elements else None

    def ancestors(self, element, depth: int = 4):
        """Return [element, parent, …] up to `depth` ancestors via one
        execute_script round-trip. Mirror of `webrunner_je_only.BrowserPort.
        ancestors` (which uses element-relative find_element) — both yield the
        same chain so `expand_character_section` lives in the shared module."""
        out = wr.execute_script(
            """
            const el = arguments[0], depth = arguments[1];
            const out = [el];
            let cur = el;
            for (let i = 0; i < depth; i++) {
              if (!cur.parentElement) break;
              cur = cur.parentElement;
              out.push(cur);
            }
            return out;
            """,
            element, depth,
        )
        return out or [element]

    # ---- interaction ----
    def click(self, element, pause: float = 0.15):
        """滑過去、停 `pause` 秒、再按（反偵測的節奏），失敗退回 JS 點選。

        與 selenium 變體同一個動作序列（擁有者 2026-10-01 裁定移植，§8.222 G3）；`pause`
        沿用各呼叫點原值（多數 0.15，少數 0.1 / 0.12）。在那之前這一側是純
        `element.click()`——游標從不移動、點下去之前也沒有停頓。

        能偵測到 ActionChains 失敗，前提是包裝層把失敗丟出來
        （`_require_wrapper_contract` 開機時釘的政策）；在吞例外的政策下 `perform()` 失敗
        只會回 None，JS 退路永遠不會被走到。
        """
        try:
            wr.move_to_element(element)
            wr.pause(pause)
            wr.left_click()
            wr.perform()
        except Exception:  # pylint: disable=broad-except
            _drop_queued_actions()
            wr.execute_script("arguments[0].click();", element)

    def click_native(self, element):
        """driver 層的**真**點選，不做任何退路——按不動就 raise。

        與 `click` 的分工刻意不同，兩件事都要記住：

        1. **不退回合成點選。** `click` 失敗時會安靜地改用
           `execute_script("arguments[0].click();")`，於是外面分不出「真的按到了」
           與「其實是合成的」。額度對話框那條路需要分得出來：站方第二層對話框不吃
           合成點選，而「退回合成」正好會把那個事實藏起來（本專案已經記過四次
           「log 報的是嘗試不是結果」）。階梯改由 `_webrunner_shared.
           _click_dismiss_target` 一階一階走，每一階都出聲。
        2. **走 W3C 的 element click，不走 ActionChains。** ActionChains 是低階
           指標動作，不做遮擋檢查，會直接打在座標上——換句話說它可能點到蓋在目標
           上面的東西。`element.click()` 會先檢查「收到這個點選的是不是目標本身或
           它的後代」，不是就丟 `ElementClickInterceptedException`。在購買對話框上
           這個檢查就是安全性質本身（我們自己已經不再由 JS 決定點誰）；而那個例外
           連帶會指名蓋在上面的元素，正好是我們要的診斷。所以這一支**兩個變體實作
           相同**，而且跟 `click` 不一樣、不走 ActionChains。
        """
        element.click()

    def mouse_wiggle(self):
        """登入頁上隨機擺動兩三下滑鼠（§8.222 G4）。盡力而為：失敗就算了，不影響登入。

        與 selenium 變體同一組數字（位移 ±60／±40、每段停 0.08～0.2 秒）。`move_by_offset`
        從游標目前的位置算，剛開的頁面游標在左上角，負的位移可能超出視窗而失敗——兩邊都是
        吞掉，這一側另外清掉排到一半的佇列（見 `_drop_queued_actions`）。
        """
        try:
            for _ in range(random.randint(2, 3)):
                wr.move_by_offset(random.randint(-60, 60), random.randint(-40, 40))
                wr.pause(random.uniform(0.08, 0.2))
            wr.perform()
        except Exception:  # pylint: disable=broad-except
            _drop_queued_actions()

    def press_enter(self, element):
        element.send_keys(Keys.ENTER)

    def press_escape(self):
        """driver 層的真 Escape（`isTrusted` 的事件）。回是否送出去了。

        鏡像 `webrunner_je_only.BrowserPort.press_escape`（§8.222 G5）：先送 ActionChains
        的 Escape（打給目前有焦點的東西，跟人按鍵盤一樣），失敗才退回對焦點元素
        `send_keys`。ActionChains 走包裝層的佇列，不碰 selenium 的驅動 API。
        """
        try:
            wr.send_keys([Keys.ESCAPE])
            wr.perform()
            return True
        except Exception:  # pylint: disable=broad-except
            _drop_queued_actions()
        try:
            element = wr.execute_script(
                "return document.activeElement || document.body;")
            if element is not None:
                element.send_keys(Keys.ESCAPE)
                return True
        except Exception:  # pylint: disable=broad-except
            pass
        return False

    # ---- page / session ----
    def get(self, url):
        wr.to_url(url)

    def current_url(self):
        return wr.get_current_url() or ""

    def get_title(self):
        return wr.get_title() or ""

    def refresh(self):
        wr.refresh()

    def save_screenshot(self, path):
        return wr.save_screenshot(path)


# Module-global port. Stateless wrapper over the `wr` singleton, so a single
# instance is valid for the whole process (including across Chrome restarts).
port = BrowserPort()


# ---------- config (must match webrunner_je_only.py) ------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_FILE = PROJECT_ROOT / "auth.md"  # main shell reads credentials before boot
# Queue / fallback files (TODO_*_FILE / PROMPT_FILE / CHARACTER2_FALLBACK_FILE /
# UNDESIRED_FILE) moved to _webrunner_shared with read_queues + run_batch (C6).
# `OUTPUT_ROOT` 已於 2026-09-11 從本變體移除：它在這個檔裡**只有寫、沒有讀**
# （實測：一個 `Store`、零個 `Load`），輸出資料夾的決定權在
# `_webrunner_shared.allocate_output_dir`。留著的害處是 grep 的人會看到三份定義
# （兩個變體 ＋ shared）對到一個活的用途。novelai 那份還在，因為它真的被讀
# （`OUTPUT_ROOT / "_verify"`，隔離驗證模式用）。
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
# DOM_REQUEST_FILE 已移入 _webrunner_shared（check_dom_request 一併搬走，C3）。
# 單張生成請求 — 必須與 webrunner_je_only.py 完全對應。bot 寫請求進
# single_image_request.json，webrunner 啟動時（idle one-shot）或 iteration
# 邊界（batch 進行中 in-band）poll 它，只產 1 張、結果走 events.ndjson 的
# `single_image_done` event 回傳 bot。one-shot 圖一律存進
# output/_oneshot/<request_id>/。serve/check 已搬入 _webrunner_shared（C4），
# SINGLE_IMAGE_REQUEST_FILE 與 SINGLE_IMAGE_OUTPUT_ROOT 一併移入 shared。
# **本變體刻意不再持有 SINGLE_IMAGE_REQUEST_FILE**：pass 3 之後 main() 用
# ws.parse_run_mode(sys.argv) 判斷這一輪是不是單圖伺服器，不再從磁碟上有沒有
# 請求檔推論（推論錯過一次，事故紀錄在 ws.RUN_MODE_BATCH 上面那一段）。
CHROME_PROFILE_DIR = PROJECT_ROOT / ".chrome_profile"

# ---- Opt-in isolated verification mode (default OFF; production unchanged) ----
# `verify_browser.py --full --variant je` launches THIS script as a subprocess with
# `NAI_VERIFY_MODE=setup`（2026-10-01 起，§8.222 G1）。本體與環境變數契約在
# `_webrunner_shared`（`run_setup_verification`、`VERIFY_*_ENV`），與 selenium 變體
# 共用；這裡只有 `_run_setup_verification` 那層薄殼與下面這個旗標。
# 只有 `_run_setup_verification` 會把它設成 True，讓 `start_driver` 重試之間那次
# `_kill_orphan_chrome()` 變成 no-op——一次驗證不得核彈式殺掉使用者自己的 Chrome 或
# 同時在跑的正式批次的瀏覽器。正式路徑永遠是 False，掃描照舊；驗證那條路收尾時只外科式
# 回收自己的 driver 行程樹。
_SUPPRESS_ORPHAN_SWEEP = False


def hide_chrome_window() -> bool:
    """確保瀏覽器視窗在螢幕外，回 True 表示找到視窗、而且沒有任何一個留在螢幕上。

    與 `webrunner_je_only.hide_chrome_window` 同構，只差這支沒有 driver 參數
    （這個變體的 driver 狀態在模組層）。實際動作在
    `_webrunner_shared.hide_browser_windows`，兩邊共用一份。**不再最小化**
    （2026-09-22）：理由見 `_webrunner_shared.OFFSCREEN_WINDOW_POSITION`。
    """
    hidden, exposed = hide_browser_windows(
        [CHROME_PROFILE_DIR, CHROME_PROFILE_SNAPSHOT])
    return hidden > 0 and exposed == 0


# CONSECUTIVE_FAIL_ALERT moved to _webrunner_shared with generate_loop (C5).
LOGIN_URL = "https://novelai.net/login"
IMAGE_URL = "https://novelai.net/image"
# 產圖模型的候選字面住在 `batch_config.json` 的 `model_candidates`（預設值與
# 型別檢查在 `_batch_config`），`_setup_session` 每次 setup 讀一次。
# **不要在這裡放回一份模組常數**：兩個 webrunner 變體各存一份、又必須永遠一致，
# 正是 CLAUDE.md 點名的漂移陷阱——設定檔是唯一來源，兩邊都從那裡讀。
# 站方全部候選都落空時 `ws.select_model` 會把當下看得到的選項 dump 進 log，
# 那行 log 就是「該把 `model_candidates` 改成什麼」的答案。

# Batch params live in `batch_config.json` at repo root; loaded once per
# character iteration via `load_batch_config()` so edits hot-reload to the
# NEXT character without killing the run. Defaults in `_batch_config
# ._DEFAULT_BATCH_CONFIG` match the historical hardcoded values.

REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(params);
}
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
"""


# DOM diagnostics（_DOM_DIAG_JS / dump_textareas_diag / format_textareas_diag）
# 已移入 _webrunner_shared（C3）。


# ---------- single-image (one-shot) request ---------------------------------
# 跟 webrunner_je_only 對應函式 isomorphic、只差用 je_web_runner 的 module-level
# driver state（沒 driver arg）。詳見對應函式 docstring。


# ---------- je_web_runner thin helpers --------------------------------------

def wait_until(predicate, timeout: float = 30.0, poll: float = 0.3,
               description: str = "predicate") -> bool:
    """Poll-until-true with timeout. Returns True if predicate became truthy,
    False on timeout. Swallows predicate exceptions (treated as falsey) —
    except a browser that is gone for good, which re-raises.

    那一個例外刻意往上丟：包裝層的失敗政策釘成丟例外之後（`_require_wrapper_contract`），
    瀏覽器整個不見了也會丟進這裡，而那不是「還沒好」——再等滿 `timeout` 也不會好，只會
    讓呼叫端（`with_retry`）把一個死掉的 session 誤當成登入慢。selenium 變體的
    `WebDriverWait.until` 也只吞「找不到元素」，其餘一律往上丟。
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception as error:  # pylint: disable=broad-except
            if ws.is_browser_gone_error(error):
                raise
        time.sleep(poll)
    print(f"  wait_until: timed out waiting for {description}")
    return False


# `_DEBUG_SCREENSHOTS` / `snap` and `_CHROME_CRASH_TITLE_MARKERS` /
# `_is_chrome_crash_page` moved to _webrunner_shared (C3).


# ---------- driver setup ----------------------------------------------------

# Chrome 在 user-data-dir 留下的 singleton lock 檔名。兩個用途：
# `_SNAPSHOT_IGNORE_NAMES`（複製 profile 時跳過）與 `_clear_snapshot_locks`。
#
# **不要照抄「殘留的 lock 檔會擋住下一個 Chrome」那句話。** 那是 POSIX 的行為；
# 2026-09-07 在這台機器上量過，Windows 不是這樣：
#   * 五個名字裡只有 `lockfile` 會真的出現，而且它的行為與
#     `CreateFile(dwShareMode=0, FILE_FLAG_DELETE_ON_CLOSE)` 逐項相符——實測用一個
#     跟 Chrome 無關的子行程重現過三件事：持有者活著時檔案在、另一個行程連唯讀
#     開啟都拿到 `PermissionError 13`、**持有者被硬砍之後檔案自己消失**。活著的
#     `.chrome_profile_snap/lockfile` 對第二點的反應一字不差。
#   * 對照觀察：`.chrome_profile/`（Chrome 2026-05-20 之後就沒再從那裡開過）與
#     使用者自己的 Chrome user-data-dir，**兩邊五個名字一個都沒有**——而本專案的
#     `_kill_orphan_chrome` 這一輪就對全機硬砍過 30 次。
#   * 另外四個是 POSIX 那一側的構造，兩個 profile 目錄從來沒有出現過。
# 也就是說「殘留的 lock 檔擋住啟動」在這個平台上不是一個可到達的狀態。整段推論
# 與它砍掉了什麼，見 `_clear_snapshot_locks` 的 docstring。
_CHROME_LOCK_FILES = (
    "SingletonLock", "SingletonCookie", "SingletonSocket",
    "lockfile", "RunningChromeVersion",
)


def _kill_orphan_chrome() -> None:
    """過去的實例由工作物件回收；不再掃描或停止其他人的瀏覽器。"""
    return None


# Snapshot 設計：跟 webrunner_je_only 對稱 — 每次 spawn 前 copy
# .chrome_profile/ → .chrome_profile_snap/、Chrome 用 snapshot 完全避開
# 原 profile 的 lockfile。跑完 _sync_chrome_profile_back 同步登入。
CHROME_PROFILE_SNAPSHOT = PROJECT_ROOT / ".chrome_profile_snap"
_CURRENT_SNAPSHOT_PROFILE: Path | None = None  # set by start_driver, read by main finally
# cookie jar 在 `Default/Network/` 底下（Chrome 96 起），不是 `Default/`。
# 為什麼舊路徑不留相容、以及那四個月的實測代價，見 webrunner_je_only 對應常數。
_SESSION_CRITICAL = (
    "Default/Network/Cookies", "Default/Network/Cookies-journal",
    "Default/Login Data", "Default/Login Data-journal",
    "Default/Preferences",
    "Default/Web Data", "Default/Web Data-journal",
    "Default/Network/Device Bound Sessions",
    "Default/Network/Device Bound Sessions-journal",
    "Local State",
)
# **登入態不在 cookie 裡，在 localStorage。** 完整的實測證據鏈（那個站只有三筆
# cookie、全不是驗證用的；`session` key 在 leveldb；snapshot 有更新的一筆而來源
# 停在 2026-05-20）寫在 webrunner_je_only 對應常數上方。
_SESSION_CRITICAL_DIRS = (
    "Default/Local Storage/leveldb",
)
# LevelDB 目錄裡不帶走的檔名（lock ＋ leveldb 自己的除錯文字輸出）。理由見
# webrunner_je_only 對應常數。
_LEVELDB_SKIP_NAMES = ("LOCK", "LOG", "LOG.old")
_SNAPSHOT_IGNORE_NAMES = frozenset(
    _CHROME_LOCK_FILES + (
        "Cache", "Code Cache", "GPUCache", "Service Worker",
        "ShaderCache", "GraphiteDawnCache", "DawnGraphiteCache",
        "Crashpad",
    )
)


def _clear_snapshot_locks(snapshot: Path) -> None:
    """把 `snapshot/` 裡的 singleton lock 檔案刪掉，best-effort，永不 raise。

    **目標一定要是 Chrome 真正開的那個目錄**（`--user-data-dir` ＝ snapshot），
    不是 `.chrome_profile/`。這裡以前有一整條「檔案鎖復原鏈」
    （`_cleanup_chrome_locks` → `_force_unlink` → `_kill_chrome_holders_of` →
    `_diagnose_file_lock`，兩個變體各一份、有測試、**沒有任何呼叫端**），而它清
    的正是 `.chrome_profile/`——自 snapshot 架構上線以來 Chrome 就沒有再從那裡
    開過，所以那條鏈就算接上去也等於沒接。2026-09-07 移除，證據：

    * `.chrome_profile/` 現在一個 lock 檔都沒有；`.chrome_profile_snap/` 在
      Chrome 活著的時候有一個 `lockfile`。lock 檔只出現在後者。
    * 那個 `lockfile` 是 delete-on-close 的，不可能變成「殘留」——見
      `_CHROME_LOCK_FILES` 上方那段實測。
    * 反過來，持有者還活著的時候那個檔連唯讀開啟都會 `PermissionError`（實測），
      所以再長的刪除階梯在那個情境下也一樣失敗，而且那個情境我們本來就不該刪。
    * 那條鏈最後兩階（psutil 找 handle 持有者 / 印診斷）在這台機器上還是**空轉**
      的：`psutil.Process.open_files()` 對 330 個行程裡的 141 個丟
      `RuntimeError: SystemExtendedHandleInformation buffer too big`，而那個型別
      不在它們的 `except (NoSuchProcess, AccessDenied, OSError)` 裡，會一路掀翻
      整個掃描迴圈；跑完一輪還要超過兩分鐘。

    所以這個呼叫實務上是 no-op。留著的理由是它盯的是**正確的**目錄、成本只有五次
    `exists()`，而且萬一上面的分析哪天在別的平台不成立，這裡是唯一正確的位置。
    刪不掉會印一行（報結果不報嘗試）——那代表有行程還握著它，是真正該看的訊號。
    """
    for fname in _CHROME_LOCK_FILES:
        path = snapshot / fname
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as error:
            # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會印出 snapshot
            # profile 的完整主機路徑。
            print(f"snapshot lock `{fname}` 刪不掉（有行程還握著它？）: "
                  f"{error!r}", file=sys.stderr)


def _leveldb_manifest_ok(dirpath: Path) -> bool:
    """`dirpath` 是不是一份自洽的 LevelDB（`CURRENT` 在、它指到的 MANIFEST 也
    在）。缺一個被 manifest 參照的檔案，Chrome 會安靜地丟掉整個 DB。詳見
    webrunner_je_only 對應函式。"""
    try:
        current = (dirpath / "CURRENT").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not current.startswith("MANIFEST-"):
        return False
    # `startswith` 不是包含性檢查（`MANIFEST-` ＋ `../` 就能走出去）；今天無害的
    # 三個理由、以及「402 份真 LevelDB 抽樣 0 份誤擋」的實測，寫在
    # `webrunner_je_only.py` 對應的那一段。
    if not _is_single_path_component(current):
        return False
    return (dirpath / current).is_file()


def _session_entry_present(base: Path, relpath: str) -> bool:
    """`base` 底下這一筆登入資料算不算「在」。目錄那幾筆要求 LevelDB 完整。"""
    if relpath in _SESSION_CRITICAL_DIRS:
        return _leveldb_manifest_ok(base / relpath)
    return (base / relpath).exists()


def _reclaim_dir_sync_residue(dst: Path) -> None:
    """收掉上一次目錄交換的殘骸，並救回「死在兩個 rename 之間」那一刻。詳見
    webrunner_je_only 對應函式。"""
    import shutil
    tmp = dst.with_name(dst.name + ".sync.tmp")
    old = dst.with_name(dst.name + ".sync.old")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    if not old.exists():
        return
    if dst.exists():
        shutil.rmtree(old, ignore_errors=True)
        return
    try:
        os.replace(old, dst)
        print(f"dir-sync: 從 {old.name}/ 救回 {dst.name}/"
              f"（上一輪死在交換的那一刻）", file=sys.stderr)
    except OSError as error:
        # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會把 profile 的完整
        # 主機路徑帶進 log。
        print(f"dir-sync: {old.name}/ 救不回 {dst.name}/：{error!r}。"
              f"這一輪的 localStorage 是空的 → 要重新登入。", file=sys.stderr)


def _sync_profile_dir_back(snapshot: Path, relpath: str) -> tuple[bool, int]:
    """把 snapshot 裡的一整個 LevelDB 目錄寫回 `.chrome_profile/`。
    回 `(有沒有換過去, 帶了幾個檔案)`，永不 raise。

    為什麼要整組換而不能逐檔覆蓋、目錄級原子性的取捨怎麼選、為什麼一定要在
    `driver.quit()` 之後跑、為什麼不做選擇性複製——四段理由都寫在
    webrunner_je_only 對應函式的 docstring 裡。"""
    import shutil
    src = snapshot / relpath
    dst = CHROME_PROFILE_DIR / relpath
    _reclaim_dir_sync_residue(dst)
    if not src.is_dir():
        return False, 0
    tmp = dst.with_name(dst.name + ".sync.tmp")
    old = dst.with_name(dst.name + ".sync.old")
    copied = 0
    try:
        tmp.mkdir(parents=True)
        for entry in sorted(src.iterdir()):
            if entry.name in _LEVELDB_SKIP_NAMES or not entry.is_file():
                continue
            shutil.copy2(entry, tmp / entry.name)
            copied += 1
        if not _leveldb_manifest_ok(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"dir-sync {relpath}: 組出來的 LevelDB 不完整（帶了 {copied} "
                  f"個檔案，CURRENT 指到的 MANIFEST 不在）→ 不換，保留舊的。"
                  f"下一輪要重新登入。", file=sys.stderr)
            return False, copied
        if dst.exists():
            os.replace(dst, old)
        os.replace(tmp, dst)
        shutil.rmtree(old, ignore_errors=True)
        return True, copied
    except (OSError, shutil.Error) as error:
        # `!r` 刻意保留：`shutil.Error` 也是 `OSError` 的子類別，這裡收不到
        # selenium 例外；`str()` 會印出 profile 的完整主機路徑。
        print(f"dir-sync {relpath} failed: {error!r}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        _reclaim_dir_sync_residue(dst)
        return False, copied


def _snapshot_chrome_profile() -> Path:
    """Copy .chrome_profile/ → .chrome_profile_snap/、跳過 lock 檔跟 cache
    dir。詳見 webrunner_je_only 對應函式。"""
    import shutil
    # 先砍舊 snapshot
    if CHROME_PROFILE_SNAPSHOT.exists():
        try:
            shutil.rmtree(CHROME_PROFILE_SNAPSHOT, ignore_errors=True)
        except OSError:
            pass
    CHROME_PROFILE_SNAPSHOT.mkdir(parents=True, exist_ok=True)
    if not CHROME_PROFILE_DIR.exists():
        print(f"snapshot profile: 來源 {CHROME_PROFILE_DIR.name}/ 不存在"
              f"（首次執行？）→ snapshot 是空的，這一輪要重新登入")
        return CHROME_PROFILE_SNAPSHOT
    # 一定要在 walk 之前：上一輪若死在目錄交換正中間，來源的 `leveldb/` 會不見、
    # 資料在 `leveldb.sync.old/`。不先救回來就會複製一份沒有 localStorage 的
    # profile 過去，而且看起來一切正常。
    for relpath in _SESSION_CRITICAL_DIRS:
        _reclaim_dir_sync_residue(CHROME_PROFILE_DIR / relpath)
    copied = 0
    skipped: list[str] = []
    try:
        for root, dirs, files in os.walk(CHROME_PROFILE_DIR):
            rel_root = Path(root).relative_to(CHROME_PROFILE_DIR)
            dirs[:] = [d for d in dirs if d not in _SNAPSHOT_IGNORE_NAMES]
            dst_root = CHROME_PROFILE_SNAPSHOT / rel_root
            dst_root.mkdir(parents=True, exist_ok=True)
            for fname in files:
                if fname in _SNAPSHOT_IGNORE_NAMES:
                    continue
                src = Path(root) / fname
                dst = dst_root / fname
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except (OSError, shutil.Error) as error:
                    # `!r` 刻意保留：這一行會被收進 `skipped` 再印出來，`str()`
                    # 會讓每一筆都帶上完整主機路徑。
                    skipped.append(f"{rel_root}/{fname}: {error!r}")
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有 `os.walk` ＋ `shutil.copy2`，碰不到 driver
        # （這一步跑在 Chrome 起來**之前**）；`str(OSError)` 會印出完整路徑。
        print(f"_snapshot_chrome_profile walk failed: {error!r}",
              file=sys.stderr)
    # **無條件報結果。** 摘要原本包在 `if copied:` 裡，於是最嚴重的結果
    # （一個檔案都沒複製到 → Chrome 用一份空 profile 開機 → 這一輪必然重新
    # 登入）剛好是唯一一個什麼都不印的。
    print(f"snapshot profile: copied {copied} files to "
          f"{CHROME_PROFILE_SNAPSHOT.name}/"
          + (f"; skipped {len(skipped)} (locked / inaccessible)"
             if skipped else ""))
    # 真正可行動的訊號：**snapshot 裡找不到**的 session-critical 檔案。判準刻意
    # 不要求來源存在——一個要求來源存在的檢查抓不到路徑寫錯（實測見
    # webrunner_je_only 對應函式）。來源也沒有的那幾筆會被標出來。
    # 目錄那幾筆的判準是「完整」不是「在」——`CURRENT` 指不到 MANIFEST 的目錄
    # 存在，但 Chrome 會把整個 DB 丟掉。
    entries = _SESSION_CRITICAL + _SESSION_CRITICAL_DIRS
    missing = [rel for rel in entries
               if not _session_entry_present(CHROME_PROFILE_SNAPSHOT, rel)]
    if missing:
        detail = ", ".join(
            rel if _session_entry_present(CHROME_PROFILE_DIR, rel)
            else f"{rel}（來源也沒有）"
            for rel in missing)
        print(f"snapshot profile: {len(missing)}/{len(entries)} 個登入"
              f"相關檔案不在 snapshot 裡 → 這一輪很可能要重新登入: {detail}",
              file=sys.stderr)
    return CHROME_PROFILE_SNAPSHOT


def _sync_chrome_profile_back(snapshot: Path) -> None:
    """Webrunner 結束時呼叫：把 snapshot 內的 session-critical 檔案
    複製回 .chrome_profile/，保留登入。詳見 webrunner_je_only 對應函式——
    摘要為什麼一定要帶分母、而且無條件印，以及為什麼單檔與目錄的分母刻意分成
    兩個不合併，都寫在那一邊的 docstring。"""
    import shutil
    if not snapshot.exists():
        print(f"sync-back: snapshot {snapshot.name}/ 不存在 → "
              f"0/{len(_SESSION_CRITICAL)} 個登入相關檔案 + "
              f"0/{len(_SESSION_CRITICAL_DIRS)} 個登入相關目錄寫回，"
              f"下一輪要重新登入", file=sys.stderr)
        return
    synced: list[str] = []
    absent: list[str] = []
    for relpath in _SESSION_CRITICAL:
        src = snapshot / relpath
        if not src.exists():
            absent.append(relpath)
            continue
        dst = CHROME_PROFILE_DIR / relpath
        # **同目錄 temp → os.replace。** `shutil.copy2` 是「開目標為 wb 立刻
        # 截斷」，所以中途被砍（/stop、taskkill /F、當機、機器睡著）留在磁碟上
        # 的是**半個 Cookies**——下一輪 Chrome 不會報錯，它會安靜地以未登入狀態
        # 開機，然後整輪重新登入。CLAUDE.md 的跨行程原子寫入硬規則講的「不是
        # 崩潰而是安靜的錯誤結果」就是這個；範本見 `_run_progress._atomic_write`。
        tmp = dst.with_name(dst.name + ".sync.tmp")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            synced.append(relpath)
        except OSError as error:
            # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會印出登入 profile
            # 的完整主機路徑。
            print(f"sync-back {relpath} failed: {error!r}",
                  file=sys.stderr)
            # os.replace 成功會自己移除 temp，失敗才要善後——留著就是一份登入
            # 資料的殘骸躺在磁碟上。
            try:
                tmp.unlink()
            except OSError:
                pass
    dirs_ok = 0
    dir_notes: list[str] = []
    for relpath in _SESSION_CRITICAL_DIRS:
        swapped, files = _sync_profile_dir_back(snapshot, relpath)
        dirs_ok += 1 if swapped else 0
        dir_notes.append(f"{relpath}: {files} files"
                         + ("" if swapped else " NOT swapped"))
    print(f"synced {len(synced)}/{len(_SESSION_CRITICAL)} session files "
          f"+ {dirs_ok}/{len(_SESSION_CRITICAL_DIRS)} session dirs "
          f"[{'; '.join(dir_notes)}] back to {CHROME_PROFILE_DIR.name}/")
    if absent:
        print(f"sync-back: {len(absent)} 個登入相關檔案在 snapshot 裡找不到，"
              f"沒有寫回去（路徑寫錯？Chrome 沒有建出來？）: "
              f"{', '.join(absent)}", file=sys.stderr)


def _selenium_manager_binary() -> Path:
    """selenium 內附的 `selenium-manager` 執行檔（給 `ws.warm_up_selenium_manager`）。

    詳見 `webrunner_je_only` 的同名函式；兩份逐字相同（鏡像對帳）。
    """
    from selenium.webdriver.common.selenium_manager import SeleniumManager
    return SeleniumManager._get_binary()  # pylint: disable=protected-access


# 函式庫自己的 logger 名稱（`je_web_runner.utils.logging.loggin_instance`）。
_WRAPPER_LOGGER_NAME = "WEBRunner"


def _require_wrapper_contract() -> None:
    """Pin je_web_runner to raise on failure and quiet its logger; raise
    `RuntimeError` when the loaded library lacks an API this variant needs.

    `start_driver` 的第一件事，所以三條開機路徑（`main()`、定期重啟、隔離驗證）都在開任何
    瀏覽器、做任何快照之前先過這一關。冪等、作用於整個行程（函式庫的設定本來就是行程層級
    的單例）。

    本變體依賴的兩個 API 都比 `requirements.txt` 的下限（`je_web_runner>=0.0.88`）新，所以
    當場查、**缺了就讓這次開機失敗**，不退回舊行為（擁有者 2026-10-01 的要求：缺 API 要在
    開機時大聲失敗，不能安靜地降級）：

    1. **`executor.set_raise_wrapper_errors(True)`——包裝層的失敗改成丟例外**（§8.222 G8）。
       函式庫預設把每一個包裝層呼叫的例外吞掉、回 None／False：瀏覽器死掉時
       `login_if_needed` 讀到空的網址而判成「session 還在」、`has_blocking_dialog` 的 None
       被讀成「已經關掉」、`port.click` 的 JS 退路在死瀏覽器上「成功」，而
       `port.TRANSPORT_ERRORS` 那整條分類鏈是死碼。改成丟例外之後，本變體走到的每一條共用
       路徑就跟 selenium 變體同一種語意，而共用層本來就是照那種語意寫的。
    2. **`set_driver(..., install_driver=False)`**（G10）。預設每次開瀏覽器前先跑一次
       webdriver-manager 的 `install()`、再把結果丟掉（真正選 driver 的是 Selenium Manager，
       §8.40），於是 driver 下載來源連不上時開機就失敗、還計入快速失敗。

    另外把函式庫的 `WEBRunner` logger 調到只留 CRITICAL（G9）：它對每一次失敗都往自己的記錄檔
    寫一筆帶整段 JS 的 ERROR，而那個檔就是 `webrunner.log`——舊版寫工作目錄的 `WEBRunner.log`
    （正式環境的工作目錄是 repo 根目錄、磁碟不分大小寫），新版寫 `WEBRUNNER_LOG_PATH`，開頭的
    `ws.put_webrunner_tree_first()` 把它指向同一個檔（2026-09-12 那一次 je 執行留下 146 筆，含亂碼）。
    失敗改成丟例外之後由我們自己的格式器回報（`_short_error`／`full_error_detail`，會砍掉
    `Stacktrace:`、有長度上限），那一筆只剩重複的雜訊。

    訊息刻意不印函式庫的檔案位置：這一行會進 `webrunner.log`，而那個檔可以被送進對話平台。
    """
    setter = getattr(getattr(je_web_runner, "executor", None),
                     "set_raise_wrapper_errors", None)
    if not callable(setter):
        raise RuntimeError(
            "載入的 je_web_runner 沒有 executor.set_raise_wrapper_errors："
            "本變體要求包裝層的失敗丟例外，不在會吞例外的版本上開機。"
            "請更新 WebRunner 的開發簽出（或環境變數 WEBRUNNER_PATH／設定 webrunner_path 指到的那一份）。")
    try:
        parameters = inspect.signature(wr.set_driver).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "install_driver" not in parameters:
        raise RuntimeError(
            "載入的 je_web_runner 的 set_driver 沒有 install_driver 參數："
            "本變體要跳過 webdriver-manager 的下載、讓 Selenium Manager 選 driver，"
            "舊版會把這個參數轉給瀏覽器類別而開機失敗。請更新 WebRunner。")
    setter(True)
    logging.getLogger(_WRAPPER_LOGGER_NAME).setLevel(logging.CRITICAL)


def start_driver() -> None:
    """Boot Chrome via WebDriverWrapper with full anti-bot stealth + snapshot
    profile（避開 .chrome_profile/ 可能被 Defender / OneDrive / Explorer
    抓住的 lockfile）。

    **第一件事是 `_require_wrapper_contract()`**：函式庫缺本變體要的 API 就在這裡失敗，
    還沒暖機、還沒快照、還沒開任何瀏覽器。

    **開機前先看 Selenium Manager 的解析紀錄，到期才暖機——這一側也需要。**
    送進 `webdriver.Chrome` 的是下面那個沒有 `executable_path` 的 `ChromeService`，所以
    driver 跟 selenium 變體一樣由 Selenium Manager 解析（`architecture.md`
    §8.40 實測），到期時一樣要多花 1～3 分鐘重抓版本清單。`wr.set_driver` 預設還會先跑
    一次 webdriver-manager 的 `install()` 再把結果丟掉，所以呼叫時帶
    `install_driver=False` 把那一步關掉（§8.222 G10）。

    **chromedriver 的記錄檔在這一側也會產生（2026-09-09 接上）。** 在那之前這個
    變體完全沒有它，於是 spawn 失敗只留下本函式結尾那句猜測；而 `/run` 的預設變體
    就是 je，`_watch_for_fallback` 又會在 5 分鐘的 startup window 內靜靜轉跑
    selenium 變體，所以「je 為什麼起不來」在正式環境是**查不到**的。

    做得到的關鍵是 `wr.set_driver` 把 `**kwargs` **原封不動**轉給
    `webdriver.Chrome(...)`（`webdriver_wrapper.py`：
    `webdriver_value(options=driver_options, **kwargs)`），所以傳一個
    `service=ChromeService(log_output=…)` 就會變成 chromedriver 的
    `--log-path=`。sibling clone 與 site-packages 兩份都實測過，守門在
    `test_je_facade.py`——**上游哪天把 `**kwargs` 收掉，那支會紅**，而不是等到某次
    無人值守的 spawn 失敗才發現記錄又是空的。

    順序與 selenium 變體相同、理由也相同（見 `_trim_chromedriver_log`）：封頂與
    改名都必須在 `ChromeService(...)` **之前**，那之後那個檔就有 chromedriver
    握著，中途截斷會被作業系統補零。
    """
    from selenium.webdriver.chrome.service import Service as ChromeService

    # 函式庫的契約排在一切之前：缺 API 時不該先花 1～3 分鐘暖機、也不該先碰 profile。
    _require_wrapper_contract()

    # 與 selenium 變體同一個位置、同一個理由：最先做、只做一次、絕不丟例外。
    ws.warm_up_selenium_manager(_selenium_manager_binary)

    _trim_chromedriver_log()
    _rotate_chromedriver_log()
    snapshot = _snapshot_chrome_profile()
    _clear_snapshot_locks(snapshot)

    # Memory-conservative flags — see webrunner_je_only._MEMORY_FLAGS for the
    # rationale (cap caches + keep the minimized renderer active to reduce the
    # "Aw, Snap! Out of Memory" crash on long runs). GPU left ON so NovelAI's
    # canvas still paints (image-src detection depends on it).
    cli_args = [
        "--disable-blink-features=AutomationControlled",
        # 開窗就在螢幕外、固定大小；**不要**換回 `--start-maximized`（理由見
        # `_webrunner_shared.OFFSCREEN_WINDOW_POSITION`）。與 selenium 變體同一組。
        "--window-position=-32000,-32000",
        "--window-size=1920,1080",
        "--lang=en-US",
        f"--user-agent={REAL_USER_AGENT}",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disk-cache-size=33554432",
        "--media-cache-size=33554432",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-background-timer-throttling",
        f"--user-data-dir={snapshot}",
    ]
    experimental = {
        "excludeSwitches": ["enable-automation"],
        "useAutomationExtension": False,
    }

    # Up to 3 attempts. `_kill_orphan_chrome()` between attempts frees the
    # RAM held by a half-spawned chrome.exe — on a low-RAM box that leftover
    # is what makes the retry OOM too ("retries until crash").
    max_attempts = 3
    last_err: Exception | None = None
    ok = False
    for attempt in range(1, max_attempts + 1):
        # `log_output=`，**不是** `log_path=`。selenium 的 `Service` 沒有
        # `log_path` 這個參數，傳進去會一路掉進 `**kwargs` 被最底層安靜丟掉——
        # 不警告、不報錯，記錄檔從此不存在。這個坑在 selenium 變體上真的踩過幾個
        # 月（見 `test_selenium_facade.py` 的模組 docstring），所以兩個變體的
        # `ChromeService(...)` 關鍵字都由那支測試對著**裝著的** selenium 簽名比對。
        # `--log-level=INFO` 而不是 `--verbose`：實測 root cause 一字不差，但平時
        # 的雜訊少九成（4370 → 484 bytes/命令）。
        #
        # `ws._CHROMEDRIVER_LOG` 用屬性讀取而不是綁一份同名常數進本模組——理由見
        # 檔頭那段 import 的註解。
        service = ChromeService(
            log_output=str(ws._CHROMEDRIVER_LOG),
            service_args=["--log-level=INFO"],
        )
        try:
            # `install_driver=False`：跳過 webdriver-manager 那次結果會被丟掉的下載
            # （見本函式 docstring）。`_require_wrapper_contract` 已經確認過這個參數在。
            wr.set_driver("chrome", options=cli_args,
                          experimental_options=experimental,
                          service=service, install_driver=False)
            ok = True
            break
        except Exception as err:  # pylint: disable=broad-except
            last_err = err
            # je_web_runner 包裝層可能吞掉底層訊息；連 __cause__ 一起印，
            # 才看得到真正的 selenium / webdriver_manager 錯誤。
            # ⚠️ `__cause__` 是**最可能**放著真的 selenium 例外的位置（包裝層在
            # 外面再包一層），所以這裡用 `{...!r}` 的代價比外層那個還大。
            cause = (f" (cause: {full_error_detail(err.__cause__)})"
                     if err.__cause__ else "")
            print(f"Chrome spawn attempt {attempt}/{max_attempts} failed: "
                  f"{full_error_detail(err)}{cause}", file=sys.stderr)
            # 包裝層那句 `set_driver failed: …` 同樣沒有 root cause。傾印必須在
            # 下一次嘗試**之前**——chromedriver 每次啟動會清空 `--log-path` 指的
            # 檔，所以這一次的內容在 attempt+1 建立 service 時就沒了。
            _dump_chromedriver_log_tail()
            if attempt < max_attempts:
                _kill_orphan_chrome()
                time.sleep(2.0)
                snapshot = _snapshot_chrome_profile()
                # selenium 變體的重試路徑一直有這一步，je 這邊漏掉了——
                # 2026-09-07 補上，讓兩個變體的重試行為一致。
                _clear_snapshot_locks(snapshot)
                cli_args[-1] = f"--user-data-dir={snapshot}"
    if not ok:
        raise RuntimeError(
            f"Chrome failed to start after {max_attempts} attempts "
            f"(likely Out of Memory or a chromedriver/Chrome version "
            f"mismatch — see chromedriver.log above). Last error: "
            f"{full_error_detail(last_err)}"
        ) from last_err
    # 與 selenium 變體同步：每次 spawn 記一行版本。`current_webdriver` 是包裝層
    # 底下那個真的 selenium driver。
    try:
        ws.log_driver_versions(wr.current_webdriver.capabilities)
    except Exception as error:  # pylint: disable=broad-except
        print(f"driver version log failed: {full_error_detail(error)}",
              file=sys.stderr)

    # Pre-document stealth — runs before any page script on every new document.
    # 注入失敗＝這次開機失敗（擁有者 2026-10-01 裁定，§8.222 G2），見該函式。
    _install_stealth_script()

    # 整次開機都成功才記下 snapshot 路徑（main() 結束時拿來 sync back）。
    global _CURRENT_SNAPSHOT_PROFILE
    _CURRENT_SNAPSHOT_PROFILE = snapshot


def _install_stealth_script() -> None:
    """在每一個新文件的頁面腳本之前注入 `STEALTH_JS`；**沒有確定注入成功就讓這次開機失敗**。

    擁有者 2026-10-01 裁定（§8.222 G2）：隱身腳本注入失敗時，je 變體原本記一行就**不帶
    隱身照跑**——`navigator.webdriver` 等幾個站方會看的特徵就這樣露出來，而 log 裡只有
    一行容易被捲走的警告。selenium 變體一直是直接開機失敗（`execute_cdp_cmd` 丟的例外
    從 `build_stealth_driver` 往上丟），現在兩邊一樣是硬失敗。

    「確定」的判準是 CDP 回了 script identifier：函式庫回 None 代表它沒拿到一個像樣的回應
    （在會吞例外的失敗政策下，這就是唯一的失敗徵兆）。

    失敗時先把剛開的瀏覽器收掉再丟例外，這一點比 selenium 變體多做：不收的話，`main()`
    的開機失敗那條路直接 return 1、不經過收尾，留下一個用著正式 profile 快照的 Chrome，
    要等下一輪開機的孤兒掃描才清得掉；隔離驗證那條路也會留下一個握著隔離目錄的瀏覽器，
    讓清理刪不掉它。
    """
    try:
        identifier = wr.add_script_to_evaluate_on_new_document(STEALTH_JS)
    except Exception as error:  # pylint: disable=broad-except
        _quit_after_a_failed_boot()
        raise RuntimeError(
            f"stealth script injection failed: {full_error_detail(error)}"
        ) from error
    if not identifier:
        _quit_after_a_failed_boot()
        raise RuntimeError(
            "stealth script injection was not confirmed (CDP returned no script "
            f"identifier: {identifier!r}); refusing to run without it")


def _quit_after_a_failed_boot() -> None:
    """開機在 `set_driver` 之後失敗時收掉剛開的瀏覽器。盡力而為、永不 raise——它跑在
    一條正要往上丟真正原因的路上，自己再丟一個會把那個原因蓋掉。"""
    try:
        wr.quit()
    except Exception as error:  # pylint: disable=broad-except
        print(f"quit after a failed boot failed: {full_error_detail(error)}",
              file=sys.stderr)


# ---------- login ------------------------------------------------------------


def login_if_needed(email: str, password: str) -> bool:
    """Visit /login; if NovelAI redirects us elsewhere, we're already signed in.

    **判準是「`/login` 會不會轉址」這個結構性訊號，不是頁面上的字。**
    這裡曾經有一個對照組 `_webrunner_shared.is_logged_in`：它
    `return port.execute_script(...)`，JS 掃 `document.body.innerText` 找
    `Anlas:` / `End Session`（有就算已登入）與 `Anonymous Trial` / `Sign Up`
    （有就算沒登入）。2026-09-08 刪除，三個理由都記在這裡——**任何想用頁面文字
    判狀態的新程式都會踩到同一組**：

    1. **裸的 `execute_script` 回傳值不可以直接當布林。** 頁面切換中或記憶體吃緊
       時它真的會回 `None`（不是丟例外），`None` 讀成 False ＝「沒登入」，於是
       觸發一次多餘的完整登入流程。**je 那一側在包裝層預設的失敗政策下更絕對**：
       `webdriver_wrapper.execute_script` 吞掉所有 driver 例外並回 `None`，所以
       瀏覽器一死就必定判成「沒登入」（je 變體 2026-10-01 起在開機時把那個政策
       釘成丟例外，見 `webrunner_je_only._require_wrapper_contract`）。同一個形狀在
       `set_variety_plus` / `_has_rescale` 也有。
    2. **失敗的探測長得跟乾淨的探測一模一樣。** 三態（是／否／判斷不出來）被壓成
       兩態，呼叫端沒有辦法分辨「站方說沒登入」與「我根本讀不到」。本專案已經在
       `_find_all_chrome_processes` / `_load_pid` / `dashboard_server._read_pid`
       各踩過一次。
    3. **寫死的站方字面會過期，而且是靜默過期。** 那四個字面是 2026-05-20 寫的，
       之後沒有再對過實機。手上唯一的實機文字證據是 `WEBRunner.log` 裡額度對話框
       的 `innerText` 傾印（08-24～09-08）：`Anonymous Trial` / `Sign Up` /
       `End Session` **各 0 次**，`Anlas` 出現 472 次但**一次都沒有帶冒號**。
       那份傾印只涵蓋對話框、不是整頁，所以這不構成「字面已經失效」的證明——但它
       也完全沒有支持那四個字面還活著，而其中一個正面證據（`End Session`）依它
       自己的註解是在**帳號選單**裡，收合的選單通常不會進 `innerText`。寫死字面
       靜默失效在這個專案有前科：同意橫幅那顆 `Reject All` 站方根本沒有這個字面，
       從來沒命中過一次，是靠寬鬆退路才看起來正常的。

    轉址判準沒有這三個問題：它問的是**導覽結果**不是頁面上的字，讀不到會丟例外而
    不是安靜回 False，站方改文案也不影響它。要新增登入狀態檢查請沿用它，不要再寫
    一個掃 `innerText` 的版本。
    """
    port.get(LOGIN_URL)
    human_pause(3.0, 4.5)
    if "/login" not in port.current_url():
        print(f"session restored — redirected to {port.current_url()}")
        return True
    print("no session — going through /login flow")
    return _do_login(email, password)


def _do_login(email: str, password: str) -> bool:
    if "/login" not in port.current_url():
        port.get(LOGIN_URL)
        human_pause(1.5, 2.5)
    ws.reject_cookies(port, timeout=5.0)
    # 與 selenium 變體同一個位置：關掉同意橫幅之後、碰任何欄位之前（§8.222 G4）。
    port.mouse_wiggle()
    if not wait_until(
            lambda: port.find_element_xpath("//input[@type='email' or @name='email']") is not None,
            timeout=30, description="email field"):
        ws.snap(port, "no_email_field")
        return False
    email_box = port.find_element_xpath("//input[@type='email' or @name='email']")
    if email_box is None:
        ws.snap(port, "no_email_field")
        return False
    email_box.click()
    human_pause(0.15, 0.4)
    human_type(email_box, email)
    human_pause(0.3, 0.7)

    pw_box = port.find_element_xpath("//input[@type='password']")
    if pw_box is None:
        ws.snap(port, "no_password_field")
        return False
    pw_box.click()
    human_pause(0.15, 0.4)
    human_type(pw_box, password)
    human_pause(0.4, 0.9)

    submit_btn = port.execute_script(
        """
        for (const inp of document.querySelectorAll(
                "input[type='submit'], button[type='submit']")) {
          if (inp.offsetParent === null) continue;
          if (inp.closest('form')) return inp;
        }
        return null;
        """
    )
    if submit_btn:
        port.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        port.click(submit_btn)
    else:
        port.press_enter(pw_box)
    if wait_until(lambda: "/login" not in port.current_url(),
                  timeout=30, description="login redirect"):
        return True
    ws.snap(port, "login_failed")
    return False


# ---------- model + cookies + prompt areas ----------------------------------


# ---------- prompt fill helpers ---------------------------------------------


def click_add_character(gender: str = "Female", timeout: float = 8.0) -> bool:
    """加一個角色欄位。實作在共用層（`ws.click_add_character_control`），兩個
    webrunner 變體共用同一份 —— V5 把「Add Character」按鈕換成沒有文字、沒有
    aria-label 的圖示鈕，這種會跟著站方改版的邏輯只該有一份。"""
    return ws.click_add_character_control(port, gender, timeout=timeout)


def ensure_two_characters() -> bool:
    have = ws.count_characters(port)
    print(f"existing character slots: {have}")
    safety = 0
    while have > 2 and safety < 20:
        safety += 1
        label = f"Character {have}"
        ok = ws.remove_character_slot(port, label)
        human_pause(0.5, 0.9)
        new_count = ws.count_characters(port)
        if not ok or new_count >= have:
            print(f"  could not remove {label} (delete clicked={ok}, count={new_count})")
            break
        have = new_count
        print(f"  removed {label}; now {have}")
    while have < 2:
        click_add_character(gender="Female")
        human_pause(0.8, 1.2)
        new_count = ws.count_characters(port)
        if new_count == have:
            print("  Add Character clicked but no new slot appeared; breaking")
            break
        have = new_count
        print(f"  added; now {have} slot(s)")
    return have == 2


# ---------- resolution + sampler --------------------------------------------


# ---------- generation + download -------------------------------------------


def _setup_session(email: str, password: str) -> bool:
    """Full pre-generation setup (login → model → characters → resolution →
    sampler → minimize) on the freshly-booted module-global `wr` driver.
    Returns False on login failure. Used for the initial session AND every
    mid-run Chrome restart, so it fills no per-pair prompts (caller does)."""
    if not with_retry("login", lambda: login_if_needed(email, password),
                      max_attempts=3, sleep_range=(5, 10)):
        return False
    print("login ok")
    if not port.current_url().startswith(IMAGE_URL):
        port.get(IMAGE_URL)
        wait_until(lambda: port.current_url().startswith(IMAGE_URL),
                   timeout=30, description="navigate to /image")
    human_pause(2.0, 3.5)
    ws.reject_cookies(port)
    if not with_retry("select_model",
                      lambda: ws.select_model(
                          port, ws.GENERATION_MODELS),
                      max_attempts=3, sleep_range=(2, 4)):
        return False
    if not ensure_two_characters():
        print("character slot setup failed")
        return False
    if not with_retry("select_resolution",
                      lambda: ws.select_resolution(
                          port, GENERATION_PROFILE.settings.resolution,
                          GENERATION_PROFILE.settings.orientation),
                      max_attempts=3, sleep_range=(2, 4)):
        return False
    if not ws.configure_sampler_settings(port):
        print("sampler setup failed")
        return False
    ws.snap(port, "after_setup")
    # Catch a renderer crash during setup before entering / resuming the loop.
    ws._abort_if_chrome_crashed(port, "setup")
    if hide_chrome_window():
        print("browser window is off-screen; generation runs in the background")
    else:
        print("WARN: hide_chrome_window could not confirm the browser window is "
              "off-screen (continuing anyway)")
    return True


def _restart_chrome_session(email: str, password: str) -> None:
    """Quit Chrome + relaunch a fresh, fully-set-up session to flush the
    renderer's leaked memory (NovelAI's SPA grows unbounded across hundreds
    of generations → "Aw, Snap! Out of Memory"). Syncs the login profile
    back, quits, reaps orphans, re-runs `start_driver` + `_setup_session`
    (rebinding the module-global `wr` driver). Raises RuntimeError if the
    fresh session can't log in / set up. Caller re-fills the current pair's
    prompts afterwards."""
    snap_dir = _CURRENT_SNAPSHOT_PROFILE
    try:
        wr.quit()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    if snap_dir is not None:
        try:
            _sync_chrome_profile_back(snap_dir)
        except Exception as error:  # pylint: disable=broad-except
            # `!r` 刻意保留。⚠️ 這一站曾被列為「該換格式器」的候選，但 `try` 裡
            # 只有 `_sync_chrome_profile_back(...)` ＝ 純檔案 I/O：Chrome 在上面
            # 幾行已經 `quit()` 了，這裡碰不到 driver。而 `str(OSError)` 會把
            # 登入 profile 的完整主機路徑寫進 log。
            print(f"  sync-back during restart failed: {error!r}",
                  file=sys.stderr)
    _kill_orphan_chrome()
    start_driver()  # rebinds the wr driver + resets _CURRENT_SNAPSHOT_PROFILE
    if not _setup_session(email, password):
        raise RuntimeError("re-login failed after Chrome restart")
    print("  [memory] Chrome restarted, re-set-up, ready to resume")


# ---------- isolated verification mode (opt-in; default OFF) -----------------

def _run_setup_verification() -> int:
    """隔離、非正式的 setup 可達性驗證（`verify_browser.py --full --variant je`）。

    只由 `NAI_VERIFY_MODE` 觸發（`ws.verify_mode_requested()`；正式啟動一律沒設，所以
    `main()`／批次／單張／監督者都不受影響）。本體在
    `_webrunner_shared.run_setup_verification`，與 selenium 變體共用；這層殼只做兩件
    **必須在開 driver 之前成立、而且讀本模組全域**的事：

      * 把 `CHROME_PROFILE_SNAPSHOT` 指到隔離目的地——`start_driver` 的
        `_snapshot_chrome_profile()` 就會把登入 profile 複製到那裡（絕不是
        `.chrome_profile_snap/`）；`hide_chrome_window` 讀同一個全域，所以移到螢幕外那
        一步也跟著這個驗證用的 Chrome 走；
      * 設 `_SUPPRESS_ORPHAN_SWEEP`——`start_driver` 重試之間的全機掃描變成 no-op。

    開機走正式的 `start_driver()`，所以 `_require_wrapper_contract` 也在這條路上：函式庫
    缺介面時這裡印 `VERIFY-SETUP: FAIL RuntimeError: …`、回 1，跟正式變體拒絕開機一致。
    隔離保證與 selenium 變體相同：絕不 sync-back（這條路沒有呼叫它）、不寫
    `webrunner.pid`（`__main__` 在 `run_with_liveness_signal(main)` 之前就分流到這裡）、
    不碰佇列、圖只進隔離目錄、收尾只回收自己的 driver 行程樹。
    """
    global CHROME_PROFILE_SNAPSHOT, _SUPPRESS_ORPHAN_SWEEP
    ws.require_current_profile(ws.GENERATION_PROFILE)
    ws.require_expected_profile(ws.GENERATION_IDENTITY)
    plan = ws.verify_plan()
    CHROME_PROFILE_SNAPSHOT = plan.profile
    _SUPPRESS_ORPHAN_SWEEP = True
    return ws.run_setup_verification(
        port, plan,
        credentials=lambda: read_credentials(AUTH_FILE),
        boot=start_driver,
        service_pid=lambda: wr.current_webdriver.service.process.pid,
        setup=_setup_session,
        quit_driver=wr.quit)


# ---------- main (thin shell — orchestration lives in ws.run_batch, P6 C6) --

def main() -> int:
    try:
        ws.require_current_profile(ws.GENERATION_PROFILE)
        ws.require_expected_profile(ws.GENERATION_IDENTITY)
        ws.publish_generation_identity(ws.GENERATION_IDENTITY)
    except (OSError, ValueError, ws.GenerationProfileChanged) as error:
        print(f"generation profile unavailable: {error}", file=sys.stderr)
        return ws.RC_GENERATION_BLOCKED
    print("===== webrunner_je_only — pure je_web_runner driver =====")
    # 啟動當下的程式碼指紋，凍在這裡（import 都跑完了）。這一行是事後判讀
    # traceback 原始碼文字、以及回答「那個修正在這個行程裡生效了沒」的錨點——
    # 見 `ws.log_code_fingerprint`。必須在這裡呼叫、不能等要查時才算。
    ws.log_code_fingerprint()
    # 函式庫從哪一層來、實際載入哪個檔、哪一版（見 `ws.log_webrunner_library`）。
    ws.log_webrunner_library(WEBRUNNER_LIBRARY)
    try:
        email, password = read_credentials(AUTH_FILE)
    except FileNotFoundError:
        # 「檔案不在」與「檔案在但內容不對」在 `read_credentials` 裡是刻意分開的
        # 兩件事（見 `ws.CredentialsError`），這裡兩種都收，因為對**使用者**來說
        # 處置一樣：去把憑證填好。回 `RC_SETUP_INCOMPLETE` 讓監督者直接收工——
        # 缺檔案重生一百次也不會變成功。
        print(ws.missing_credentials_message(AUTH_FILE), file=sys.stderr)
        return ws.RC_SETUP_INCOMPLETE
    except ws.CredentialsError as error:
        print(f"webrunner: {error}", file=sys.stderr)
        return ws.RC_SETUP_INCOMPLETE
    # 這一輪是批次還是單圖伺服器，由 spawn 端在 argv 上**宣告**（規則與 2026-06-27
    # 的事故紀錄見 ws.RUN_MODE_BATCH 上面那一段）。不要改回從磁碟上的請求檔推論
    # ——那個檔回答的是「有沒有人排了一張單圖」，不是「我這個行程是誰」。
    mode = ws.parse_run_mode(sys.argv)
    # Preflight BEFORE Chrome boot: nothing to do (and this run was not spawned
    # as a single-image server) → rc=1 without spawning Chrome (preserves the
    # old "don't boot for empty"). 空佇列 ＋ 磁碟上躺著一個舊請求檔，現在不會
    # 再讓我們白開一次 Chrome。
    if not ws.run_preflight(mode == ws.RUN_MODE_SINGLE_IMAGE_SERVER):
        return 1
    # Reap orphan Chrome from any prior crashed / killed run before we spawn
    # our own (the "舊實例" spiral — see _kill_orphan_chrome).
    _kill_orphan_chrome()
    try:
        start_driver()
    except Exception as error:  # pylint: disable=broad-except
        # `full_error_detail`：這是 spawn 路徑的終結性回報（下一行就 return 1）。
        # 今天 `start_driver` 把三次嘗試都失敗的情況轉成 `RuntimeError`，那種例外
        # `args` 非空、`!r` 讀得到訊息，所以現況不是活著的缺陷——但那是
        # `start_driver` **內部**的轉換，不是這裡的保證：迴圈外面任何一個
        # `wr.*` 呼叫漏出一個 selenium 例外，`!r` 就會把訊息整段丟掉。
        # 「start_driver」不是「set_driver」：失敗也可能發生在 set_driver 之前（函式庫缺
        # API，`_require_wrapper_contract`）。
        print(f"start_driver failed: {full_error_detail(error)}", file=sys.stderr)
        return 1
    try:
        return ws.run_batch(
            port, email, password,
            setup_fn=lambda: _setup_session(email, password),
            minimize_fn=hide_chrome_window,
            mode=mode)
    finally:
        try:
            wr.quit()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        # quit 後 chrome 釋放 snapshot 的 file handle，再 sync back 登入。
        if _CURRENT_SNAPSHOT_PROFILE is not None:
            try:
                _sync_chrome_profile_back(_CURRENT_SNAPSHOT_PROFILE)
            except Exception as error:  # pylint: disable=broad-except
                # `!r` 刻意保留，理由同 `_restart_chrome_session` 裡那一站：
                # 這是 `finally` 的收尾，driver 已經 `quit()`，`try` 裡只剩檔案
                # I/O，而 `str(OSError)` 會帶出登入 profile 的完整路徑。
                print(f"_sync_chrome_profile_back failed: {error!r}",
                      file=sys.stderr)


if __name__ == "__main__":
    if os.name == "nt" and not _JOB_GUARDED:
        # 裸跑也由父行程持有工作物件；真正的腳本名稱與 PID 契約不變。
        sys.exit(_job_popen([sys.executable, *sys.argv], guarded=True).wait())
    # 隔離驗證模式（verify_browser.py --full --variant je）。正式啟動一律沒設，所以照舊
    # 走 main()；驗證走這條**另外的**分支，所以它不會認領 webrunner.pid。
    if ws.verify_mode_requested():
        sys.exit(_run_setup_verification())
    # 裸跑時沒有父行程發布存活訊號——見 `ws.claim_liveness_signal`。
    sys.exit(ws.run_with_liveness_signal(main))
