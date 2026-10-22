# 程式審核：Axiomatic

審核者＝工作區程式審核；日期＝2026-10-22；對象＝`Axiomatic`；結論＝有條件通過。

## 審核者

工作區程式審核。日期 2026-10-22。對象為本工作樹的可散布自動化主機（套件在 `axiomatic/`，多平台、多行程）。此為熱點審核，不是逐行審完。未改其他檔、未跑測試、未啟動 bot 或批次、未連線。

## 審核規範

更嚴的數字從嚴。與已知產品差異（含對另一專案的契約差異）不記成缺陷。

1. 設計：分層、一個平台一個行程、bot 與 webrunner 只經磁碟與被動共用模組溝通，跨專案邊界以 `architecture.md` §6 為準。
2. 可讀性：循環複雜度不超過 15、函式不超過 80 行、巢狀不超過 4、參數不超過 7、一行不超過 120；專案若更嚴，從嚴。
3. 錯誤處理：不吞掉該處理的失敗；不靠裸 `except`。
4. 死碼：執行不到、或正式進入點綁定不到的函式，視同缺陷，不因測試能匯入而算存在。
5. 型別與 docstring：對外行為要有型別與說明；本輪只抽查熱點，未全量。
6. 安全：不把祕密寫進程式或版控；不可信輸入不進 `eval`／`exec`／`shell=True`／`pickle`／不安全的 `yaml.load`；不關閉 TLS 驗證。聊天指令把使用者輸入送進 shell 時，必須先過擁有者閘，且非擁有者失敗關閉。
7. 測試：守門與熱點要能在正式進入點打到，不能只在「當模組匯入」時為真。本輪未執行測試。
8. 效能：不在這輪熱點裡另開。
9. §6：瀏覽器與桌面函式庫的入口、版本下限與載入順序以架構文件為準；不把另一專案的契約差異改寫成缺陷。
10. 文件同步：`architecture.md` 的分層與 §6、以及會被使用者或下一個維護者當成現況的句子，要和程式一致；未完成項目以 `progress.md` 為準，不重複開缺陷。

嚴重度：缺陷必須處理才算通過；建議不擋通過，但是未關閉的改善項；備註只記核對結果。通過＝沒有未關閉缺陷。有條件通過＝沒有立即損害（憑證外洩、未授權的 shell、關掉 TLS 這類）。需修正＝未關閉缺陷會造成立即損害。

## 審核紀錄

### 方法

讀了 `architecture.md` 的分層（§2）與 §6、`CLAUDE.md` 的 Secrecy、擁有者閘、憑證不進版控與子行程編碼相關段落（未把整份規則檔當逐行審閱），以及 `progress.md` 前 80 行。

在 `axiomatic/*.py`（不含測試與 `__pycache__`）搜尋 `eval(`、`exec(`、`shell=True`、`pickle`、`yaml.load`、`verify=False`、裸 `except`、`subprocess`。命中後讀呼叫點與閘門，不抄任何祕密值。

以 `git ls-files` 對檔名核對 token、credential、auth、`bot_config.json`、`presence_*.json`。只確認追蹤與否與範本是不是佔位，不讀取正式憑證檔，也不引用其內容。另對已存在的追蹤文字檔做形狀掃描，輸出只留路徑、行號與長度，不留值；測試裡名為 `secret` 的字串都是「確認不會外洩」的夾具，不是產品憑證。

### 發現

沒有憑證進版控、沒有把未授權的聊天輸入送進 shell、沒有 `eval`／`exec`／`shell=True`／`pickle`／不安全 yaml、沒有 `verify=False`。下列兩則是讀過程式後確認的，不是推測。

#### CR-01

- 嚴重度：缺陷
- 位置：`axiomatic/discord_bot.py` 約 13534（呼叫）、14198（文字入口）、26409（斜線註冊）、29334–29350（定義在進入點之後）。正式啟動見 `start_discord_bot.py` 約 170–171 行：以腳本路徑執行 `axiomatic/discord_bot.py`。
- 說明：`_render_claude_local_usage` 寫在 `if __name__ == "__main__": sys.exit(main())` 之後。正式行程的模組名是 `__main__`，`sys.exit` 不會返回，這段 `def` 不會執行。擁有者呼叫本機用量指令時，名稱尚未綁定，得到 `NameError`。斜線錯誤處理只回型別名，不把內部字串外送。以 `import` 載入時 `__name__` 不是 `__main__`，函式會被定義，所以匯入式測試看不出這個洞。
- 依據：規範 4（正式進入點綁定不到）與規範 7（測試路徑與正式路徑不一致）。規範 3：失敗被收成一般內部錯誤，不放大，但指令本身在正式行程不可用。
- 狀態：未關閉
- 建議：把這個函式移到 `if __name__` 之前。不要只加一支會 `import` 模組的測試；正式腳本路徑也要能解析到這個名稱。

#### CR-02

- 嚴重度：建議
- 位置：`architecture.md` 第 179 行（§8 結束、檔案收束之後）。
- 說明：檔尾有一段沒有章節、也沒有放進 §4 或 §6 的英文筆記，描述已有的用量與模型設定行為。架構檔自己的約定是短總覽、散文用繁體中文與泛稱。這段不是安全問題，也不推翻 §2 或 §6。
- 依據：規範 10。
- 狀態：未關閉（不升為缺陷）
- 建議：收進既有章節，或若只是暫記就移出本檔。不要另開一份與 §6 平行的邊界說明。

### 總評

熱點裡沒有立即損害。憑證檔沒有被追蹤；範本是佔位字串。聊天面上的任意指令有擁有者閘，而且不是 `shell=True`。唯一未關閉缺陷是用量回覆函式在正式啟動路徑上不存在，影響範圍是擁有者的一則查詢，失敗時關閉，不執行 shell、不外送憑證。

### 已核對

- 版控檔名：追蹤到的只有 `auth.example.md`、`discord_bot_token.example.md`、`telegram_bot_token.example.md`、三份 `presence_*.example.json`，以及 `test/test_credentials.py`。`.gitignore` 排除 `auth.md`、`discord_bot_token.md`、`telegram_bot_token.md`、`bot_config.json`、三份正式 `presence_*.json`。範本是佔位，不是寫死的憑證。既有追蹤文字檔的形狀掃描沒有產品憑證；測試夾具不計。結論：未見寫死憑證。
- `eval`／`exec`：`axiomatic/*.py` 沒有內建 `eval(` 或 `exec(`。`/fun calc` 走 `simpleeval`，有長度與乘冪上限，套件缺失時指令停用（`discord_bot.py` 約 362–377、3943–3964）。`gen_command_docs.py` 用 `ast.literal_eval` 讀自己的指令樹，不是聊天輸入。
- `shell=True`、裸 `except`、`pickle`、`yaml.load`、`verify=False`：上述範圍內沒有。子行程都是 argv 清單。
- 使用者輸入與 shell：`/host sh`、`/host job`、`/watch` 的 `--then`、排程 payload 會進 `axiomatic/_gui_control.py` 的 `shell_argv`（Windows 為 PowerShell `-Command`，其他平台為 `/bin/sh -c`），`subprocess.Popen(..., shell=False)`。閘在群組規則、`on_message`、斜線 `interaction_check`，handler 內再對擁有者比對一次。非擁有者得到泛用拒絕。
- 其他平台：`resolve_identity` 把該平台擁有者映成既有擁有者編號，其餘為負數，對不上擁有者比較（`axiomatic/_chat_platform.py`）。轉發進來、且以 `!` 開頭的文字不執行（`axiomatic/_telegram_transport.py`）。外部訊息仍走既有的 `on_message`／mention 閘（`dispatch_external_message`）。
- 對話後端的完整工具模式可以在主機上跑指令，但是提問入口先比對擁有者（`mcmd_dorossi`）；預設工具模式是關閉。這是設定後的擁有者能力，不是未授權輸入。
- `/proc launch` 只啟動白名單或別名，`shell=False`。對不上就拒絕。
- 儀表板（`axiomatic/dashboard_server.py`）只有讀取用的 GET，沒有子行程。非 loopback 會警告。指令主控台仍列在 `progress.md`，本輪不另開缺陷。
- §6：單一 `webrunner_je_only.py` 入口與桌面函式庫下限寫在架構檔。工作樹沒有 `axiomatic/webrunner_novelai.py`，但索引仍列這個路徑；`progress.md` 已把收成單一變體列為未完成。殘留的「兩個變體」字句不另開缺陷。

### 未涵蓋

未逐函式量複雜度、行數、巢狀與參數數，因此不宣稱全庫符合規範 2。未跑測試、未做冒煙匯入、未啟動任何行程、未連線。未讀正式憑證檔、記錄檔、`state/` 或個人提示詞。未比對另一專案的契約。未審 `test/` 的行為是否為綠。形狀掃描看的是已存在的追蹤文字檔，不是整個 git 歷史。

### 本專案加嚴

安全條從嚴：祕密不進版控、不可信輸入不進 `eval`／`exec`／`shell=True`／`pickle`／不安全 yaml、不關 TLS。本輪熱點符合這幾項。擁有者才能把文字送進指令解譯器，而且解譯器在 argv 裡，不經 `shell=True`。可讀性數字本輪未全量測量。另一專案的契約差異（單一變體尚未收乾淨、儀表板主控台尚未做）維持為已知未完成，不記缺陷。

### 結論

有條件通過。未關閉缺陷 1 則（CR-01），建議 1 則（CR-02），備註 0 則。CR-01 不構成立即損害：正式行程上該用量指令會失敗，不放行 shell，也不外送憑證。沒有需要以「需修正」擋下的立即損害。CR-01 修掉之前，不能改記為通過。
