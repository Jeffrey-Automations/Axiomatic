# 專案簡介與架構

## 系統概觀

```text
        ┌─────────────────┐   待辦 / 設定 (檔案)   ┌──────────────────────┐
        │   平台行程      │ ─────────────────────▶ │  webrunner_*.py      │
        │（一個平台一個） │                        │  （瀏覽器批次產圖）  │
        │                 │ ◀───────────────────── │                      │
        └─────────────────┘   圖片 / log / 事件     └──────────────────────┘
                 ▲                                            │
                 │ 指令（斜線或文字，視平台而定）             │ 登入 + 產圖
                 │                                            ▼
            你（使用者）                                  出圖服務網站
```

- 你在自己常用的聊天平台上，用指令把角色、prompt 排進待辦佇列。
- webrunner 從佇列讀出工作，登入出圖服務、逐一填欄位、連續產圖、存到
  `output/<角色>/`。
- webrunner 把進度寫成事件（`events.ndjson`）與紀錄（`webrunner.log`），bot
  讀回後在頻道貼通知，並提供 `/gen progress` / `/gen current` / `/out rate` 等查詢。

## 模組邊界（重要原則）

兩個長時間執行的腳本**只透過磁碟上的檔案溝通**，永遠不互相 `import`：

| 產生者 | 檔案 | 消費者 |
|---|---|---|
| `discord_bot.py` | `todo_prompt.md`、`todo_character{1,2}.md`、`todo_undesired.md`、`prompt.md`、`undesired.md`、`webrunner.pid` | webrunner |
| `webrunner_*.py` | `output/<角色>/*.png`、`debug_*.png`、`webrunner.log`、`events.ndjson` | bot（`/log tail`、`/latest`、`/gen progress`、`/gen current`…） |

跨行程的狀態都留在磁碟，所以 bot 重啟是無害的——它會從 `webrunner.pid`
重新認領仍在跑的 webrunner，從 `.backup/` 重建 undo 堆疊，從 `events.ndjson`
的尾端接續監看。

## 單一 webrunner 入口

`webrunner_je_only.py` 透過瀏覽器函式庫取得 driver，DOM 操作、佇列配對、批次與單圖服務集中在 `_webrunner_shared.py`。
`/run`、本機批次與開機啟動都使用這個入口；異常結束依退避策略重啟，快速失敗或零產出連續達上限就停止，等待人工處理。

## 目錄結構（重點）

```text
Axiomatic/
├── axiomatic/
│   ├── webrunner_je_only.py     # Selenium 批次產圖（長時間執行）
│   ├── webrunner_je_only.py     # je_web_runner 變體
│   ├── discord_bot.py           # Discord 事件迴圈
│   ├── dorossi_backend.py       # Dorossi 問答後端（bot 端）
│   ├── _batch_config.py         # batch_config.json 載入 / 寫入
│   ├── _bot_config.py           # bot_config.json 載入
│   ├── _webrunner_shared.py     # 瀏覽器流程核心（run_batch）
│   ├── _queue_consume.py        # 動態佇列消耗決策（純函式）
│   ├── _run_progress.py         # 中斷角色的續跑檢查點
│   ├── _supervisor.py           # 監督器退避的純數學（兩個啟動器共用）
│   ├── _process_control.py      # 行程探查 / 終止原語（bot 端）
│   ├── _gui_control.py          # 桌面自動化門面（bot 端；實作在函式庫）
│   └── presence_probe.py        # 本機 presence（遊戲 / 音樂）偵測
├── start_webrunner.py           # webrunner 監督式啟動器（單一 je 入口）
├── start_discord_bot.py         # bot 監督式啟動器（無限重啟）
├── batch_config.json            # 產圖批次參數（hot-reload，每角色重讀；唯一追蹤的設定檔）
├── *.example.json / *.example.md  # 版本庫帶的範本，複製成同名正式檔再填
├── bot_config.json              # bot runtime 設定（啟動時讀一次；gitignored）
├── auth.md / discord_bot_token.md  # 憑證（gitignored）
├── prompt.md / character{1,2}.md / undesired.md  # 各佇列為空時的 fallback（gitignored）
├── todo_prompt.md / todo_character{1,2}.md / todo_undesired.md  # 待辦佇列（gitignored）
├── output/                      # 產出 PNG（gitignored）
├── .chrome_profile/             # 保存的 Chrome 登入狀態（gitignored）
└── webrunner.{log,pid} / events.ndjson         # 行程產物
```
