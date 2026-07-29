# mitake-sms 交接手冊（給接手第二、三部分的 Claude session）

> 2026-07-29 由 n8n2vps-hub tab 的 session 交接。第一部分（VPS 部署 + 驗收）已完成，
> 本手冊供新 session 在本資料夾開工，直接進行第二部分（餘額告警）與第三部分（Web 發送介面）。
>
> ⚠️ **本 repo 是 public**：任何憑證只能放 `/etc/mitake-sms.env`（VPS）或本機 `.env`（已 gitignore），絕不進版控、絕不寫進本手冊。

---

## 1. 目前狀態（第一部分已完成 ✅）

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
3. **點數與 App 團隊共用**：App 靠同一池發註冊驗證碼。**測試一律用 `query_balance`（免費），絕不用 `send_sms` 當測試**；tests 必須攔截 `mitake._OPENER.open`（**不是** `urllib.request.urlopen` —— `_fetch_raw` 已改走模組級 opener，patch 舊位置會讓測試真的連上三竹）。
4. 中文 70 字 = 1 則 = 1 點，超過倍增。**上限保護在 `send_sms(max_segments=)`（預設 5 則），不在 `count_sms_segments`** —— 後者只負責計數、不會擋。護欄刻意放模組層而非 Web 層，因為 Web 層由別人實作且無存取控制。
5. **禁止自動重試**：模組刻意不實作重試，失敗是否重送必須由人判讀 `possibly_charged` 後決定（見 §2.1）。
6. **不要在 `send_sms` 前先呼叫 `query_balance` 檢查餘額**：本模組刻意不這樣做，以避免「查完到送出之間點數被 App 團隊用掉」的 TOCTOU，也避免查詢失敗連帶擋掉發送。好心加上這層檢查反而會引入 race。

## 4. VPS 環境鐵律（n8n2vps-hub tab 的既有經驗）

- **一律用 `python3.12` 直接路徑**：`/usr/bin/python3`（symlink）會被未知機制 SIGKILL。systemd `ExecStart` 同樣。
- VPS 系統時區是 **UTC**（crontab、journalctl 都是 UTC）；n8n2vps-hub 的 APScheduler 讀 config 用 **Asia/Taipei**。先分清楚跑在哪一層再寫時間。
- Gmail App Password **含空格**：寫進 env 檔或 SMTP login 前必須去空格（`/etc/mitake-sms.env` 內已是去空格版本）。
- VPS 的 git credential store 是**唯讀 PAT**：push 需另外帶寫入 token（embedded URL 用完即清），或改在本機 push。

## 5. 第二部分：餘額告警排程（建議做成 n8n2vps-hub 的 job_010）

**建議架構**（前一 session 的評估，接手者可再斟酌）：

- 做在 **n8n2vps-hub repo**（`%USERPROFILE%\workspace\n8n2vps-hub\`，VPS `/home/claude/n8n2vps-hub/`，systemd `n8n2vps-hub.service`）— 它已有現成 Gmail/Telegram 通知（`core/notifier.py`）、retry（`core/retry.py`）、heartbeat 健康監控（`core/health.py`）
- 新增 `jobs/job_010_mitake_balance/handler.py`：import `/home/claude/mitake-sms/mitake.py`（`sys.path.insert` 該路徑即可，模組零依賴）→ `query_balance()` → 低於 `MITAKE_ALERT_THRESHOLD` 就 notify
- `config.json` 加 `job_010_mitake_balance`（排程建議每天 08:00 台灣時間）+ `health.jobs_tolerance` 加對應容忍值（分鐘）
- env：n8n2vps-hub 的 `.env.prod` 或 systemd unit 要能讀到 `/etc/mitake-sms.env` 的三竹憑證（可在 unit 加第二個 `EnvironmentFile=`，或 handler 內自行解析該檔 — 後者不用動 unit）
- **部署 SOP（鐵律）**：n8n2vps-hub 只能「本機改 → commit → `bash scripts/deploy.sh`」，禁止在 VPS 直接改。deploy.sh 會做安全檢查 + push + VPS pull + restart service + md5 驗證
- 參考範本：`jobs/job_009_weather_umbrella/`（同樣是「查外部 API → 門檻判斷 → 三管道通知」的 job）

**替代方案**：獨立 cron 包 wrapper script — 較簡單但通知/重試/健康監控都要自己重寫，前一 session 不建議。

## 6. 第三部分：Web 發送介面

**需求**：網頁輸入手機號碼 + 訊息內容 → 呼叫 `send_sms` 發送。

**建議架構**：

- stdlib `http.server` 常駐（沿用零依賴原則；n8n2vps-hub 的 `job_005_pm25_linebot` 曾用同模式，佔 port 8765 但**該服務目前 inactive**）
- Port 建議 **8766**（8123/8124/8765 已被 langgraph ch21/ch25/pm25-linebot 佔用）
- 公網：Cloudflare Tunnel `vps-webhook`（tunnel id `5bd4cc70-9178-4c78-8d1e-7017940acf6b`）在 `~/.cloudflared/config.yml` 加 ingress（如 `sms.chenghyang.uk` → `http://localhost:8766`）+ `cloudflared tunnel route dns` 設 CNAME + `sudo systemctl restart cloudflared`（不支援 reload）
- 🔴 **必加認證再上線**：發簡訊直接燒共用點數池。建議 Cloudflare Zero Trust Access 限定 `chenghyang2001@gmail.com`（比 ch21/ch25 的「無 auth 待補」教訓更不能重蹈）。上線順序：先只綁 localhost 測通 → 加 CF Access → 才開 tunnel ingress
- systemd unit（如 `mitake-web.service`）：`ExecStart=/usr/bin/python3.12 ...`、`EnvironmentFile=/etc/mitake-sms.env`、`User=claude`、`Restart=always`
- 防呆建議：確認頁（顯示則數與扣點數）、單次發送則數上限、發送 log 留底（誰在何時發了什麼）

## 7. 開工前 checklist（新 session 第一步）

```
- [ ] 讀本手冊 + README.md
- [ ] 本機跑 python mitake.py（離線冒煙）與 pytest tests/（若有 pytest）
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
