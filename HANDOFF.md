# mitake-sms 交接手冊（給接手第二、三部分的 Claude session）

> 2026-07-29 由 n8n2vps-hub tab 的 session 交接。第一部分（VPS 部署 + 驗收）已完成，
> 本手冊供新 session 在本資料夾開工，直接進行第二部分（餘額告警）與第三部分（Web 發送介面）。
>
> ⚠️ **本 repo 是 public**：任何憑證只能放 `/etc/mitake-sms.env`（VPS）或本機 `.env`（已 gitignore），絕不進版控、絕不寫進本手冊。

---

## 1. 目前狀態：三個部分全數上線 ✅

| 部分 | 內容 | 狀態 |
| ------ | ------ | ------ |
| 第一 | 核心模組 + VPS 部署 + 驗收 | ✅ 上線（見本節） |
| 第二 | 餘額告警排程 | ✅ 上線，每日 08:00（見 §5） |
| 第三 | Web 發送介面 | ✅ 上線 <https://sms.chenghyang.uk>（見 §6） |

| 項目 | 值 |
| ------ | ----- |
| GitHub | <https://github.com/chenghyang2001/mitake-sms>（public，workspace 編號 52） |
| 本機開發目錄 | `%USERPROFILE%\workspace\mitake-sms\` |
| VPS 部署路徑 | `/home/claude/mitake-sms`（HTTPS clone，更新用 `git pull`） |
| VPS 憑證檔 | `/etc/mitake-sms.env`（claude:claude，權限 600）— 已填入三竹帳密、告警門檻、Telegram/Gmail 通知憑證 |
| 驗收結果 | 2026-07-29 `query_balance()` 真實連線成功，餘額 **12,571 點** → IP 白名單 + 帳密 + Big5 解碼全通 |

VPS 上驗證指令（免費、不扣點，隨時可重跑）：

```bash
ssh claude@187.127.109.145
cd ~/mitake-sms && set -a && source /etc/mitake-sms.env && set +a
python3.12 -c "import mitake; print('餘額點數:', mitake.query_balance())"
```

## 2. 核心模組介面（mitake.py，零外部依賴）

- `query_balance(*, timeout=25.0) -> int` — SmQuery，**免費**，回傳剩餘點數
- `send_sms(phone, body, *, timeout=25.0, max_segments=5) -> dict` — SmSend，**1 點/則**
  - 回傳 `parse_response()` 的全部欄位（`success` / `statuscode` / `msgid` / `error` / `account_point` / `batch_index` / `raw_fields` / `raw_text`）再加 `segments` / `chars`，方便呼叫端記錄這次實際扣了幾點
  - `max_segments` 是**送出任何網路請求之前**的防呆上限（預設 `MAX_SEGMENTS_PER_SEND = 5`），超過直接丟 `MitakeValidationError`，**不扣點**。要送超長內容必須明確傳 `max_segments=`，讓「我知道這會扣很多點」變成寫得出來的動作
- `validate_phone()` / `count_sms_segments()` / `decode_response()` / `parse_response()` / `classify_statuscode()` — 純函式
- 憑證只從環境變數讀：`MITAKE_USERNAME` / `MITAKE_PASSWORD`（缺少時明確報錯）
- `python3.12 mitake.py` = 離線冒煙測試（不碰網路、不扣點）；完整測試在 `tests/test_mitake.py`

### 2.1 🔴 錯誤處理：`possibly_charged` 是第三部分最需要先讀懂的欄位

失敗有**兩種**，`MitakeAPIError` 用 `possibly_charged` 區分，Web 層必須渲染成不同畫面：

| `possibly_charged` | 意義 | 該顯示什麼 |
| ------ | ------ | ------ |
| `False` | 三竹明確拒絕，**沒扣點** | 「發送失敗，可安全重試」 |
| `True` | 請求已送達三竹但結果未確認，**多半已扣點** | 「狀態未確認，**請勿重送**，請至三竹後台以 msgid 查證」 |

一律顯示「發送失敗，請重試」會直接誘導使用者重送 → **扣兩次點 + 對方收到兩封簡訊**。

`kind` 讓上層不必比對中文字串就能分流：`ip_blocked`（找三竹加白名單）／`auth_failed`（改環境變數）／`network`／`decode`／`unconfirmed`（三竹收了請求但沒回可辨識的成敗）／`api`。

例外階層：`MitakeError`（基底，可一句 `except MitakeError` 收斂）
→ `MitakeConfigError`（環境變數缺漏）
／`MitakeValidationError`（輸入不合法，**保證在送出網路請求前丟出，沒扣點**）
／`MitakeAPIError`（呼叫三竹失敗，帶 `kind` 與 `possibly_charged`）

⚠️ 已知待補：`possibly_charged` 目前**只存在於 `MitakeAPIError`**。Web 層若寫成 `except MitakeError as e: ... e.possibly_charged`，遇到 `MitakeValidationError` 會 `AttributeError`。動工前建議先在 `MitakeError` 補一個 class-level `possibly_charged = False`。

## 3. 三竹 API 鐵律（違反必踩坑）

1. **IP 白名單強制**：未登記 IP 一律 `statuscode=k`／`無效的連線位址`，與帳密無關。已登記：`59.124.85.79`（公司）、`187.127.109.145`（VPS）。換機器先寄 `service@mitake.com.tw` 申請。
2. **回應是 Big5**：UTF-8 硬解會亂碼（模組已處理，別繞過 `decode_response`）。
3. **點數與 App 團隊共用**：App 靠同一池發註冊驗證碼。**測試一律用 `query_balance`（免費），絕不用 `send_sms` 當測試**；tests 必須攔截 `mitake._OPENER.open`（**不是** `urllib.request.urlopen` —— `_fetch_raw` 走模組級 opener，patch 舊位置會讓測試真的連上三竹）。
   - 這條已由 `tests/conftest.py` **從文件升級成機制**：預設封鎖 `_OPENER.open`，忘記攔截的測試會直接紅燈（拋 `RealMitakeAPICallBlocked`）而不是靜默扣點。錯誤訊息本身帶 nodeid 與修法。
   - 真的要連外的測試掛 `@pytest.mark.allow_network`（唯讀的 `query_balance` 免費；`send_sms` 每則扣共用池 1 點，掛之前先想清楚）。
   - **已知邊界**（護欄擋的是「忘記 mock」，不是「刻意繞過」，別當沙箱信任）：繞開 `_OPENER` 直接用 `urlopen`／裸 `socket`／第三方 client、`importlib.reload(mitake)`（會重建 `_OPENER`）、`subprocess`／`multiprocessing`（全新 process），以及比 `tests/conftest.py` 更早載入者（repo 根的 `conftest.py`、pytest 外掛）。
4. 中文 70 字 = 1 則 = 1 點，超過倍增。**上限保護在 `send_sms(max_segments=)`（預設 5 則），不在 `count_sms_segments`** —— 後者只負責計數、不會擋。護欄刻意放模組層而非 Web 層，因為 Web 層由別人實作且無存取控制。
5. **禁止自動重試**：模組刻意不實作重試，失敗是否重送必須由人判讀 `possibly_charged` 後決定（見 §2.1）。
6. **不要在 `send_sms` 前先呼叫 `query_balance` 檢查餘額**：本模組刻意不這樣做，以避免「查完到送出之間點數被 App 團隊用掉」的 TOCTOU，也避免查詢失敗連帶擋掉發送。好心加上這層檢查反而會引入 race。

## 4. VPS 環境鐵律（n8n2vps-hub tab 的既有經驗）

- **一律用 `python3.12` 直接路徑**：`/usr/bin/python3`（symlink）會被未知機制 SIGKILL。systemd `ExecStart` 同樣。
- VPS 系統時區是 **UTC**（crontab、journalctl 都是 UTC）；n8n2vps-hub 的 APScheduler 讀 config 用 **Asia/Taipei**。先分清楚跑在哪一層再寫時間。
- Gmail App Password **含空格**：寫進 env 檔或 SMTP login 前必須去空格（`/etc/mitake-sms.env` 內已是去空格版本）。
- VPS 的 git credential store 是**唯讀 PAT**：push 需另外帶寫入 token（embedded URL 用完即清），或改在本機 push。

## 5. 第二部分：餘額告警排程 ✅ 已完成上線（2026-07-29）

| 項目 | 值 |
| ------ | ------ |
| 位置 | **n8n2vps-hub repo**，`jobs/job_010_mitake_balance/`（commit `cb0230e`） |
| 排程 | 每日 **08:00 台灣時間**（APScheduler 帶 `Asia/Taipei`，非 UTC —— 已由 heartbeat 的 `+08:00` 時間戳證實） |
| 門檻 | `warn=3000` / `critical=1000`（在 hub 的 `config.json`，**不是** env 檔） |
| 通知 | Gmail + Telegram + LINE 三管道 |
| 驗收 | 2026-07-29 手動觸發成功，餘額 12,571 點、level=normal、三管道全送達、heartbeat 已建 |

### 5.1 動它之前必須知道的四件事

1. **改完要走 `bash scripts/deploy.sh`**（hub repo 的鐵律），禁止在 VPS 直接改。
2. 🔴 **`scripts/deploy.sh` 不會更新 `/home/claude/mitake-sms`** —— 它只 pull hub。本專案的 `mitake.py` 要自己 `cd ~/mitake-sms && git pull`，**而且 pull 完必須再 `sudo systemctl restart n8n2vps-hub.service`**，否則 `sys.modules` 裡還是舊模組。
   （2026-07-29 部署當天就踩到：VPS 的 mitake-sms 落後 5 個 commit，跑的是還沒修過四類安全缺陷的舊版。）
3. **告警門檻只看 hub 的 `config.json`**。`/etc/mitake-sms.env` 裡的 `MITAKE_ALERT_THRESHOLD` 是歷史殘留，**改了不生效也不報錯**。
4. **`/etc/mitake-sms.env` 只有兩個 key 會被載入**（`MITAKE_USERNAME` / `MITAKE_PASSWORD`）。該檔實際還含 `GMAIL_APP_PASSWORD` / `GMAIL_USER` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` —— handler 用 allowlist 擋掉它們，因為全量載入會**永久污染 hub 那個 24/7 常駐 process**，讓其他 job 靜默改用這些憑證發通知。**不要把 allowlist 拿掉。**

### 5.2 部署後驗證清單（deploy.sh 不會幫你檢查）

```
- [ ] cd ~/mitake-sms && git pull（有更新則再 restart 一次 hub）
- [ ] ls -l /etc/mitake-sms.env → 權限 600
- [ ] journalctl 確認「[job_010_mitake_balance] 排程已加入：08:00」
- [ ] 手動觸發一次（SmQuery 免費不扣點）：
      cd ~/n8n2vps-hub && env -u MITAKE_USERNAME -u MITAKE_PASSWORD \
        ENV=prod python3.12 main.py --job job_010_mitake_balance
      ⚠️ 一定要用 env -u 清掉變數。若先 source 那個 env 檔，handler 會走
         「憑證已存在、跳過檔案載入」分支 —— 等於把要驗的路徑繞過去了，
         而 log 依然全綠、通知照樣送達，看起來完全成功。
      預期 log：「已從 /etc/mitake-sms.env 載入 2 個環境變數（值不記錄）」
- [ ] ls logs/heartbeat/job_010_mitake_balance.json（沒有的話 25 小時後會誤發健康警報）
```

### 5.3 失敗告警的設計（改之前先看）

只有「重試救不回、需要人動手」的失敗才發 job 專屬信：`ip_blocked` / `auth_failed` / `api` / 憑證檔問題 / 模組問題。
`network` / `decode` **刻意不發** —— `core/retry.py` 的通用警報已含 traceback，資訊完全重疊，再發一封只是噪音。
⚠️ **`api` 不可從清單移除**：`classify_statuscode` 把所有非 `k`/`e` 的錯誤碼都歸這裡，「帳號被停權」正落在此，那是這個 job 最該尖叫的一天。

---

<details>
<summary>原始建議架構（2026-07-29 前一 session 的評估，保留供對照）</summary>

**建議架構**（前一 session 的評估，接手者可再斟酌）：

- 做在 **n8n2vps-hub repo**（`%USERPROFILE%\workspace\n8n2vps-hub\`，VPS `/home/claude/n8n2vps-hub/`，systemd `n8n2vps-hub.service`）— 它已有現成 Gmail/Telegram 通知（`core/notifier.py`）、retry（`core/retry.py`）、heartbeat 健康監控（`core/health.py`）
- 新增 `jobs/job_010_mitake_balance/handler.py`：import `/home/claude/mitake-sms/mitake.py`（`sys.path.insert` 該路徑即可，模組零依賴）→ `query_balance()` → 低於 `MITAKE_ALERT_THRESHOLD` 就 notify
- `config.json` 加 `job_010_mitake_balance`（排程建議每天 08:00 台灣時間）+ `health.jobs_tolerance` 加對應容忍值（分鐘）
- env：n8n2vps-hub 的 `.env.prod` 或 systemd unit 要能讀到 `/etc/mitake-sms.env` 的三竹憑證（可在 unit 加第二個 `EnvironmentFile=`，或 handler 內自行解析該檔 — 後者不用動 unit）
- **部署 SOP（鐵律）**：n8n2vps-hub 只能「本機改 → commit → `bash scripts/deploy.sh`」，禁止在 VPS 直接改。deploy.sh 會做安全檢查 + push + VPS pull + restart service + md5 驗證
- 參考範本：`jobs/job_009_weather_umbrella/`（同樣是「查外部 API → 門檻判斷 → 三管道通知」的 job）

**替代方案**：獨立 cron 包 wrapper script — 較簡單但通知/重試/健康監控都要自己重寫，前一 session 不建議。

（實際採用了上述 n8n2vps-hub 方案，細節見 §5.1–5.3。）

</details>

## 6. 第三部分：Web 發送介面 ✅ 已完成上線（2026-07-29）

| 項目 | 值 |
| ------ | ------ |
| 網址 | **<https://sms.chenghyang.uk>** |
| 程式 | 本 repo `web/`（server / templates / audit）＋ `deploy/mitake-web.service` |
| 服務 | `mitake-web.service`（VPS，綁 **127.0.0.1:8766**，不直接對外） |
| 對外 | Cloudflare Tunnel `vps-webhook` → ingress `sms.chenghyang.uk` → `localhost:8766` |
| 稽核 | `/var/log/mitake-sms/send-audit.jsonl`（**含收件號碼，已 gitignore，勿進版控**） |
| 驗收 | 2026-07-29 實發 1 則，msgid `0315772761`，三竹查詢狀態碼 `4`（已送達手機） |

### 6.1 兩層存取保護（缺一不可）

1. **Cloudflare Zero Trust Access**（外層，真正的認證）
   - application `mitake-web` / domain `sms.chenghyang.uk` / session 24h
   - policy `only-me`：Allow → Emails → `chenghyang2001@gmail.com`
   - 未登入者被導向 `chenghyang.cloudflareaccess.com` 登入頁，**碰不到服務**
2. **應用層 header 檢查**（內層，`MITAKE_WEB_REQUIRE_ACCESS_EMAIL`，unit 檔已啟用）
   - 檢查 `Cf-Access-Authenticated-User-Email` 是否相符，不符回 403；`/health` 豁免
   - ⚠️ **這不是真正的認證**（header 可偽造）。它改變的是**失敗模式**：「Access 忘了設／設錯／先開 tunnel 才去設」原本會安靜地變成一個對全世界開放的付費簡訊閘道，加了之後會變成一眼看得見的 403。**壞掉會有人回報，安靜被利用不會。**

**若整站 403 而 `/health` 仍 200**，看 journal 判讀（log 刻意不印 header 原值，避免 log injection）：

| log 訊息 | 意義 |
| ------ | ------ |
| 「標頭**缺少**」 | CF 送的 header 不叫這個名字 → 改 `ACCESS_EMAIL_HEADER` |
| 「標頭**與設定值不符**」 | 名稱對、email 值不同 → 對齊 unit 檔的設定值 |

回滾：把 unit 檔那行改回註解 → `daemon-reload` → `restart`（約一分鐘）。

### 6.2 常用運維指令

```bash
ssh claude@187.127.109.145
systemctl status mitake-web
sudo journalctl -u mitake-web -n 50 --no-pager
sudo cat /var/log/mitake-sms/send-audit.jsonl | tail -5   # 號碼已遮罩、內容不落地

# 改完程式後重新部署
cd ~/mitake-sms && git pull
sudo cp deploy/mitake-web.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart mitake-web
```

### 6.3 動 tunnel 設定前必讀

`~/.cloudflared/config.yml` 是**所有** tunnel 服務共用的單一檔案（`vps` / `prompts` / `mcp` / `langgraph` / `ch25` / `linebot` / `sms`）。改它之前：

```bash
cp ~/.cloudflared/config.yml ~/.cloudflared/config.yml.bak-$(date +%Y%m%d)
# 新 ingress 必須插在 catch-all「- service: http_status:404」之前
cloudflared tunnel ingress validate     # 不可省略
sudo systemctl restart cloudflared      # 不支援 reload
# 改完回頭驗既有 hostname 還活著（tunnel 壞會是 530/1033 或連不上；404/502 是後端自己的狀態）
```

⚠️ **改共用設定檔前先量 baseline**（2026-07-29 的疏失）：當時沒先記錄既有 hostname 的狀態碼，事後看到 `prompts` 404、`mcp` 502 只能從「錯誤碼性質」推論不是自己造成的。推論成立，但先量就是直接比對。

### 6.5 🔴 投遞狀態查詢：已寫完但**未經 QA/reviewer**（下個 session 第一件事）

commit `e6b3523` 新增了「查詢簡訊投遞狀態」功能，**程式碼在版控裡但尚未部署**，生產環境仍是 `cba2d45`。

| 已驗證 | 未驗證 |
| ------ | ------ |
| **179 passed**（既有 79 零回歸 + 新增 100） | ❌ code-qa 的 20+ case 獨立驗證 |
| ruff 全綠、語法通過 | ❌ code-reviewer 的 adversarial review |
| `mitake.py` **0 deletions**（既有函式一行未動） | ❌ 突變測試（新測試鎖不鎖得住） |
| 零外部依賴 | ❌ VPS 真實環境驗證 |
| 狀態碼對照表逐碼實測正確 | |

> ⚠️ commit `e6b3523` 的 message 誤寫「146 passed」—— 那是我在 writer 尚未寫完
> `test_web.py` 時測到的中間數字。實際最終為 **179 passed**（writer Manifest 與事後
> 複測皆為此數）。commit 已 push 故不 amend，以本表為準。

**writer 自主做的幾個判斷（接手時別當成疏漏改掉）**：

| 決策 | 理由 |
| ------ | ------ |
| `/status` 用 **GET** 不用 POST | 查詢唯讀免費冪等，GET 才能收藏／重整／分享。POST 結果頁重整會跳「要重新送出表單嗎」，而本專案使用者已被訓練成「重送＝可能多扣點」，那個對話框本身就是誤導。也讓成功頁的入口能是純 `<a>`，不必在「刻意不放任何送出元件」的成功頁擺按鈕 |
| 查詢**另開**一組節流器（30 次/5 分鐘） | 不與發送額度共用：查詢免費，共用計數器會讓「查狀態」吃掉「還能發幾則」的預算。但查詢仍是對三竹的真實請求，連刷會讓來源 IP 被限流，而那會連**發簡訊**一起壞（`statuscode=k`）—— 節流保的是發送能力，不是查詢的錢 |
| 帳戶錯誤與系統錯誤**不同調** | 帳戶類（`c`/`e`/`k`…）丟 `MitakeAPIError` 走既有 ip_blocked/auth_failed 分流 —— 三竹根本沒去查那則簡訊，回傳它等於謊稱「這則簡訊的狀態是帳密錯誤」。系統類（`*`/`a`/`r`…）照常回傳並標 `is_final=False`，讓畫面說「稍後再查」而不是把人導去改一個沒壞的設定 |
| `templates.py` 不 import `mitake` | `web/__init__.py` 載明本套件匯入順序脆弱，為幾個字串常數建立那條相依不划算。代價是 6 個分類字串重複一份，已用測試機械釘死，任一邊改名立刻紅燈 |

**writer 過程中抓到並修掉的一個真 bug**：`parse_status_response` 原本用 `line.strip()`，
遇到**空 msgid** 的回應（`\t4\t2026...`）會吃掉前導 Tab、導致欄位整排左移而**靜默解出錯誤狀態**。
已改為 `strip(" \n")`（不去 Tab）並留測試鎖住。這是它自己寫測試時發現的。

缺 QA/reviewer 是 2026-07-29 session 收尾時間限制所致，**不是**判斷不需要。使用者當時已同意分級為 complex（20+ case + reviewer）。

**接手步驟**：

```
1. 派 code-qa：20+ case。重點驗
   - 1/2/3 歸類為 pending、只有 4 是 delivered（把前者講成後者是最貴的錯）
   - 每個狀態碼逐碼對到正確中文與分類
   - query_message_status 不扣點（endpoint 是 SmQuery 不是 SmSend）
   - msgid 驗證、Big5 解碼、欄位不足/空回應等異常格式
   - 成功頁真的有帶 msgid 的查詢連結
   - 回歸：既有 79 條不可壞，mitake.py 既有行為零改動
2. 派 code-reviewer：adversarial review
3. 通過後才部署：VPS git pull + restart mitake-web（見 §6.2）
```

**功能規格**：`query_message_status(msgid, *, timeout=25.0) -> dict`，走 `SmQuery?...&msgid=<id>`（**唯讀免費不扣點**），回應是 Tab 分隔的 `msgid \t statuscode \t yyyyMMddHHmmss`。狀態碼對照見 `mitake.DELIVERY_STATUS_TABLE`。

### 6.4 已知限制與待辦

- **`possibly_charged` 只存在於 `MitakeAPIError`** —— Web 層已用 `getattr(..., True)` 保守處理，但 `MitakeError` 基底補一個 class-level `possibly_charged = False` 會更乾淨（見 §2.1）
- 純 ASCII 內容的則數會**高估**（一律 70 字/則，三竹對純英數通常 160 字/則）。方向保守不會少扣，但確認頁的數字會偏多
- 稽核檔目前無 logrotate。日後若加，注意速率回填是從稽核檔 tail 讀的，輪替當下該小時額度會重置

---

<details>
<summary>原始建議架構（2026-07-29 前一 session 的評估，保留供對照）</summary>

**需求**：網頁輸入手機號碼 + 訊息內容 → 呼叫 `send_sms` 發送。

**建議架構**：

- stdlib `http.server` 常駐（沿用零依賴原則；n8n2vps-hub 的 `job_005_pm25_linebot` 曾用同模式，佔 port 8765 但**該服務目前 inactive**）
- Port 建議 **8766**（8123/8124/8765 已被 langgraph ch21/ch25/pm25-linebot 佔用）
- 公網：Cloudflare Tunnel `vps-webhook`（tunnel id `5bd4cc70-9178-4c78-8d1e-7017940acf6b`）在 `~/.cloudflared/config.yml` 加 ingress（如 `sms.chenghyang.uk` → `http://localhost:8766`）+ `cloudflared tunnel route dns` 設 CNAME + `sudo systemctl restart cloudflared`（不支援 reload）
- 🔴 **必加認證再上線**：發簡訊直接燒共用點數池。建議 Cloudflare Zero Trust Access 限定 `chenghyang2001@gmail.com`（比 ch21/ch25 的「無 auth 待補」教訓更不能重蹈）。上線順序：先只綁 localhost 測通 → 加 CF Access → 才開 tunnel ingress
- systemd unit（如 `mitake-web.service`）：`ExecStart=/usr/bin/python3.12 ...`、`EnvironmentFile=/etc/mitake-sms.env`、`User=claude`、`Restart=always`
- 防呆建議：確認頁（顯示則數與扣點數）、單次發送則數上限、發送 log 留底（誰在何時發了什麼）

（上述建議全數採用，實際配置與運維見 §6.1–6.4。）

</details>

## 7. 開工前 checklist（新 session 第一步）

```
- [ ] 讀本手冊 + README.md（特別是 §2.1 possibly_charged 與 §3 鐵律）
- [ ] 本機跑 python mitake.py（離線冒煙）與 pytest tests/（應為 6 passed）
- [ ] 寫新測試前先讀 tests/conftest.py 檔頭：它預設封鎖所有對三竹的呼叫，忘記攔截會紅燈
- [ ] SSH VPS 跑一次 query_balance 確認環境仍通（見 §1 指令）
- [ ] 第二部分動工前：先讀 n8n2vps-hub 的 CLAUDE.md（部署鐵律）與 jobs/job_009 範本
- [ ] 兩個功能都 >50 行 → 依「規格先行」原則先出規格 MD 給使用者確認再寫碼
```

## 8. 相關資源

| 資源 | 位置 |
| ------ | ------ |
| n8n2vps-hub repo（job_010 要做在這） | `%USERPROFILE%\workspace\n8n2vps-hub\`（CLAUDE.md 有完整部署規則） |
| VPS 部署記憶（含本專案） | `~/.claude/projects/C--Users-B00332-workspace-n8n2vps-hub/memory/mitake_sms_vps_deployment.md` |
| Cloudflare Tunnel 設定 | VPS `~/.cloudflared/config.yml` |
| 三竹客服（IP 白名單申請） | `service@mitake.com.tw` / 02-25367777 |
