# Session 5 摘要

日期：2026-07-30

## 完成事項

### 1. 首頁改造成仿 AIHCR 兩欄式版面（v0.001）

把發送表單從單欄改成「左側欄（品牌＋版本＋2 導覽入口）＋右側主內容」的兩欄式版面，逼近使用者提供的 AIHCR 內網平台截圖外觀。純排版改動，`/send`、token、速率上限、Access 檢查、CSP、noindex 一字未動。新增 `/trial-email` 占位路由（當時為「建置中」stub）。

### 2. 發送對象改成下拉選單，讀 producer 名單（Part B consumer）

「手機號碼」欄從自由輸入改成 `<select>` 下拉，資料來自 `recipients.json`（另一支內網 producer 產出）。新增 `web/recipients.py`：`RecipientBook.get()` 只在 id 為 `ok` 且有電話時回傳，其餘一律 `None` —— 伺服器端反查電話、不信任前端送來的號碼。未設定名單檔時完全退回手動輸入（現況不變），確保既有測試不受影響。

### 3. Producer 腳本：爬體驗借出頁 + 比對 users 產名單（Part A）

新增 `tools/build_recipients.py`（獨立內網工具，不受核心零依賴鐵律限制）：Playwright 爬 AIHCR「體驗借出管理」頁（Streamlit，無 HTML `<table>`，解析 innerText）取「體驗中」名單 → 姓名精確比對 `acfh_api.users`（唯讀 SELECT，僅 `status=active`）取 `user_id`/電話 → 原子寫入（temp + `os.replace`）`recipients.json`。純函式 `match_recipients` 三態分類：唯一命中→`ok`、多筆命中→`ambiguous`（不帶電話）、查無→`not_found`（不帶電話）——絕不用猜的配電話。

第一版 writer 誤用 `wait_for_selector("table tbody tr")` 抓真頁，因頁面根本沒有 HTML table 而必然逾時；改用 `innerText` + `○`（U+25CB）列分隔解析後修復，並補上「表頭消失→拋錯」的防呆，避免版面改版時默默產出空名單。

### 4. 整條鏈打通並部署上線

`deploy/mitake-web.service` 新增 `MITAKE_WEB_RECIPIENTS_PATH=/home/claude/mitake-sms-data/recipients.json`。實跑 producer（真頁+真 DB）→ scp 到 VPS → `git pull` + `systemctl restart` → 生產站驗證：8 筆名單、7 可選+1 灰掉、確認頁正確顯示收件人姓名+號碼。

### 5. 品牌與版本微調

分頁標題／h1 從「三竹簡訊發送」改「addwii 簡訊發送」（v0.002）。

### 6. 訊息範本 radio 快選（v0.003）

「簡訊內容」上方加 3 個單選 radio（出貨通知／14天體驗結束通知／濾網更換通知），選一個自動帶入預設內容並即時重算則數/扣點。純前端擴充既有唯一的 nonce `<script>`（`addEventListener`，非 inline handler），`web/server.py` 零改動。

### 7. `/trial-email` 改成體驗借出表格（鏡像 AIHCR）

從「建置中」占位頁改成顯示體驗借出表格（設備／客戶／接機日／天數／已用天／業務／狀態），資料同樣讀 `recipients.json`。Producer 補存 4 個原本讀到但沒輸出的欄位；表格**不顯示電話**（比 AIHCR 原頁更收斂，避免此唯讀頁曝露一整排號碼）。唯讀頁，不寄信、不花錢。

## 關鍵技術筆記

- **VPS 到不了公司內網**：`sms.chenghyang.uk` 跑在 Hostinger VPS（187.127.109.145，公有雲），體驗借出資料在公司內網 AIHCR（192.168.23.186，RFC1918 私有 IP）。兩者用「檔案合約」`recipients.json` 解耦：producer 在內網產檔 + scp 推 VPS，consumer 純讀檔、缺檔自動降級（不 crash）。
- **AIHCR 體驗借出頁是 Streamlit，無 HTML `<table>`**：抓取只能解析 `page.inner_text("body")`，用列分隔字元 `○`（U+25CB）切段，不能用 DOM `<table>`/`<td>` 選擇器（會抓到 0 筆或永遠逾時）。
- **姓名比對的殘留風險（reviewer 提出，實測未觸發）**：AIHCR 頁只有姓名、無 user_id，只能靠 `first_name`+`last_name` 精確比對 `acfh_api.users`。可偵測的同名碰撞（≥2 個 active 同名）已正確標成 `ambiguous`；但若某體驗客戶本人不在 users、卻恰好有另一位 active 用戶同名同姓，程式會誤配對方電話——這是資料層限制，無法從現有資料偵測。今日實測 7 筆 ok 全數正確（id↔phone 逐筆比對相符）。
- **acfh_api 是生產 MySQL（唯讀鐵律）**：帳密 `rd2`/`microjetrd2`，host `acfh-db.cx1c1xdf6koi.ap-northeast-1.rds.amazonaws.com`，與舊 `zap_api` 同帳密不同 host。全程只執行 `SELECT`。
- **Producer 尚未排程**：目前每次都是手動在本機（同時連得到內網+DB 的那台）執行。名單是靜態快照，「已用天」「狀態」不會自動更新，需下次挑一台常開的 LAN 機器設 cron/schtasks。
- 全程走 `code-writer → code-qa → code-reviewer` 三 agent 鐵律；QA 與 reviewer 在多個環節做了**真實整合測試**（真頁 Playwright + 真 DB SELECT），不只是靜態推論——這正是這次抓到「wait_for_selector 對真頁必然逾時」這個純單元測試測不到的 bug 的原因。

## 產出檔案

| 檔案 | 說明 |
| --- | --- |
| `web/__init__.py` | 版本常數 0.001→0.003（單一真相來源） |
| `web/templates.py` | 側欄兩欄式版面、發送對象下拉、訊息範本 radio、`/trial-email` 表格渲染 |
| `web/server.py` | `/trial-email` 路由、`handle_trial_email` 傳入 recipient book |
| `web/recipients.py`（新） | `RecipientBook`／`Recipient`／`load_recipients`（防竄改、降級不 crash） |
| `tools/build_recipients.py`（新） | Producer：爬體驗借出頁＋比對 users＋原子寫入 |
| `tools/requirements.txt`（新） | producer 專用依賴（pymysql、playwright），與核心零依賴服務分離 |
| `tools/__init__.py`（新） | 空 package 標記 |
| `tests/test_web.py` | +9 測試（側欄／下拉／範本／trial-email 表格） |
| `tests/test_build_recipients.py`（新） | 3 測試（match_recipients ok/ambiguous/not_found） |
| `deploy/mitake-web.service` | 新增 `MITAKE_WEB_RECIPIENTS_PATH` env |

Commits：`65d7df3` `e6f573a` `7c6103f` `b289c42` `de49f14` `a7db45b` `523fb03`（全數已 push 並部署至 VPS，生產站逐一驗證通過）。

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] **幫 producer 設排程**：挑一台常開、連得到公司內網 + AWS RDS 的 LAN 機器，用 cron/schtasks 定期跑 `tools/build_recipients.py` + scp 到 VPS，否則「已用天」「狀態」會永遠停在手動執行的那一刻。
- [ ] **Task #1（擱置中）**：把「體驗借出管理」的完整資料轉成使用者指定格式（HTML/Excel/JSON/圖表，尚未問過使用者要哪種）——這是最早排的待辦，一直被更急的任務插隊。
- [ ] 把「同名碰撞殘留風險」正式記進 `doc/architecture.md`（ADR）或 `HANDOFF.md`，供未來維護者知悉這個資料層限制。

### 進行中（需接續）

- 無阻塞中的半成品；本 session 每個功能都走完三 agent 流程並部署驗證，沒有留下 WIP。

### 注意事項

- `recipients.json`（VPS：`/home/claude/mitake-sms-data/recipients.json`）是**手動產生的靜態快照**，目前對應 2026-07-30 13:54 那次爬取，之後再手動跑過一次含新 4 欄的版本。下次若名單看起來過期，先確認 producer 有沒有重跑。
- 三竹帳密與 acfh_api 帳密都出現在本 session 對話中（使用者主動貼上），皆未寫入任何程式碼或 commit——一律走環境變數傳遞，用完即棄。
- `/trial-email` 是唯讀頁，不寄信、不花任何點數，即使頁面看起來像「功能」也不要誤以為它會產生副作用。
