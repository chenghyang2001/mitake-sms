# 規格：/trial-email「寄送體驗報告」功能

狀態：**規格草案，待使用者確認後才進入實作（三 agent 鐵律流程）**。
本檔只是規格文件（不是程式碼），依專案慣例可由主 Claude 直接寫，不受
`writer-qa-iron-rule` 限制。

## 背景

`/trial-email` 頁（`web/templates.py` 的 `render_trial_email()`）目前已經有一顆
「寄送體驗報告」按鈕（純外觀 placeholder，commit `cbcc8f4`），依「已用天數 ≥ 天數」
決定是否可點。這份規格定義按下去之後要做的事。

## 目標

按下「寄送體驗報告」→ 伺服器端重新驗證資格 → 撈該客戶最近 14 天的
PM2.5/CO2/VOC/溫濕度 telemetry → 沿用 `aihcr-daily` repo 既有的統計/畫圖/PDF 邏輯
（`scripts/acfh_daily_report_14_days_experiencing.py`，把 `hours=24` 換成
`hours=336`）→ Gmail 寄一封附 PDF 的報告信給客戶本人，並密件副本給固定的內部信箱清單。

## 已驗證的技術前提（2026-07-31 乾跑測試，唯讀、未寄信）

- mitake-web 所在的 VPS（187.127.109.145）**可以直接連到 `acfh_api` MySQL**
  （AWS RDS `acfh-db.cx1c1xdf6koi.ap-northeast-1.rds.amazonaws.com`）。這點與
  `CLAUDE.md` 原本「VPS 到不了內網」的敘述不一致——那句話描述的是 AIHCR
  體驗借出 Streamlit 頁面本身連不到，不代表這個 RDS 連不到。**待實作時同步更新
  `CLAUDE.md`，避免下一個 session 被舊敘述誤導。**
- `web/recipients.py` 的 `Recipient.id`（例如 `"u46"`）就是 `acfh_api.users.id`
  去掉前綴 `u`。實測 `user_id=46`（陳筱琪）：email 查得到、1 台裝置、14 天內
  telemetry 3743 筆。
- 實測也證實「一位客戶同時借多台裝置」是真實存在的情況（`user_id=43` 青蘋果
  同時對到「一樓教室」「二樓教室」兩筆試用紀錄），所以下面明確訂為 MVP 不支援。

## 核心邏輯

新路由：`POST /trial-email/send-report`（body 帶 recipient id）。

1. 用 recipient id 反查 `RecipientBook`；查無此人或 `match_status != "ok"` →
   400，不執行、不查資料庫。
2. **伺服器端自己重算**「已用天數 ≥ 天數」。已用天數改用 `_compute_used_days`
   （今天－接機日 `borrow_date` 動態算，`today` 可注入固定日期供測試使用），
   不再信任 producer 快照的 `used_days` 字串——那份快照只在 producer 重新跑
   一次時才會更新，兩次同步之間會凍結在舊數字（2026-08-03 修正）。天數仍用
   `_parse_trial_day_count` 讀 producer 快照的 `days` 欄（那是活動設定值，不是
   日期能推出來的）。未達標 → 400，不執行。**不信任前端按鈕的 disabled 狀態**
   （devtools 可繞過，這點在上一輪 code review 就已提醒）。
3. 用 recipient id 去掉 `u` 前綴得到 acfh `user_id`，查
   `fetch_user_devices(conn, user_id)`：
   - 裝置數 = 0 → 400「查無裝置」
   - 裝置數 ≥ 2 → 400「多裝置客戶暫不支援寄送，請手動處理」（MVP 明確不猜、不亂寄）
   - 裝置數 = 1 → 繼續下一步
4. 查 `fetch_user_info(conn, user_id)` 拿 email；email 為空 → 400，不寄殘缺信。
5. 查 `fetch_telemetry(conn, device_id, hours=336)`；筆數為 0 → 400「14 天內無資料」。
6. 呼叫（從 `aihcr-daily` 複製過來的）`analyze(rows, hours=336)` +
   `build_pdf(...)` 產生報告文字與 PDF。PDF 產不出來（CJK 字型偵測失敗）時，
   沿用 `aihcr-daily` 既有降級行為：改寄純文字信、不附 PDF，不整個失敗。
7. `send_gmail()`：
   - To：客戶本人 email（步驟 4 查到的）
   - Bcc：固定四個內部信箱（見下方「收件人設定」），**不寫進 `Bcc` header**，
     只放進 `smtp.sendmail()` 信封收件人（沿用 `aihcr-daily` 既有寫法，避免
     "好心" 補上 Bcc header 讓所有人互相看到彼此信箱）
8. 寫一筆稽核記錄（時間、recipient id、user_id、成功/失敗、失敗原因），格式仿
   `mitake.py` 的 `send-audit.jsonl`。**用途是事後追蹤，不是拿來擋重複寄送**
   （見下方「已知限制」）。
9. 回傳結果頁：成功顯示「已寄送給 xxx」；失敗顯示明確原因，不含資料庫連線字串
   等內部細節（沿用本專案 500 錯誤不洩漏內部資訊的慣例）。

## 收件人設定

- To：客戶本人 email（`acfh_api.users.email`，DB 查到什麼就是什麼，不做二次驗證/白名單，因為是公司自己資料庫的既有客戶資料）。
- Bcc（固定四個，不跟表格的「業務」欄位做名字對照——已改用固定清單簡化）：
  - <chenghyang2001@gmail.com>
  - <peter_yang@addwii.com>
  - <kerwin_ma@addwii.com>
  - <nicole_zeng@addwii.com>
- 這份清單**不寫進程式碼、不進 git**（公開 repo，避免把公司信箱清單攤在
  GitHub 上）。做法比照 `MITAKE_WEB_RECIPIENTS_PATH` 的模式：新增環境變數
  `MITAKE_WEB_STAFF_BCC`（逗號分隔的 email 清單），本機 `.env` 與 VPS
  `/etc/mitake-sms.env`（mode 600）各自設一份，`.env.example` 只放示意值。

## 已知限制（MVP 刻意不做，之後有需要再補）

- **不支援多裝置客戶**：擋下並提示「請手動處理」，不猜測要寄哪一台的資料。
- **不防重複寄送**：同一筆試用紀錄短時間內被重複點擊會重複寄信，交給操作者
  自行判斷（稽核記錄只用來事後追蹤，不用來擋按鈕）。
- **不做寄送頻率限制 / 沒有花費點數的顧慮**（這是 Email，不經過 `mitake.py`
  的三竹 SMS 通道，不影響共用點數池）。

## 依賴與部署變更

- 新增第三方依賴：`pymysql`（DB）、`matplotlib`（PDF 折線圖）。這打破
  `web/` 目前「零第三方依賴」的架構原則，是刻意的取捨（沿用
  `aihcr-daily` 已經驗證過的做法），需要記錄進 `doc/architecture.md` 的
  ADR 表。
- VPS 上 `mitake-web` 需要改用 venv 執行（比照 `acfh-report/.venv`
  的做法，因為 VPS 是 PEP 668 externally-managed 環境），`deploy/mitake-web.service`
  的 `ExecStart` 要跟著改成 venv 內的 python。
- 新增 VPS 環境變數（`/etc/mitake-sms.env`，mode 600，不進 git）：
  - `MYSQL_RD2_PASSWORD`（沿用 `acfh_api` 唯讀帳號 `rd2` 的密碼，跟
    `aihcr-daily` 用同一組帳密，但**各自存一份**在自己的 env 檔，不共用同一個檔案，
    保持兩個 repo 互不依賴）
  - `GMAIL_USER` / `GMAIL_APP_PASSWORD`（沿用 `aihcr-daily` 現有帳號，已於
    上一輪確認）
  - `MITAKE_WEB_STAFF_BCC`（見上方「收件人設定」）
- `web/recipients.py` 需要一個新的 helper：從 `Recipient.id` 解析出 acfh
  `user_id`（去掉 `u` 前綴），供新路由使用。

## 待更新文件（實作時一併處理）

- `CLAUDE.md`：更正「VPS 到不了內網」的敘述（見上方「已驗證的技術前提」）、
  補充新路由與新依賴到架構圖。
- `doc/architecture.md`：新增一筆 ADR，記錄「為何 `/trial-email` 這個功能
  破例引入 pymysql/matplotlib，其餘 `web/` 仍維持零依賴」的取捨理由。
- `HANDOFF.md`：補充 `MYSQL_RD2_PASSWORD` / `GMAIL_USER` / `GMAIL_APP_PASSWORD` /
  `MITAKE_WEB_STAFF_BCC` 這幾個新環境變數的說明。
