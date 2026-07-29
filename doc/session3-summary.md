# Session 3 摘要（2026-07-29）

> 承接 Session 2（安全與計費正確性修復）。本 session 把第二、三部分從規格草案做到**實際上線**。

## 一句話

三竹簡訊專案三個部分全數上線：餘額告警每日跑、Web 發送介面對外服務且有雙層存取保護。

## 產出

**n8n2vps-hub**（1 commit）

| commit | 內容 |
| ------ | ------ |
| `cb0230e` | job_010 三竹簡訊餘額每日告警 |

**mitake-sms**（5 commits）

| commit | 內容 |
| ------ | ------ |
| `b4b364e` | 第二／三部分規格 + gitignore 擋稽核記錄 |
| `f51c55c` | Web 發送介面（~3,900 行） |
| `cba2d45` | 啟用應用層存取檢查 |
| `48466ae` | HANDOFF：第三部分已上線 |
| `e6b3523` | 投遞狀態查詢（⚠️ 未經 QA/reviewer） |

## 第二部分：餘額告警（已上線）

每日 08:00 台灣時間，三管道通知。**SmQuery 唯讀免費不扣點。**

三輪 writer/QA/reviewer 逼出來的設計，每一項都有實測依據：

| 決策 | 為什麼 |
| ------ | ------ |
| env 檔採 **allowlist** 只取 2 個 key | 該檔實際含 `GMAIL_APP_PASSWORD` / `GMAIL_USER` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`。全量載入會永久污染 hub 那個 24/7 常駐 process，讓其他 job 靜默改用別人的憑證。reviewer 用假 env 檔實測 9 個 key 全進，**而真實檔案裡真的有那些 key** |
| 跳過判準用 truthy 而非 `key in os.environ` | unit 設空字串時，檔案裡的正確密碼永遠讀不到，卻報「憑證缺漏」把人引去查一個填對的檔案 |
| 刻意不支援行尾註解 | 密碼可能含 `#`，切掉會靜默截短密碼 |
| 告警放 `run_with_retry` **外層** | 放內層會因 3 次重試發 3 封重複信；且「第 1 次失敗、第 2 次成功」會先發失敗再發正常，兩封自相矛盾 |
| 告警 gate 必須含 `api` | `classify_statuscode` 把所有非 `k`/`e` 的碼歸此，**帳號被停權正落在這裡** —— 漏掉等於把最該尖叫的一天當成網路抖動 |
| `MITAKE_CREDENTIAL_KEYS` 維持 tuple | 該常數在三處錯誤訊息用 `' / '.join()`，CPython 預設開 hash randomization，改 set 會讓每次重啟順序不同、兩封失敗信文字不一致（reviewer 跨 6 個 process 實測） |

## 第三部分：Web 發送介面（已上線）

<https://sms.chenghyang.uk> —— 二階段送出、一次性 token、`possibly_charged` 分流、速率以「則」計、稽核留底。

**兩層存取保護**：CF Access（真認證）+ 應用層 header 檢查（改變失敗模式，把「Access 忘了設」從安靜被利用變成一眼可見的 403）。

reviewer 用**突變測試**驗過測試鎖得住：把 `except MitakeAPIError` 併進 `MitakeError`、給 unconfirmed 頁加重送按鈕、`consume` 的 `pop` 改 `get`、誤退還額度、確認頁顯示內容截短 —— 每一種改法都會讓測試變紅。

它也抓到一項「測了但鎖不住」：XSS 跳脫只鎖住 5 個反射點中的 1 個，補了參數化測試（5 反射點 × 2 攻擊形態）後，QA 用突變測試逐一確認每處拿掉 `_e()` 都會紅。

## 上線過程的實際踩坑

1. **VPS 的 `mitake.py` 落後 5 個 commit** —— job_010 部署當天就踩到，跑的是還沒修過四類安全缺陷的舊版。`scripts/deploy.sh` 只更新 hub，不碰 `/home/claude/mitake-sms`，而且 pull 完必須 restart 服務否則 `sys.modules` 還是舊模組。
2. **驗證時用 `source` 繞過了要測的目標** —— 第一次手動觸發 job_010 時我先 `source` 了 env 檔，log 顯示「憑證已存在、跳過檔案載入」，等於把待驗的 `_load_env_file` 路徑繞過去，而 log 全綠、通知照送、**看起來完全成功**。改用 `env -u` 清掉變數才真正驗到。
3. **改共用設定檔前沒量 baseline** —— `~/.cloudflared/config.yml` 掛著 7 個服務，改完看到 `prompts` 404、`mcp` 502 只能從「錯誤碼性質」推論不是自己造成的（tunnel 壞會是 530/1033）。推論成立，但先量就是直接比對。

## 簡訊沒收到的排查

使用者發了 2 則都沒收到手機。查證結果：**兩則三竹都回報狀態碼 `4`（已送達手機）**，時間戳分別在發送後 4 秒與 7 秒，且都沒落在 `6`（門號有錯誤）。

系統這端全鏈路正常，問題在電信商或手機端（垃圾簡訊夾、反詐騙攔截、封鎖清單）。持 msgid `0315772761` / `0315794968` 可找三竹客服 `02-25367777` 查電信商回執。

**這次排查直接催生了投遞狀態查詢功能** —— 當時要知道答案得手動組 API、解 Big5、查對照表。

## 點數帳

| 時間 | 事件 | 餘額 |
| ------ | ------ | ------ |
| 06:37 | 使用者第 1 則 | 12571 → 12570 |
| 06:37~06:51 | **App 團隊用掉 1 點** | 12570 → 12569 |
| 06:51 | 使用者第 2 則 | 12569 → 12568 |

中間那 1 點證明「點數與 App 團隊共用」不是理論風險，池子確實在持續消耗 —— 餘額告警有存在必要。

## 未完成（下個 session 第一件事）

**投遞狀態查詢功能（`e6b3523`）已寫完但未經 QA/reviewer**，程式碼在版控裡、**尚未部署**，生產維持 `cba2d45`。

已驗：**179 passed**（既有 79 零回歸 + 新增 100）、ruff 全綠、`mitake.py` 0 deletions、
狀態碼對照表逐碼正確、零外部依賴。
（commit `e6b3523` 的 message 誤寫 146 —— 那是 writer 尚未寫完測試時的中間數字，
已 push 故不 amend，以此處為準。）
未驗：code-qa 的 20+ case、code-reviewer 的 adversarial review、突變測試、VPS 真實環境。

接手步驟見 `HANDOFF.md` §6.5。

## 其他待辦

- `MitakeError` 基底補 class-level `possibly_charged = False`（Web 層寫 `except MitakeError` 再讀該屬性會 `AttributeError`）
- 純 ASCII 內容則數高估（一律 70 字/則，三竹對英數通常 160 字/則）
- 稽核檔無 logrotate，而速率回填依賴 tail 它
- CF API token 若未撤銷請撤銷（`cfut_` 開頭那個）
