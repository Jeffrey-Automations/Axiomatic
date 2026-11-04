"""瀏覽器自動化函式庫（`je_web_runner`）的工作樹放在哪裡：唯一的一份規則。

**擁有者 2026-10-01 裁定**：仍然優先用本機的開發工作樹，但位置由設定決定，不再寫死成
repo 旁邊的同層資料夾。優先序**只在這裡定義一次**：

1. 環境變數 `WEBRUNNER_PATH`（非空白）；
2. `bot_config.json` 的 `webrunner_path`（非空白）；
3. 同層資料夾 `<repo 的上一層>/WebRunner`（原本兩個變體寫死的預設）；
4. 以上都沒有 → 不插任何路徑，落到已安裝的套件。

1、2 指的資料夾不存在時不採用、記進 `skipped`、往下一層找——套用的那一支替每一筆印
一行警告。3 不存在是正常情況（fresh clone、可散布版本、`.jeffrey_probe` 底下的稀疏
clone），安靜地落到 4。相對路徑一律以 repo 根目錄為準：`verify_browser` 的 je 子行程
刻意在一個拋棄式工作目錄裡跑，以工作目錄為準的話它跟正式變體會解析到不同的地方。

**三方用同一份**：兩個變體（經 `_webrunner_shared.put_webrunner_tree_first`）、
`verify_browser` 的 je 子行程（刻意不 import 任何 webrunner 模組），以及 bot 的
`/sys doctor`。所以它是 `CLAUDE.md` 的**被動共用模組**：純標準函式庫、與 driver 無關，
而且**不 import 專案裡的任何東西**。

**為什麼不放進 `_bot_config`**（鍵明明住在那裡）：批次要用這套規則，放進 `_bot_config`
就等於把整個 `_bot_config` 拉進產圖批次的匯入閉包——而那個檔是 bot 這一側最常改的
模組之一（2026-09 一個月 5 個 commit，比 `_webrunner_shared` 還多），批次在跑時每一次
改它都得改走待套分支（`architecture.md` §8.213）。所以反過來：鍵的預設值與逐鍵
警告仍在 `_bot_config`，它的 coercer 從這裡借（`coerce_webrunner_path`），規則只有一份；
批次只讀這一個鍵，不載入整份 bot 設定。

**函式庫的記錄檔也在這裡決定（2026-10-01，WebRunner X-6）。** 新版函式庫不再在匯入時於
工作目錄開 `WEBRunner.log`，改讀環境變數 `WEBRUNNER_LOG_PATH`（第一次匯入
`je_web_runner.utils.logging.loggin_instance` 時讀一次，第一筆紀錄才開檔、附加），沒設就寫
使用者家目錄。本專案一直把函式庫那份與自己的 `webrunner.log` 當成同一個檔（Windows 不分
大小寫、同一個工作目錄），所以 `put_webrunner_path_first` 在插路徑的同時把那個變數**沒設才
設**成 `WEBRUNNER_LOG`：擁有者明確給的值照樣勝出，`verify_browser` 給子行程的隔離值也是。
這一支是兩個變體與 `verify_browser` 的 je 子行程在 `import je_web_runner` 之前唯一都會經過
的地方，所以放這裡；舊版函式庫（套件庫的 0.0.88）不看這個變數，照舊寫工作目錄的
`WEBRunner.log`——正式批次的工作目錄是 repo 根目錄，落點相同。

**這裡絕不 import `je_web_runner`**：舊版一 import 就在當下的工作目錄開記錄檔，新版一 import
就讀定那個環境變數，所以這個模組只回答「路徑在哪」、只動環境變數，載入與載入後的紀錄
由呼叫端做。
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import NamedTuple, TextIO

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 與 `_bot_config.BOT_CONFIG_FILE` 同一個檔、同一個名字（跨行程路徑常數的對帳要求同名同檔）。
BOT_CONFIG_FILE = _PROJECT_ROOT / "bot_config.json"

WEBRUNNER_PATH_ENV = "WEBRUNNER_PATH"
# 函式庫記錄檔的完整路徑（函式庫自己的變數名；它在第一次匯入記錄模組時讀一次）。
LIBRARY_LOG_ENV = "WEBRUNNER_LOG_PATH"
# 本專案自己的執行 log。與 `discord_bot.WEBRUNNER_LOG`、`start_webrunner.WEBRUNNER_LOG`、
# `dashboard_server.WEBRUNNER_LOG` 同名同檔（跨行程路徑常數的對帳要求同名同檔）。
WEBRUNNER_LOG = _PROJECT_ROOT / "webrunner.log"
WEBRUNNER_PATH_KEY = "webrunner_path"
WEBRUNNER_SOURCE_ENV = "env"
WEBRUNNER_SOURCE_CONFIG = "config"
WEBRUNNER_SOURCE_SIBLING = "sibling"
WEBRUNNER_SOURCE_INSTALLED = "installed"
WEBRUNNER_SOURCES = (WEBRUNNER_SOURCE_ENV, WEBRUNNER_SOURCE_CONFIG,
                     WEBRUNNER_SOURCE_SIBLING, WEBRUNNER_SOURCE_INSTALLED)
# 從 `__file__` 推，不寫成 `_PROJECT_ROOT.parent / ...`：`test_gitignore_coverage` 把「root
# 名字開頭、卻沒有第一段字面值的鏈式接合」當成可能落在 repo 根目錄的問題形狀。
_SIBLING_WEBRUNNER = Path(__file__).resolve().parent.parent.parent / "WebRunner"
_SOURCE_LABELS = {
    WEBRUNNER_SOURCE_ENV: f"環境變數 {WEBRUNNER_PATH_ENV}",
    WEBRUNNER_SOURCE_CONFIG: f"設定檔的 {WEBRUNNER_PATH_KEY}",
    WEBRUNNER_SOURCE_SIBLING: "repo 旁邊的同層資料夾",
    WEBRUNNER_SOURCE_INSTALLED: "已安裝的套件",
}


def coerce_webrunner_path(value, default: str) -> str:
    """`webrunner_path` 的 coercer：字串去掉前後空白；**空字串是合法值**（＝沒有指定）。

    `_bot_config._COERCERS` 用的就是這一支（所以 bot 載入設定時的逐鍵警告與批次讀到的值
    是同一條規則）。去空白是因為手打的路徑前後多一個空白，`Path(" D:\\x")` 就指到一個不
    存在的資料夾，結果是安靜地落到下一層。非字串（數字、清單、`null`）退回 `default`；
    路徑存不存在不在這裡驗——載入時不碰檔案系統，用的那一刻才判斷。
    """
    if isinstance(value, str):
        return value.strip()
    return default


def read_configured_webrunner_path(config_file: Path | None = None) -> str:
    """只讀 `bot_config.json` 的 `webrunner_path` 一個鍵，套 `coerce_webrunner_path`。

    檔案不存在、讀不到、解不開或不是物件 → `""`（＝沒有指定，與 `_bot_config` 退回預設的
    結果相同）。讀不到與解不開各印一行 stderr（`!r`：不把設定檔的完整路徑寫進記錄）；
    值型別不對不在這裡出聲——bot 載入整份設定時 `_bot_config` 的逐鍵警告會講。永不 raise。
    """
    path = BOT_CONFIG_FILE if config_file is None else config_file
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as error:
        print(f"_webrunner_location: 讀不到設定檔：{error!r}", file=sys.stderr)
        return ""
    try:
        raw = json.loads(text or "{}")
    except json.JSONDecodeError as error:
        print(f"_webrunner_location: 設定檔解不開：{error!r}", file=sys.stderr)
        return ""
    if not isinstance(raw, dict):
        return ""
    return coerce_webrunner_path(raw.get(WEBRUNNER_PATH_KEY, ""), "")


class WebRunnerPathChoice(NamedTuple):
    """`resolve_webrunner_path()` 的結論：用哪一層、哪個資料夾，以及被跳過的指定。

    `path` 只在 `source` 是 `installed` 時為 None。`skipped` 是 `(來源, 路徑)` 的序列，
    只會含 `env` 與 `config`——有人明確指定、資料夾卻不存在的那幾層。
    """
    source: str
    path: Path | None
    skipped: tuple[tuple[str, Path], ...] = ()

    def describe(self) -> str:
        """一行人讀的來源說明（含路徑）。只給記錄檔用，不送進聊天平台。"""
        label = _SOURCE_LABELS[self.source]
        return label if self.path is None else f"{label}（{self.path}）"

    def fallback_warnings(self) -> list[str]:
        """每一個被跳過的指定各一行警告：點名來源、說它退到哪裡。"""
        return [f"WARN: {_SOURCE_LABELS[source]} 指的工作樹不存在（{path}），"
                f"已退到下一層：{self.describe()}"
                for source, path in self.skipped]


def _webrunner_candidate(raw: str) -> Path | None:
    """把一個指定值變成候選路徑；空白回 None，相對路徑以 repo 根目錄為準。"""
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _is_existing_dir(path: Path) -> bool:
    """資料夾存在嗎。路徑本身不合法（`ValueError`）或讀不到（`OSError`）都當成不存在。"""
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


def resolve_webrunner_path(*, environ: Mapping[str, str] | None = None,
                           configured: str | None = None,
                           sibling: Path | None = None,
                           is_dir: Callable[[Path], bool] | None = None,
                           ) -> WebRunnerPathChoice:
    """照模組說明的優先序決定函式庫的工作樹。

    **只回傳路徑**：不 import 函式庫、不動 `sys.path`、不 raise。`configured` 是 None 時
    讀 `bot_config.json` 的 `webrunner_path`（`read_configured_webrunner_path`）；`environ`
    預設 `os.environ`；`sibling`／`is_dir` 只給測試替換。
    """
    environ = os.environ if environ is None else environ
    if configured is None:
        configured = read_configured_webrunner_path()
    sibling = _SIBLING_WEBRUNNER if sibling is None else sibling
    exists = _is_existing_dir if is_dir is None else is_dir
    skipped: list[tuple[str, Path]] = []
    for source, raw in ((WEBRUNNER_SOURCE_ENV, environ.get(WEBRUNNER_PATH_ENV, "")),
                        (WEBRUNNER_SOURCE_CONFIG, configured)):
        candidate = _webrunner_candidate(raw)
        if candidate is None:
            continue
        if exists(candidate):
            return WebRunnerPathChoice(source, candidate, tuple(skipped))
        skipped.append((source, candidate))
    if exists(sibling):
        return WebRunnerPathChoice(WEBRUNNER_SOURCE_SIBLING, sibling, tuple(skipped))
    return WebRunnerPathChoice(WEBRUNNER_SOURCE_INSTALLED, None, tuple(skipped))


def point_library_log_at_ours(environ: MutableMapping[str, str] | None = None) -> str:
    """`LIBRARY_LOG_ENV` 沒設（或只有空白）才設成 `WEBRUNNER_LOG`，回傳生效的那個值。

    **沒設才設，不覆寫**：擁有者在環境裡明確指定的位置勝出，`verify_browser` 替子行程
    指好的隔離位置也勝出（它刻意不讓驗證碰正式記錄檔）。空白算沒設，與函式庫自己的
    判斷相同（它 `strip()` 之後是空字串就退回下一層）。必須在第一次匯入函式庫**之前**
    呼叫：函式庫只在那一刻讀一次。`environ` 預設 `os.environ`，只給測試替換。
    """
    environ = os.environ if environ is None else environ
    current = environ.get(LIBRARY_LOG_ENV, "")
    if current.strip():
        return current
    environ[LIBRARY_LOG_ENV] = str(WEBRUNNER_LOG)
    return environ[LIBRARY_LOG_ENV]


def put_webrunner_path_first(choice: WebRunnerPathChoice, *,
                             sys_path: list[str] | None = None,
                             stream: TextIO | None = None,
                             environ: MutableMapping[str, str] | None = None,
                             ) -> WebRunnerPathChoice:
    """`import je_web_runner` 之前的兩件事：記錄檔指向（`point_library_log_at_ours`），再把
    `choice` 套用到 `sys.path`——先替每個被跳過的指定印一行警告，再把資料夾插到最前面。

    兩個變體（經 `_webrunner_shared.put_webrunner_tree_first`）與 `verify_browser` 的 je
    子行程共用這一支，所以「警告怎麼講、插在哪裡、函式庫的記錄寫到哪」也只有一份。資料夾
    已經在 `sys.path` 上就不再插（與原本變體開頭那段相同）；`installed` 什麼都不插，但記錄
    檔的變數照樣設（日後的發行版也看它）。回傳 `choice`。
    """
    point_library_log_at_ours(environ)
    sys_path = sys.path if sys_path is None else sys_path
    stream = sys.stderr if stream is None else stream
    for line in choice.fallback_warnings():
        print(line, file=stream)
    if choice.path is not None and str(choice.path) not in sys_path:
        sys_path.insert(0, str(choice.path))
    return choice
