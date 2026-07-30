"""tools/build_recipients.py 純函式 match_recipients 的離線回歸鎖。

只測純比對邏輯：不連 AIHCR 體驗借出頁、不連 MySQL、不碰檔案系統。
（scrape_active_trials / fetch_active_users / connect_db 是薄 I/O 封裝，
走手動整合驗證，不在本檔範圍。）

本檔刻意不 import pymysql / playwright：build_recipients.py 把這兩個第三方套件
延遲到函式內 import，因此匯入純函式做單元測試時完全不需要它們。
"""

from tools.build_recipients import match_recipients


def test_two_token_customer_with_single_match_is_ok() -> None:
    """兩段客戶名剛好對到 1 位 active 使用者 → ok，id 帶前綴、電話取自該使用者。

    這是最主要的 happy path：頁面客戶「筱琪 陳」對到 first='筱琪'/last='陳' 的唯一帳號，
    consumer 端才拿得到能撥號的電話。name 必須維持頁面原字串（操作者看得懂的顯示名）。
    """
    trials = [
        {"customer": "筱琪 陳", "device": "體驗活動14天-陳筱琪4c74", "borrow_date": "2026-07-29"}
    ]
    active_users = [
        {"id": 46, "first_name": "筱琪", "last_name": "陳", "phone_number": "0918123424"},
    ]

    result = match_recipients(trials, active_users)

    assert len(result) == 1
    row = result[0]
    assert row["match_status"] == "ok"
    assert row["id"] == "u46"
    assert row["phone"] == "0918123424"
    assert row["name"] == "筱琪 陳"


def test_two_token_customer_with_multiple_matches_is_ambiguous() -> None:
    """兩段客戶名對到 ≥2 位同名同姓 active 使用者 → ambiguous，電話必須為 None。

    同名同姓時無法唯一決定是哪個帳號，若亂填其中一支電話，就會把簡訊發給錯的人 ——
    這是絕不能填電話的核心理由。
    """
    trials = [
        {"customer": "青 蘋果", "device": "體驗活動14天-青蘋果", "borrow_date": "2026-07-20"}
    ]
    active_users = [
        {"id": 43, "first_name": "青", "last_name": "蘋果", "phone_number": "0911111111"},
        {"id": 99, "first_name": "青", "last_name": "蘋果", "phone_number": "0922222222"},
    ]

    result = match_recipients(trials, active_users)

    row = result[0]
    assert row["match_status"] == "ambiguous"
    assert row["phone"] is None


def test_single_token_org_name_is_not_found_with_unique_loan_id() -> None:
    """單段機構名（無對應使用者）→ not_found、電話 None，且 id 是穩定唯一的 loan-*。

    consumer 用 id 當下拉 <option> 的 value，非 ok 者的 id 必須穩定不重複，
    否則會選錯人。這裡驗它落在 loan- 命名空間、不與 ok 的 u<id> 撞。
    """
    trials = [
        {"customer": "青化活動中心", "device": "體驗活動14天-青化", "borrow_date": "2026-07-18"}
    ]
    active_users = [
        {"id": 46, "first_name": "筱琪", "last_name": "陳", "phone_number": "0918123424"},
    ]

    result = match_recipients(trials, active_users)

    row = result[0]
    assert row["match_status"] == "not_found"
    assert row["phone"] is None
    assert row["id"].startswith("loan-")
