"""AIHCR 體驗借出名單 producer：爬體驗借出頁 + 比對 users 表，產出 recipients.json。

這支工具是 **Part A（producer）**，獨立於 repo 根的核心零依賴服務（mitake.py / web/）。
它產出的 `recipients.json` 由 Part B 的 web 下拉選單（consumer）消費，兩者只透過這個檔案
交換資料，程式碼上完全解耦 —— 本檔絕不 import web/ 或 mitake.py。

流程
----
1. Playwright 開 AIHCR 體驗借出頁，點側邊欄「🎁 體驗借出管理」，讀表格中「狀態＝體驗中」的列。
2. 連 acfh_api MySQL（唯讀），撈出所有 status='active' 的使用者。
3. 用純函式 `match_recipients` 把「頁面客戶名」對到「users 的 first/last name」，
   產出帶 match_status（ok / ambiguous / not_found）的收件人清單。
4. 以「暫存檔 + os.replace」原子寫入 recipients.json，避免 consumer 讀到寫一半的檔。

環境變數
--------
必要：
  DB_HOST       acfh_api MySQL 主機
  DB_DATABASE   資料庫名稱
  DB_USERNAME   帳號（務必用唯讀帳號）
  DB_PASSWORD   密碼
  RECIPIENTS_OUTPUT  recipients.json 的輸出絕對路徑
可選：
  DB_PORT       MySQL port（預設 3306）
  AIHCR_URL     體驗借出頁網址（預設 http://192.168.23.186:8580/）

執行方式
--------
  python tools/build_recipients.py
首次使用 Playwright 需先安裝瀏覽器：`playwright install chromium`（見 tools/requirements.txt）。

鐵律
----
- **資料庫唯讀**：本工具只對 acfh_api 執行 SELECT，絕不 INSERT/UPDATE/DELETE/DDL。
  acfh_api 是生產庫，寫入會污染真實客戶資料（比照 zap_api 唯讀鐵律）。
- **憑證只走環境變數**：帳密不寫進程式碼、不進版控。

為什麼 pymysql / playwright 延遲到函式內 import：本檔的可測核心是純函式
`match_recipients`（不碰任何 I/O）。若在頂層 import 這兩個第三方套件，只想跑純函式
單元測試的環境就被迫得先安裝它們；延遲 import 讓「import 本模組」零第三方依賴。
"""

import json
import os
import sys
from datetime import datetime, timezone

# 體驗借出頁預設網址（內網 Streamlit 服務，非使用者家目錄路徑，允許寫死為預設值）。
DEFAULT_AIHCR_URL = "http://192.168.23.186:8580/"

# 側邊欄那個要點的項目文字。獨立成常數：頁面若改字，只改這一處。
SIDEBAR_ITEM_TEXT = "🎁 體驗借出管理"

# 每一列固定 7 個欄位，順序：設備/客戶/接機日/天數/已用天/業務/狀態。
# 用具名索引常數而非魔術數字，欄位順序若變動時一眼看得出要改哪個。
_COL_DEVICE = 0
_COL_CUSTOMER = 1
_COL_BORROW_DATE = 2
_COL_DAYS = 3
_COL_USED = 4
_COL_BUSINESS = 5
_COL_STATUS = 6
ROW_FIELD_COUNT = 7  # 每列欄位數：innerText 中一個列標記之後緊跟的 7 個非空行

# 這頁是 Streamlit，名單**不是** HTML <table>（實機實測 <table> 數=0、table tbody tr=0），
# 只能解析 body 的 innerText。每一列在 innerText 中以一行「列標記」起始，其後緊跟該列的
# 7 個欄位。列標記是 U+25CB（空心圓）。刻意用 ○ escape 而非字面字元，避免與字母 O
# 或 U+25EF（較大的空心圓）在原始碼裡肉眼混淆、被人手誤改成錯字元。
ROW_MARKER = "○"

# 版面健康檢查用的表頭標題：innerText 裡若這些字都找不到 → 判定頁面結構已變。
# 用「客戶」+「狀態」兩個表頭同時判斷，避免單一字誤命中資料列內容。
HEADER_TITLES = ("客戶", "狀態")

# 只收「狀態」欄含這個字的列。
TRIAL_ACTIVE_KEYWORD = "體驗中"


def match_recipients(trials, active_users, *, id_prefix="u"):
    """把體驗借出客戶名對到 active 使用者，回傳帶 match_status 的收件人清單（純函式）。

    參數
    ----
    trials : list[dict]
        每筆為 ``{"customer", "device", "borrow_date", "days", "used_days",
        "business", "status"}``（呼叫端已篩成「體驗中」）。後四欄只供 /trial-email
        表格顯示，缺漏時原樣帶空字串，不影響比對邏輯。
    active_users : list[dict]
        每筆為 ``{"id", "first_name", "last_name", "phone_number"}``。
        **呼叫端只該傳 status='active' 的使用者**（已 merged/deleted 的不該進來比對）。
    id_prefix : str
        比對成功時 id 的前綴，預設 "u"，例：user.id=46 → "u46"。

    比對規則
    --------
    客戶名用空白切成 tokens。頁面客戶欄格式為「first_name 空格 last_name」，
    因此**唯有剛好兩段**時才有意義去比對 users：
      - tokens[0] 對 first_name、tokens[1] 對 last_name。
      - 命中剛好 1 筆 → match_status="ok"，id=f"{id_prefix}{user.id}"、phone=該使用者電話。
      - 命中 ≥2 筆 → match_status="ambiguous"、phone=None（同名同姓無法唯一決定，
        不能亂填電話，否則會發簡訊給錯的人）。
    非兩段（單段機構名等）或兩段但 0 命中 → match_status="not_found"、phone=None。

    id 穩定性
    ---------
    非 ok 者用 ``f"loan-{index}"``（index 為該筆在 trials 的序位）。這保證同一份輸入
    每次產生的 id 穩定且不重複 —— consumer 端用 id 當 <option> 的 value，id 漂移或
    重複都會讓下拉選單選錯人。
    """
    recipients = []
    for index, trial in enumerate(trials):
        # 頁面上的原字串一律保留當顯示名：操作者在下拉選單看到的就是這個。
        customer = trial["customer"]
        # 先給非 ok 的保守預設；命中唯一使用者時才覆蓋。
        recipient_id = f"loan-{index}"
        phone = None
        match_status = "not_found"

        tokens = customer.split()
        if len(tokens) == 2:
            first_name, last_name = tokens[0], tokens[1]
            matched = [
                user
                for user in active_users
                if user["first_name"] == first_name and user["last_name"] == last_name
            ]
            if len(matched) == 1:
                user = matched[0]
                recipient_id = f"{id_prefix}{user['id']}"
                phone = user["phone_number"]
                match_status = "ok"
            elif len(matched) >= 2:
                # 同名同姓：能定位到「是體驗客戶」但無法定位「是哪個帳號」，不填電話。
                match_status = "ambiguous"

        recipients.append(
            {
                "id": recipient_id,
                "name": customer,
                "phone": phone,
                "device": trial.get("device"),
                "borrow_date": trial.get("borrow_date"),
                "match_status": match_status,
                # 以下四欄純顯示用（/trial-email 表格），原樣帶出；缺則空字串。
                # 與比對邏輯（id/phone/match_status）完全無關，不影響發送對象下拉。
                "days": trial.get("days", ""),
                "used_days": trial.get("used_days", ""),
                "business": trial.get("business", ""),
                "status": trial.get("status", ""),
            }
        )
    return recipients


def write_recipients_atomic(path, recipients, *, generated_at):
    """把 recipients 以原子替換寫入 path（先寫暫存檔再 os.replace）。

    為什麼不就地覆寫：consumer（web 下拉）隨時可能讀這個檔。就地 open("w") 會先把檔案
    截成 0 位元組再逐步寫入，中間任一刻被讀到就是半截的壞 JSON。改成「寫到同目錄暫存檔
    → os.replace」利用 OS 的原子 rename：讀者要嘛看到舊的完整檔、要嘛看到新的完整檔，
    永遠不會看到中間態。暫存檔刻意放在**同目錄**，確保與目標同一檔案系統（跨 FS 的
    rename 不是原子操作）。
    """
    target = os.fspath(path)
    payload = {"generated_at": generated_at, "recipients": recipients}
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    # 暫存檔名帶 pid：避免同機多個 producer 行程互相覆蓋彼此的暫存檔。
    tmp_path = f"{target}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(text)
        os.replace(tmp_path, target)
    except OSError:
        # 寫入或替換失敗時清掉殘留暫存檔，免得下次誤把它當半截產物或塞滿目錄。
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            # 清理本身再失敗也不該掩蓋原始錯誤，靜默略過清理、往上拋原始例外。
            pass
        raise


def scrape_active_trials(aihcr_url, *, timeout_ms=30000):
    """用 Playwright 開體驗借出頁，回傳「狀態＝體驗中」的列 [{customer, device, borrow_date}]。

    這是薄 I/O 封裝，走手動整合驗證（不寫單元測試）。

    **為什麼解析 innerText 而不是 DOM <table>**（實機踩坑）：這頁是 Streamlit，名單
    不是 HTML <table> —— 實測 `<table>` 數=0、`table tbody tr`=0，用 selector 等表格
    必然 30 秒逾時。Streamlit 把表格畫成非 <table> 結構，唯一穩定拿得到的是 body 的
    innerText：其中每一列以一行「○」(U+25CB) 起始，後面緊跟 7 個欄位。

    導覽步驟（實機驗證能成功切到體驗借出頁）：goto(networkidle) → 等 2.5s →
    點側邊欄「🎁 體驗借出管理」→ 等 3.5s（Streamlit rerun 是非同步的，沒有可靠的
    「渲染完成」事件可 wait，只能定額等待）→ 讀 body innerText。

    任一步驟逾時或點不到側邊欄 → 拋帶網址的 RuntimeError；解析階段找不到表頭 →
    同樣拋 RuntimeError（見 _parse_trials_from_body），不默默回空清單。
    """
    # 延遲 import：只想跑純函式測試的環境不必安裝 playwright（見模組 docstring）。
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                # networkidle：等 Streamlit 初始的 websocket/xhr 靜下來再動作。
                page.goto(aihcr_url, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_timeout(2500)
                # 點側邊欄項目讓主內容切到體驗借出管理。
                page.get_by_text(SIDEBAR_ITEM_TEXT, exact=False).first.click(timeout=timeout_ms)
                # Streamlit 點擊後是非同步 rerun，沒有可靠的「表格已渲染」事件，定額等待。
                page.wait_for_timeout(3500)
                body_text = page.inner_text("body")
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"爬體驗借出頁逾時（url={aihcr_url!r}）：頁面沒在 {timeout_ms}ms 內完成載入，"
            f"或點不到側邊欄「{SIDEBAR_ITEM_TEXT}」，頁面結構可能已改變。"
        ) from exc

    return _parse_trials_from_body(body_text, aihcr_url)


def _parse_trials_from_body(body_text, aihcr_url):
    """從 body innerText 解析出「體驗中」的列 [{customer, device, borrow_date}]。

    Streamlit 名單在 innerText 中的形狀（每列以一行「○」起始，後跟固定 7 欄）：
        設備 / 客戶 / 接機日 / 天數 / 已用天 / 業務 / 狀態
    因此以 ROW_MARKER 為分隔，取其後 ROW_FIELD_COUNT 個非空行為一列。

    **區分「版面壞掉」vs「真的 0 人體驗」**（reviewer 指出的靜默失效）：
    - 找不到表頭（HEADER_TITLES 有任何一個不在 innerText）→ 判定頁面結構已變，
      拋 RuntimeError。若這裡默默回 [] ，producer 會產出空名單、把 web 下拉清空，
      是最難察覺的靜默失效。
    - 表頭找得到、但沒有任何「體驗中」列 → 合法回 []（真的沒人在體驗）。
    """
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]

    missing_headers = [title for title in HEADER_TITLES if title not in lines]
    if missing_headers:
        raise RuntimeError(
            f"體驗借出頁結構已變（url={aihcr_url!r}）：innerText 找不到表頭 "
            f"{missing_headers}，無法定位名單表格（這不等於『0 人體驗』）。"
        )

    trials = []
    index = 0
    total = len(lines)
    while index < total:
        if lines[index] != ROW_MARKER:
            index += 1
            continue
        # ○ 之後緊跟的 7 個非空行是一列的欄位；不足 7 行代表尾端截斷，略過。
        fields = lines[index + 1 : index + 1 + ROW_FIELD_COUNT]
        if len(fields) < ROW_FIELD_COUNT:
            break
        # 走到這裡 fields 一定滿 7 格（上方 len(fields) < ROW_FIELD_COUNT 已 break），
        # 所以 _COL_DAYS(3)/_COL_USED(4)/_COL_BUSINESS(5)/_COL_STATUS(6) 索引都安全，
        # 不會 IndexError。天數/已用天/業務/狀態原樣帶下去（狀態保留 emoji 原字串，
        # 例「🟢 體驗中」），供 /trial-email 表格顯示用。
        if TRIAL_ACTIVE_KEYWORD in fields[_COL_STATUS]:
            trials.append(
                {
                    "customer": fields[_COL_CUSTOMER],
                    "device": fields[_COL_DEVICE],
                    "borrow_date": fields[_COL_BORROW_DATE],
                    "days": fields[_COL_DAYS],
                    "used_days": fields[_COL_USED],
                    "business": fields[_COL_BUSINESS],
                    "status": fields[_COL_STATUS],
                }
            )
        # 跳過整列（marker + 7 欄），避免把欄位值誤當下一個 marker 掃描。
        index += 1 + ROW_FIELD_COUNT
    return trials


def fetch_active_users(conn):
    """從 users 表撈出所有 status='active' 的使用者 [{id, first_name, last_name, phone_number}]。

    唯讀：只執行這一句固定字串 SELECT，無任何使用者輸入拼接、無寫入操作。
    """
    # 固定字串、無參數拼接 —— 沒有注入面，也沒有任何寫入語句。
    sql = (
        "SELECT id, first_name, last_name, phone_number "
        "FROM users WHERE status = 'active'"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    # DictCursor 已回傳 dict，dict(row) 只是防呼叫端換了 cursor 類型時形狀不一致。
    return [dict(row) for row in rows]


def connect_db():
    """用環境變數的憑證連 acfh_api MySQL，回傳 connection（呼叫端負責 close）。

    缺任何必要 env → 拋 RuntimeError（由 main 統一轉成 stderr 訊息 + 退出碼 1）。
    設了 connect/read timeout：內網 DB 若不通，不該讓 producer 無限期卡住。
    """
    import pymysql  # 延遲 import：純函式測試環境免安裝

    required = ["DB_HOST", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"缺少資料庫環境變數：{missing}")

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_DATABASE"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def main():
    """串起 scrape → 查 DB → 比對 → 原子寫檔的整支流程。"""
    import pymysql  # 延遲 import：讓 except 子句取得 pymysql.MySQLError

    try:
        aihcr_url = os.environ.get("AIHCR_URL", DEFAULT_AIHCR_URL)
        output_path = os.environ.get("RECIPIENTS_OUTPUT")
        if not output_path:
            # 不給預設輸出路徑：寫錯地方比不寫更糟（consumer 會讀到過期或空名單）。
            print(
                "錯誤：缺少環境變數 RECIPIENTS_OUTPUT（recipients.json 輸出路徑）",
                file=sys.stderr,
            )
            sys.exit(1)

        trials = scrape_active_trials(aihcr_url)

        conn = connect_db()
        try:
            active_users = fetch_active_users(conn)
        finally:
            # 不論查詢成敗都要關連線，避免占住 DB 連線數。
            conn.close()

        recipients = match_recipients(trials, active_users)

        # 帶時區的當地時間 ISO 字串（例 2026-07-30T11:00:00+08:00），與合約一致。
        generated_at = datetime.now(timezone.utc).astimezone().isoformat()
        write_recipients_atomic(output_path, recipients, generated_at=generated_at)

        matched = sum(1 for r in recipients if r["match_status"] == "ok")
        print(
            f"已產生 {len(recipients)} 筆收件人（可撥號 {matched} 筆）→ {output_path}"
        )
    except pymysql.MySQLError as exc:
        # 附上 host 但不印帳密：診斷夠用、不外洩憑證。
        print(
            f"錯誤：資料庫操作失敗（host={os.environ.get('DB_HOST')!r}）：{exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as exc:
        print(f"錯誤：檔案或網路 I/O 失敗：{exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        # scrape 逾時、缺 DB env 等都收斂成 RuntimeError。
        print(f"錯誤：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
