import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import _reply_confirm
import discord_bot as bot


def _interaction(uid=42):
    return SimpleNamespace(user=SimpleNamespace(id=uid), response=SimpleNamespace(
        send_message=AsyncMock(), edit_message=AsyncMock()))


def test_buttons_recheck_permission_and_execute_only_once():
    async def scenario():
        allowed = False
        checks, executions = [], []
        async def authorize(interaction):
            checks.append(interaction.user.id)
            return interaction if allowed else None
        async def execute(interaction):
            executions.append(interaction.user.id)
        view = _reply_confirm.ConfirmView(owner_id=42, authorize=authorize, execute=execute)
        await view.confirm.callback(_interaction(99))
        assert checks == executions == []
        await view.confirm.callback(_interaction())
        assert checks == [42] and executions == [] and not view.used
        allowed = True
        await view.confirm.callback(_interaction())
        await view.confirm.callback(_interaction())
        assert checks == [42, 42] and executions == [42]
        assert all(button.disabled for button in view.children)
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_cancel_and_timeout_never_execute(ending):
    async def scenario():
        execute = AsyncMock()
        view = _reply_confirm.ConfirmView(owner_id=42, authorize=AsyncMock(return_value=True), execute=execute)
        if ending == "cancel":
            await view.cancel.callback(_interaction())
        else:
            await view.on_timeout()
        await view.confirm.callback(_interaction())
        execute.assert_not_awaited()
    asyncio.run(scenario())


def test_slash_confirmation_reuses_the_command_gate_at_click_time(monkeypatch):
    async def scenario():
        command = SimpleNamespace(qualified_name="macro delete")
        proxy = SimpleNamespace(finish=AsyncMock(), ack=AsyncMock())
        original = _interaction()
        original.command = command
        original.namespace = SimpleNamespace(name="sample")
        original.extras = {"proxy": proxy}
        views, checks, actions = [], [], []
        async def reply(message, content=None, **kwargs):
            views.append(kwargs["view"])
            return None
        async def gate(checked):
            checks.append((checked.command, checked.user.id))
            checked.extras["proxy"] = proxy
            return True
        async def handler(message, value):
            actions.append(value)
        monkeypatch.setattr(bot, "safe_reply", reply)
        monkeypatch.setattr(bot, "_tree_check", gate)
        await bot._slash_run(original, handler, "sample")
        assert actions == checks == []
        component = _interaction()
        component.response.is_done = lambda: True
        await views[0].confirm.callback(component)
        assert checks == [(command, 42)] and actions == ["sample"]
    asyncio.run(scenario())


def test_text_confirmation_is_scoped_expires_and_can_be_cancelled(monkeypatch):
    async def scenario():
        pending = {}
        clock = [0.0]
        replies = []
        async def reply(message, text):
            replies.append(text)
        monkeypatch.setattr(bot, "_pending_confirmations", pending)
        monkeypatch.setattr(bot, "safe_reply", reply)
        monkeypatch.setattr(bot.time, "monotonic", lambda: clock[0])
        # asyncio also uses monotonic: no timeouts are used while this clock is injected.
        a = SimpleNamespace(author=SimpleNamespace(id=42), channel=SimpleNamespace(id=1))
        b = SimpleNamespace(author=SimpleNamespace(id=43), channel=SimpleNamespace(id=1))
        assert not await bot._text_confirmation(a, "!macro delete x")
        assert not await bot._text_confirmation(b, "!macro delete x")
        assert not await bot._text_confirmation(a, "!macro delete y")
        assert not await bot._text_confirmation(a, "取消")
        assert not await bot._text_confirmation(a, "!macro delete y")
        clock[0] = 61
        assert not await bot._text_confirmation(a, "!macro delete y")
        assert await bot._text_confirmation(a, "!macro delete y")
        assert bot._requires_confirmation(SimpleNamespace(qualified_name="input key clear")) is False
    # Replace the module clock rather than stdlib time shared with the event loop.
    original_time = bot.time
    monkeypatch.setattr(bot, "time", SimpleNamespace(monotonic=original_time.monotonic))
    asyncio.run(scenario())


def test_all_deletion_spellings_require_confirmation_and_previews_do_not():
    for name in ("todo prompt pop", "macro rm_line", "sys cleanup_debug",
                 "dorossi workspace_clean", "dorossi queue failed_clear"):
        assert bot._requires_confirmation(SimpleNamespace(qualified_name=name)), name
    assert not bot._requires_confirmation(
        SimpleNamespace(qualified_name="dorossi workspace_clean"), SimpleNamespace(dry=True))
