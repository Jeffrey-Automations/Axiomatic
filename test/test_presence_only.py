"""只偵測狀態模式的註冊、啟動與事件邊界，不連線也不操作桌面。"""
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "axiomatic")]

import discord_bot as b
import install_autostart as ia
import start_discord_bot as launcher
import wake_autostart as wa


@pytest.fixture
def presence_only(monkeypatch):
    monkeypatch.setattr(b, "_PRESENCE_ONLY", True)
    monkeypatch.setattr(b, "_last_mirrored_presence_key", "old")
    for name in ("_load_pid", "_load_image_msgs", "_dorossi_restore_queued_turns_once",
                 "_schedule_coro", "_build_chat_transports",
                 "_ensure_chat_transports_alive"):
        monkeypatch.setattr(b, name, Mock(side_effect=AssertionError(name)))


def test_presence_ready_initializes_mirror_without_other_startup(presence_only,
                                                               monkeypatch):
    monkeypatch.setattr(b, "TARGET_PRESENCE_USERNAME", "test")
    member = SimpleNamespace()
    mirror = AsyncMock()
    revive = Mock()
    monkeypatch.setattr(b, "client", SimpleNamespace(
        user=None, guilds=[SimpleNamespace(members=[member], name="test")]))
    monkeypatch.setattr(b, "_matches_target_user", lambda item: item is member)
    monkeypatch.setattr(b, "_mirror_member_presence", mirror)
    monkeypatch.setattr(b, "_ensure_background_tasks_alive", revive)
    keep, sync = Mock(), AsyncMock()
    monkeypatch.setattr(b, "_keep_only_commands", keep)
    monkeypatch.setattr(b, "_sync_slash_commands", sync)
    asyncio.run(b.on_ready())
    mirror.assert_awaited_once_with(member)
    revive.assert_called_once_with(source="on_ready")
    assert b._last_mirrored_presence_key is None
    # the commands this mode still takes are synced, and only those
    keep.assert_called_once_with(b.tree, b._PRESENCE_ONLY_COMMANDS)
    sync.assert_awaited_once()


def test_presence_mode_keeps_only_its_commands_on_the_tree():
    tree = b.discord.app_commands.CommandTree(b.discord.Client(intents=b.discord.Intents.none()))

    async def handler(interaction):
        return None

    for name in ("3d", "run", "help"):
        tree.add_command(b.discord.app_commands.Command(name=name, description="x", callback=handler))
    b._keep_only_commands(tree, b._PRESENCE_ONLY_COMMANDS)
    assert [command.name for command in tree.get_commands()] == ["3d"]
    assert "3d" in {command.name for command in b.tree.get_commands()}    # the real /3d is in that set


def test_presence_mode_takes_only_its_commands(presence_only):
    def asked(name):
        return asyncio.run(b._tree_check(SimpleNamespace(
            type=b.discord.InteractionType.application_command, command=SimpleNamespace(qualified_name=name))))
    assert asked("3d")
    assert not asked("run") and not asked("dorossi ask")


def test_presence_background_recovery_starts_only_probe(presence_only, monkeypatch):
    started = []
    task = SimpleNamespace(done=lambda: False)
    def start(coro, label):
        coro.close()
        started.append(label)
        return task
    monkeypatch.setattr(b, "_presence_probe_task", None)
    monkeypatch.setattr(b, "_start_supervised_task", start)
    b._ensure_background_tasks_alive(source="test")
    b._ensure_background_tasks_alive(source="test")
    assert started == ["presence-probe"]
    assert b._presence_probe_task is task


def test_presence_resume_reapplies_without_resuming_work(presence_only, monkeypatch):
    revive = Mock()
    apply = AsyncMock()
    monkeypatch.setattr(b, "_ensure_background_tasks_alive", revive)
    monkeypatch.setattr(b, "_apply_combined_presence", apply)
    asyncio.run(b.on_resumed())
    revive.assert_called_once_with(source="on_resumed")
    apply.assert_awaited_once()
    assert b._last_mirrored_presence_key is None


def test_presence_mode_ignores_messages_reactions_and_autocomplete(presence_only):
    asyncio.run(b.on_message(object()))
    asyncio.run(b.on_raw_reaction_add(object()))
    for kind in (b.discord.InteractionType.autocomplete,
                 b.discord.InteractionType.application_command):
        assert not asyncio.run(b._tree_check(SimpleNamespace(type=kind)))


def test_direct_bot_mode_skips_desktop_cleanup(monkeypatch):
    monkeypatch.setattr(b._platform_runtime, "ensure_state_dir", Mock())
    lock = SimpleNamespace(release=Mock())
    monkeypatch.setattr(b, "_PRESENCE_ONLY", False)
    monkeypatch.setattr(b, "acquire_single_instance_lock", lambda path: lock)
    for name in ("_log_code_fingerprint", "_install_reconnect_log_filter",
                 "_claim_batch_supervision", "check_setup"):
        monkeypatch.setattr(b, name, Mock())
    monkeypatch.setattr(b, "read_token", lambda path: "test-token")
    run = Mock()
    monkeypatch.setattr(b, "client", SimpleNamespace(run=run))
    cleanup = Mock(side_effect=AssertionError("desktop cleanup"))
    monkeypatch.setattr(b, "_gui", SimpleNamespace(
        job_stop_all=cleanup, release_all_inputs=cleanup))
    assert b.main(["--presence-only"]) == 0
    assert b._PRESENCE_ONLY
    run.assert_called_once_with("test-token")
    cleanup.assert_not_called()
    lock.release.assert_called_once()


@pytest.mark.parametrize("enabled", [True, False])
def test_launcher_restarts_keep_selected_mode(monkeypatch, tmp_path, enabled):
    monkeypatch.setattr(launcher._platform_runtime, "ensure_state_dir", Mock())
    commands = []
    lock = SimpleNamespace(degraded=False, release=Mock())
    def child(cmd, log, **kwargs):
        commands.append(list(cmd))
        if len(commands) == 2:
            raise KeyboardInterrupt
        return 0
    monkeypatch.setattr(launcher, "BOT_LOG", tmp_path / "bot.log")
    monkeypatch.setattr(launcher, "acquire_single_instance_lock", lambda path: lock)
    monkeypatch.setattr(launcher, "python_command", lambda: ["python"])
    monkeypatch.setattr(launcher, "stream_child", child)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(
        monotonic=lambda: 0, sleep=lambda delay: None))
    assert launcher.main(["--presence-only"] if enabled else []) == 0
    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert ("--presence-only" in commands[0]) is enabled


def test_registration_preserves_mode_and_disables_existing_batch(monkeypatch):
    monkeypatch.setattr(ia._platform_runtime, "enabled_platforms", lambda config: ["discord", "telegram"])
    calls = []
    xml = []
    def scheduler(*args):
        calls.append(args)
        if args[0] == "/Create":
            xml.append(Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16"))
        return subprocess.CompletedProcess(args, 0, "TaskName: " + ia.BATCH_TASK[0] + "\nTaskName: " + ia.bot_task("telegram")[0], "")
    monkeypatch.setattr(ia, "_schtasks", scheduler)
    monkeypatch.setattr(ia, "_current_user", lambda: "HOST\\USER")
    assert ia.install(["bot"], presence_only=True) == 0
    assert len(xml) == 1
    arguments = ElementTree.fromstring(xml[0]).findtext(
        ".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Arguments")
    assert arguments.endswith(" --presence-only")
    assert ("/Change", "/TN", ia.BATCH_TASK[0], "/DISABLE") in calls
    assert ("/Change", "/TN", ia.bot_task("telegram")[0], "/DISABLE") in calls
    assert not any("/End" in call or "/Delete" in call for call in calls)


@pytest.mark.parametrize("query_rc,change_rc,expected", [(1, 0, 1), (0, 1, 1), (0, 0, 0)])
def test_disable_does_not_hide_failures(monkeypatch, query_rc, change_rc, expected):
    monkeypatch.setattr(ia, "_schtasks", lambda *args: subprocess.CompletedProcess(
        args, query_rc if args[0] == "/Query" else change_rc,
        "TaskName: " + ia.BATCH_TASK[0], ""))
    assert ia._disable_other_autostart() == expected


@pytest.mark.parametrize("args", [
    ["--status", "--presence-only"], ["--remove", "--presence-only"],
    ["--install", "--batch-only", "--presence-only"],
])
def test_registration_rejects_incompatible_mode(args):
    with pytest.raises(SystemExit) as error:
        ia.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_wake_presence_mode_checks_registration_before_start(monkeypatch, enabled):
    xml = ia._task_xml("start_discord_bot.py", "HOST\\USER", ["--platform", "discord"] + (["--presence-only"] if enabled else []))
    monkeypatch.setattr(wa, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(wa, "_schtasks", lambda *args: subprocess.CompletedProcess(
        args, 0, xml, ""))
    wake = Mock(return_value=0)
    monkeypatch.setattr(wa, "wake", wake)
    assert wa.main(["--presence-only"]) == (0 if enabled else 1)
    assert wake.call_args_list == ([((ia.bot_task("discord")[0],),)] if enabled else [])


@pytest.mark.parametrize("module", [b, launcher])
def test_presence_rejects_other_platforms_before_start(module):
    with pytest.raises(SystemExit) as error:
        module.main(["--platform", "telegram", "--presence-only"])
    assert error.value.code == 2
