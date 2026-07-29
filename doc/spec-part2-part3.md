# 第二／三部分規格（2026-07-29 Session 3）

> 依 dev-workflow 原則 1 與 HANDOFF §7 checklist：>50 行的功能先出規格、確認後才寫碼。
> 本規格取代 HANDOFF §5／§6 的草案（那是前一 session 的評估，本文為可執行版本）。

---

# 第二部分：餘額告警（n8n2vps-hub 的 job_010）

## 目標

每天定時查三竹餘額，低於門檻就三管道告警，避免點數用完導致 App 團隊發不出註冊驗證碼。

## 落點

**程式碼不在 mitake-sms repo**，而在 `%USERPROFILE%\workspace\n8n2vps-hub\`：

```
jobs/job_010_mitake_balance/
├── __init__.py
└── handler.py          # 唯一的檔，仿 job_009 但不需要 query.py（查詢邏輯已在 mitake.py）
config.json             # 加 jobs.job_010_mitake_balance + health.jobs_tolerance
```

## 輸入／輸出

| | |
| ------ | ------ |
| 輸入 | `mitake.query_balance()` 的回傳（int，剩餘點數）＋ config 的門檻設定 |
| 輸出 | 三管道通知（Gmail HTML + Telegram/LINE 純文字）、`write_execution_log`、`update_heartbeat` |
| 副作用 | **無扣點**（SmQuery 唯讀免費） |

## 核心邏輯

1. `sys.path.insert` 加入 `/home/claude/mitake-sms`，`import mitake`（模組零外部依賴，可直接借用）
2. 讀取三竹憑證（見下方決策 D1）
3. `balance = mitake.query_balance(timeout=job_cfg 的 api_timeout_seconds)`
4. 依門檻分三級判斷：
   - `balance < critical_threshold` → 🔴 緊急
   - `balance < warn_threshold` → 🟡 警告
   - 否則 → 🟢 正常
5. 依 `notify_when` 設定決定是否發送（見決策 D2）
6. 組 HTML email + 純文字訊息，呼叫 `notify(job_id, subject, email_body, text_message)`
7. `write_execution_log` + `update_heartbeat`
8. 例外時 `write_execution_log(failure)` 後 `raise`，交給 `run_with_retry` 退避重試

## 邊界條件（必須處理）

| # | 情況 | 處理 |
| --- | ------ | ------ |
| 1 | **IP 不在白名單**（`statuscode=k`） | `MitakeAPIError.kind == "ip_blocked"`。這是**設定問題不是暫時故障**，retry 無用；訊息要明確指出「VPS IP 掉出白名單，需寄 <service@mitake.com.tw>」，避免每天重試每天失敗 |
| 2 | **帳密錯誤**（`kind == "auth_failed"`） | 同上，明確指向「檢查 `/etc/mitake-sms.env`」，不要只報「查詢失敗」 |
| 3 | **憑證環境變數缺漏** | `MitakeConfigError`。若走決策 D1 的方案 B，代表 `/etc/mitake-sms.env` 讀不到或格式壞 —— 訊息要含實際路徑 |
| 4 | 網路逾時／DNS 失敗 | `kind == "network"`，屬暫時故障，讓 `run_with_retry` 正常重試 |
| 5 | **查詢失敗本身要不要告警** | 要。餘額查不到 ≠ 餘額沒問題。失敗時仍發通知，主旨標「⚠️ 餘額查詢失敗」 |
| 6 | 門檻設定錯誤（critical > warn） | 啟動時檢查，不合理就以 warn 為準並在 log 警告，不讓 job 靜默用錯門檻 |

## 依賴

- `core.config` / `core.notifier` / `core.retry` / `core.logger` / `core.execution_log` / `core.health`（皆已存在）
- `/home/claude/mitake-sms/mitake.py`（VPS 已部署，`git pull` 可更新）
- **無新增第三方套件**

## config.json 追加內容

```jsonc
"job_010_mitake_balance": {
  "enabled": true,
  "_comment": "2026-07-29 新增。每日查三竹簡訊餘額，低於門檻三管道告警。SmQuery 唯讀免費不扣點。",
  "name": "三竹簡訊餘額告警",
  "schedules": [{ "hour": 8, "minute": 0 }],
  "notify_channels": ["gmail", "telegram", "line"],
  "api_timeout_seconds": 25,
  "warn_threshold": 3000,
  "critical_threshold": 1000,
  "notify_when": "always",
  "email": { "subject_template": "📱 三竹簡訊餘額 {balance} 點 {level}", "recipients_override": null }
}
```

`health.jobs_tolerance` 加 `"job_010_mitake_balance": 1500`（同 job_009，日更 job 的慣例值）。

---

# 第三部分：Web 發送介面

## 目標

網頁輸入手機號碼 + 訊息 → 發送簡訊。**這是本專案唯一會花錢的介面**。

## 落點

`mitake-sms` repo 新增：

```
web/
├── server.py           # stdlib http.server，零外部依賴
├── templates.py        # HTML 產生（不引入樣板引擎）
└── audit.py            # 發送記錄留底
deploy/
└── mitake-web.service  # systemd unit
tests/
└── test_web.py         # 端點測試（沿用 conftest 護欄，絕不真發）
```

## 核心邏輯

```
GET  /          → 表單頁（號碼、內容、即時顯示則數與預估扣點）
POST /preview   → 確認頁（顯示解析後號碼、則數、扣點數，要求二次確認）
POST /send      → 實際發送（需帶 preview 產生的一次性 token）
GET  /health    → 健康檢查（不需認證，供 systemd/監控用）
```

**二階段送出**是刻意的：`send_sms` 不可逆且花錢，不接受「一個按鈕直接送出」。

## 🔴 安全設計（HANDOFF §6 標為必做）

| 層 | 措施 |
| ------ | ------ |
| 網路 | 先只綁 `127.0.0.1`，本機測通後才接 tunnel |
| 認證 | Cloudflare Zero Trust Access 限定 `chenghyang2001@gmail.com` |
| 應用 | POST 需一次性 token（防重放與 CSRF）；`/send` 對同一 token 只接受一次 |
| 成本 | 沿用 `send_sms(max_segments=)` 護欄；**另加單位時間發送次數上限**（見決策 D4） |
| 稽核 | 每次發送寫 `audit.py`：時間、號碼（遮罩後四碼）、則數、msgid、結果 |
| 錯誤呈現 | **依 `possibly_charged` 分流兩種畫面**（HANDOFF §2.1）—— 這是最容易做錯且代價最高的一點 |

## 邊界條件（必須處理）

| # | 情況 | 處理 |
| --- | ------ | ------ |
| 1 | `possibly_charged=True` 的失敗 | 顯示「狀態未確認，**請勿重送**，請以 msgid 至三竹後台查證」，**且不提供重送按鈕** |
| 2 | `possibly_charged=False` 的失敗 | 顯示「發送失敗，可安全重試」＋重送按鈕 |
| 3 | 使用者按瀏覽器上一頁重複送出 | 一次性 token 已失效 → 明確告知「此請求已處理過」，不重發 |
| 4 | 超過則數上限 | 在 preview 階段就擋下並顯示會扣幾點，不要等到 send 才報錯 |
| 5 | 號碼格式錯 | `MitakeValidationError`，保證未扣點，明確標示 |
| 6 | 併發送出 | stdlib `http.server` 預設單執行緒；若改 `ThreadingHTTPServer` 需確認 token 存取有鎖 |

## 上線順序（不可跳步）

1. 本機 `127.0.0.1:8766` 跑通，全程用假憑證與 mock，**不真發**
2. 部署 VPS，仍只綁 localhost，用 `curl` 驗證
3. **真發一則測試簡訊到自己的號碼**（扣 1 點，需你確認）
4. 設 Cloudflare Access（限定 email）
5. **最後**才加 tunnel ingress + DNS CNAME

---

# 需要你決策的 4 件事

## D1：job_010 怎麼讀到三竹憑證？

systemd unit 目前只有 `EnvironmentFile=/home/claude/n8n2vps-hub/.env.prod`，讀不到 `/etc/mitake-sms.env`。

| 方案 | 做法 | 代價 |
| ------ | ------ | ------ |
| **A** | 改 `deploy/n8n2vps-hub.service` 加第二個 `EnvironmentFile=/etc/mitake-sms.env` | 要改 unit + `daemon-reload`，動到共用基礎設施，影響所有 job |
| **B**（建議） | handler 內自行解析 `/etc/mitake-sms.env` 寫進 `os.environ` | 不動 unit、憑證維持單一來源；需寫 ~15 行解析（處理 `export`、引號、註解） |
| C | 把三竹憑證複製進 `.env.prod` | 憑證兩份會漂移，**不建議** |

## D2：餘額正常時要不要每天通知？

- `always`（建議）—— 每天都發，順便當「job 還活著」的證據
- `on_alert` —— 只在低於門檻時發，安靜但無法分辨「正常」與「job 掛了」

## D3：門檻設多少？

目前餘額 12,571 點。建議 `warn=3000` / `critical=1000`，你可調整。

## D4：Web 介面的速率上限？

模組層目前只有「單次 5 則」，擋不住連按。建議加「每小時最多 N 則」，N 設多少？（建議 20）

## D5：範圍與順序

第三部分涉及公網服務與 DNS，我建議**先完成第二部分並實際跑過一次**，再動第三部分 —— 你要照這個順序，還是兩個一起做？
