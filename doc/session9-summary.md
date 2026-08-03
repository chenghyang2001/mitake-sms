# Session 9 摘要：修復體驗借出「已用天」凍結問題（根因調查→三 agent 鐵律實作→時區修正→VPS 上線驗證）

**日期**：2026-08-03

## 完成事項

### 1. 使用者回報問題與根因調查

- 使用者貼出 `/trial-email` 頁截圖：黃燕虹接機日 2026-07-20、天數 14 天，畫面顯示已用天卻只有 10 天——但今天是 2026-08-03，14 天早該到期
- 追程式碼確認根因：「已用天」不是動態算的，是 producer（`tools/build_recipients.py`）從 AIHCR Streamlit 頁面 innerText 原樣抓下來、寫進 `recipients.json` 的快照字串（`Recipient.used_days`），只在 producer 重新跑一次時才會更新——VPS 上這份快照卡在 2026-07-30 沒再同步過
- 發現這不只是顯示問題：`web/trial_report.py::send_trial_report()` 的伺服器端寄送資格驗證也直接信任同一個 stale 字串，代表只要 producer 沒重跑，「寄送體驗報告」按鈕即使畫面上已達標也可能永遠解鎖不了——這可能就是 `trial-report-feature-live.md` 記錄的「功能上線但尚無成功寄送」的部分原因

### 2. 修法規劃並經使用者確認

- 把「已用天數」的唯一事實來源從「producer 快照字串」換成「今天－接機日（`borrow_date`，穩定的 YYYY-MM-DD）動態計算」，畫面顯示與伺服器端驗證兩處共用同一函式，兩者都新增可注入的 `today` 參數供測試脫離真實 wall-clock
- 用 `AskUserQuestion` 與使用者確認複雜度分級（中等、3 個 QA test case）與是否派 code-reviewer（要派，因為牽涉會真的寄信的路徑）

### 3. 三 agent 鐵律實作（兩輪，第一輪抓到真時區 bug）

- **第一輪**：code-writer 新增 `web/templates.py::_compute_used_days(borrow_date, *, today=None)`，`render_trial_email`／`send_trial_report` 皆改用它；修正既有 9 處依賴 `used_days=` 的測試改用 `borrow_date`＋顯式 `today` 注入；新增 3 個 QA 案例；同步更新 `CLAUDE.md`／`doc/spec-trial-report.md`。code-qa 5 層驗證全 PASS（368 測試全過）
- **code-reviewer 第一輪 `CHANGES_REQUESTED`**：MUST_FIX——`_compute_used_days` 未傳 `today` 時的 fallback 用 `datetime.now(timezone.utc).astimezone().date()`（系統本地時區），但 VPS 系統時區是 UTC（`HANDOFF.md` 已有記錄），`borrow_date` 語意上是台灣曆日，兩者比對會在台灣時間 00:00–08:00 這 8 小時窗口內少算 1 天——等於用同樣性質的問題換了個更隱蔽的形式重新引入舊 bug
- **第二輪修正**：改用 `datetime.now(ZoneInfo("Asia/Taipei")).date()`（stdlib `zoneinfo`，不隨系統時區漂移）。連帶發現 Windows 沒有內建 IANA 時區資料庫，缺 `tzdata` 套件會讓 `ZoneInfo("Asia/Taipei")` 直接拋 `ZoneInfoNotFoundError`（整頁 500，不只是寄送子功能壞掉），因此把 `tzdata` 加進 `requirements.txt` 作為正式依賴（模組頂層 import，非延遲 import），並在 `CLAUDE.md` 誠實記錄這打破了 `web/templates.py` 原本零外部依賴的定位
- code-qa 複驗：**實測**（真的 `pip uninstall tzdata` 再重裝）證實這個依賴確有必要，非過度防禦；368 測試全過。code-reviewer 最終覆核：獨立驗證 `ZoneInfo("Asia/Taipei")` 解出 `+08:00` 正確，**APPROVED**（留 1 個不阻擋的 NICE_TO_HAVE：某測試的 `borrow_date` 建構未同步改時區寫法，但因 14 天門檻夠寬不影響正確性）
- 使用者要求順手修這個 NICE_TO_HAVE：另跑一輪簡化版鐵律（複雜度=簡單，QA 2 case、不派 reviewer），修正 `tests/test_web.py` 一處測試改用 `ZoneInfo("Asia/Taipei")` 建構 `borrow_date`，QA PASS（173 個檔案內測試、全套 368 個皆綠）

### 4. Commit、Push 與 VPS 部署

- Commit `1697ad5`（7 檔：`web/templates.py`／`web/trial_report.py`／`tests/test_web.py`／`tests/test_trial_report.py`／`requirements.txt`／`doc/spec-trial-report.md`／`CLAUDE.md`）+ push
- VPS 部署：`git pull`（`ec96d85`→`1697ad5`）→ `mitake-sms-venv/bin/pip install -r requirements.txt`（新裝 `tzdata-2026.3`）→ 確認 venv 內 `ZoneInfo("Asia/Taipei")` 解出 `+08:00` → `systemctl restart mitake-web` 乾淨重啟 → `/health` 200
- 用帶 `Cf-Access-Authenticated-User-Email` 標頭的本機 curl 直接驗證線上頁面：黃燕虹／劉怡君（接機日皆 07-20）已用天顯示 **14 天**（不再是快照的 10 天）、寄送按鈕已解鎖（無 `disabled`）；其餘各列（10/12/12/12/13 天）都正確反映「今天－接機日」——問題完整解決，端對端驗證通過

## 關鍵技術筆記

- **兩個呼叫端共用同一份「今天」計算邏輯，才不會重蹈「兩邊各自解讀」的覆轍**：本次 bug 的根因之一就是「畫面顯示」與「伺服器端驗證」原本各自讀同一個 stale 快照卻沒人重算；修法刻意讓 `render_trial_email` 與 `send_trial_report` 都呼叫同一個 `_compute_used_days`，之後兩邊只會一起對、一起錯，不會再漂開。
- **VPS 系統時區與業務曆日語意不同，不能用「這裡也是要拿 now()」的直覺去對齊既有慣例**：`web/server.py`／`web/audit.py` 既有的 `datetime.now(timezone.utc).astimezone()` 用途是「絕對時刻」（稽核時間戳、rate limiter 參考時間），這種場景本來就該用系統時區／UTC；但 `borrow_date` 要比對的是「台灣業務曆日」，語意不同，套用同一慣例反而是新 bug 的來源。code-reviewer 抓到這個混淆，教訓是「同一份程式碼裡不同的『現在』可能語意不同，不能只認函式名字長得像」。
- **`zoneinfo` 是 stdlib 但不代表零依賴**：`ZoneInfo("Asia/Taipei")` 在 Linux（VPS）通常吃系統內建的 IANA 時區資料庫就夠，但 Windows 開發機沒有，必須額外裝 `tzdata` 套件才能用——這是「stdlib 模組」與「stdlib 模組實際可用」之間的落差，多平台專案要留意。
- **三 agent 鐵律的多輪修正，「未變」宣稱難以逐位元組獨立核對**：第二輪 QA 曾指出，因為每輪 QA 都是全新 context、且 commit 前 git diff 混合了兩輪變更，無法直接對前一輪的 SHA256 記錄核帳，只能用 mtime／內容邏輯等旁證交叉驗證。若之後還有多輪修正流程，建議把上一輪 QA VERDICT 的 SHA256 表原文一併餵給下一輪 QA，讓「未變」宣稱能被真正獨立核對。

## 產出檔案

| 檔案 | 類型 | 說明 |
| --- | --- | --- |
| `web/templates.py` | 修改 | 新增 `_compute_used_days`；`render_trial_email` 改用動態算法顯示已用天 |
| `web/trial_report.py` | 修改 | `send_trial_report` 伺服器端驗證改用 `_compute_used_days` |
| `tests/test_web.py` | 修改 | 修正既有測試改用 `borrow_date`＋`today` 注入；新增 3 個 QA 案例＋1 個時區收尾修正 |
| `tests/test_trial_report.py` | 修改 | `_make_recipient` 新增 `borrow_date` 參數；既有測試改用顯式 `today` 注入；新增 integration 測試 |
| `requirements.txt` | 修改 | 新增 `tzdata` 依賴 |
| `doc/spec-trial-report.md` | 修改 | 核心邏輯第 2 點更新為動態算法描述 |
| `CLAUDE.md` | 修改 | `web/trial_report.py` 段落同步更新，誠實記錄 `tzdata` 打破零依賴定位 |

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] 確認 `tools/build_recipients.py`（producer）目前的排程頻率——這次修法解決了「天數計算凍結」，但 `recipients.json` 名單本身（誰在體驗中、業務、狀態）仍然是快照，多久沒同步就多久看不到新客戶／已歸還客戶
- [ ] 找機會驗證一次真實的「寄送體驗報告」端對端成功案例（目前劉怡君／黃燕虹已達 14 天、按鈕已解鎖，可作為下次驗證的候選對象，但要先確認裝置數剛好 1、email 有效）
- [ ] `Recipient.used_days` 欄位目前已完全 vestigial（無邏輯使用，只剩展示殘留）——code-reviewer 建議在 dataclass docstring 補一句說明，避免未來誤用，非本次範圍

### 進行中（需接續）

- 無未完成工作，本次修復已完整走完三 agent 鐵律流程並在 VPS 驗證上線

### 注意事項

- **VPS 系統時區是 UTC，任何跟「台灣業務曆日」比對的新邏輯都要用 `ZoneInfo("Asia/Taipei")`，不要沿用 `datetime.now(timezone.utc).astimezone()` 這個既有慣例**——那個慣例只適用於「絕對時刻」場景（稽核時間戳等），套到曆日比對會產生 8 小時偏移的隱性 bug
- 新增 `tzdata` 到 `requirements.txt` 後，任何新開的開發環境（尤其 Windows）第一次跑涉及 `zoneinfo` 的程式碼前要先 `pip install -r requirements.txt`，否則會拋 `ZoneInfoNotFoundError`
- VPS 部署後的 venv 位置固定在 `/home/claude/mitake-sms-venv`，repo 在 `/home/claude/mitake-sms`，重啟服務指令是 `sudo systemctl restart mitake-web`（本次未觸碰 `mitake.py`，因此不需要連帶重啟 `n8n2vps-hub`——只有 `mitake.py` 被改到時才需要）
