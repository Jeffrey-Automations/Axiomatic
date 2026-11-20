"""直接叫醒工作排程器裡的監督者（`\\Axiomatic\\` 底下那些工作）。

用法（repo 根目錄）::

    py -3 wake_autostart.py                  # 全部叫醒（已經在跑的略過）
    py -3 wake_autostart.py --bot-only       # 只叫醒平台的監督者（每個平台一支）
    py -3 wake_autostart.py --batch-only     # 只叫醒批次的監督者（會開始產圖）
    py -3 wake_autostart.py --list-profiles  # 列出生成設定並標出預設，不啟動工作
    py -3 wake_autostart.py --profile v4_5   # 這次叫醒的監督者改用 v4_5（不改預設）

在終端機直接執行、沒帶 `--profile` 時會先列出生成 profile 讓人選（Enter ＝ 預設）；
選到的等同 `--profile NAME`。不是互動終端機（排程器、bot 的主機指令、管線）時不問，
照預設；`--presence-only` 不產圖，也不問；本專案已有行程在跑時也不問（選別套會被拒）。

**叫醒的是排程器裡現在真的有的工作**，不是設定檔算出來的那一份。bot 的工作是
**一個平台一筆**（`\\Axiomatic\\Bot-<平台>`），而剛被關掉的平台，它的工作仍然留在
排程器裡——用「現在開著哪些」去算要叫醒誰，那一筆就會變成永遠叫不醒也刪不掉。

工作本身由 `install_autostart.py --install` 註冊；這支只負責「現在就跑一次」，
跟登入時自動觸發的是同一份工作、同一個直譯器、同一個工作目錄。

**為什麼不直接執行 `start_discord_bot.py`。** 從某個終端機或 IDE 直接啟動時，監督者
和它底下的 bot、webrunner 都會繼承那個終端機的環境變數（`PYCHARM_HOSTED`、IDE 塞進來
的 `PYTHONPATH`、開發工具自己的工作階段變數……），bot 再原樣傳給後端 CLI 子行程。
透過排程器啟動，拿到的是登入工作的乾淨環境，和重新開機後自動拉起來的那一份完全一樣。

**`/sys restart` 需要監督者。** 它只是結束 bot、等監督者重生；bot 若是直接啟動、
沒有監督者，就只會關掉（2026-09-22 實際發生）。那時用這支把監督者叫起來。

**預設兩支都叫醒**（擁有者 2026-09-22 裁定），和登入時自動啟動的那一組一樣。批次一起來
就開始消耗佇列產圖，所以刻意用 `/stop` 停掉批次、只想把 bot 拉回來時，要用 `--bot-only`。

**`--profile NAME` 只管這一次叫醒。** `generation_config.json` 的 `active_profile` 是
持久的預設值，這支腳本不改它；NAME 經由每支監督者各一個一次性選擇檔交過去（排程器
不轉送環境變數），bot 的監督者一個平台一支，所以也是一個平台一份。監督者啟動時讀掉，
整個生命週期的子孫（重生的子行程、週期重啟的瀏覽器、bot 自己 spawn 的批次）都用 NAME。
之後沒有指定的叫醒、登入觸發、重新開機一律照預設。

**不會多開。** 已經有啟動器在跑的那一支直接略過（`_supervisor.other_launcher_pids`，
只是診斷用）；就算這個判斷失準，排程器的 `IgnoreNew` 與啟動器自己的單一實例鎖也會
擋掉第二份。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import shlex
from xml.etree import ElementTree
import sys
import time

from install_autostart import (BATCH_TASK, bot_task, _schtasks,
                               _wanted_task_names)
from axiomatic._supervisor import (
    GENERATION_SELECTION_TTL_SEC, discard_generation_selection, other_launcher_pids,
    write_generation_selection,
)
from axiomatic._process_control import (
    cmdline_runs_script, looks_like_python_process, python_script_argument,
)

REPO_ROOT = Path(__file__).resolve().parent
GENERATION_CONFIG_FILE = REPO_ROOT / "generation_config.json"
_PROFILE_PROCESS_PATHS = (
    REPO_ROOT / "start_discord_bot.py", REPO_ROOT / "start_webrunner.py",
    REPO_ROOT / "run_batch.py", REPO_ROOT / "wake_autostart.py",
    REPO_ROOT / "start_platforms.py",
    REPO_ROOT / "axiomatic" / "discord_bot.py",
    REPO_ROOT / "axiomatic" / "webrunner_je_only.py",
)


# 叫醒之後等啟動器出現在行程表的上限與輪詢間隔（秒）。排程器接受 `/Run` 之後，
# 啟動器通常一兩秒內就看得到；等不到不算失敗（可能只是慢，或被單一實例鎖擋掉），
# 改成叫人去看 `install_autostart.py --status` 的「上次結果」。
APPEAR_TIMEOUT_SEC = 15.0
APPEAR_POLL_SEC = 0.5


def _pid_text(pids: list[int]) -> str:
    return "、".join(str(pid) for pid in pids)


# bot 工作名的固定前綴（`\Axiomatic\Bot-`），從 `bot_task` 本身推出來，不另抄一份字面值。
_BOT_TASK_PREFIX = bot_task("x")[0].removesuffix("x")


def task_script(task_name: str) -> tuple[str, str | None]:
    """工作名 →（那筆工作跑的腳本, 用來分辨平台的那個參數）。

    bot 那幾筆共用同一個腳本檔名，所以「它在跑嗎」不能只比檔名——否則其中一個平台
    的那一支會替別的平台回答「在跑」。平台名是工作名去掉固定前綴之後的**整段**：
    平台名本身可以含 `-`（`my-plat`），從最後一個 `-` 切的話會取成 `plat`，選擇檔與
    「是否在跑」都會對到別的平台。認不出來的工作名直接報錯，不猜。
    """
    if task_name == BATCH_TASK[0]:
        return BATCH_TASK[1], None
    if not task_name.startswith(_BOT_TASK_PREFIX) or task_name == _BOT_TASK_PREFIX:
        raise ValueError(f"不是本專案的 bot 或批次工作：{task_name!r}")
    return "start_discord_bot.py", task_name[len(_BOT_TASK_PREFIX):]


def selection_target(task_name: str) -> tuple[str, str | None]:
    """工作名 → 那支監督者讀的選擇檔（`_supervisor.generation_selection_file` 的引數）。

    批次 → `("batch", None)`；bot → `("bot", 平台)`：bot 的監督者一個平台一支，各讀
    自己那個平台的那一份。"""
    _script, platform = task_script(task_name)
    return ("batch", None) if platform is None else ("bot", platform)


def wake(task_name: str, *, run=_schtasks, running=other_launcher_pids,
         sleep=time.sleep, clock=time.monotonic,
         timeout: float = APPEAR_TIMEOUT_SEC) -> int:
    """叫醒 `task_name` 那一份工作；回傳 0＝已經在跑或已叫醒，1＝沒註冊或排程器拒絕。

    `run`（排程器呼叫）、`running`（找啟動器行程）、`sleep`、`clock` 都可以注入，
    測試因此不必碰真的工作排程器與行程表。排程器只會被要求執行**一次**：等不到
    啟動器出現時不重試，因為第二次 `/Run` 在 `IgnoreNew` 之下什麼都不會做，只會讓
    訊息變得難懂。
    """
    script, platform = task_script(task_name)
    label = script if platform is None else f"{script} --platform {platform}"
    already = running(script, also_contains=platform)
    if already:
        print(f"{task_name}：{label} 已經在跑（pid {_pid_text(already)}），不再叫醒。")
        return 0
    if run("/Query", "/TN", task_name).returncode != 0:
        print(f"{task_name}：這份工作還沒註冊。先執行 "
              "py -3 install_autostart.py --install", file=sys.stderr)
        return 1
    done = run("/Run", "/TN", task_name)
    if done.returncode != 0:
        print(f"{task_name}：排程器拒絕執行：{done.stdout.strip()} "
              f"{done.stderr.strip()}", file=sys.stderr)
        return 1
    deadline = clock() + timeout
    while clock() < deadline:
        pids = running(script, also_contains=platform)
        if pids:
            print(f"{task_name}：已叫醒，{label} 在跑（pid {_pid_text(pids)}）。")
            return 0
        sleep(APPEAR_POLL_SEC)
    print(f"{task_name}：排程器已經接受，但 {timeout:.0f} 秒內沒看到 {label}。"
          "用 py -3 install_autostart.py --status 看「上次結果」。")
    return 0


def _registered_presence_only() -> bool:
    task_name, _script, _extra = bot_task("discord")
    result = _schtasks("/Query", "/TN", task_name, "/XML")
    if result.returncode != 0:
        return False
    try:
        root = ElementTree.fromstring(result.stdout)
        arguments = root.findtext(
            ".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Arguments",
            default="")
        return "--presence-only" in shlex.split(arguments, posix=False)
    except (ElementTree.ParseError, ValueError):
        return False


def _read_profile_config() -> tuple[bytes, dict]:
    raw = GENERATION_CONFIG_FILE.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if (not isinstance(data, dict) or not isinstance(data.get("profiles"), dict)
            or not data["profiles"]):
        raise ValueError("generation_config.json 必須包含非空的 profiles 物件。")
    return raw, data


def _profile_processes() -> tuple[list[int], bool]:
    try:
        import psutil
    except ImportError:
        return [], False
    pids = []
    complete = True
    targets = {path.resolve() for path in _PROFILE_PROCESS_PATHS}
    names = tuple(path.name for path in targets)
    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            if proc.info.get("pid") == os.getpid():
                continue
            try:
                name = proc.info.get("name")
                if name is None:
                    complete = False
                    continue
                if not looks_like_python_process(name):
                    continue
                cmdline = proc.cmdline()
                if not cmdline:
                    complete = False
                    continue
                if cmdline_runs_script(cmdline, names):
                    script = Path(python_script_argument(cmdline).strip('"'))
                    if not script.is_absolute():
                        script = Path(proc.cwd()) / script
                else:
                    module = _python_module_argument(cmdline)
                    known_modules = {path.stem for path in targets}
                    known_modules.update(".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
                                         for path in targets if path.is_relative_to(REPO_ROOT))
                    if module not in known_modules:
                        continue
                    script = Path(proc.cwd()).joinpath(*module.split(".")).with_suffix(".py")
                    if script.resolve() not in targets and not script.is_file():
                        complete = False
                        continue
                script = script.resolve()
                if (proc.info["pid"] == os.getppid()
                        and script == Path(__file__).resolve()):
                    continue
                if script in targets:
                    pids.append(proc.info["pid"])
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError, ValueError):
                complete = False
    except (psutil.Error, OSError):
        complete = False
    return pids, complete


def _python_module_argument(cmdline: list[str]) -> str | None:
    index = 1
    while index < len(cmdline):
        argument = cmdline[index]
        if not isinstance(argument, str) or not argument.startswith("-") or argument == "-":
            return None
        if argument in {"-X", "-W", "--check-hash-based-pycs"}:
            index += 2
            continue
        if not argument.startswith("--"):
            for offset, flag in enumerate(argument[1:], 2):
                if flag == "c":
                    return None
                if flag == "m":
                    return argument[offset:] or (cmdline[index + 1] if index + 1 < len(cmdline) else None)
                if flag in "XW":
                    break
        index += 1
    return None


def _prepare_profile(name: str, targets: list[str]) -> list[str]:
    """驗證 NAME，替這次要叫醒的每一筆工作的監督者各寫一個一次性選擇檔，回寫了哪些工作。

    **不改 `generation_config.json`**：`active_profile` 是持久的預設值，`--profile` 只管
    這一次叫醒起來的行程。排程器的 `/Run` 不轉送環境變數與參數，所以選擇經由磁碟交給
    監督者（`_supervisor.write_generation_selection`）；監督者啟動時讀掉、刪掉，放進自己
    的環境變數，整個生命週期的子孫都繼承它。bot 的監督者一個平台一支，所以一個平台一份。

    NAME 就是預設時什麼都不寫，只把殘留的選擇清掉——跟沒有指定一樣。
    """
    _raw, data = _read_profile_config()
    if name not in data["profiles"]:
        raise ValueError(f"找不到 profile {name!r}；請用 --list-profiles 查看名稱。")
    from axiomatic._generation_config import parse_generation_profile
    parse_generation_profile(data, GENERATION_CONFIG_FILE.parent, profile=name)
    default = data.get("active_profile")
    if default == name:
        _discard_selections(targets)
        print(f"生成 profile {name} 就是預設，照預設啟動。")
        return []
    # 有行程在跑就不指定：在跑的那些用的是它們啟動時的那一套，這次叫醒會略過它們，於是
    # 只有一部分換成 NAME（bot 與批次、平台與平台之間各用一套，bot 也收養不了那個批次）。
    # 看不出哪一套在跑（行程的環境變數不一定讀得到，監督者的轉接殼也沒有那個值），所以
    # 一律拒絕。先叫醒 bot、晚點才產圖的話，用 bot 的 `/run`：它 spawn 的批次繼承 bot 那一套。
    pids, complete = _profile_processes()
    if pids or not complete:
        raise RuntimeError("無法指定生成 profile：本專案的 bot、批次或監督者仍在執行"
                           "（它們沿用啟動時的生成設定），或無法確認行程狀態。"
                           "要用已在跑的 bot 那一套開始產圖，請改用 /run；"
                           "要換一套，先全部停止再重試。")
    for task_name in targets:
        if _schtasks("/Query", "/TN", task_name).returncode != 0:
            raise RuntimeError("要叫醒的工作尚未註冊或無法查詢，沒有指定生成設定。")
    written: list[str] = []
    try:
        for task_name in targets:
            key, platform = selection_target(task_name)
            write_generation_selection(key, name, platform=platform)
            written.append(task_name)
    except OSError:
        _discard_selections(written)
        raise
    print(f"這次叫醒的行程使用生成 profile {name}；之後沒有指定 --profile 的啟動"
          f"（含登入、重新開機）照預設 {default} 啟動。")
    return written


def _interactive() -> bool:
    """有人在終端機前面：stdin 與 stdout 都是主控台。

    排程器、bot 的主機指令、管線、`pythonw` 都不是——那些情境絕不能停下來等輸入，
    否則叫醒就卡死在一個沒有人會回答的提示上。
    """
    try:
        return bool(sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _ask_profile() -> str | None:
    """直接執行、沒有 `--profile` 時讓人選這次叫醒的生成 profile。

    回傳選到的名字，交給 `_prepare_profile` 照 `--profile` 的路走（驗證、只管這一次）。
    回傳 None ＝ 不顯示選單、照預設：設定讀不到、只有一套、或本專案已有行程在跑——
    那時選別套也只會被 `_prepare_profile` 拒絕，先問再拒只是白問一次。
    Enter 選預設；EOF／Ctrl+C 往上拋，由呼叫端當作取消。
    """
    try:
        _raw, config = _read_profile_config()
    except (OSError, ValueError) as error:
        print(f"讀不到生成設定（{error}），不顯示版本選單，照預設啟動。", file=sys.stderr)
        return None
    names = list(config["profiles"])
    default = config.get("active_profile")
    if len(names) < 2:
        return None
    pids, complete = _profile_processes()
    if pids or not complete:
        print("本專案的 bot、批次或監督者已在執行（或無法確認行程狀態），不顯示版本選單；"
              "新叫醒的照預設啟動。要換版本請先全部停止。")
        return None
    print("這次叫醒要用哪一套生成 profile？（直接按 Enter 用預設）")
    for number, name in enumerate(names, 1):
        print(f"  {number}) {name}{'（預設）' if name == default else ''}")
    while True:
        answer = input(f"編號或名稱 [{default}]：").strip()
        if not answer:
            return default if default in names else None
        if answer in names:
            return answer
        if answer.isdecimal() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        print(f"沒有 {answer!r} 這個選項。", file=sys.stderr)


def _discard_selections(targets: list[str]) -> None:
    """清掉這些工作的監督者的選擇檔（best-effort；清不掉只印一行，靠時效兜底）。"""
    for task_name in targets:
        key, platform = selection_target(task_name)
        try:
            discard_generation_selection(key, platform=platform)
        except OSError as error:
            print(f"{task_name}：舊的生成設定選擇刪不掉（{error!r}），"
                  f"{GENERATION_SELECTION_TTL_SEC:.0f} 秒後會自動失效。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="直接叫醒工作排程器裡的監督者（預設全部叫醒）。")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--bot-only", action="store_true",
                       help="只叫醒平台的監督者（每個平台一支）")
    scope.add_argument("--batch-only", action="store_true",
                       help="只叫醒批次的監督者（會開始產圖）")
    scope.add_argument("--presence-only", action="store_true",
                       help="只叫醒已註冊為僅狀態模式的 discord 工作")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile", metavar="NAME",
                           help="這次叫醒的行程改用生成 profile NAME（不改預設；"
                                "之後沒有指定的啟動照預設）")
    selection.add_argument("--list-profiles", action="store_true",
                           help="列出生成 profile 並標出預設，不啟動工作")
    args = parser.parse_args(argv or [])

    if args.list_profiles:
        try:
            _raw, config = _read_profile_config()
            for name in config["profiles"]:
                default = name == config.get("active_profile")
                print(f"* {name}（預設）" if default else f"  {name}")
            return 0
        except (OSError, ValueError) as error:
            print(f"無法讀取生成設定：{error}", file=sys.stderr)
            return 1
    if os.name != "nt":
        print("這支腳本只支援 Windows 工作排程器。", file=sys.stderr)
        return 1

    which = (["bot"] if args.bot_only
             else ["batch"] if args.batch_only
             else ["bot", "batch"])
    if args.presence_only:
        if not _registered_presence_only():
            print("請先執行 py -3 install_autostart.py --install --presence-only。",
                  file=sys.stderr)
            return 1
        targets = [bot_task("discord")[0]]
    else:
        targets = _wanted_task_names(which)
    # 排程器裡可能有手動建立或舊式命名、認不出平台的工作：略過並講出來，不讓整次叫醒當掉。
    recognised = []
    for task_name in targets:
        try:
            task_script(task_name)
        except ValueError:
            print(f"略過認不出平台的工作：{task_name}", file=sys.stderr)
            continue
        recognised.append(task_name)
    targets = recognised
    if not targets:
        print("`\\Axiomatic\\` 底下沒有符合的工作。先執行 "
              "py -3 install_autostart.py --install", file=sys.stderr)
        return 1
    selected: list[str] = []
    profile = args.profile
    # 直接執行（有人在終端機前、沒帶 --profile）→ 問一次。只偵測狀態的 bot 不產圖，不問。
    if profile is None and not args.presence_only and _interactive():
        try:
            profile = _ask_profile()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，沒有叫醒任何工作。", file=sys.stderr)
            return 1
    if profile is not None:
        try:
            selected = _prepare_profile(profile, targets)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"無法指定生成設定：{error}", file=sys.stderr)
            return 1
    else:
        # 沒有指定 → 這次照預設。先前某次叫醒失敗留下、還沒過期的選擇不得冒出來。
        _discard_selections(targets)
    rc = 0
    for task_name in targets:
        result = wake(task_name)
        if result != 0 and task_name in selected:
            # 排程器沒接受（或工作不見了）：監督者不會起來讀它，留著只會讓下一次啟動撿到。
            _discard_selections([task_name])
        rc |= result
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
