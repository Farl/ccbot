# Slack App 設定與測試清單

## Slack App 設定

### 1. 建立 App

1. 前往 [api.slack.com/apps](https://api.slack.com/apps) > Create New App
2. 選擇 "From an app manifest" 或 "From scratch"

### 2. Socket Mode

1. Settings > Socket Mode > 啟用 Socket Mode
2. 產生一個 App-Level Token，scope 選 `connections:write` > 複製為 `SLACK_APP_TOKEN`（格式：`xapp-...`）

### 3. OAuth & Permissions

在 OAuth & Permissions > Scopes 加入以下 Bot Token Scopes：

| Scope | 用途 |
|-------|------|
| `chat:write` | 發送訊息 |
| `im:history` | 讀取 DM 訊息（含 conversations.replies） |
| `im:read` | 讀取 DM 頻道資訊 |
| `im:write` | 開啟 DM 頻道 |
| `files:read` | 下載使用者上傳的檔案（圖片、音訊） |
| `files:write` | 上傳截圖 |
| `assistant:write` | Assistants 框架（thread 標題、建議提示、狀態） |

安裝到 workspace > 複製 Bot User OAuth Token 為 `SLACK_BOT_TOKEN`（格式：`xoxb-...`）

### 4. Event Subscriptions

在 Event Subscriptions > Subscribe to bot events 訂閱：

| Event | 用途 |
|-------|------|
| `message.im` | 接收 DM 訊息 |

備註：`file_share` 事件會作為 `message.im` 的 subtype 送達，不需額外訂閱。

### 5. Agents & Assistants

1. Features > Agents & Assistants > 啟用
2. 這會啟用 Slack DM 中的 assistant thread UI

### 6. Slash Commands

在 Features > Slash Commands 註冊以下指令：

| 指令 | 簡短描述 | Usage Hint |
|------|----------|------------|
| `/esc` | Send Escape to interrupt Claude | |
| `/unbind` | Unbind session from this thread | |
| `/screenshot` | Capture terminal screenshot | |
| `/history` | Show session message history | `[page]` |

### 7. 重新安裝 App

每次修改 scopes、新增 slash commands、或啟用 Agents & Assistants 後，都必須重新安裝：

OAuth & Permissions > **Reinstall to Workspace** > 點擊授權

### 8. 環境變數

```ini
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
ALLOWED_USERS=U090KUZEKRQ
```

找你的 Slack user ID：點擊自己的頭像 > "..." > Copy member ID。

---

## 測試清單

### 前置條件

- [ ] Bot 已啟動：`uv run ccbot --transport slack`
- [ ] 終端機日誌顯示 `Slack bot is running!` 和 `Session monitor started`
- [ ] Slack workspace 中能看到 bot（在 DM 列表或 Apps 裡）

---

### A. Thread 與 Session 生命週期

- [ ] **A1. 開啟新 thread**
  - 操作：在 Slack 中找到 bot 的 DM，發送任意訊息（例如 "hello"）
  - 預期反應：Bot 回覆一條訊息 "Send a message to start a Claude Code session"，下方出現 3 個建議提示按鈕（suggested prompts）

- [ ] **A2. 目錄瀏覽器啟動**
  - 操作：在同一個 thread 中再發送一條訊息（例如 "test"）
  - 預期反應：出現 Block Kit 目錄瀏覽器，顯示目前目錄的子資料夾按鈕、「..」返回上層按鈕、底部有 Prev/Next 分頁按鈕和 Select/Cancel 按鈕

- [ ] **A3. 瀏覽目錄導航**
  - 操作：點擊某個資料夾按鈕進入子目錄，再點「..」返回上層，再點 Next/Prev 翻頁
  - 預期反應：每次點擊後，瀏覽器訊息原地更新（不是發新訊息），顯示對應目錄的內容

- [ ] **A4. 建立新 session（無既有 session）**
  - 操作：導航到一個沒有 Claude session 的專案目錄（例如 `~/Documents/some-new-project`），點擊 Select
  - 預期反應：瀏覽器訊息被替換為 "Session created: some-new-project"，tmux 中出現一個新 window 正在啟動 Claude Code

- [ ] **A5. Thread 標題設定**
  - 操作：接續 A4，觀察 thread 標題（thread 頂部顯示的文字）
  - 預期反應：Thread 標題變為 `~/Documents/some-new-project`（使用 `~` 縮寫 home 目錄）

- [ ] **A6. Session 選擇器（有既有 session）**
  - 操作：開一個新 thread，導航到一個已經有 Claude session 的目錄（例如剛才 A4 用過的目錄），點擊 Select
  - 預期反應：不會直接建立 session，而是出現 session 選擇器，列出既有 session（顯示 session ID 前 8 碼 + 路徑 + 最後修改時間），底部有 "New session" 和 "Cancel" 按鈕

- [ ] **A7. 恢復既有 session**
  - 操作：在 A6 的 session 選擇器中，點擊某個既有 session 按鈕
  - 預期反應：訊息更新為 "Resumed session xxxxxxxx"，thread 標題設定為對應路徑，tmux 中 Claude Code 以 `--resume` 模式啟動

- [ ] **A8. 從選擇器建立新 session**
  - 操作：在 session 選擇器中，點擊 "New session" 按鈕
  - 預期反應：訊息更新為 "New session: <路徑>"，thread 標題設定為對應路徑，tmux 中出現新 window

---

### B. 訊息流

- [ ] **B1. 發送文字給 Claude**
  - 操作：在已綁定 session 的 thread 中輸入 "what files are in this directory?"
  - 預期反應：訊息透過 tmux 轉發給 Claude Code，Claude 的回覆出現在 thread 中（可能需要幾秒）

- [ ] **B2. Claude 自動重啟**
  - 操作：在 tmux 中手動退出 Claude Code（按 Ctrl+C 或輸入 `/exit`），然後在 thread 中發送一條訊息
  - 預期反應：Bot 偵測到 Claude 已退出，自動重啟 Claude Code，訊息在重啟後被轉發

- [ ] **B3. 狀態輪詢**
  - 操作：在 thread 中發送一個需要 Claude 執行工具的提示（例如 "list all Python files"）
  - 預期反應：在 Claude 工作期間，thread 中出現狀態訊息，顯示 Claude 目前的活動（如 spinner 文字 "Reading file..."）

- [ ] **B4. 狀態訊息轉為內容訊息**
  - 操作：接續 B3，等待 Claude 產生輸出
  - 預期反應：原本的狀態訊息被編輯為第一條內容訊息（不是另發新訊息），後續內容才作為新訊息發送

- [ ] **B5. 訊息合併**
  - 操作：發送一個會觸發多段快速回覆的提示（例如 "explain this codebase briefly"）
  - 預期反應：連續的短訊息被合併為一條訊息，而不是每段各發一條

- [ ] **B6. tool_use / tool_result 配對**
  - 操作：發送會觸發工具呼叫的提示（例如 "read the README.md file"）
  - 預期反應：先出現一條 tool_use 訊息（顯示工具名稱和參數），然後這條訊息被原地編輯，追加 tool_result 內容

- [ ] **B7. 長訊息分割**
  - 操作：發送會產生很長回覆的提示（例如 "explain every function in this file in detail"）
  - 預期反應：超過 3000 字元的回覆被分割為多條訊息，每條帶有 `[1/N]`、`[2/N]` 等後綴

---

### C. 互動式 UI

- [ ] **C1. AskUserQuestion 提示**
  - 操作：觸發 Claude 問問題（例如讓 Claude 進入 plan mode 後問確認）
  - 預期反應：出現 Block Kit 互動訊息，顯示問題內容（code block 格式），下方有導航按鈕（Space、Up/Down、Tab、Left/Right、Esc、Refresh、Enter）

- [ ] **C2. Permission 權限提示**
  - 操作：發送會觸發需要權限的工具呼叫的提示（例如 "edit this file"，且 Claude 未設為 auto-approve）
  - 預期反應：出現互動式 UI 顯示權限請求內容，可透過導航按鈕選擇 approve 或 deny

- [ ] **C3. 導航按鈕操作**
  - 操作：在 C1 或 C2 的互動 UI 中，點擊各個按鈕（Up、Down、Enter、Esc 等）
  - 預期反應：每個按鈕對應的按鍵被發送到 tmux window，UI 訊息原地更新為最新的終端機內容

---

### D. 指令

#### 文字指令（以 `!` 開頭）

- [ ] **D1. `!esc` 中斷 Claude**
  - 操作：在 Claude 正在工作時，在 thread 中輸入 `!esc`
  - 預期反應：Bot 回覆 "↩ Escape sent."，Claude Code 收到 Escape 鍵被中斷

- [ ] **D2. `!unbind` 解除綁定**
  - 操作：在已綁定 session 的 thread 中輸入 `!unbind`
  - 預期反應：Bot 回覆 "Session unbound."，對應的 tmux window 被終止。之後在同一 thread 發送訊息會重新出現目錄瀏覽器

- [ ] **D3. `!screenshot` 終端截圖**
  - 操作：在已綁定 session 的 thread 中輸入 `!screenshot`
  - 預期反應：Bot 上傳一張 PNG 終端截圖到 thread 中（帶有 ANSI 顏色），截圖下方出現導航按鈕（Space、Up/Down、Tab、Left/Right、Esc、Refresh、Enter）

- [ ] **D4. `!screenshot` 導航按鈕互動**
  - 操作：在 D3 的截圖下方，點擊方向鍵按鈕（如 Up、Down），再點擊 Refresh
  - 預期反應：按鍵被發送到 tmux window。點擊任何按鈕後，舊的截圖和舊的導航按鈕被刪除，上傳新的截圖和新的導航按鈕（替換效果）

- [ ] **D5. `!history` 歷史記錄**
  - 操作：在已綁定 session 且有對話記錄的 thread 中輸入 `!history`
  - 預期反應：顯示最後一頁的 session 歷史記錄（分頁格式），底部有 Prev/Next 翻頁按鈕

- [ ] **D6. `!history 1` 指定頁碼**
  - 操作：輸入 `!history 1`
  - 預期反應：顯示第 1 頁的歷史記錄

- [ ] **D7. 歷史記錄翻頁**
  - 操作：在 D5 的歷史記錄中，點擊 Prev 或 Next 按鈕
  - 預期反應：訊息原地更新為對應頁碼的內容

#### Slash 指令

- [ ] **D8. `/esc`**
  - 操作：在已綁定 session 的 thread 中輸入 `/esc`
  - 預期反應：與 `!esc` 相同（"↩ Escape sent."）

- [ ] **D9. `/unbind`**
  - 操作：在已綁定 session 的 thread 中輸入 `/unbind`
  - 預期反應：與 `!unbind` 相同（"Session unbound."）

- [ ] **D10. `/screenshot`**
  - 操作：在已綁定 session 的 thread 中輸入 `/screenshot`
  - 預期反應：與 `!screenshot` 相同（截圖 + 導航按鈕）

- [ ] **D11. `/history`**
  - 操作：在已綁定 session 的 thread 中輸入 `/history`
  - 預期反應：與 `!history` 相同（分頁歷史記錄）

#### 邊界情況

- [ ] **D12. 未綁定 thread 中使用指令**
  - 操作：在還沒有綁定 session 的 thread 中輸入 `!esc`（或任何指令）
  - 預期反應：Bot 回覆 "No active session in this thread."

---

### E. 檔案處理

- [ ] **E1. 傳送圖片**
  - 操作：在已綁定 session 的 thread 中上傳一張圖片（PNG/JPG）
  - 預期反應：圖片被下載並儲存到 `~/.ccbot/images/`，路徑被轉發給 Claude（Claude 可以看到圖片內容）

- [ ] **E2. 圖片附帶文字**
  - 操作：上傳圖片時同時輸入文字說明（例如 "這個 UI 有什麼問題？"）
  - 預期反應：文字說明和圖片路徑一起被轉發給 Claude

- [ ] **E3. 傳送音訊/語音**
  - 操作：在已綁定 session 的 thread 中上傳一段音訊檔案
  - 預期反應：音訊透過 OpenAI API 轉錄為文字，轉錄結果作為普通文字訊息轉發給 Claude

- [ ] **E4. 未綁定 thread 中傳送檔案**
  - 操作：在還沒有綁定 session 的 thread 中上傳一個檔案（附帶文字說明）
  - 預期反應：如果有文字說明，觸發目錄瀏覽器；如果只有檔案沒有文字，則被忽略

---

### F. 錯誤處理與邊界情況

- [ ] **F1. tmux window 被外部終止**
  - 操作：在 tmux 中手動關閉一個已綁定的 window（`tmux kill-window -t <window_id>`）
  - 預期反應：下次在對應 thread 發送訊息時，Bot 偵測到 window 不存在，自動解除綁定並顯示目錄瀏覽器

- [ ] **F2. Bot 重啟不重播訊息**
  - 操作：在有活躍 session 的情況下重啟 bot（Ctrl+C 然後重新 `uv run ccbot --transport slack`）
  - 預期反應：重啟後不會重播已經送過的訊息（read offset 追蹤正常運作），新訊息正常送達

- [ ] **F3. 取消目錄瀏覽器**
  - 操作：在目錄瀏覽器出現後，點擊 Cancel 按鈕
  - 預期反應：瀏覽器訊息被替換為 "Cancelled."

- [ ] **F4. 取消 session 選擇器**
  - 操作：在 session 選擇器出現後，點擊 Cancel 按鈕
  - 預期反應：選擇器訊息被替換為 "Cancelled."

- [ ] **F5. 多個 thread 同時使用**
  - 操作：開啟 2 個以上的 thread，各自綁定不同的 session，同時在兩個 thread 中發送訊息
  - 預期反應：訊息各自路由到正確的 session，回覆出現在對應的 thread 中，互不干擾
