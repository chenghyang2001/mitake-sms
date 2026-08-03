# Session 8 摘要：發送對象支援「個人手動輸入」與「多人批次上傳名單」（設計→實作→上線）

**日期**：2026-08-03

## 完成事項

### 1. 需求釐清與規格

- 使用者提出三點需求：發送對象可以是個人或多人；個人模式下拉選單旁要加手動輸入號碼欄位（現況是二選一，改成並存）；多人模式支援上傳 `.txt` 名單，同一則內容發給名單裡每一支號碼
- 探索現有架構後確認：整條發送流程（token、確認頁、二選一失敗頁、稽核、速率限制）都是圍繞「一次送一人」設計的，三竹也沒有批次 API，群發本質是伺服器端迴圈呼叫 `send_sms()` N 次
- 用 `AskUserQuestion` 確認三個會實質影響架構的決策：名單輸入走真正的檔案上傳（需自刻 multipart/form-data 解析器，因為 `http.server` 沒有內建支援）／群發沿用同一組每小時速率上限（超過整批擋下要求分批）／無效或重複號碼跳過並在確認頁列出（不整批拒收）
- 寫 `doc/spec-multi-recipient-sms.md`：目標、UI 變化、核心邏輯（§1~§6）、邊界條件、安全性、依賴、已知限制、分階段實作順序

### 2. 完整功能實作（三 agent 鐵律，複雜度=複雜，QA 20+ case、一定派 code-reviewer）

- **新增 `web/multipart.py`**（239 行）：multipart/form-data 最小子集解析器，純標準庫，畸形輸入一律拋 `MultipartParseError` 不嘗試盡量解析
- **新增 `web/batch_recipients.py`**（128 行）：名單解析純函式，每行呼叫既有 `mitake.validate_phone()` 驗證、正規化後去重，跳過原因分「格式錯誤」／「重複」
- **`web/templates.py`**：發送對象改成「個人／多人」radio 切換（純前端顯示切換，沿用既有 CSP nonce 腳本機制）；個人模式下拉選單旁新增手動輸入欄位（不再互斥）；新增 `render_preview_batch`（列出將發送 N 人／跳過 M 人／共扣 N×M 點）與 `render_batch_result`（三分組：已送達／未扣點失敗可重送／未確認絕不可有 `<form>`）
- **`web/server.py`**：請求分派層依 Content-Type 分流 urlencoded／multipart（新常數 `MAX_MULTIPART_BODY_BYTES`，urlencoded 仍用既有 `MAX_REQUEST_BODY_BYTES`，不放寬）；`PendingBatchSend`／`TokenStore.issue_batch`；個人模式下拉＋手動輸入同時有值時 400 擋下、不猜優先序；批次 `/preview` 用 `len(phones)*segments` 當速率成本；批次 `/send` 依序呼叫 `send_sms`、依 `possibly_charged` 三分類、`RateLimiter.release_partial()` 只退「確定沒扣點」的部分
- **`web/audit.py`**：`record_attempt`/`record_result` 新增選填 `batch_id`，單筆模式維持 `None` 不受影響
- **測試**：`tests/test_multipart.py`（25）、`tests/test_batch_recipients.py`（15）、`tests/test_web.py` 大量擴充，最終全套 **364 個測試全過**

### 3. 三輪 QA 與一輪 code-review（含一次需修正的真缺陷）

- QA 第一輪 PASS，但誤報「Manifest 沒附 SHA256」——主 Claude 用 `python hashlib` 獨立重算 8 個檔案雜湊，發現與 writer 宣稱值完全一致，判定是 QA 誤判而非真缺陷，未打回 writer 重做（見下方「關鍵技術筆記」）
- QA 同時指出兩個規格邊界條件缺測試（非 `.txt` 副檔名放行、批次速率上限剛好相等放行），請 writer 補上兩個測試後複驗
- code-reviewer 第一輪 `CHANGES_REQUESTED`，抓到兩個 MUST_FIX：
  1. 批次送出的稽核寫入失敗（`AuditLog.record` 回 `False`）完全沒有畫面警示——單筆模式失敗時會顯示醒目警示框叫操作者手動記下 msgid，批次模式沒有沿用這條安全網，磁碟故障時整批花費紀錄可能無聲消失
  2. `x-www-form-urlencoded` 這條既有 HTTP 路徑完全沒有自動化測試覆蓋（所有測試都繞過 `_read_request_body` 真正的 Content-Type 分派，直接餵 dict 給 `app.route()`）
- writer 修正：`_handle_send_batch` 新增 `_note_audit_outcome` 逐筆追蹤稽核寫入成敗，任一失敗收進 `audit_failures` 傳給 `render_batch_result` 在頁面最上方渲染警示框；補 4 條真正走 socket + HTTP 層的測試涵蓋 urlencoded 路徑與 body 上限
- QA 最終複驗 PASS（364 測試全過，兩項修正皆用突變測試證實鎖得住）→ code-reviewer 最終複核 **APPROVED**

### 4. 版本號與 VPS 部署

- `web/__init__.py`：`0.004` → `0.005`，發布日期 `2026-08-03`（commit `ec96d85`）
- **實際部署到 VPS**（`187.127.109.145`）：`git pull --ff-only` fast-forward `c6c90bb` → `ec96d85`；本次功能純標準庫、無新依賴，不需重裝 venv 套件；`systemctl restart mitake-web` 乾淨重啟，journalctl 無錯誤/警告；`curl /health` → 200；用帶 `Cf-Access-Authenticated-User-Email` 標頭的本機 curl 確認線上頁面已顯示 `v0.005 ・ 2026-08-03`、`send-mode` 切換 UI、`.txt` 上傳欄位；對外 `https://sms.chenghyang.uk/health` 回 302（Cloudflare Access 要求登入，屬預期行為）
- 使用者選擇自行上瀏覽器操作驗證，本次未派 agent 發真實測試簡訊（會扣 1 點真實點數）

## 關鍵技術筆記

- **QA sub-agent 的宣稱不能無條件信任，即使其他發現都紮實**：本次 QA 第一輪對 8 個檔案的語法/354→357 測試/4 項突變測試都做得很仔細，但「Manifest 沒附 SHA256」這個具體斷言是誤判——它自己算出的雜湊值其實跟 writer 宣稱的完全一致。主 Claude 用一行 `python hashlib` 指令就能獨立驗證這種可量化的斷言，之後才決定要不要打回 writer；沒有獨立驗證的話會平白多跑一輪不必要的 writer 修正。**教訓**：QA VERDICT 裡任何「數字／雜湊／存在與否」類的具體斷言，若成本低（幾秒鐘可驗），主 Claude 應該自己核對一次再決定要不要相信，不要照單全收，也不要一律不信任——是選擇性核實最貴的那幾個宣稱。
- **`possibly_charged` 語意延伸到批次情境時，既有的安全網（稽核警示）不會自動繼承**：code-reviewer 找到的 MUST_FIX 1 說明了「單筆模式已經做對的事，批次模式重寫一份邏輯時很容易漏掉」——這類「防呆」是散落在多個 `except` 分支裡的行為，改寫成迴圈版本時必須逐一確認每個分支都有對應。
- 三竹沒有批次發送 API，`web/multipart.py`／`web/batch_recipients.py` 是本次唯一的新檔案，其餘全是既有檔案擴充；兩個新檔案皆為純標準庫、無 I/O 的純函式，容易離線測試。
- VPS 部署對這次功能特別單純：因為零新依賴，跳過了 session7 那次「PEP 668 externally-managed 環境裝不了新套件、要建 venv」的整套麻煩。

## 產出檔案

| 檔案 | 類型 | 說明 |
| --- | --- | --- |
| `web/multipart.py` | 新增 | multipart/form-data 解析器 |
| `web/batch_recipients.py` | 新增 | 批次名單解析、驗證、去重 |
| `web/templates.py` | 修改 | 模式切換 UI、批次確認頁/結果頁、稽核警示框 |
| `web/server.py` | 修改 | 請求分派、批次 preview/send 流程、`RateLimiter.release_partial` |
| `web/audit.py` | 修改 | `batch_id` 欄位 |
| `web/__init__.py` | 修改 | 版本號/日期 |
| `tests/test_multipart.py` | 新增 | 25 個測試 |
| `tests/test_batch_recipients.py` | 新增 | 15 個測試 |
| `tests/test_web.py` | 修改 | 大量新增批次相關測試，全套最終 364 個測試 |
| `doc/spec-multi-recipient-sms.md` | 新增 | 完整規格 |

**Commits**：`bada73b`（完整功能）→ `ec96d85`（版本號）

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] 使用者正在瀏覽器上手動驗證新功能（個人模式手動輸入、下拉+手動輸入衝突擋下、多人模式上傳名單、確認頁/結果頁三分組）。下次 session 開始前先問操作結果，若有發現任何跟預期不符，優先處理
- [ ] 尚未做過一次**真正的發送測試**（會扣 1 點真實點數）驗證整條路徑，包含批次模式的多筆送出——照 repo 自己的上線 runbook，這是建議但非強制的下一步，等使用者主動要求再做
- [ ] `HANDOFF.md` 目前只涵蓋單筆發送流程，本次批次功能的 `PendingBatchSend`/`TokenStore.issue_batch`/`RateLimiter.release_partial` 等新介面尚未補進去，下次動到批次流程前建議先補文件

### 進行中（需接續）

- 無——本次功能規格→實作→三輪 QA→code-review→部署已完整跑完一輪，沒有半成品

### 注意事項

- 批次上傳大小上限 `MAX_MULTIPART_BODY_BYTES`（`web/server.py`）是 `MAX_BATCH_RECIPIENTS`（500 筆）× 256 bytes 的估算值，如果未來要調高名單筆數上限，記得同步調整這個常數，否則會出現「明明沒超過筆數上限卻莫名 400」的情況
- `doc/spec-multi-recipient-sms.md` 的「已知限制」章節明確排除：批次中斷續傳、跨小時自動分批排隊、CSV/Excel 名單、跨批次查重——這些都是刻意的 MVP 範圍外，不是遺漏
- 稽核檔 `send-audit.jsonl` 現在同一批次的多筆紀錄用共同的 `batch_id` 但各自獨立的 `request_id`，人工 `grep` 對帳時可以用 `batch_id` 把同一批的 N 筆兜起來
