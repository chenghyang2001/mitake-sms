# mitake-sms — 系統架構文件

**版本基準**：`d1d201b`（2026-07-29，230 passed / ruff 全綠）
**生產部署**：`50a0fc0`（VPS `mitake-web` 與 `n8n2vps-hub` 皆在此版）

---

## 1. 系統概觀

一個**發簡訊要花別人的錢**的系統。點數池與 App 團隊共用（App 靠同一池發註冊驗證碼），
所以整份架構的第一優先不是效能、不是擴充性，而是**不讓任何人不小心多花錢、也不讓任何人
因為誤解畫面而重複發送**。

```
  開發                邊緣                 應用                  外部
┌─────────┐      ┌──────────────┐    ┌────────────────┐    ┌──────────────┐
│ 本機     │      │ CF Tunnel    │    │ mitake-web     │    │ 三竹 API      │
│ NB00547  │ push │ vps-webhook  │    │ 127.0.0.1:8766 │    │ SmSend 扣點   │
│          ├─────>│ (7 服務共用) │───>│                │    │ SmQuery 免費  │
└─────────┘      ├──────────────┤    ├────────────────┤    ├──────────────┤
     │            │ CF Access    │    │  mitake.py     │───>│ 點數池 12568  │
     │ 手動 pull  │ only-me      │    │  零外部依賴     │    │ 與 App 共用   │
     v            └──────────────┘    │  ★ 唯一出口 ★  │    └──────────────┘
┌─────────┐                           └────────────────┘
│ GitHub   │                                  ^
│ public   │                                  │
└─────────┘                           ┌────────────────┐
                                      │ n8n2vps-hub    │
                                      │ job_010 08:00  │
                                      │ 餘額告警        │
                                      └────────────────┘
```

**收斂式架構**：兩個消費端（Web、排程）都只透過 `mitake.py` 接觸三竹，沒有任何一層自己組
HTTP 請求。所有「會花錢」與「會誤導人」的判斷都集中在同一個模組，改一次就兩邊生效。

---

## 2. 組件與角色

| 層 | 組件 | 角色 | 關鍵約束 |
| --- | --- | --- | --- |
| **核心模組** | `mitake.py`（1229 行） | 唯一對三竹發請求的地方。35 個公開符號 | 零外部依賴。既有函式被 job_010 每日 08:00 使用，改動即生產風險 |
| 純函式層 | `validate_phone` / `validate_msgid` / `count_sms_segments` / `decode_response` / `parse_response` / `parse_status_response` / `classify_statuscode` / `describe_delivery_status` | 不碰網路，可離線全測 | 保證不扣點。`python mitake.py` 就是這層的冒煙測試 |
| I/O 層 | `_fetch_raw` / `_request` / `send_sms` / `query_balance` / `query_message_status` | 唯一走網路的函式 | 走模組級 `_OPENER`（**不是** `urllib.request.urlopen`）—— 測試攔截點就在這 |
| 錯誤層 | `MitakeError` → `MitakeConfigError` / `MitakeValidationError` / `MitakeAPIError` | 一句 `except MitakeError` 可收斂 | `MitakeAPIError` 帶 `kind`（8 種）與 `possibly_charged` |
| **Web 進入點** | `web/server.py`（2039 行） | 路由、二階段送出、速率、存取檢查 | stdlib `http.server`，多執行緒 |
| Web 支援 | `web/templates.py`（696 行） | HTML 產生，使用者輸入一律 `html.escape` | 刻意不 import `mitake`（避免脆弱的匯入順序） |
| Web 支援 | `web/audit.py`（265 行） | 發送留底 | 號碼遮罩留後四碼；速率回填從此檔 tail |
| **排程進入點** | `n8n2vps-hub/jobs/job_010_mitake_balance/`（**另一個 repo**） | 每日 08:00 查餘額、低於門檻三管道告警 | 門檻在 hub 的 `config.json`，**不是** env 檔 |
| 測試護欄 | `tests/conftest.py`（184 行） | 預設封鎖 `_OPENER.open` | 忘記 mock 的測試直接紅燈（`RealMitakeAPICallBlocked`），不是靜默扣點 |
| 外部 | 三竹 API `smsapi.mitake.com.tw` | SmSend 扣點 / SmQuery 免費 | 回應 Big5、IP 白名單強制、無 API Key 機制（帳密走 query string） |
| 邊緣 | Cloudflare Tunnel + Access | 對外唯一入口 | `~/.cloudflared/config.yml` 為 7 個服務共用單檔 |

---

## 3. 組件互動模式

### 3.1 線程模型

`http.server` 多執行緒，每個請求一條。因此所有跨請求的可變狀態都必須自帶鎖：

```
RateLimiter         ── threading.Lock ── 發送額度（20 則/小時，以「則」計非「次」）
StatusQueryThrottle ── threading.Lock ── 查詢額度（30 次/5 分鐘，與發送分離）
TokenStore          ── threading.Lock ── 一次性確認 token（consume 用 pop 不是 get）
```

實測：8 執行緒各搶 200 次、上限 100 → 恰好放行 100，無 race。

### 3.2 兩層存取控制（缺一不可）

```
請求 ──> CF Access（真認證，未登入者碰不到服務）
      ──> 應用層 header 檢查（Cf-Access-Authenticated-User-Email）
      ──> 路由
```

第二層**不是**真正的認證（header 可偽造）。它改變的是**失敗模式**：「Access 忘了設／設錯／
先開 tunnel 才去設」原本會安靜地變成一個對全世界開放的付費簡訊閘道，加了之後變成一眼可見的
403。**壞掉會有人回報，安靜被利用不會。**

### 3.3 錯誤分流：一個 kind，一種下一步

| kind | 意義 | 畫面要人做什麼 |
| --- | --- | --- |
| `ip_blocked` | IP 不在白名單 | 找三竹加白名單 |
| `auth_failed` | 帳密錯 | 改環境變數 |
| `network` | 連線問題 | 稍後再試 |
| `decode` | Big5 解不開 | 稍後再試 |
| `unconfirmed` | 三竹收了請求但沒回可辨識成敗 | **請勿重送**，拿 msgid 去後台查 |
| `api` | 其他錯誤碼（**帳號被停權落在這裡**） | 看訊息 |
| `bad_response` | 回應超過 64 KiB 或格式解不開 | 別重查，去三竹後台 |
| `msgid_mismatch` | 三竹回的是另一則的狀態 | 本次查詢無效，去後台核對 |

`kind` 讓上層不必比對中文字串就能分流。**新增 kind 前必須確認 job_010 的
`ACTIONABLE_API_KINDS` gate**（`ip_blocked` / `auth_failed` / `api`）不會因此漏報。

### 3.4 「拒絕猜測」原則（模組層明文規則）

當手上的資訊不足以確定一件事，**寧可拋錯，不可猜**。目前貫徹於兩處：

- **格式異常**：空 msgid 的回應（`\t4\t2026…`）→ 拋錯，不做欄位對齊猜測
- **身分不符**：三竹回的 msgid ≠ 查詢的 msgid → 拋錯，不顯示那筆狀態

理由是不對稱的代價：猜錯會渲染出一個**自信的綠色「已送達手機」頁面**，使用者看了就停止追查；
而拋錯只是讓他多跑一趟三竹後台。放寬任一半都會回到這條路。

---

## 4. 使用者操作觸發的資料流

### 流程 A：發送一則簡訊（唯一會花錢的路徑）

```
GET /
  └─ CF Access 驗證 → 應用層 header 檢查
     └─ render_form（帶本小時已用額度）
POST /preview
  └─ validate_phone（純函式，不碰網路）
     └─ count_sms_segments → 則數與預估扣點
        └─ TokenStore.issue() → 確認頁 + 一次性 token
POST /send
  └─ TokenStore.consume(token)   ← pop 不是 get，重放即失效
     └─ RateLimiter.acquire(則數)  ← 以「則」計，不是「次」
        └─ mitake.send_sms()
           ├─ max_segments 防呆（送出請求前，不扣點）
           ├─ SmSend（扣 1 點/則）
           └─ 回應 Big5 → decode → parse
              ├─ statuscode=1        → 成功頁 + msgid + 查詢連結 → 寫稽核
              ├─ possibly_charged=F  → 「可安全重試」
              └─ possibly_charged=T  → 「請勿重送」（頁面不放任何送出元件）
```

### 流程 B：查投遞狀態（免費）

```
GET /status?msgid=xxx
  └─ StatusQueryThrottle.acquire()   ← 獨立額度，不吃發送預算
     └─ mitake.query_message_status()
        ├─ validate_msgid（純函式）
        ├─ SmQuery + msgid（免費）
        └─ parse_status_response → 驗證回傳 msgid 與查詢相符
           ├─ 不符 → MitakeAPIError(msgid_mismatch)  ← 拒絕顯示
           └─ 相符 → 狀態碼查表
              ├─ 1/2/3 → 處理中（已送達「業者」，非手機）
              ├─ 4     → 已送達手機（唯一 delivered 碼）
              └─ 5/6…  → 失敗類
```

### 流程 C：每日餘額告警（免費，跨 repo）

```
APScheduler 08:00 Asia/Taipei
  └─ job_010 handler
     ├─ _load_env_file（allowlist 只取 2 個 key）
     └─ run_with_retry(query_balance)   ← 告警在 retry 外層
        └─ 比對門檻 warn=3000 / critical=1000
           └─ 三管道通知（Gmail / Telegram / LINE）
```

---

## 5. 關鍵架構決策（ADR 摘要）

| # | 決策 | 理由 | 代價 |
| --- | --- | --- | --- |
| 1 | **模組層零外部依賴**（僅 stdlib） | VPS 部署不需 venv／pip；`sys.path.insert` 就能被另一個 repo 的 job import | 手刻 Big5 解碼與回應解析，無法用成熟 HTTP 客戶端的重試／連線池 |
| 2 | **禁止自動重試** | 失敗是否重送必須由人判讀 `possibly_charged` 後決定。自動重試遇上 `unconfirmed` 就是扣兩次點 + 對方收到兩封 | 暫時性網路抖動要人工重按。已由 job_010 的 `run_with_retry` 在**排程側**補償（那條路免費） |
| 3 | **`possibly_charged` 雙失敗模式** | 「明確拒絕沒扣點」與「結果未確認多半已扣點」若都顯示「請重試」，會直接誘導使用者重送 | 該屬性目前**只存在於 `MitakeAPIError`**，`except MitakeError` 讀它會 AttributeError（Web 層已用 `getattr(..., False)` 迴避，但基底補 class-level 更乾淨） |
| 4 | **則數上限保護放模組層**（`send_sms(max_segments=5)`），不放 Web 層 | Web 層由別人實作且無存取控制。護欄放呼叫端等於每個新呼叫端都要重寫一次 | 要送超長內容必須明確傳 `max_segments=` —— 這是刻意的摩擦，讓「我知道這會扣很多點」變成寫得出來的動作 |
| 5 | **不在 `send_sms` 前呼叫 `query_balance` 檢查餘額** | 避免「查完到送出之間點數被 App 團隊用掉」的 TOCTOU；也避免查詢失敗連帶擋掉發送 | 餘額不足只能靠三竹回應得知，不能提前攔截。好心加這層檢查反而引入 race |
| 6 | **測試預設封鎖真實 API**（`conftest.py` 攔 `_OPENER.open`） | 把「測試不可真的連三竹」從文件升級成機制。忘記 mock 直接紅燈，而不是靜默扣共用池 | 護欄擋的是「忘記 mock」不是「刻意繞過」：裸 socket、`importlib.reload`、subprocess 都能繞開。別當沙箱信任 |
| 7 | **env 檔 allowlist 只載 2 個 key** | `/etc/mitake-sms.env` 實際含 `GMAIL_APP_PASSWORD` / `TELEGRAM_BOT_TOKEN` 等。全量載入會**永久污染 hub 那個 24/7 常駐 process**，讓其他 job 靜默改用這些憑證發通知 | 新增三竹相關環境變數時要記得同步改 allowlist，否則「設了沒生效」 |
| 8 | **「拒絕猜測」：資訊不足即拋錯** | 猜錯會渲染出自信的「已送達手機」頁面讓人停止追查；拋錯只是多跑一趟後台。代價不對稱 | 使用者可能拿到錯誤頁而非資訊。已用「同時顯示查詢與回傳兩個 msgid」降低困惑 |
| 9 | **`/status` 用 GET 不用 POST** | 查詢唯讀免費冪等，GET 才能收藏／重整／分享。POST 結果頁重整會跳「要重新送出表單嗎」，而本專案使用者已被訓練成「重送＝可能多扣點」，那對話框本身就是誤導 | msgid 會進 CF/journald 存取紀錄（可接受：msgid 非機密，稽核檔的號碼也已遮罩） |
| 10 | **查詢另開節流器**（30 次/5 分鐘） | 查詢免費，與發送共用計數器會讓「查狀態」吃掉「還能發幾則」的預算 | 兩套限流邏輯要各自維護。查詢仍是對三竹的真實請求，連刷會讓來源 IP 被限流而**連發簡訊一起壞** |
| 11 | **回應讀取上限 64 KiB，超限拒絕而非截斷** | 三竹正常回應數十位元組，超過代表拿到的不是預期內容（代理器 HTML、DNS 劫持）。截斷後解出的任何狀態都是憑空捏造 | `_fetch_raw` 是 `query_balance`/`send_sms` 共用底層，改動需完整回歸（已用逐位元組比對鎖住） |
| 12 | **`templates.py` 不 import `mitake`** | `web/__init__.py` 載明匯入順序脆弱，為幾個字串常數建立那條相依不划算 | 6 個分類字串重複一份。已用測試機械釘死，任一邊改名立刻紅燈（實測有效） |

---

## 6. 部署與測試拓撲

```
本機開發 ──> pytest（230 passed，conftest 封鎖真實 API）
   │         ruff check
   │
   ├─ git commit + push ──> GitHub（public，憑證絕不進版控）
   │
   └─ VPS 手動 git pull       ⚠️ n8n2vps-hub 的 deploy.sh 不會更新這個專案
         │
         ├─ sudo systemctl restart mitake-web
         └─ sudo systemctl restart n8n2vps-hub   ⚠️ 不重啟則 sys.modules 是舊模組
               │
               └─ 驗收：python3.12 mitake.py（離線冒煙）
                        query_balance()（真實但免費，不扣點）
```

### 測試分層

| 層 | 內容 | 可否碰網路 |
| --- | --- | --- |
| 純函式 | 驗證、計數、解析、分類 | 完全不碰 |
| I/O（mock） | `send_sms` / `query_balance` / `query_message_status` 全程攔 `_OPENER.open` | 被 conftest 封鎖 |
| Web | 路由、限流、token、模板跳脫 | 注入假例外，不碰真模組 I/O |
| 真實環境 | 只在 VPS 部署後手動跑 `query_balance()` 與 `/status` 查詢 | **只用免費的 SmQuery，絕不用 SmSend 當測試** |

### 已知測試缺口

Web 層測試全是注入假例外，鎖的是「Web 層拿到某 kind 會渲染什麼」，**沒鎖「`mitake.py` 真的
會產出那個 kind」**。缺一支「真 `mitake` + 假 `_OPENER`」的兩層整合測試（2026-07-29 由
code-reviewer 人工補跑驗證過，五情境全通過，但沒留成自動化測試）。

---

## 附錄：踩過的坑（改動前必讀）

| 坑 | 實情 |
| --- | --- |
| 測試攔錯位置 | 必須攔 `mitake._OPENER.open`，攔 `urllib.request.urlopen` 會讓測試真的連上三竹並扣點 |
| 驗證 env 載入先 `source` | 會走「憑證已存在、跳過檔案載入」分支，繞過待驗路徑，而 log 全綠、通知照送、看起來完全成功。要用 `env -u` |
| 改 tunnel 設定沒量 baseline | `config.yml` 承載 7 個服務。改完看到別的 hostname 異常，沒有 baseline 就只能靠「錯誤碼性質」推論 |
| 告警門檻改了不生效 | 門檻只看 hub 的 `config.json`；`/etc/mitake-sms.env` 的 `MITAKE_ALERT_THRESHOLD` 是歷史殘留，改了不生效**也不報錯** |
| Windows checkout 後 hash 漂移 | 已由 `.gitattributes`（`*.py text eol=lf`）鎖住。沒有它時 SHA256 稽核在另一台機器會誤判成竄改 |
