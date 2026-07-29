# Session 2 摘要（2026-07-29）

> 承接 Session 1（VPS 部署 + 驗收，交接手冊見 `HANDOFF.md`）。
> 本 session 未動第二／三部分功能，全部投入**既有模組的安全性與計費正確性修復**。

## 一句話

把「發簡訊會扣共用點數池」這件事的每一條錯誤路徑補到不會誤導使用者重送，並把「測試不准打真的 API」從文件鐵律升級成程式機制。

## 產出（3 個 commit，皆已 push）

| commit | 內容 |
| ------ | ------ |
| `d1aea2d` | 修復 mitake.py 四項安全與計費正確性缺陷 |
| `b39658a` | 修正 HANDOFF / README 的介面漂移 |
| `e6d9f02` | 新增 tests/conftest.py + 文件同步 |

程式碼：`mitake.py` 719 → 777 行、`tests/test_mitake.py` 203 行、`tests/conftest.py` 184 行（新）。測試 6 passed，ruff 全綠。

## 修掉的缺陷

### 安全性

1. **憑證可從 traceback frame locals 明文外洩**
   `_scrub_credentials_from_exception` 原本只清例外的 `url` / `filename` 屬性，但 `raise ... from exc` 保留的 `__cause__.__traceback__` 抓著 urllib 內部呼叫鏈 —— 實測 6 個 frame 的 locals 仍持有帶帳密的完整 URL，其中 `open.fullurl`、`urlopen.url` 還是純 `str`。Werkzeug debugger／Sentry 對 frame locals 做 `repr()` 就明文外洩。
   修法：`exc.__traceback__ = None`。診斷需要的 endpoint 與 reason 早已寫進外層訊息，不損失可追查性。

2. **洗滌的 except 型別過窄**
   原本限縮 `(OSError, http.client.HTTPException)`，實測 `ValueError` 逃出時仍有 18 處外洩點。改 `except BaseException`（下一行即裸 `raise`，不吞例外）。

### 計費正確性

1. **`IncompleteRead` / `BadStatusLine` 整個逃出模組**
   它們屬 `http.client.HTTPException`，**不是** `OSError`（`issubclass` 實測為 False），原本的 `except OSError` 攔不到。而「回應讀到一半斷掉」正是**請求已送達、點數已扣**的最貴失敗模式，卻沒洗憑證、沒有 `possibly_charged`，上層 `except MitakeError` 也攔不到。

2. **DNS 失敗被誤標成「可能已扣點」**
   `possibly_charged` 的定義是「請求已送達三竹」，但原本對所有 `URLError` 一律標 `True`。DNS 解析失敗／連線被拒／憑證驗證失敗都發生在送出任何位元組**之前**，會逼使用者去後台找一筆不存在的 msgid，並把「可安全重送」講成「請勿重送」。

3. **🔴 修 4 時引入的回歸：redirect 後誤判「未扣點」**
   `urlopen` 預設掛 `HTTPRedirectHandler`。第一跳已送達三竹（已扣點），第二跳 DNS 失敗 → `URLError(gaierror)` → 命中新加的 `never_reached_mitake` → `possibly_charged=False` → **誘導重送，扣兩次點 + 對方收兩封**。
   reviewer 架假三竹實測捕獲（伺服器 hits=1 但回報未扣點）。
   修法：`_NoRedirectHandler` + 模組級 `_OPENER`，301/302/303/307/308 五碼實測全擋。

### 例外分類

1. `RemoteDisconnected` 同時繼承 `OSError` 與 `HTTPException`，排在後面的 `HTTPException` 分支拿不到它 → 分支前移。
2. `_raise_if_failed` 兩處 kind 統一為 `KIND_UNCONFIRMED if charged else KIND_NETWORK`（唯讀查詢的正解永遠是重試）。

## tests/conftest.py（新）

**動機**：HANDOFF §3.3 規定「tests 必須 mock」，但完全靠自律 —— 忘記攔截就真的扣點。

**機制**：模組層 + function 層雙鋪封鎖 `mitake._OPENER.open`，忘記攔截直接紅燈；`@pytest.mark.allow_network` 為逃生門；`pytest_unconfigure` 還原。

**三個非顯而易見的設計點**（都是實測逼出來的，別回退）：

| 設計 | 為什麼 |
| ------ | ------ |
| 哨兵繼承 `BaseException` 而非 `Exception` | `_request` 有 `except URLError/TimeoutError/HTTPException/OSError` 分類器。哨兵若不慎繼承到那四型之一（直覺上「網路相關的假錯誤」很自然會想繼承 `OSError`），會被翻譯成 `MitakeAPIError`，使 `pytest.raises(MitakeAPIError)` **反而通過**、護欄靜默失效。A/B 對照組實測確認 |
| 封鎖鋪在**模組層**而非 `pytest_configure` | 落在命令列參數路徑上的子目錄 conftest 在 `_preparse` 階段匯入，早於 `pytest_configure`。實測該時機會真的連上 `smsapi.mitake.com.tw:443` |
| 必須有 `pytest_unconfigure` | 不還原的話，同 process 跑第二次 pytest 時 conftest 重新匯入，`_REAL_OPENER_OPEN` 會捕獲到**封鎖版** → 下一輪的逃生門「還原」成封鎖版而靜默失效 |

**已知邊界**（護欄擋的是「忘記 mock」，不是「刻意繞過」）：繞開 `_OPENER`、`importlib.reload(mitake)`、`subprocess`、比本檔更早載入者（repo 根 conftest、pytest 外掛）。

## 文件漂移修正

HANDOFF §2 的 `send_sms` 簽名缺 `max_segments`；§3.4「`count_sms_segments` 已實作上限保護」是**錯的**（上限在 `send_sms`）——這種**指名真實函式**的錯誤最危險，讀者不會懷疑只會直接信任。新增 §2.1 完整說明 `possibly_charged`／`kind`／例外階層，這是第三部分 Web 層最需要先讀懂的。

## 流程紀錄

全程走 writer → QA → reviewer 三 agent 鐵律。**每一輪的 🔴 都不是讀程式碼推論出來的**，是架假伺服器、掛 socket tripwire、連跑兩輪 `pytest.main()` 實際觸發出來的：

- `mitake.py`：2 輪（第 1 輪 ultra-review 報 3 🔴 → 第 2 輪 reviewer 抓到修正自己引入的回歸）
- `conftest.py`：4 輪（🔴 實測捕獲 2 次真實 TCP 連線 → 修完剩 2 項 🟡 → 修完剩 1 句過度宣稱 → 措辭修正）

socket tripwire connect 次數：**第一輪 2 次 → 最終 0 次**。

## 未處理（留給下一個 session）

- `MitakeError` 基底沒有 `possibly_charged`，Web 層寫 `except MitakeError as e: e.possibly_charged` 會 `AttributeError`（HANDOFF §2.1 已標註）
- 模組層只有「單次則數」護欄，沒有「單位時間發送次數」護欄 —— 一個迴圈或連按 20 次，5 則上限擋不住
- send 端點 decode 失敗仍回 `KIND_DECODE`、`is_unconfirmed=False`，Web 層照 `is_unconfirmed` 分流會漏掉這個同樣不可重送的 case
- `_scrub_credentials_from_exception` 的 `getattr` 未包在 try 內（放寬到 `BaseException` 後，第三方例外的 property getter 拋錯會反過來蓋掉真正的例外）
- redirect 回歸情境尚未固化成測試進版控
- 第二部分（餘額告警 job_010）、第三部分（Web 發送介面）皆未動工，規格見 HANDOFF §5 / §6
