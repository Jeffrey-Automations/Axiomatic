"""瀏覽器自動化函式庫（`je_web_runner`）的工作樹從哪裡載入：`_webrunner_location`。

擁有者 2026-10-01 裁定：仍然優先用本機的開發工作樹，但位置由 `bot_config.json` 的
`webrunner_path` 決定，不再寫死成 repo 旁邊的同層資料夾。優先序只定義一次：

    環境變數 `WEBRUNNER_PATH` → `webrunner_path` → 同層資料夾 → 已安裝的套件

這一檔量四件事：

1. **每一層各一支**，含「指定了卻不存在」退到下一層並印一行警告（點名是哪一層）。
2. **只有一份規則。** 2026-10-01 之前兩個變體與 `verify_browser` 各寫一份「環境變數，
   否則同層資料夾」；靜態守門要求整個套件只有 `_webrunner_location.py` 讀那個環境變數、
   組那個同層資料夾，抄回一份就紅（抄本會少掉設定那一層，而兩份抄本彼此照樣對得上）。
3. **`_bot_config` 與批次讀到的是同一條規則**：鍵的預設與逐鍵警告在 `_bot_config`，
   coercer 借自這裡；批次只讀這一個鍵，不載入整份 bot 設定（`_bot_config` 因此不在產圖
   批次的匯入閉包裡，見 `_webrunner_location` 的模組說明）。
4. **啟動紀錄**：兩個變體在 `main()` 開頭記下來源、實際載入的檔案與版本。
5. **函式庫的記錄檔**（2026-10-01，WebRunner X-6）：新版函式庫看 `WEBRUNNER_LOG_PATH`、
   不看工作目錄；`put_webrunner_path_first` 在匯入之前把它**沒設才設**成本專案的
   `webrunner.log`，擁有者明確給的值與驗證工具給子行程的隔離值照樣勝出。
"""
from __future__ import annotations

import ast
import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

import _bot_config
import _webrunner_location as wl
import _webrunner_shared as ws

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent


def _resolve(tmp_path, *, env=None, configured="", sibling_exists=False):
    """在 `tmp_path` 裡造一個同層資料夾（要不要存在由參數決定），其餘照實判斷。"""
    sibling = tmp_path / "sibling" / "WebRunner"
    if sibling_exists:
        sibling.mkdir(parents=True)
    environ = {} if env is None else {wl.WEBRUNNER_PATH_ENV: env}
    return wl.resolve_webrunner_path(environ=environ, configured=configured,
                                     sibling=sibling)


# ---------------------------------------------------------------------------
# 一、每一層各一支
# ---------------------------------------------------------------------------

def test_the_environment_variable_wins_over_everything(tmp_path):
    env_dir = tmp_path / "from-env"
    env_dir.mkdir()
    config_dir = tmp_path / "from-config"
    config_dir.mkdir()
    choice = _resolve(tmp_path, env=str(env_dir), configured=str(config_dir),
                      sibling_exists=True)
    assert choice == wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_ENV, env_dir, ())


def test_the_config_key_is_used_when_no_environment_variable_is_set(tmp_path):
    config_dir = tmp_path / "from-config"
    config_dir.mkdir()
    choice = _resolve(tmp_path, configured=str(config_dir), sibling_exists=True)
    assert choice == wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_CONFIG, config_dir, ())


def test_the_sibling_folder_is_the_default(tmp_path):
    choice = _resolve(tmp_path, sibling_exists=True)
    assert choice.source == wl.WEBRUNNER_SOURCE_SIBLING
    assert choice.path == tmp_path / "sibling" / "WebRunner"
    assert choice.skipped == ()


def test_nothing_found_falls_through_to_the_installed_package(tmp_path):
    choice = _resolve(tmp_path)
    assert choice == wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_INSTALLED, None, ())


def test_a_configured_path_that_does_not_exist_falls_back_with_one_warning(tmp_path):
    """設定的資料夾不存在：不採用、往下一層（這裡是同層資料夾），而且**只印一行**、點名是設定。"""
    missing = tmp_path / "not-there"
    choice = _resolve(tmp_path, configured=str(missing), sibling_exists=True)
    assert choice.source == wl.WEBRUNNER_SOURCE_SIBLING
    assert choice.skipped == ((wl.WEBRUNNER_SOURCE_CONFIG, missing),)
    stream, sys_path = io.StringIO(), []
    wl.put_webrunner_path_first(choice, sys_path=sys_path, stream=stream)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, lines
    assert "webrunner_path" in lines[0] and str(missing) in lines[0], lines[0]
    assert "退到下一層" in lines[0] and "同層資料夾" in lines[0], lines[0]
    assert sys_path == [str(tmp_path / "sibling" / "WebRunner")]


def test_an_environment_path_that_does_not_exist_falls_to_the_config_key(tmp_path):
    config_dir = tmp_path / "from-config"
    config_dir.mkdir()
    missing = tmp_path / "env-not-there"
    choice = _resolve(tmp_path, env=str(missing), configured=str(config_dir))
    assert choice.source == wl.WEBRUNNER_SOURCE_CONFIG
    assert choice.skipped == ((wl.WEBRUNNER_SOURCE_ENV, missing),)
    warning, = choice.fallback_warnings()
    assert wl.WEBRUNNER_PATH_ENV in warning and str(missing) in warning, warning


def test_two_missing_locations_give_two_warnings_and_the_installed_package(tmp_path):
    choice = _resolve(tmp_path, env=str(tmp_path / "a"), configured=str(tmp_path / "b"))
    assert choice.source == wl.WEBRUNNER_SOURCE_INSTALLED
    assert [source for source, _p in choice.skipped] == [
        wl.WEBRUNNER_SOURCE_ENV, wl.WEBRUNNER_SOURCE_CONFIG]
    stream, sys_path = io.StringIO(), ["already"]
    wl.put_webrunner_path_first(choice, sys_path=sys_path, stream=stream)
    assert len(stream.getvalue().splitlines()) == 2
    assert sys_path == ["already"], "落到已安裝的套件時不該插任何路徑"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_setting_means_not_set_and_is_not_a_warning(tmp_path, blank):
    choice = _resolve(tmp_path, env=blank, configured=blank, sibling_exists=True)
    assert choice.source == wl.WEBRUNNER_SOURCE_SIBLING
    assert choice.skipped == (), "空白不是「指定了一個不存在的位置」，不該警告"


def test_a_relative_setting_is_anchored_at_the_repo_root_not_the_working_dir(
        tmp_path, monkeypatch):
    """`verify_browser` 的 je 子行程在拋棄式工作目錄裡跑；以工作目錄為準的話它跟正式變體
    會解析到不同的地方。"""
    monkeypatch.chdir(tmp_path)
    probed = []
    wl.resolve_webrunner_path(environ={}, configured="lib/WebRunner",
                              sibling=tmp_path / "none",
                              is_dir=lambda p: probed.append(p) or False)
    assert probed[0] == wl._PROJECT_ROOT / "lib" / "WebRunner", probed


def test_an_unusable_path_string_reads_as_missing_instead_of_raising(tmp_path):
    """路徑本身不合法時 `is_dir` 可能丟 `ValueError`（內嵌 NUL）；模組開頭的這一行絕不能讓
    批次起不來。"""
    choice = _resolve(tmp_path, configured="bad\x00path", sibling_exists=True)
    assert choice.source == wl.WEBRUNNER_SOURCE_SIBLING
    assert [source for source, _p in choice.skipped] == [wl.WEBRUNNER_SOURCE_CONFIG]


def test_putting_the_path_first_does_not_duplicate_an_existing_entry(tmp_path):
    choice = wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_SIBLING, tmp_path)
    sys_path = ["x", str(tmp_path)]
    wl.put_webrunner_path_first(choice, sys_path=sys_path, stream=io.StringIO())
    assert sys_path == ["x", str(tmp_path)]
    fresh = ["x"]
    wl.put_webrunner_path_first(choice, sys_path=fresh, stream=io.StringIO())
    assert fresh == [str(tmp_path), "x"], "要插在最前面，排在已安裝的套件之前"


def test_the_default_reads_the_real_environment_and_config(tmp_path, monkeypatch):
    """不傳參數時讀 `os.environ` 與 `bot_config.json`——正式那條路走的就是這個預設。"""
    env_dir = tmp_path / "from-env"
    env_dir.mkdir()
    monkeypatch.setenv(wl.WEBRUNNER_PATH_ENV, str(env_dir))
    assert wl.resolve_webrunner_path().path == env_dir
    monkeypatch.delenv(wl.WEBRUNNER_PATH_ENV)
    config_dir = tmp_path / "from-config"
    config_dir.mkdir()
    config = tmp_path / "bot_config.json"
    config.write_text(json.dumps({"webrunner_path": str(config_dir)}), encoding="utf-8")
    monkeypatch.setattr(wl, "BOT_CONFIG_FILE", config)
    assert wl.resolve_webrunner_path() == wl.WebRunnerPathChoice(
        wl.WEBRUNNER_SOURCE_CONFIG, config_dir, ())


# ---------------------------------------------------------------------------
# 讀設定檔的那一支：只讀一個鍵、讀不到就當沒有指定、永不 raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload, expected", [
    (None, ""),                                        # 檔案不存在
    ('{"webrunner_path": "  D:/x  "}', "D:/x"),         # 去空白
    ('{"webrunner_path": 5}', ""),                     # 型別不對 → 沒有指定
    ('{"other": 1}', ""),                              # 沒寫這個鍵
    ('[1, 2]', ""),                                    # 不是物件
    ("", ""),                                          # 空檔
])
def test_reading_the_one_key(tmp_path, payload, expected):
    path = tmp_path / "bot_config.json"
    if payload is not None:
        path.write_text(payload, encoding="utf-8")
    assert wl.read_configured_webrunner_path(path) == expected


def test_a_broken_config_file_reads_as_not_set_and_says_so(tmp_path, capsys):
    path = tmp_path / "bot_config.json"
    path.write_text("{not json", encoding="utf-8")
    assert wl.read_configured_webrunner_path(path) == ""
    err = capsys.readouterr().err
    assert "解不開" in err and str(tmp_path) not in err, err


# ---------------------------------------------------------------------------
# 二、只有一份規則
# ---------------------------------------------------------------------------

def _production_sources() -> list[Path]:
    return sorted(PKG_ROOT.glob("*.py")) + sorted(REPO_ROOT.glob("*.py"))


def _docstring_nodes(tree) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


# 只有規則的那一份可以出現這兩個字面值：讀環境變數要用它的名字、組同層預設要用那個資料夾名。
_RULE_LITERALS = (wl.WEBRUNNER_PATH_ENV, "WebRunner")


def _hand_rolled_rule_sites(source: str) -> list[tuple[int, str]]:
    """`(行號, 字面值)`：程式碼裡（不含 docstring）跟 `_RULE_LITERALS` **完全相等**的字串常值。

    比對「完全相等」而不是子字串：錯誤訊息裡提到 `WEBRUNNER_PATH` 是正常的，而讀環境變數、
    組 `<parent>/WebRunner` 都必須把那個字當成一個完整的字面值寫出來。
    """
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    return sorted((node.lineno, node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and id(node) not in skip
                  and node.value in _RULE_LITERALS)


def test_only_the_rule_module_reads_the_variable_or_builds_the_sibling_default():
    """兩個變體與 `verify_browser` 不得再手寫「環境變數，否則同層資料夾」。

    抄回一份的話少的正是設定那一層，而兩份抄本彼此照樣對得上——2026-10-01 之前
    `test_verify_browser` 就是在對帳兩份抄本，那種守門看不見「兩份一起過時」。
    """
    sources = _production_sources()
    assert len(sources) >= 40, f"只掃到 {len(sources)} 個檔，範圍壞了"
    offenders, rule_hits = [], []
    for path in sources:
        hits = _hand_rolled_rule_sites(path.read_text(encoding="utf-8"))
        if path.name == "_webrunner_location.py":
            rule_hits = hits
        elif hits:
            offenders.append(f"{path.name}:{hits}")
    # 正面對照：規則本身一定用得到這兩個字面值，抽不到代表抽取器壞了。
    assert {value for _line, value in rule_hits} == set(_RULE_LITERALS), rule_hits
    assert not offenders, (
        "這些檔案又自己決定函式庫的位置了——改呼叫 `_webrunner_location` 的解析器"
        f"（變體經 `ws.put_webrunner_tree_first()`）：{offenders}")


def test_the_hand_rolled_rule_detector_bites():
    """合成對照：今天的樹是乾淨的，所以上一支刪掉也會綠。"""
    rolled = ('import os\nfrom pathlib import Path\n'
              'X = os.environ.get("WEBRUNNER_PATH")\n'
              'Y = Path(__file__).parent / "WebRunner"\n')
    assert [value for _l, value in _hand_rolled_rule_sites(rolled)] == [
        "WEBRUNNER_PATH", "WebRunner"]
    harmless = ('"""Mentions WEBRUNNER_PATH and WebRunner in a docstring."""\n'
                'MSG = "請更新 WebRunner 的開發簽出（或 WEBRUNNER_PATH 指到的那一份）"\n')
    assert _hand_rolled_rule_sites(harmless) == []


def test_the_rule_module_imports_nothing_from_the_project():
    """它被產圖批次載入，又被 bot 與 `_bot_config` 載入；只要它 import 了專案裡的任何東西，
    那個東西就跟著進了批次的匯入閉包——放在這裡而不是 `_bot_config` 的理由就沒了。也不得
    import 函式庫本身（一 import 就在工作目錄開記錄檔）。"""
    tree = ast.parse((PKG_ROOT / "_webrunner_location.py").read_text(encoding="utf-8"))
    local = {path.stem for path in PKG_ROOT.glob("*.py")} | {"axiomatic"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported, "一個 import 都沒抽到，抽取器壞了"
    assert not imported & local, f"`_webrunner_location` import 了專案模組：{imported & local}"
    assert "je_web_runner" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


def test_the_webrunner_path_candidate_is_only_ever_probed():
    """`test_bot_helpers._JOIN_GUARD_EXEMPT` 那一筆（`_webrunner_candidate`）的前提。

    那個接合會讓結果離開 repo 根目錄，豁免的理由是「結果只拿去問 `is_dir()` 與插進
    `sys.path`，不開檔、不寫檔」。這一支釘住它：`_webrunner_candidate` 只有解析器一個
    呼叫端，而解析器裡那個候選路徑只出現在三個地方——判斷存在、放進結論、記進被跳過的
    清單（加上 `is None` 的檢查）。多一個用法（例如拿它 `read_text`）就紅。
    """
    tree = ast.parse((PKG_ROOT / "_webrunner_location.py").read_text(encoding="utf-8"))
    callers = {func.name for func in ast.walk(tree)
               if isinstance(func, ast.FunctionDef)
               for call in ast.walk(func)
               if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
               and call.func.id == "_webrunner_candidate"}
    assert callers == {"resolve_webrunner_path"}, callers
    resolver = next(func for func in ast.walk(tree)
                    if isinstance(func, ast.FunctionDef)
                    and func.name == "resolve_webrunner_path")
    allowed_calls = {"exists", "WebRunnerPathChoice", "append"}
    parents = {id(child): parent for parent in ast.walk(resolver)
               for child in ast.iter_child_nodes(parent)}
    uses = [node for node in ast.walk(resolver)
            if isinstance(node, ast.Name) and node.id == "candidate"
            and isinstance(node.ctx, ast.Load)]
    assert len(uses) >= 4, f"只找到 {len(uses)} 個用法，抽取器壞了"
    for node in uses:
        parent = parents[id(node)]
        if isinstance(parent, ast.Compare):
            continue
        assert isinstance(parent, ast.Call) or isinstance(parent, ast.Tuple), (
            f"第 {node.lineno} 行的候選路徑用在一個沒有登記的地方")
        call = parent if isinstance(parent, ast.Call) else parents[id(parent)]
        name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
        assert name in allowed_calls, f"第 {node.lineno} 行把候選路徑交給了 `{name}`"


# ---------------------------------------------------------------------------
# 三、`_bot_config` 與批次讀到的是同一條規則
# ---------------------------------------------------------------------------

def test_the_bot_config_key_uses_the_rule_modules_coercer():
    assert _bot_config._DEFAULT_BOT_CONFIG["webrunner_path"] == ""
    coerce, kwargs = _bot_config._COERCERS["webrunner_path"]
    assert coerce is wl.coerce_webrunner_path and kwargs == {}, (
        "`_bot_config` 自己又寫了一份 coercer——bot 的逐鍵警告與批次讀到的值會分岔")


def test_the_bot_config_loader_strips_and_warns_like_any_other_key(tmp_path, monkeypatch,
                                                                    capsys):
    path = tmp_path / "bot_config.json"
    monkeypatch.setattr(_bot_config, "BOT_CONFIG_FILE", path)
    path.write_text(json.dumps({"webrunner_path": "  D:/lib  "}), encoding="utf-8")
    assert _bot_config.load_bot_config()["webrunner_path"] == "D:/lib"
    path.write_text(json.dumps({"webrunner_path": 5}), encoding="utf-8")
    assert _bot_config.load_bot_config()["webrunner_path"] == ""
    assert "webrunner_path" in capsys.readouterr().err, "型別錯了卻沒有出聲"


def test_the_batch_never_loads_the_bot_config_module():
    """產圖批次的匯入閉包裡不得有 `_bot_config`（理由見 `_webrunner_location` 的模組說明）。"""
    import mutation_harness  # noqa: PLC0415

    for entry in mutation_harness._BATCH_ENTRY_POINTS:
        closure = mutation_harness._import_closure(entry)
        assert "_webrunner_location" in closure, (entry, sorted(closure))
        assert "_bot_config" not in closure, (
            f"`{entry}` 的匯入閉包把整個 `_bot_config` 拉進來了——批次在跑時改 bot 設定的"
            "鍵就得改走待套分支。")


# ---------------------------------------------------------------------------
# 四、啟動紀錄
# ---------------------------------------------------------------------------

def test_both_variants_log_the_library_at_start_up():
    for variant in ("webrunner_je_only.py",):
        tree = ast.parse((PKG_ROOT / variant).read_text(encoding="utf-8"))
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [ast.unparse(node) for node in ast.walk(main) if isinstance(node, ast.Call)]
        assert "ws.log_webrunner_library(WEBRUNNER_LIBRARY)" in calls, (variant, calls[:8])


def _fake_library(tmp_path, *, version=None, dist_version=None):
    package = tmp_path / "site" / "je_web_runner"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    if dist_version is not None:
        info = tmp_path / "site" / "je_web_runner-1.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: je_web_runner\nVersion: {dist_version}\n",
            encoding="utf-8")
    module = type(sys)("je_web_runner")
    module.__file__ = str(package / "__init__.py")
    if version is not None:
        module.__version__ = version
    return module


def test_the_start_up_line_names_the_layer_the_file_and_the_metadata_version(
        tmp_path, capsys):
    choice = wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_CONFIG, tmp_path / "site")
    line = ws.log_webrunner_library(choice, _fake_library(tmp_path, dist_version="9.9.9"))
    assert "webrunner_path" in line and "__init__.py" in line, line
    assert "9.9.9（中繼資料）" in line, line
    assert line in capsys.readouterr().out


def test_the_start_up_line_falls_back_to_the_module_version(tmp_path):
    choice = wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_INSTALLED, None)
    line = ws.log_webrunner_library(choice, _fake_library(tmp_path, version="1.2.3"))
    assert "1.2.3（__version__）" in line and "已安裝的套件" in line, line
    bare = ws.log_webrunner_library(choice, _fake_library(tmp_path / "b"))
    assert "版本 未知" in bare, bare


def test_the_start_up_line_says_when_nothing_is_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "je_web_runner", raising=False)
    choice = wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_INSTALLED, None)
    assert "尚未載入" in ws.log_webrunner_library(choice)


def test_the_start_up_line_names_where_the_library_log_goes(tmp_path):
    """載入了的話，行尾記函式庫 logger 的檔案 handler 指到哪——讀 handler 本身，不是猜變數。

    重啟之後判斷「函式庫的紀錄真的進了 `webrunner.log`」就看這一段；舊版程式碼印的那一行
    沒有「記錄檔」三個字，所以兩行並排就分得出跑的是哪一版。"""
    target = tmp_path / "lib.log"
    handler = logging.FileHandler(target, delay=True, encoding="utf-8")
    logger = logging.getLogger(ws.LIBRARY_LOGGER_NAME)
    logger.addHandler(handler)
    try:
        choice = wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_INSTALLED, None)
        line = ws.log_webrunner_library(choice, _fake_library(tmp_path, version="1"))
    finally:
        logger.removeHandler(handler)
        handler.close()
    # 測試行程裡函式庫可能已經掛著它自己的 handler（停放目錄），所以清單裡不只這一個。
    assert "記錄檔 " in line, line
    assert handler.baseFilename in line.split("記錄檔 ", 1)[1].split("、"), line
    assert not target.exists(), "問位置不得讓延遲開檔的 handler 開檔"


def test_the_start_up_line_says_when_the_library_has_no_file_handler(tmp_path):
    """正面對照：沒有檔案 handler 時照實說，不是空字串（空字串讀起來像「記錄檔是空的」）。"""
    assert ws._library_log_destination("axiomatic.test.no-such-logger") == "沒有檔案 handler"


# ---------------------------------------------------------------------------
# 五、函式庫的記錄檔：沒設才設成本專案的 `webrunner.log`
# ---------------------------------------------------------------------------

def test_the_library_log_points_at_our_own_log_only_when_unset():
    """沒設（或只有空白）才設；已經有值就原樣留著——那是擁有者或驗證工具的決定。"""
    ours = str(wl.WEBRUNNER_LOG)
    unset: dict = {}
    assert wl.point_library_log_at_ours(unset) == ours
    assert unset == {wl.LIBRARY_LOG_ENV: ours}
    blank = {wl.LIBRARY_LOG_ENV: "   "}
    assert wl.point_library_log_at_ours(blank) == ours, "空白要當成沒設，跟函式庫自己的判斷相同"
    assert blank[wl.LIBRARY_LOG_ENV] == ours
    owner = {wl.LIBRARY_LOG_ENV: "E:/elsewhere/lib.log"}
    assert wl.point_library_log_at_ours(owner) == "E:/elsewhere/lib.log"
    assert owner == {wl.LIBRARY_LOG_ENV: "E:/elsewhere/lib.log"}, "覆寫了擁有者明確給的值"


def test_the_library_log_target_is_the_project_log_and_the_variable_is_the_librarys():
    """目標是 repo 根目錄的 `webrunner.log`（跨行程路徑常數另有同名同檔的對帳），變數名是
    函式庫自己讀的那一個——拼錯一個字，函式庫就安靜地寫到家目錄去。"""
    assert wl.WEBRUNNER_LOG == REPO_ROOT / "webrunner.log"
    assert wl.LIBRARY_LOG_ENV == "WEBRUNNER_LOG_PATH"


def test_putting_the_path_first_also_points_the_library_log(tmp_path):
    """兩個變體與 `verify_browser` 的 je 子行程都經過這一支——落到已安裝的套件時也要設
    （日後的發行版也看這個變數），而且同樣是沒設才設。"""
    for choice in (wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_INSTALLED, None),
                   wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_SIBLING, tmp_path)):
        environ: dict = {}
        wl.put_webrunner_path_first(choice, sys_path=[], stream=io.StringIO(),
                                    environ=environ)
        assert environ == {wl.LIBRARY_LOG_ENV: str(wl.WEBRUNNER_LOG)}, choice
    isolated = {wl.LIBRARY_LOG_ENV: str(tmp_path / "verify" / "WEBRunner.log")}
    wl.put_webrunner_path_first(wl.WebRunnerPathChoice(wl.WEBRUNNER_SOURCE_INSTALLED, None),
                                sys_path=[], stream=io.StringIO(), environ=isolated)
    assert isolated == {wl.LIBRARY_LOG_ENV: str(tmp_path / "verify" / "WEBRunner.log")}


def test_the_default_environment_is_the_process_environment(monkeypatch):
    """不傳 `environ` 時改的是 `os.environ`——正式那條路走的就是這個預設，子行程才繼承得到。"""
    monkeypatch.delenv(wl.LIBRARY_LOG_ENV, raising=False)
    assert wl.point_library_log_at_ours() == str(wl.WEBRUNNER_LOG)
    assert os.environ[wl.LIBRARY_LOG_ENV] == str(wl.WEBRUNNER_LOG)
