# Session 7 摘要：/trial-email「寄送體驗報告」功能（設計→實作→上線）

**日期**：2026-07-31

## 完成事項

### 1. 「寄送體驗報告」按鈕 UI（純外觀 placeholder）

- `/trial-email` 表格最後新增一欄「寄送體驗報告」，每列一顆按鈕，依「已用天數 ≥ 天數」決定灰階/可點
- 三 agent 鐵律（code-writer → code-qa → code-reviewer）完整跑過一輪，APPROVED
- 部署到 VPS 驗證通過（commit `cbcc8f4`）

### 2. 需求釐清與技術可行性驗證

- 使用者提供既有「AIHCR 24 小時空氣品質日報」Email 範例（`aihcr-daily` repo 產出），要求做出 14 天版本
- 派 Explore agent 完整研究 `aihcr-daily/scripts/acfh_daily_report_14_days_experiencing.py`（1750+ 行），確認：查詢/統計/畫圖/PDF/寄信邏輯全部參數化（`hours` 可調），可改寫重用
- **在 VPS 上做過一次唯讀乾跑測試**（不寫入、未寄信）：確認 `mitake-web` 所在的 VPS **可以直接連到 `acfh_api` MySQL**（AWS RDS，IP 白名單含這台 VPS）——這推翻了 `CLAUDE.md` 原本「VPS 到不了內網」的敘述（那句話其實只適用於 AIHCR Streamlit 頁面本身），已在文件中更正
- 確認 `Recipient.id`（如 `"u46"`）就是 `acfh_api.users.id` 去掉前綴 `u`，可直接反查客戶 email/裝置
- 確認真實資料裡存在「一位客戶同時借多台裝置」的情況（`user_id=43`「青蘋果」對到 2 筆試用紀錄）→ 定為 MVP 不支援，明確擋下不猜

### 3. 規格文件

- 寫 `doc/spec-trial-report.md`：目標、核心邏輯 9 步、收件人設定、已知限制、依賴與部署變更、待更新文件清單

### 4. 完整功能實作（三 agent 鐵律，複雜度=複雜）

- **新增 `web/trial_report.py`**（1064 行）：改寫自 `aihcr-daily` 的 DB 查詢/規則式健康分析/PDF 產生/Gmail 寄送邏輯，`hours=24` → `hours=336`（14 天）。`conn_factory`/`email_sender` 全可注入，測試零機會打到真實 DB/SMTP
- **`web/recipients.py`**：新增 `parse_acfh_user_id()` / `Recipient.acfh_user_id`
- **`web/server.py`**：新路由 `POST /trial-email/send-report`，伺服器端**獨立重算**「已用天數 ≥ 天數」（不信任前端 disabled 狀態，與按鈕共用同一份 `_parse_trial_day_count`）
- **`web/templates.py`**：按鈕改成真的 `<form method="post">` 表單
- **`requirements.txt`**：新增 `pymysql`/`matplotlib`——`web/` 唯一破例引入第三方依賴的模組，兩者皆延遲 import，其餘子模組不受影響
- 兩輪打回修正才過關：
  1. code-qa 抓到 `mask_email()` 星號數量與 docstring/測試矛盾 → 改成固定 3 星（避免遮罩長度洩漏帳號長度資訊）
  2. code-reviewer 抓到 MUST_FIX：`_send_real_gmail()` 忽略 `smtp.sendmail()` 的拒收回傳值，客戶被拒收時會誤報成功 → 補上檢查，客戶被拒收時正確回報失敗，Bcc 被拒收僅記 log 不影響客戶端結果
- 最終 289 個測試全過（含新增 32 個），commit `626dd0b`

### 5. 文件更新

- `CLAUDE.md`：更正 VPS 網路可達性敘述、補充 `web/trial_report.py` 架構說明
- `doc/architecture.md`：新增 ADR #13（為何破例引入 pymysql/matplotlib）
- `HANDOFF.md`：新增 §6.6，記錄新環境變數、與 `aihcr-daily` 共用憑證但各自存檔的重要提醒

### 6. 版本號與 VPS 部署

- `web/__init__.py`：`0.003` → `0.004`，發布日期 `2026-07-31`（commit `a12c7a7`）
- `deploy/mitake-web.service`：改用獨立 venv（PEP 668 externally-managed，系統 Python 裝不了新依賴）、新增 `MITAKE_WEB_TRIAL_AUDIT_PATH`（commit `c6c90bb`）
- **實際部署到 VPS**：
  - 建 `/home/claude/mitake-sms-venv`，裝 `pymysql 2.2.8`/`matplotlib 3.11.1`
  - `/etc/mitake-sms.env` 補齊 `MYSQL_RD2_PASSWORD`、`MITAKE_WEB_STAFF_BCC`（4 個固定 Bcc），並**修正**了原本就存在但值錯誤的 `GMAIL_APP_PASSWORD`（雜湊比對後改成與 `aihcr-daily` 一致的正確值）
  - 意外把 env 檔權限從 `root:root 600` 改成 `root:claude 640`，發現後立即改回（systemd `EnvironmentFile=` 由 root 讀取，service process 不需要直接檔案存取權限）
  - 重啟服務、`/health` 200
  - 安全驗證：`POST /trial-email/send-report` 打未知 recipient → 400（未觸資料庫）；打真實客戶「陳筱琪」（1/14 天，未達標）→ 伺服器端正確擋下、稽核記錄寫入且無 PII 洩漏、**全程未連資料庫、未寄出任何信**

## 關鍵技術筆記

- **VPS 網路可達性認知需更新**：`acfh_api` RDS（`acfh-db.cx1c1xdf6koi.ap-northeast-1.rds.amazonaws.com`）對 VPS（`187.127.109.145`）IP 已白名單放行，`aihcr-daily` 的每日報告 cron 就是直接在這台 VPS 上連的
- **`aihcr-daily` 與 `mitake-sms` 共用同一組 Gmail 帳號**（`GMAIL_USER`/`GMAIL_APP_PASSWORD`），但憑證**各自存一份**在不同的 env 檔（`~/.env_vars` vs `/etc/mitake-sms.env`），輪替密碼時兩邊都要改，這次剛好抓到兩邊不一致的舊值
- `Recipient.id` 的 `u<n>` 前綴編碼了 acfh `users.id`，是這次功能最關鍵的「白撿」設計巧合
- `systemd` 的 `ProtectHome=read-only` 讓服務對整個 `/home`（含 repo 自己）唯讀，任何新的可寫檔案路徑都要明確指到 `LogsDirectory=` 建出的目錄，這次的 `trial-report-audit.jsonl` 踩過一次（部署前就在 code review 階段想到，寫進了 systemd unit）

## 產出檔案

| 檔案 | 類型 | 說明 |
| --- | --- | --- |
| `web/trial_report.py` | 新增 | 核心邏輯：DB 查詢、統計分析、PDF、Email |
| `web/recipients.py` | 修改 | `parse_acfh_user_id()` / `acfh_user_id` |
| `web/server.py` | 修改 | 新路由、依賴注入、環境變數 |
| `web/templates.py` | 修改 | 按鈕改真表單 |
| `web/__init__.py` | 修改 | 版本號/日期 |
| `requirements.txt` | 修改 | 新增 pymysql/matplotlib |
| `deploy/mitake-web.service` | 修改 | venv 化、新環境變數 |
| `tests/test_trial_report.py` | 新增 | 32 個測試 |
| `tests/test_web.py` | 修改 | 新增 9 個路由測試 |
| `doc/spec-trial-report.md` | 新增 | 完整規格 |
| `CLAUDE.md` | 修改 | 架構說明、更正網路可達性敘述 |
| `doc/architecture.md` | 修改 | ADR #13 |
| `HANDOFF.md` | 修改 | §6.6 部署備忘 |

**Commits**：`cbcc8f4`（UI placeholder）→ `626dd0b`（完整功能）→ `a12c7a7`（版本號）→ `c6c90bb`（部署設定）

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] 目前只驗證過「失敗路徑」安全無虞（未觸資料庫/未寄信），**還沒有一次真正成功寄出的端對端驗證**。等有客戶真的用滿 14 天，第一次真實點擊後要回頭確認 email 真的收得到、PDF 附件正常
- [ ] 考慮補 reviewer 提過的 nice-to-have：`/trial-email/send-report` 加輕量節流（同 recipient_id 短時間內只接受一次），因為這個 Gmail 帳號跟 `aihcr-daily` 排程共用，連點有極小機率觸發 Google 帳號異常登入防護、連帶讓兩邊都寄不出信
- [ ] 若之後要支援多裝置客戶（如「青蘋果」），需要先設計「trial-loan 設備欄位」對「acfh devices.alias」的比對邏輯，目前刻意跳過

### 進行中（需接續）

- 無——本次功能規格→實作→部署→驗證已完整跑完一輪，沒有半成品

### 注意事項

- `MYSQL_RD2_PASSWORD`/`GMAIL_USER`/`GMAIL_APP_PASSWORD` 這三個憑證同時存在於 `aihcr-daily`（`~/.env_vars`）與 `mitake-sms`（`/etc/mitake-sms.env`）兩份**各自獨立**的檔案，未來密碼輪替務必兩邊都改，否則會悄悄用舊密碼失敗
- `mitake-web` 現在跑在 `/home/claude/mitake-sms-venv`（不是系統 Python），未來若要再加新依賴記得裝進這個 venv
- `/trial-email/send-report` 的稽核檔在 `/var/log/mitake-sms/trial-report-audit.jsonl`，不在 repo 的 `logs/` 底下
- 多裝置客戶（同一 acfh user_id 借 ≥2 台裝置）目前一律擋下不寄，這是刻意的 MVP 限制，不是 bug
