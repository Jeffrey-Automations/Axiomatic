"""刪除／清空前的確認元件；權限由呼叫端在點選當下重新檢查。"""
from __future__ import annotations

import sys
import discord


class ConfirmView(discord.ui.View):
    def __init__(self, *, owner_id, authorize, execute, timeout=60.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.authorize = authorize
        self.execute = execute
        self.used = False
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("只有發起操作的人可以確認或取消。", ephemeral=True)
        return False

    async def _finish(self, interaction, text):
        self.used = True
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(content=text, view=self)
        except discord.HTTPException as error:
            print(f"confirmation edit failed: {type(error).__name__}", file=sys.stderr)

    @discord.ui.button(label="確認", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        if not await self.interaction_check(interaction):
            return
        if self.used:
            await interaction.response.send_message("這次確認已結束。", ephemeral=True)
            return
        authorized = await self.authorize(interaction)
        # 權限查詢可能讓出事件迴圈；逾時與重複點選都必須在執行前再檢查。
        if authorized is None or self.used:
            return
        await self._finish(interaction, "✅ 已確認，正在執行。")
        await self.execute(authorized)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _button):
        if not await self.interaction_check(interaction):
            return
        if not self.used:
            await self._finish(interaction, "已取消，沒有變更。")

    async def on_timeout(self):
        self.used = True
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="確認已逾時，沒有變更。", view=self)
            except discord.HTTPException as error:
                print(f"confirmation timeout edit failed: {type(error).__name__}", file=sys.stderr)
