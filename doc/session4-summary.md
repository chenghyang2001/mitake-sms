# Session 4 摘要（2026-07-29）

> 承接 Session 3 欠下的技術債。一句話：**把「寫完但沒驗」的功能驗到能上線，過程中 reviewer 抓到一條會誤導使用者的硬傷。**

## 一句話

補跑 Session 3 跳過的 QA/reviewer，修掉「msgid 身分不符會顯示別則簡訊狀態」的 MUST_FIX，部署上線，並補上系統架構文件與圖表合輯。

## 產出

| commit | 內容 |
| ------ | ------ |
| `50a0fc0` | 修 msgid 身分不符缺陷 + 三項 NICE（**生產版本**） |
| `9890537` | 新增 `.gitattributes` 鎖定 LF 行尾 |
| `d1d201b` | 更新交接手冊：投遞狀態查詢已通過並上線 |
| `a3e86b7` | 系統架構文件 + 架構圖表合輯（arch-deck 一條龍） |

## 完成事項

### 1. 補跑三 agent 流程（QA → reviewer → writer → QA → reviewer 共兩輪）

Session 3 因收尾時間限制跳過 QA/reviewer，HANDOFF §6.5 列為「下個 session 第一件事」。

**第一輪**：QA 給 PASS（37 個獨立 case、10/10 突變測試鎖住），但 reviewer 抓到一條 QA 驗不到的缺陷。

**第二輪**（修完後）：QA PASS（231 passed、12/12 突變鎖住、R1–R4 回歸全綠）、reviewer APPROVED、MUST_FIX 零。

### 2. 🔴 MUST_FIX：msgid 身分不符會顯示**別則簡訊**的狀態

`query_message_status` 遇到三竹回的 msgid 與查詢的不符時只 `logger.warning` 就放行。

reviewer 用假回應實測：查 `0315772761`、三竹回 `9999999999\t4\t...` → 整頁綠色「已送達手機」，而**使用者問的 msgid 一個字都沒出現**（`"0315772761" in html` → False）。唯一線索是 journald 一行 WARNING —— 而這功能存在的理由就是「手機沒收到、要查卡在哪」，他不會去看 journald，他會停止追查。

反向亦然：多行回應時 `parse_status_response` 只取第一行，畫面可能說「沒送達，可以重新發送」而那則其實已送達 → **多扣 1 點 + 對方收到兩封**。

**成因**是兩個各自合理的決定疊起來：解析層寫死「取第一個非空行」，呼叫端沒有任何一處斷言「那一筆就是你問的那筆」。同一模組對「拒絕猜測」原則只貫徹一半 —— 空 msgid（較安全）寧可拋錯，錯 msgid（會渲染出自信的綠色頁面）卻只 log。

**修法**（使用者核准「拋錯，拒絕顯示」）：丟 `MitakeAPIError`（新 kind `msgid_mismatch`、`possibly_charged=False`），訊息同時帶兩個 msgid。

### 3. 同輪三項 NICE（使用者核准）

| 修正 | 實測依據 |
| ------ | ------ |
| `_fetch_raw` 加 `MAX_RESPONSE_BYTES = 64 KiB`，超限**拒絕**而非截斷解析 | 20 萬字元回應 → 202,751 bytes 錯誤頁 |
| `_read_capped()` 用**迴圈**而非單次 `read(limit)` | 單次寫法遇短讀會把 `AccountPoint=12571` 靜默截成 `AccountPoi`，讓 job_010 發出**不存在的低點數警報** |
| `describe_delivery_status` / `classify_statuscode` 查表前正規化大小寫 | 大寫 `K` 原落到 `unknown` 而非 `ip_blocked`，把純設定問題誤導成「狀態不明」 |
| 錯誤頁文案分流（新增 `_status_unretryable_error_response`） | 「格式解不開」原說「稍後再查」，但重試一百次也一樣 |

### 4. 部署上線

VPS `git pull` → restart `mitake-web` **和** `n8n2vps-hub`（後者不重啟則 `sys.modules` 是舊模組）。

**真實環境驗收**（SmQuery 唯讀免費）：實查 `0315772761` / `0315794968` → 皆 `code='4'` 已送達手機；`query_balance()` 仍解出 **12568**（確認零回歸、查詢不扣點）。

### 5. `.gitattributes` 鎖定 LF

QA 上一輪挖到的跨機器稽核陷阱：repo `core.autocrlf=true` 且無 `.gitattributes`，Windows 重新 checkout 後 `mitake.py` 從 47867 → 48915 bytes、SHA256 漂移，但 git 認定內容零差異 → Manifest hash 稽核在家用機會誤判成竄改。commit 當下 git 對三檔發出的 `LF will be replaced by CRLF` 警告正是現場證明。

`git add --renormalize .` 對既有檔案零改動（git 內部本來就存 LF）。

### 6. 架構文件與圖表（`/mmd-gen` + `/arch-deck`）

- `doc/architecture.md`（225 行六節），**§5 是 12 條 ADR，每條都寫理由「與代價」**
  - ⚠️ arch-deck skill 預設輸出 `docs/architecture.md`，本專案既有慣例是 `doc/`（session summary 都在那），故落地時改用 `doc/` —— **這是刻意偏離 skill 預設，不是疏漏**。為 skill 預設值另開一個語意相同的目錄只會製造兩份真相
- 架構圖 5 張 + GitNexus wiki 圖 11 張 + PPTX 合輯 18 頁
- GitNexus 知識圖譜：540 nodes / 1,664 edges / 27 clusters / 47 flows

## 關鍵技術筆記

### QA 與 reviewer 的分工在這次顯出價值

| 角色 | 驗什麼 | 這次抓到什麼 |
| ------ | ------ | ------ |
| code-qa | 「有沒有做到宣稱的行為」 | 37 case + 12 突變全鎖住、R1–R4 回歸、`_FakeResponse` 有狀態化的 A/B 驗證 |
| code-reviewer | 「宣稱的行為本身對不對」 | msgid 身分不符（QA 驗不到，因為那條路徑**從未被測**） |

**突變測試只能驗「已被測到的行為鎖不鎖得住」，鎖不住從未被測的路徑。** `grep "mismatch" tests/` 在 status 相關零命中 —— 這正是它同時躲過 writer 自測與 QA 10 個突變的原因。writer 確實寫了 mismatch 分支（代表他想到了），但那個分支的**使用者可見後果從未被驗證**。

### reviewer 推翻了我提的一個 trade-off

我請它權衡「為了接住大寫 `K` 而讓 `' 4 '`（帶空白）也被判為已送達」。它實測後指出**那個代價在真實路徑上不存在** —— 兩條解析路徑在查表前早已 strip（`mitake.py:1074` 與 `:404`，皆為既有程式碼）。`_normalize_statuscode` 的 `.strip()` 實質是死碼。

它還發現有支新測試把那段死碼**釘成了契約**（`(" k ", KIND_IP_BLOCKED)`），會擋掉未來的窄化 —— 那一行已刪。

### reviewer 做了我沒要求的事：讀下游

實際打開 `n8n2vps-hub/jobs/job_010_mitake_balance/handler.py` 讀出 gate `ACTIONABLE_API_KINDS = ("ip_blocked", "auth_failed", "api")`，HEAD/新版雙載入跑 7 種回應 A/B → 新 kind `bad_response` 唯一入口是 64 KiB 上限，而**同一輸入在舊版是 `network`，兩者都不在 gate 內** → 告警行為逐格相同。「新增 kind 會不會讓每日告警漏報」被實測而非推論回答。

### grep 假陰性（我犯的錯）

我用 `grep "status"` 判斷首頁有無投遞查詢入口，結論「沒有、需要補」。**錯的** —— 該行寫的是常數 `STATUS_PATH`（大寫），小寫過濾漏掉了。

驗「某功能存不存在」時，比 grep 原始碼更可靠的是**直接打線上 endpoint 看實際輸出**。使用者的截圖也不是伺服器狀態的證據（他看到的是部署前的瀏覽器快取）。

### `block-beta` 的跨層連線不可用

第一版系統架構圖用跨層 `-->`，渲染出的線橫穿整張圖（GitHub → mitake-web 那條斜線劃過三層）。改成純分層、移除所有跨層箭頭 —— **由上而下的排列本身就表達流向，不需要線**。

## 產出檔案

| 檔案 | 說明 |
| ------ | ------ |
| `mitake.py` | +198 / -17（`_read_capped` / `_preview_for_error` / 兩個新 kind / 正規化） |
| `web/server.py` | +66 / -3（兩條 kind 分流 + 不可重試錯誤頁） |
| `tests/test_mitake.py` | +410 / -3（身分驗證 13 + 大小上限 13 + 正規化 18 + 截斷 2） |
| `tests/test_web.py` | +113（身分不符頁面斷言 + 反向回歸） |
| `.gitattributes` | 新增，鎖 LF（含後續補的 `*.mmd` / `*.pptx`） |
| `doc/architecture.md` | 新增 225 行，12 條 ADR |
| `mermaid/20260729-mitake-sms-架構/` | 16 張圖（mmd + png）+ 18 頁 PPTX |
| `HANDOFF.md` | §6.4/§6.5 重寫、章節編號修正、§8 加入新資源 |

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] **無急件。** 三部分 + 投遞狀態查詢全部上線且通過完整三 agent 流程，生產 `50a0fc0`（VPS 已同步至 `a3e86b7`）。
- [ ] 若要動 `_preview_for_error` 覆蓋不對稱（`HANDOFF.md` §6.5 第 1 項）：那是**唯一有實測數字**的待辦 —— 20 KiB nginx 錯誤頁 → 20,053 字元訊息，而 Telegram `sendMessage` 上限 4096，推論後果是三管道告警靜默退化成只剩 email。⚠️ 動的是 `_raise_if_failed` / `query_balance`，屬 job_010 每日 08:00 路徑，需完整跑一輪 writer/QA/reviewer。
- [ ] `MitakeError` 基底補 class-level `possibly_charged = False`（低優先，Web 層已用 `getattr` 迴避）。

### 進行中（需接續）

無。本 session 所有工作皆已完成、驗證、部署、commit + push。

工作區唯一殘留：`mermaid/.../~$mitake-sms-架構圖表合輯.pptx`（PowerPoint COM 做視覺 QA 留下的鎖檔，刪不掉因進程佔用，**已加進 `.gitignore`**，PowerPoint 釋放後自行消失）。

### 注意事項

- 🔴 **「拒絕猜測」現在是模組層明文規則**：格式異常拋錯、msgid 身分不符也拋錯。改 `query_message_status` 或 `parse_status_response` 前先讀該區段開頭的規則註解。**放寬任一半都會回到「顯示別則簡訊狀態」那條路。**
- 🔴 狀態碼 **1/2/3 是「已送達業者」屬 pending，只有 `4` 是「已送達手機」**。`DELIVERED_STATUSCODE = "4"` 是唯一 delivered 碼，已有突變測試鎖住。把前者講成後者是本專案最貴的錯誤。
- `_fetch_raw` 是 `query_balance` / `send_sms` / `query_message_status` **三者共用底層**，job_010 每天 08:00 呼叫它。改動需逐位元組回歸驗證（已有測試用 6 種尺寸 + 短讀鎖住）。
- 新增 `kind` 前必須確認 job_010 的 `ACTIONABLE_API_KINDS` gate 不會因此漏報（本次已實測 `bad_response` 零影響）。
- 想知道「為什麼這樣設計」先讀 `doc/architecture.md` §5 的 12 條 ADR —— 每條都寫了代價，不只理由。
- 測試基線 **230 passed**（`HANDOFF.md` §7 checklist 已更新，舊值 6 passed 是過時的）。
