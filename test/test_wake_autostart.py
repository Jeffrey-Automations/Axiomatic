"""`wake_autostart.py`：直接叫醒工作排程器裡的監督者。

這支腳本存在的理由是 2026-09-22 那一次：bot 是直接啟動的、沒有監督者，擁有者下了
`/sys restart`，bot 就只是關掉了，當下只能手動去工作排程器把 `\\Axiomatic\\Bot`
叫起來。所以要守的是三件事：已經在跑的不要再叫（不多開）、沒註冊或被拒絕要講清楚
（不假裝成功）、預設**排程器裡有的全部**叫醒，`--bot-only`／`--batch-only` 只叫一邊。

bot 的工作是一個平台一筆，而它們共用同一個腳本檔名，所以「它在跑嗎」要連平台一起
問——不然其中一個平台的那一支會替別的平台回答「在跑」，而那個平台就永遠叫不醒。

測試不碰真的工作排程器，也不看真的行程表：排程器呼叫、啟動器探測、睡眠與時鐘
全部是注入的替身。選擇檔的路徑由 `conftest._SIDE_EFFECT_LOGS` 導到暫存目錄。

`--profile` 的語意：只管這次叫醒起來的行程，`active_profile` 是持久的預設值，這支
腳本不改它。
"""
from __future__ import annotations

import subprocess
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install_autostart as ia  # noqa: E402
import wake_autostart as wa  # noqa: E402
from axiomatic import _supervisor as sup  # noqa: E402  與 wake_autostart 同一個模組物件

# 合成的工作名：這幾支不碰真的排程器，所以名字只要形狀對就好。
BOT_TASK = ia.bot_task("discord")[0]
BOT_SCRIPT = "start_discord_bot.py"
BATCH_TASK = ia.BATCH_TASK[0]


class _Scheduler:
    """記下每一次排程器呼叫；`registered`／`accept` 決定 `/Query`／`/Run` 的回應。"""

    def __init__(self, *, registered: bool = True, accept: bool = True):
        self.calls: list[tuple[str, ...]] = []
        self.registered = registered
        self.accept = accept

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        ok = self.registered if args[0] == "/Query" else self.accept
        return subprocess.CompletedProcess(
            ["schtasks", *args], 0 if ok else 1, stdout="",
            stderr="" if ok else "ERROR: access denied")

    def runs(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == "/Run"]


class _Launchers:
    """`running(script)` 的替身：依序回傳排好的 pid 清單，用完之後一直回最後一個。"""

    def __init__(self, *answers: list[int]):
        self.answers = list(answers)
        self.asked: list[str] = []
        self.filters: list = []

    def __call__(self, script: str, *, also_contains=None) -> list[int]:
        self.asked.append(script)
        self.filters.append(also_contains)
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


class _Clock:
    """假時鐘：只有 `sleep` 會讓它前進，所以等待迴圈永遠不會真的睡。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def _wake(scheduler, launchers, clock=None, timeout=wa.APPEAR_TIMEOUT_SEC):
    clock = clock or _Clock()
    return wa.wake(BOT_TASK, run=scheduler, running=launchers,
                   sleep=clock.sleep, clock=clock.clock, timeout=timeout)


def test_a_launcher_that_is_already_running_is_left_alone(capsys):
    scheduler = _Scheduler()
    assert _wake(scheduler, _Launchers([4321])) == 0
    assert scheduler.calls == [], "已經在跑還去叫排程器，就是多開的第一步"
    assert "4321" in capsys.readouterr().out


def test_an_unregistered_task_is_reported_and_never_run(capsys):
    scheduler = _Scheduler(registered=False)
    assert _wake(scheduler, _Launchers([])) == 1
    assert scheduler.runs() == []
    assert "install_autostart.py --install" in capsys.readouterr().err


def test_a_refused_run_is_a_failure_not_a_success(capsys):
    scheduler = _Scheduler(accept=False)
    assert _wake(scheduler, _Launchers([])) == 1
    assert "access denied" in capsys.readouterr().err


def test_it_waits_until_the_launcher_shows_up(capsys):
    scheduler = _Scheduler()
    launchers = _Launchers([], [], [], [987])
    assert _wake(scheduler, launchers) == 0
    assert scheduler.runs() == [("/Run", "/TN", BOT_TASK)]
    assert set(launchers.asked) == {BOT_SCRIPT}
    assert "987" in capsys.readouterr().out


def test_waiting_in_vain_asks_the_scheduler_only_once_and_points_at_status(capsys):
    """等不到啟動器不重試：`IgnoreNew` 之下第二次 `/Run` 什麼都不會做。"""
    scheduler = _Scheduler()
    clock = _Clock()
    assert _wake(scheduler, _Launchers([]), clock=clock, timeout=3.0) == 0
    assert len(scheduler.runs()) == 1
    assert clock.sleeps == int(3.0 / wa.APPEAR_POLL_SEC), "等待有上限，也真的等到上限"
    assert "install_autostart.py --status" in capsys.readouterr().out


_REGISTERED = [ia.bot_task("discord")[0], ia.bot_task("telegram")[0],
               ia.BATCH_TASK[0]]


@pytest.mark.parametrize("argv, expected", [
    ([], _REGISTERED),
    (None, _REGISTERED),
    (["--bot-only"], _REGISTERED[:2]),
    (["--batch-only"], _REGISTERED[2:]),
])
def test_everything_registered_is_woken_unless_one_side_is_asked_for(
        monkeypatch, argv, expected):
    """預設和登入時自動啟動的那一組一樣：**排程器裡有的全部**。

    `--bot-only` 是「所有平台的那幾筆」，不是「某一個平台」——一個平台一個行程之後
    那已經不是一筆工作了。
    """
    monkeypatch.setattr(ia, "registered_tasks", lambda: list(_REGISTERED))
    woken: list[str] = []
    monkeypatch.setattr(wa, "wake", lambda name: woken.append(name) or 0)
    assert wa.main(argv) == 0
    assert woken == expected


def test_nothing_registered_is_a_failure_that_says_what_to_run(monkeypatch, capsys):
    """一筆工作都沒有時要出聲：「叫醒完成、但一支都沒叫」跟成功印起來一模一樣。"""
    monkeypatch.setattr(ia, "registered_tasks", lambda: [])
    assert wa.main([]) == 1
    assert "install_autostart.py --install" in capsys.readouterr().err


def test_one_failure_makes_the_whole_run_fail(monkeypatch):
    monkeypatch.setattr(ia, "registered_tasks", lambda: list(_REGISTERED))
    monkeypatch.setattr(
        wa, "wake", lambda name: 1 if name == ia.BATCH_TASK[0] else 0)
    assert wa.main([]) == 1


def test_the_two_only_flags_cannot_be_combined():
    with pytest.raises(SystemExit):
        wa.main(["--bot-only", "--batch-only"])


def test_it_wakes_the_very_tasks_the_installer_registers():
    """工作名稱與排程器呼叫只有一份（`install_autostart`）；抄一份就會有一天對不上。"""
    assert wa.BATCH_TASK is ia.BATCH_TASK
    assert wa._schtasks is ia._schtasks
    assert wa._wanted_task_names is ia._wanted_task_names


@pytest.mark.parametrize("task, script, platform", [
    (ia.bot_task("discord")[0], "start_discord_bot.py", "discord"),
    (ia.bot_task("telegram")[0], "start_discord_bot.py", "telegram"),
    (ia.BATCH_TASK[0], ia.BATCH_TASK[1], None),
])
def test_each_task_maps_back_to_its_script_and_platform(task, script, platform):
    """工作名 →（腳本, 平台）。平台那一半就是「它在跑嗎」問得準不準的關鍵。"""
    assert wa.task_script(task) == (script, platform)


def test_the_running_probe_is_asked_about_this_platform_only():
    """否則其中一個平台的監督者會替另一個平台回答「在跑」，那個平台就永遠叫不醒。"""
    launchers = _Launchers([1234])
    wa.wake(ia.bot_task("telegram")[0], run=_Scheduler(), running=launchers)
    assert launchers.filters == ["telegram"], launchers.filters
    batch = _Launchers([1234])
    wa.wake(ia.BATCH_TASK[0], run=_Scheduler(), running=batch)
    assert batch.filters == [None], batch.filters


@pytest.fixture
def profile_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ia, "registered_tasks", lambda: list(_REGISTERED))
    path = tmp_path / "generation_config.json"
    data = {"active_profile": "v5", "profiles": {
        "v5": {"settings": {"steps": 23}},
        "v4_5": {"settings": {"steps": 28}, "todo": {"prompt": "other/todo.md"}},
    }}
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(wa, "GENERATION_CONFIG_FILE", path)
    monkeypatch.setattr(wa, "_profile_processes", lambda: ([], True))
    return path, data


def _selection(task_name):
    """那筆工作的監督者的選擇檔內容；不在 → None。"""
    key, platform = wa.selection_target(task_name)
    path = sup.generation_selection_file(key, platform)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _no_selections():
    return all(_selection(name) is None for name in _REGISTERED)


def test_selection_files_are_redirected_out_of_the_repo():
    """下面每一支都會寫選擇檔；它們必須落在暫存目錄（`conftest._SIDE_EFFECT_LOGS`）。"""
    for name in _REGISTERED:
        key, platform = wa.selection_target(name)
        assert REPO_ROOT not in sup.generation_selection_file(key, platform).parents


@pytest.mark.parametrize("task, expected", [
    (ia.bot_task("discord")[0], ("bot", "discord")),
    (ia.bot_task("telegram")[0], ("bot", "telegram")),
    (ia.BATCH_TASK[0], ("batch", None)),
])
def test_each_task_hands_its_selection_to_its_own_supervisor(task, expected):
    """bot 的監督者一個平台一支，所以選擇也是一個平台一份；批次全機一份。"""
    assert wa.selection_target(task) == expected


def test_list_profiles_never_touches_scheduler(profile_config, monkeypatch, capsys):
    path, _data = profile_config
    original = path.read_bytes()
    monkeypatch.setattr(wa, "_schtasks", lambda *_a: pytest.fail("listing must not query scheduler"))
    monkeypatch.setattr(wa, "wake", lambda *_a: pytest.fail("listing must not wake"))
    assert wa.main(["--list-profiles"]) == 0
    assert capsys.readouterr().out.splitlines() == ["* v5（預設）", "  v4_5"]
    assert path.read_bytes() == original
    assert _no_selections()


@pytest.mark.parametrize("scope, expected", [
    ([], _REGISTERED), (["--bot-only"], _REGISTERED[:2]),
    (["--batch-only"], _REGISTERED[2:]), (["--presence-only"], [BOT_TASK]),
])
def test_profile_is_handed_to_exactly_the_woken_tasks(profile_config, monkeypatch, scope, expected):
    """`--profile` **不改** `generation_config.json`，只替這次叫醒的每一筆工作的監督者各寫
    一個選擇檔，而且在叫醒之前就寫好（監督者一起來就會去讀）。"""
    path, _data = profile_config
    original = path.read_bytes()
    scheduler = _Scheduler()
    monkeypatch.setattr(wa, "_schtasks", scheduler)
    monkeypatch.setattr(wa, "_registered_presence_only", lambda: True)
    woken = []

    def wake(name):
        selection = _selection(name)
        assert selection is not None, f"{name} 被叫醒的時候選擇檔還沒寫好"
        key, platform = wa.selection_target(name)
        assert selection["profile"] == "v4_5"
        assert selection["task"] == sup.generation_selection_tag(key, platform)
        woken.append(name)
        return 0
    monkeypatch.setattr(wa, "wake", wake)
    assert wa.main([*scope, "--profile", "v4_5"]) == 0
    assert woken == expected
    assert path.read_bytes() == original, "預設值不得被 --profile 改寫"
    for name in _REGISTERED:
        assert (_selection(name) is not None) == (name in expected), (
            f"{name}：只有這次叫醒的監督者可以拿到選擇")
    assert scheduler.calls == [("/Query", "/TN", name) for name in expected]
    assert not list(sup.BOT_GENERATION_SELECTION_FILE.parent.glob(".generation-selection-*.tmp"))


def test_the_default_profile_writes_nothing_and_does_not_require_idle(profile_config, monkeypatch):
    path, _data = profile_config
    original = path.read_bytes()
    sup.write_generation_selection("bot", "v4_5", platform="discord")   # 先前失敗的叫醒留下來的
    monkeypatch.setattr(wa, "_profile_processes", lambda: pytest.fail("not selecting"))
    monkeypatch.setattr(wa, "wake", lambda *_a: 0)
    assert wa.main(["--profile", "v5"]) == 0
    assert path.read_bytes() == original
    assert _selection(BOT_TASK) is None, "指定的就是預設，殘留的選擇要清掉"


def test_a_wake_without_profile_clears_leftover_selections(profile_config, monkeypatch):
    """沒有指定 → 照預設。先前叫醒失敗留下、還沒過期的選擇不得冒出來。"""
    for name in _REGISTERED:
        key, platform = wa.selection_target(name)
        sup.write_generation_selection(key, "v4_5", platform=platform)
    seen = []
    monkeypatch.setattr(wa, "wake", lambda name: seen.append(_selection(name)) or 0)
    assert wa.main(["--bot-only"]) == 0
    assert seen == [None, None]
    assert _selection(BATCH_TASK) is not None, "沒有叫醒的那一支不該被動到"


def test_a_refused_wake_does_not_leave_its_selection_behind(profile_config, monkeypatch):
    monkeypatch.setattr(wa, "_schtasks", _Scheduler())
    monkeypatch.setattr(wa, "wake", lambda name: 1 if name == BATCH_TASK else 0)
    assert wa.main(["--profile", "v4_5"]) == 1
    assert _selection(BOT_TASK) is not None
    assert _selection(BATCH_TASK) is None, "排程器沒接受，留著只會讓下一次啟動撿到"


@pytest.mark.parametrize("processes", [([123], True), ([], False)])
def test_busy_or_unknown_process_state_blocks_selection(profile_config, monkeypatch, processes):
    path, _data = profile_config
    original = path.read_bytes()
    monkeypatch.setattr(wa, "_profile_processes", lambda: processes)
    monkeypatch.setattr(wa, "_schtasks", lambda *_a: pytest.fail("must not query or run"))
    monkeypatch.setattr(wa, "wake", lambda *_a: pytest.fail("must not wake"))
    assert wa.main(["--profile", "v4_5"]) == 1
    assert path.read_bytes() == original
    assert _no_selections()


@pytest.mark.parametrize("name, invalid", [("absent", False), ("v4_5", True)])
def test_invalid_selection_does_not_write_or_wake(profile_config, monkeypatch, name, invalid):
    path, data = profile_config
    if invalid:
        data["profiles"]["v4_5"]["settings"]["steps"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(wa, "_profile_processes", lambda: pytest.fail("must validate first"))
    monkeypatch.setattr(wa, "wake", lambda *_a: pytest.fail("must not wake"))
    assert wa.main(["--profile", name]) == 1
    assert path.read_bytes() == original
    assert _no_selections()


def test_the_selection_is_validated_even_if_the_shell_carries_another_override(
        profile_config, monkeypatch):
    """驗證的是 NAME 本身，不是呼叫端環境裡剛好帶著的另一個名字。"""
    path, data = profile_config
    data["profiles"]["v4_5"]["settings"]["steps"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(sup.GENERATION_PROFILE_ENV, "v5")
    monkeypatch.setattr(wa, "_profile_processes", lambda: pytest.fail("must validate first"))
    monkeypatch.setattr(wa, "wake", lambda *_a: pytest.fail("must not wake"))
    assert wa.main(["--profile", "v4_5"]) == 1


def test_unregistered_task_writes_no_selection(profile_config, monkeypatch):
    path, _data = profile_config
    original = path.read_bytes()
    monkeypatch.setattr(wa, "_schtasks", _Scheduler(registered=False))
    monkeypatch.setattr(wa, "wake", lambda *_a: pytest.fail("must not wake"))
    assert wa.main(["--profile", "v4_5"]) == 1
    assert path.read_bytes() == original
    assert _no_selections()


def test_failed_atomic_replace_writes_nothing_and_removes_temp(profile_config, monkeypatch):
    monkeypatch.setattr(wa, "_schtasks", _Scheduler())
    monkeypatch.setattr(wa, "wake", lambda *_a: pytest.fail("must not wake"))
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == len(_REGISTERED):              # 最後一支（批次）寫不進去
            raise PermissionError("denied")
        return real_replace(src, dst)
    monkeypatch.setattr(sup.os, "replace", flaky)
    assert wa.main(["--profile", "v4_5"]) == 1
    assert len(calls) == len(_REGISTERED)
    assert _no_selections(), "寫到一半失敗，已經寫好的那幾支也要收回"
    assert not list(sup.BOT_GENERATION_SELECTION_FILE.parent.glob(".generation-selection-*.tmp"))


class _ProfileProcess:
    def __init__(self, pid, script, cwd):
        self.info = {"pid": pid, "name": "python.exe"}
        self.script = script
        self.directory = cwd

    def cmdline(self):
        if isinstance(self.script, Exception):
            raise self.script
        if isinstance(self.script, list):
            return self.script
        return ["python.exe", str(self.script)]

    def cwd(self):
        return str(self.directory)


@pytest.mark.parametrize("relative", [True, False])
def test_process_scan_matches_this_checkout_only(tmp_path, monkeypatch, relative):
    import psutil
    target = tmp_path / "ours" / "discord_bot.py"
    own = _ProfileProcess(os.getpid() + 100, target.name if relative else target, target.parent)
    other = _ProfileProcess(os.getpid() + 200, tmp_path / "other" / target.name, tmp_path)
    monkeypatch.setattr(wa, "_PROFILE_PROCESS_PATHS", (target,))
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([own, other]))
    assert wa._profile_processes() == ([own.info["pid"]], True)


def test_inaccessible_python_process_makes_scan_unknown(monkeypatch):
    import psutil
    proc = _ProfileProcess(os.getpid() + 100, psutil.AccessDenied(123), Path.cwd())
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([proc]))
    assert wa._profile_processes() == ([], False)


def test_disappearing_process_does_not_block_scan(monkeypatch):
    import psutil
    proc = _ProfileProcess(os.getpid() + 100, psutil.NoSuchProcess(123), Path.cwd())
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([proc]))
    assert wa._profile_processes() == ([], True)


def test_own_interpreter_redirector_does_not_block_selection(monkeypatch):
    import psutil
    parent = _ProfileProcess(os.getppid(), Path(wa.__file__), Path.cwd())
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([parent]))
    assert wa._profile_processes() == ([], True)


def test_another_wake_process_still_blocks_selection(monkeypatch):
    import psutil
    parent = _ProfileProcess(os.getppid(), Path(wa.__file__), Path.cwd())
    other = _ProfileProcess(os.getpid() + 100, Path(wa.__file__), Path.cwd())
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([parent, other]))
    assert wa._profile_processes() == ([other.info["pid"]], True)


@pytest.mark.parametrize("module, relative", [
    ("start_discord_bot", "start_discord_bot.py"),
    ("discord_bot", "discord_bot.py"),
    ("axiomatic.discord_bot", "axiomatic/discord_bot.py"),
])
def test_module_launches_are_detected(tmp_path, monkeypatch, module, relative):
    import psutil
    target = tmp_path / relative
    proc = _ProfileProcess(os.getpid() + 100, ["python.exe", "-m", module], tmp_path)
    monkeypatch.setattr(wa, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(wa, "_PROFILE_PROCESS_PATHS", (target,))
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([proc]))
    assert wa._profile_processes() == ([proc.info["pid"]], True)


def test_formatter_module_argument_is_not_a_running_bot(monkeypatch):
    import psutil
    proc = _ProfileProcess(os.getpid() + 100,
                           ["python.exe", "-m", "black", "axiomatic/discord_bot.py"], Path.cwd())
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([proc]))
    assert wa._profile_processes() == ([], True)


def test_known_module_with_unresolved_location_is_unknown(tmp_path, monkeypatch):
    import psutil
    proc = _ProfileProcess(os.getpid() + 100, ["python.exe", "-m", "discord_bot"], tmp_path)
    monkeypatch.setattr(psutil, "process_iter", lambda **_k: iter([proc]))
    assert wa._profile_processes() == ([], False)
