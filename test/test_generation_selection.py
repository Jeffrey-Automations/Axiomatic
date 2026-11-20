"""這一次叫醒用哪一套生成設定。

`generation_config.json` 的 `active_profile` 是**持久的預設值**。`wake_autostart.py
--profile NAME` 只讓那一次叫醒起來的監督者與它們整個生命週期的子孫用 NAME：子行程重生、
瀏覽器週期重啟、bot 自己 spawn 的批次都一樣；之後沒有指定的啟動（不帶旗標的叫醒、登入
觸發、重新開機）回到預設。

排程器的 `/Run` 不轉送環境變數與參數，所以選擇經由磁碟交棒：叫醒端每支監督者各寫一個
一次性選擇檔（`_supervisor.write_generation_selection`），監督者拿到單一實例鎖之後讀掉、
刪掉，放進自己的 `os.environ`（`apply_generation_selection`），`stream_child` 每次 spawn
都從那裡複製。產圖兩端（`_generation_config`）讀同一個環境變數。bot 的監督者一個平台
一支，所以 bot 那一份是一個平台一個檔。

這裡釘的是交棒的每一段：環境變數真的蓋過 `active_profile`、重讀比對的是同一套、選擇檔
只認自己那一支（含平台）而且有時效、每支啟動器各吃各的、變數真的進到子行程而且撐過重生、
沒有選擇時呼叫端環境裡的值不會被沿用。

啟動器一律在行程內跑 `main()`，`stream_child` 換成會丟例外的替身（與 `test_supervisor`
同一個理由：真的 spawn 出 webrunner 會 nuclear sweep 掉正式批次的瀏覽器）。選擇檔一律導到
`tmp_path`。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import _generation_config as gc  # noqa: E402
import _platform_runtime as pr  # noqa: E402
from axiomatic import _supervisor as sup  # noqa: E402  啟動器 import 的就是這一個模組物件

ENV = sup.GENERATION_PROFILE_ENV


def _clear_env(monkeypatch):
    """把覆寫值拿掉，**而且測試結束時一定還原**。

    只寫 `monkeypatch.delenv(ENV, raising=False)` 不夠：變數本來就不在的話 monkeypatch
    什麼都不記，之後被測程式（`apply_generation_selection`）自己設進去的值就會漏到下一支
    測試，後面的 `test_generation_config` 會整批載入成另一套。先 `setenv` 讓它記下原狀，
    再刪。"""
    monkeypatch.setenv(ENV, "placeholder")
    monkeypatch.delenv(ENV)


# ---- 產圖端：環境變數蓋過 `active_profile` -----------------------------------

def _config(tmp_path, *, active="v5", v4_steps=28):
    path = tmp_path / "generation_config.json"
    path.write_text(json.dumps({"active_profile": active, "profiles": {
        "v5": {"settings": {"steps": 23}},
        "v4_5": {"settings": {"steps": v4_steps},
                 "todo": {"prompt": "todo_prompt_v4_5.md"}},
    }}), encoding="utf-8")
    return path


def test_both_ends_name_the_same_environment_variable():
    """兩邊刻意不互相 import（見 `_generation_config.PROFILE_OVERRIDE_ENV` 的註解），
    所以名字只能靠這支釘住：改了一邊，監督者設的變數就沒有人讀，安靜地全部跑預設。"""
    assert sup.GENERATION_PROFILE_ENV == gc.PROFILE_OVERRIDE_ENV


def test_the_override_replaces_active_profile(tmp_path, monkeypatch):
    path = _config(tmp_path)
    _clear_env(monkeypatch)
    assert gc.load_generation_profile(path).name == "v5"
    monkeypatch.setenv(ENV, " v4_5 ")
    profile = gc.load_generation_profile(path)
    assert profile.name == "v4_5"
    assert profile.settings.steps == 28
    assert profile.todo.prompt == tmp_path / "todo_prompt_v4_5.md"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_override_means_the_default(tmp_path, monkeypatch, blank):
    monkeypatch.setenv(ENV, blank)
    assert gc.load_generation_profile(_config(tmp_path)).name == "v5"


def test_an_unknown_override_is_an_error_not_a_silent_default(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "absent")
    with pytest.raises(ValueError, match="selected profile"):
        gc.load_generation_profile(_config(tmp_path))


def test_an_explicit_profile_beats_the_environment(tmp_path, monkeypatch):
    """`wake_autostart` 驗證的是 NAME 本身，不是呼叫端殼裡剛好帶著的值。"""
    monkeypatch.setenv(ENV, "v5")
    assert gc.load_generation_profile(_config(tmp_path), profile="v4_5").name == "v4_5"


def test_the_default_is_still_validated_under_an_override(tmp_path, monkeypatch):
    """預設是之後每一次沒有指定的啟動要用的；壞掉要現在講，不是等下一次開機。"""
    monkeypatch.setenv(ENV, "v4_5")
    with pytest.raises(ValueError, match="active_profile"):
        gc.load_generation_profile(_config(tmp_path, active="absent"))


def test_the_override_on_a_missing_config_only_accepts_legacy(tmp_path, monkeypatch):
    absent = tmp_path / "absent.json"
    monkeypatch.setenv(ENV, "legacy")
    assert gc.load_generation_profile(absent).name == "legacy"
    monkeypatch.setenv(ENV, "v4_5")
    with pytest.raises(ValueError, match="selected profile"):
        gc.load_generation_profile(absent)


def _pinned_reload(monkeypatch, path, override):
    """讓 `require_current_profile` 重讀暫存設定檔，行程啟動時的覆寫值是 `override`。"""
    real = gc.load_generation_profile
    monkeypatch.setattr(gc, "STARTUP_PROFILE_OVERRIDE", override)
    monkeypatch.setattr(gc, "load_generation_profile",
                        lambda _path=None, **kwargs: real(path, **kwargs))
    return real(path, profile=override)


def test_a_process_started_on_an_override_does_not_see_a_change(tmp_path, monkeypatch):
    """同一套比同一套：用 `--profile` 叫醒的行程重讀時不得被「預設是另一套」誤判成變更
    ——那會讓 bot 拒絕動佇列、批次在第一個角色就停下來。"""
    path = _config(tmp_path)
    _clear_env(monkeypatch)         # 重讀時環境裡有沒有都不影響
    running = _pinned_reload(monkeypatch, path, "v4_5")
    gc.require_current_profile(running)
    _config(tmp_path, active="v4_5")               # 預設改了：不是這個行程的事
    gc.require_current_profile(running)
    _config(tmp_path, v4_steps=31)                 # 它自己那一套被改了：要停
    with pytest.raises(gc.GenerationProfileChanged, match="changed"):
        gc.require_current_profile(running)


def test_a_process_on_the_default_still_sees_the_default_change(tmp_path, monkeypatch):
    """沒有覆寫的行程照舊：預設被改＝它正在用的那一套變了，要停下來重啟。"""
    path = _config(tmp_path)
    _clear_env(monkeypatch)
    running = _pinned_reload(monkeypatch, path, None)
    gc.require_current_profile(running)
    _config(tmp_path, active="v4_5")
    with pytest.raises(gc.GenerationProfileChanged, match="changed"):
        gc.require_current_profile(running)


def test_the_bot_hands_its_environment_to_the_batch_it_spawns():
    """bot 自己 spawn 的批次（`/run`、監督者重生、一次性伺服器）要用 bot 那一套：
    靠的是 spawn 的 env 從 `os.environ` 展開（監督者放進去的覆寫值就在那裡），再加上
    `WEBRUNNER_GENERATION_IDENTITY` 讓子行程自己核對。少了展開，子行程會照預設啟動，
    然後因為身分不符而停下來。"""
    tree = ast.parse((REPO_ROOT / "axiomatic" / "discord_bot.py").read_text(encoding="utf-8"))
    envs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "env" or not isinstance(keyword.value, ast.Dict):
                continue
            keys = [k.value for k in keyword.value.keys if isinstance(k, ast.Constant)]
            if "WEBRUNNER_GENERATION_IDENTITY" in keys:
                envs.append(keyword.value)
    assert envs, "找不到 bot spawn 批次時帶 WEBRUNNER_GENERATION_IDENTITY 的那個 env"
    for env in envs:
        spreads = [v for k, v in zip(env.keys, env.values) if k is None]
        assert any(ast.unparse(v) == "os.environ" for v in spreads), ast.unparse(env)
        assert "WEBRUNNER_GENERATION_PROFILE" not in [
            k.value for k in env.keys if isinstance(k, ast.Constant)], (
            "bot 不該自己重設覆寫值；繼承它的監督者給的就對了")


# ---- 選擇檔 ---------------------------------------------------------------

@pytest.fixture
def files(tmp_path, monkeypatch):
    bot = tmp_path / ".generation_selection_bot.json"
    batch = tmp_path / ".generation_selection_batch.json"
    monkeypatch.setattr(sup, "BOT_GENERATION_SELECTION_FILE", bot)
    monkeypatch.setattr(sup, "BATCH_GENERATION_SELECTION_FILE", batch)
    _clear_env(monkeypatch)
    return {"discord": tmp_path / ".generation_selection_bot.discord.json",
            "telegram": tmp_path / ".generation_selection_bot.telegram.json",
            "batch": batch}


def test_the_selection_files_are_separate_per_supervisor_and_platform():
    """一支一個：共用的話先起來的那支會把另一支的選擇吃掉，幾支同時起來還會搶同一個檔。
    bot 的監督者一個平台一支，所以平台之間也要分開。"""
    paths = {sup.generation_selection_file("batch"),
             sup.generation_selection_file("bot", "discord"),
             sup.generation_selection_file("bot", "telegram")}
    assert len(paths) == 3
    assert all(path.parent == sup.BOT_GENERATION_SELECTION_FILE.parent for path in paths)


@pytest.mark.parametrize("key, platform", [
    ("dashboard", None),           # 不認得的鍵
    ("bot", None),                 # bot 一定要帶平台
    ("batch", "discord"),          # 批次不分平台
    ("bot", "../escape"),          # 會變成路徑的字串
    ("bot", "Discord"),            # 沒正規化過的名字
    ("bot", ""),
])
def test_an_unknown_key_or_platform_is_refused(key, platform):
    """寫錯的鍵不該安靜地寫到一個沒有人讀的檔，更不能寫到 repo 外面。"""
    with pytest.raises(KeyError):
        sup.generation_selection_file(key, platform)


def test_the_platform_name_shape_matches_the_platform_runtime():
    """`_supervisor` 刻意不 import `_platform_runtime`（批次的 import 閉包），所以平台名字的
    形狀抄了一份；兩份不一樣的話，某個合法平台的監督者會拿不到選擇。"""
    assert sup._PLATFORM_NAME.pattern == pr._VALID_NAME.pattern


def test_a_fresh_selection_is_taken_once_and_removed(files):
    sup.write_generation_selection("bot", "v4_5", platform="discord", now=1000.0)
    assert json.loads(files["discord"].read_text(encoding="utf-8")) == {
        "task": "bot:discord", "profile": "v4_5", "created": 1000.0}
    profile, note = sup.take_generation_selection("bot", platform="discord", now=1010.0)
    assert profile == "v4_5"
    assert "v4_5" in note
    assert not files["discord"].exists(), "讀過要刪掉，否則下一次啟動又會撿到"
    assert sup.take_generation_selection("bot", platform="discord", now=1011.0) == (None, None)


def test_no_selection_file_means_default_and_touches_nothing(files):
    assert sup.take_generation_selection("batch") == (None, None)
    assert not files["batch"].exists()


@pytest.mark.parametrize("age", [sup.GENERATION_SELECTION_TTL_SEC + 1, -60.0])
def test_a_stale_or_future_selection_is_ignored_and_removed(files, age):
    """叫醒失敗、監督者沒起來的話，那個選擇不得在下一次登入或開機時冒出來。"""
    sup.write_generation_selection("batch", "v4_5", now=1000.0)
    profile, note = sup.take_generation_selection("batch", now=1000.0 + age)
    assert profile is None
    assert "過期" in note
    assert not files["batch"].exists()


def test_the_edges_of_the_freshness_window_are_accepted(files):
    for age in (sup.GENERATION_SELECTION_TTL_SEC, -sup._GENERATION_SELECTION_SKEW_SEC):
        sup.write_generation_selection("bot", "v4_5", platform="discord", now=1000.0)
        assert sup.take_generation_selection(
            "bot", platform="discord", now=1000.0 + age)[0] == "v4_5"


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps(["v4_5"]),
    json.dumps({"task": "batch", "profile": "v4_5", "created": 1000.0}),          # 寫給批次的
    json.dumps({"task": "bot:telegram", "profile": "v4_5", "created": 1000.0}),   # 寫給別的平台的
    json.dumps({"task": "bot", "profile": "v4_5", "created": 1000.0}),            # 沒帶平台
    json.dumps({"task": "bot:discord", "profile": "  ", "created": 1000.0}),
    json.dumps({"task": "bot:discord", "profile": 5, "created": 1000.0}),
    json.dumps({"task": "bot:discord", "profile": "v4_5", "created": True}),
    json.dumps({"task": "bot:discord", "profile": "v4_5", "created": "1000"}),
    json.dumps({"task": "bot:discord", "profile": "v4_5"}),
    '{"task": "bot:discord", "profile": "v4_5", "created": NaN}',
])
def test_a_malformed_selection_falls_back_to_the_default_and_is_removed(files, content):
    files["discord"].write_text(content, encoding="utf-8")
    profile, note = sup.take_generation_selection("bot", platform="discord", now=1000.0)
    assert profile is None
    assert "格式不對" in note
    assert not files["discord"].exists()


def test_an_unreadable_selection_falls_back_and_says_it_could_not_be_removed(files):
    """讀不出來（這裡用「同名的是一個目錄」造）→ 預設；連刪都刪不掉也要講。"""
    files["discord"].mkdir()
    profile, note = sup.take_generation_selection("bot", platform="discord", now=1000.0)
    assert profile is None
    assert "讀不出來" in note and "刪不掉" in note


def test_a_fresh_selection_that_cannot_be_removed_is_still_used(files, monkeypatch):
    sup.write_generation_selection("bot", "v4_5", platform="discord", now=1000.0)

    def denied(_key, **_kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(sup, "discard_generation_selection", denied)
    profile, note = sup.take_generation_selection("bot", platform="discord", now=1001.0)
    assert profile == "v4_5"
    assert "刪不掉" in note


def test_a_failed_write_leaves_no_file_and_no_temp(files, monkeypatch):
    def denied(*_a):
        raise PermissionError("denied")
    monkeypatch.setattr(sup.os, "replace", denied)
    with pytest.raises(PermissionError):
        sup.write_generation_selection("batch", "v4_5")
    assert not files["batch"].exists()
    assert not list(files["batch"].parent.glob(".generation-selection-*.tmp"))


def test_discard_reports_whether_anything_was_there(files):
    assert sup.discard_generation_selection("bot", platform="discord") is False
    sup.write_generation_selection("bot", "v4_5", platform="discord")
    sup.write_generation_selection("bot", "v4_5", platform="telegram")
    assert sup.discard_generation_selection("bot", platform="discord") is True
    assert not files["discord"].exists()
    assert files["telegram"].exists(), "只刪這個平台的那一份"


class _Log:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)


def test_apply_puts_a_fresh_selection_into_the_environment(files, monkeypatch, capsys):
    sup.write_generation_selection("batch", "v4_5")
    log = _Log()
    assert sup.apply_generation_selection("batch", log) == "v4_5"
    assert os.environ[ENV] == "v4_5"
    assert any("v4_5" in line for line in log.lines), "記錄檔要看得出這次用了哪一套"
    capsys.readouterr()


def test_apply_without_a_selection_drops_an_inherited_override(files, monkeypatch, capsys):
    """沒有選擇＝預設，**不管**是誰、從哪個殼把監督者點起來。"""
    monkeypatch.setenv(ENV, "v4_5")
    log = _Log()
    assert sup.apply_generation_selection("bot", log, platform="discord") is None
    assert ENV not in os.environ
    assert any(ENV in line for line in log.lines), "丟掉呼叫端的值要講一聲"
    capsys.readouterr()


def test_apply_without_anything_is_silent(files, capsys):
    log = _Log()
    assert sup.apply_generation_selection("bot", log, platform="discord") is None
    assert ENV not in os.environ
    assert log.lines == []
    assert capsys.readouterr().out == ""


def test_stream_child_hands_the_override_to_a_real_child(files, monkeypatch, tmp_path):
    """端到端：監督者放進 `os.environ` 的值真的出現在子行程裡（`stream_child` 每次 spawn
    都從 `os.environ` 複製環境）。子行程只印一行，不碰任何專案檔案。"""
    monkeypatch.setenv(ENV, "v4_5")
    log_path = tmp_path / "child.log"
    with log_path.open("w", encoding="utf-8") as log:
        rc = sup.stream_child(
            [sys.executable, "-c",
             f"import os; print('PROFILE=' + str(os.environ.get({ENV!r})))"],
            log, cwd=str(tmp_path))
    assert rc == 0
    assert "PROFILE=v4_5" in log_path.read_text(encoding="utf-8")


# ---- 啟動器 -----------------------------------------------------------------

def _load_launcher(name):
    path = REPO_ROOT / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Lock:
    degraded = False

    def release(self):
        pass


class _Clock:
    """假時間：只有 `sleep` 會讓它前進，退避不會真的睡。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Stop(Exception):
    """假子行程的上限：丟例外拆掉監督迴圈（回 rc 的話 bot 那支會一直重生）。"""


class _Slot:
    def acquire(self, *_a, **_k):
        return True

    def release(self, *_a, **_k):
        pass


def _launcher(name, tmp_path, monkeypatch, rounds=2, platform="discord"):
    """在替身上架好 `name` 那支啟動器，回 `(module, 每一輪子行程看到的覆寫值)`。

    假的 `stream_child` 記下**當下** `os.environ` 裡的覆寫值（真的 `stream_child` 就是從
    那裡複製環境，上面那支端到端測試釘住了這一段），第一輪回 rc=1 讓監督者重生，
    跑滿 `rounds` 輪就丟 `_Stop`。bot 那支服務哪個平台由 `platform` 決定。"""
    module = _load_launcher(name)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / f"{name}.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG" if "webrunner" in name else "BOT_LOG",
                        tmp_path / f"{name}.log")
    monkeypatch.setattr(module, "acquire_single_instance_lock", lambda _path: _Lock())
    monkeypatch.setattr(module, "time", _Clock())
    monkeypatch.setattr(sys, "argv", [name])
    if "webrunner" in name:
        monkeypatch.setattr(module, "_chrome_slot", _Slot())
        monkeypatch.setattr(module, "_live_webrunner_pid", lambda: (None, True))
        monkeypatch.setattr(module, "_save_pid", lambda _pid: None)
        monkeypatch.setattr(module, "_clear_pid_if_ours", lambda _pid: None)
        monkeypatch.setattr(module, "_network_is_up", lambda: True)
        # 放棄門檻寫死得比測試的輪數高，不跟著正式設定檔走。
        monkeypatch.setattr(module, "load_bot_config", lambda: {"webrunner_supervisor": {
            "respawn_backoff_min_sec": 5, "respawn_backoff_max_sec": 300,
            "healthy_threshold_sec": 60, "rapid_fail_threshold_sec": 30,
            "rapid_fail_giveup_count": 99, "zero_progress_giveup_count": 99}})
    else:
        monkeypatch.setattr(module, "PLATFORM", platform)
        # 平台的狀態目錄不建在 repo 裡（鎖檔與記錄檔都已經導到 tmp_path）。
        monkeypatch.setattr(module._platform_runtime, "ensure_state_dir",
                            lambda *_a, **_k: tmp_path)
    seen = []

    def child(_cmd, _log, **_kwargs):
        seen.append(os.environ.get(ENV))
        if len(seen) >= rounds:
            raise _Stop
        return 1
    monkeypatch.setattr(module, "stream_child", child)
    return module, seen


# (啟動器, 它讀的選擇檔, 不該被它碰到的另外幾份)
_LAUNCHERS = [
    ("start_discord_bot.py", "discord", ("telegram", "batch")),
    ("start_webrunner.py", "batch", ("discord", "telegram")),
]


def _write(target, profile):
    if target == "batch":
        sup.write_generation_selection("batch", profile)
    else:
        sup.write_generation_selection("bot", profile, platform=target)


@pytest.mark.parametrize("name, own, others", _LAUNCHERS)
def test_each_launcher_takes_only_its_own_selection(name, own, others, files, tmp_path,
                                                    monkeypatch, capsys):
    _write(own, "v4_5")
    for other in others:
        _write(other, "other")
    module, seen = _launcher(name, tmp_path, monkeypatch, rounds=1)
    assert module.GENERATION_SELECTION_KEY == ("batch" if own == "batch" else "bot")
    with pytest.raises(_Stop):
        module.main()
    assert seen == ["v4_5"]
    assert not files[own].exists(), "自己的選擇讀過就要刪"
    for other in others:
        assert files[other].exists(), f"{other} 的選擇不得被吃掉"
    capsys.readouterr()


def test_a_bot_supervisor_reads_the_selection_of_the_platform_it_serves(
        files, tmp_path, monkeypatch, capsys):
    """一個平台一支 bot 監督者：telegram 那支只讀 telegram 那一份。"""
    _write("discord", "other")
    _write("telegram", "v4_5")
    module, seen = _launcher("start_discord_bot.py", tmp_path, monkeypatch, rounds=1,
                             platform="telegram")
    with pytest.raises(_Stop):
        module.main()
    assert seen == ["v4_5"]
    assert not files["telegram"].exists()
    assert files["discord"].exists()
    capsys.readouterr()


@pytest.mark.parametrize("name, own, _others", _LAUNCHERS)
def test_the_selection_survives_a_child_restart(name, own, _others, files, tmp_path,
                                                monkeypatch, capsys):
    """只在啟動時讀一次，之後每一輪重生的子行程都拿到同一套——選擇檔第一輪就刪掉了，
    所以「每輪重讀」的寫法第二輪會變回預設。"""
    _write(own, "v4_5")
    module, seen = _launcher(name, tmp_path, monkeypatch, rounds=3)
    with pytest.raises(_Stop):
        module.main()
    assert seen == ["v4_5", "v4_5", "v4_5"]
    capsys.readouterr()


@pytest.mark.parametrize("name, _own, _others", _LAUNCHERS)
def test_a_launch_without_a_selection_uses_the_default(name, _own, _others, files,
                                                        tmp_path, monkeypatch, capsys):
    """不帶旗標的叫醒、登入觸發、重新開機：沒有選擇檔 → 子行程裡沒有覆寫值 → 預設。
    呼叫端環境裡帶著的值也不沿用。"""
    monkeypatch.setenv(ENV, "v4_5")
    module, seen = _launcher(name, tmp_path, monkeypatch, rounds=2)
    with pytest.raises(_Stop):
        module.main()
    assert seen == [None, None]
    capsys.readouterr()


@pytest.mark.parametrize("name, own, _others", _LAUNCHERS)
def test_a_launcher_refused_by_the_lock_leaves_the_selection_alone(name, own, _others,
                                                                   files, tmp_path,
                                                                   monkeypatch, capsys):
    """被單一實例鎖擋掉的那一份不得把選擇吃掉——它不會 spawn 任何東西。"""
    _write(own, "v4_5")
    module, seen = _launcher(name, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "acquire_single_instance_lock", lambda _path: None)
    monkeypatch.setattr(module, "other_launcher_pids", lambda *_a, **_k: [])
    if hasattr(module, "_other_launcher_pids"):
        monkeypatch.setattr(module, "_other_launcher_pids", lambda: [])
    assert module.main() == 1
    assert seen == []
    assert files[own].exists()
    capsys.readouterr()
