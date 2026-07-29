"""mitake 模組的離線測試。

**全部 test case 都不碰真實 API**：不燒點數、不需要真實憑證、CI 也能跑。
需要走到網路層的兩個 case（則數上限、狀態未確認）用替身接管 ``mitake._OPENER.open``，
既能驗證流程，又保證一則簡訊都不會真的送出去。

攔截點必須跟著 ``mitake._fetch_raw`` 走：模組為了禁止 redirect 已改用自建的
``_OPENER``（不再呼叫 ``urllib.request.urlopen``），若還 patch 舊的 ``urlopen``，
替身就攔不住，測試會建立**真實**網路連線並真的扣點。

所有回應樣本都是實撥時逐字抄下來的真實輸出。
"""

import sys
from pathlib import Path

import pytest

# mitake.py 在 repo 根、測試在 tests/ 子目錄，而 pytest 只會把 tests/ 放進 sys.path，
# 故手動補上根目錄，讓測試不論從哪個工作目錄執行都能 import。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mitake  # noqa: E402

# 實撥取得的真實回應樣本（逐字照抄，含 \r\n）。
SEND_SUCCESS_RESPONSE = "[1]\r\nmsgid=0313887539\r\nstatuscode=1\r\nAccountPoint=12572\r\n"
IP_BLOCKED_RESPONSE_BIG5 = "statuscode=k\r\nError=無效的連線位址\r\n".encode("big5")
# 三竹文件的「預約中」狀態：點數已扣、也給了 msgid，但不是實測過的成功碼 1。
UNCONFIRMED_RESPONSE_BIG5 = (
    "[1]\r\nmsgid=0313887539\r\nstatuscode=0\r\nAccountPoint=12572\r\n".encode("big5")
)

# 假憑證：只為了讓 _get_credentials 通過，不會被送到任何真實端點。
FAKE_CREDENTIALS = {"MITAKE_USERNAME": "dummy_user", "MITAKE_PASSWORD": "dummy_pass"}


class _NetworkReached(Exception):
    """替身用的哨兵：被丟出來就代表流程已經走到網路層。

    刻意不用 AssertionError —— 那會和 pytest 自己的斷言失敗混在一起，
    看不出到底是「測試預期的事發生了」還是「測試壞了」。
    """


class _FakeResponse:
    """最小可用的 ``_OPENER.open`` 回傳替身（需支援 context manager 與 read）。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _use_fake_credentials(monkeypatch):
    """設定假憑證，讓測試不依賴開發機上是否有真實環境變數。"""
    for name, value in FAKE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)


def test_parse_send_success_extracts_msgid_statuscode_and_point():
    """Happy path：SmSend 成功回應要能完整拆出 msgid / statuscode / 剩餘點數。

    這是整個模組最常走的路徑，也是 Web 介面要回顯給使用者的欄位來源；
    多行格式 + 開頭的 `[1]` 批次序號是最容易解析歪掉的地方。
    """
    result = mitake.parse_response(SEND_SUCCESS_RESPONSE)

    assert result["success"] is True
    assert result["msgid"] == "0313887539"
    assert result["statuscode"] == "1"
    assert result["account_point"] == 12572
    assert result["batch_index"] == "1"
    assert result["error"] is None
    # 原始欄位要保留，日後三竹加欄位時才追查得到。
    assert result["raw_fields"]["AccountPoint"] == "12572"


def test_count_sms_segments_boundaries():
    """Edge case：計費邊界。70 字 = 1 則、71 字 = 2 則，錯一格就報錯成本。

    這支函式的輸出會直接顯示給使用者當作「這封會扣幾點」的依據，
    而點數與 App 團隊共用，低估成本的後果是 App 驗證碼發不出去。
    """
    assert mitake.count_sms_segments("") == (0, 0)
    assert mitake.count_sms_segments("字") == (1, 1)
    assert mitake.count_sms_segments("字" * 69) == (1, 69)
    assert mitake.count_sms_segments("字" * 70) == (1, 70)
    assert mitake.count_sms_segments("字" * 71) == (2, 71)
    assert mitake.count_sms_segments("字" * 140) == (2, 140)
    assert mitake.count_sms_segments("字" * 141) == (3, 141)


def test_ip_blocked_response_decodes_and_parses_as_failure():
    """Error case：IP 被擋的完整鏈路 —— Big5 位元組 → 解碼 → 解析 → 判失敗 → 分類。

    一次鎖住三個最容易回歸的行為：Big5 解碼（用 UTF-8 硬解會亂碼）、
    錯誤回應必須判為失敗（若誤判成功，Web 介面會告訴使用者「已送出」但其實沒送）、
    以及 statuscode=k 要能被分類成 ip_blocked（這是換機器部署最常見的失敗，
    上層要靠它提示「去申請白名單」而不是叫人重打帳密）。

    另外驗證 possibly_charged 為 False：三竹明確拒絕代表沒收單、沒扣點，
    這種失敗是可以安全重送的，不該和「狀態未確認」混為一談。
    """
    text = mitake.decode_response(IP_BLOCKED_RESPONSE_BIG5)
    assert "無效的連線位址" in text  # Big5 有解對，沒變成亂碼

    result = mitake.parse_response(text)
    assert result["success"] is False
    assert result["statuscode"] == "k"
    assert result["error"] == "無效的連線位址"
    assert result["msgid"] is None
    assert mitake.classify_statuscode(result["statuscode"]) == mitake.KIND_IP_BLOCKED

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake._raise_if_failed(result, mitake.ENDPOINT_SEND)
    assert excinfo.value.is_ip_blocked is True
    assert excinfo.value.possibly_charged is False


def test_validate_phone_normalizes_and_rejects():
    """回歸鎖：validate_phone 是唯一會改寫使用者輸入的函式，且在 send_sms 第一行。

    這裡回歸的後果不是算錯字數，是**把簡訊送到另一個號碼** —— 扣了點，
    而且第三方收到內部訊息。兩條 regex 特別容易被後人「順手放寬」
    （例如為了支援市話把 ^09 改成 ^0），所以正反例都要釘死：
    0210869893（市話）必須被拒絕，這條斷言就是擋那個改動的。
    """
    assert mitake.validate_phone("0910869893") == "0910869893"
    assert mitake.validate_phone("0910-869-893") == "0910869893"
    assert mitake.validate_phone("0910 869 893") == "0910869893"
    # +886 是「轉換」不是「放行」：轉成三竹實測可行的 09 開頭格式。
    assert mitake.validate_phone("+886910869893") == "0910869893"
    assert mitake.validate_phone("886910869893") == "0910869893"

    for bad in ["", "0210869893", "091086989", "09108698931", "abc"]:
        with pytest.raises(mitake.MitakeValidationError):
            mitake.validate_phone(bad)


def test_send_sms_over_segment_limit_raises_before_any_network_call(monkeypatch):
    """成本護欄：超過則數上限必須在**送出網路請求之前**被擋下。

    重點不只是「有丟例外」，而是「一個位元組都沒送出去」—— 若擋得太晚，
    那 143 點早就扣掉了。故用替身接管 ``_OPENER.open`` 並記錄呼叫次數：
    calls 必須是空的，這才證明沒扣點。
    """
    _use_fake_credentials(monkeypatch)
    calls: list[object] = []

    # 替身以 ``_OPENER.open(request, timeout=...)`` 的形式被呼叫，故收 *args/**kwargs。
    def _spy_open(*args: object, **kwargs: object):
        calls.append(args)
        raise _NetworkReached("不該送出網路請求")

    monkeypatch.setattr(mitake._OPENER, "open", _spy_open)

    over_limit = "字" * (mitake.CHARS_PER_SEGMENT * mitake.MAX_SEGMENTS_PER_SEND + 1)
    with pytest.raises(mitake.MitakeValidationError) as excinfo:
        mitake.send_sms("0910869893", over_limit)

    assert "超過單次上限" in str(excinfo.value)
    assert "max_segments" in str(excinfo.value)  # 訊息要告訴使用者怎麼合法突破
    assert calls == []  # ← 本測試的核心：_OPENER.open 一次都沒被呼叫

    # 邊界另一側：恰好等於上限不該被擋，會一路走到網路層才撞上替身。
    at_limit = "字" * (mitake.CHARS_PER_SEGMENT * mitake.MAX_SEGMENTS_PER_SEND)
    with pytest.raises(_NetworkReached):
        mitake.send_sms("0910869893", at_limit)
    assert len(calls) == 1


def test_unconfirmed_statuscode_is_flagged_as_possibly_charged(monkeypatch):
    """重複扣點防線：沒見過的 statuscode 不可被講成「發送失敗」。

    statuscode=0（三竹文件的「預約中」）點數已經扣了、也回了 msgid。
    若例外訊息說「發送失敗」，使用者會再按一次 → 扣兩點、對方收到兩封。
    故這裡釘死三件事：kind 是 unconfirmed、possibly_charged 為 True、
    訊息含「請勿重送」字樣，讓 Web 層能渲染成和「可重試」不同的畫面。
    """
    _use_fake_credentials(monkeypatch)
    monkeypatch.setattr(
        mitake._OPENER,
        "open",
        lambda *args, **kwargs: _FakeResponse(UNCONFIRMED_RESPONSE_BIG5),
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.send_sms("0910869893", "測試訊息")

    error = excinfo.value
    assert error.kind == mitake.KIND_UNCONFIRMED
    assert error.is_unconfirmed is True
    assert error.possibly_charged is True
    assert error.statuscode == "0"
    assert "請勿重送" in str(error)
    assert "0313887539" in str(error)  # msgid 要出現，人才查得到後台


# --------------------------------------------------------------------------- #
# 投遞狀態查詢（SmQuery + msgid）—— 唯讀、免費、不扣點
# --------------------------------------------------------------------------- #

# 實測回應（Big5、Tab 分隔三欄）：msgid、狀態碼、狀態時間。
STATUS_DELIVERED_RESPONSE = "0315772761\t4\t20260729143730\r\n"
STATUS_PENDING_RESPONSE = "0315772761\t1\t20260729143700\r\n"

# 三竹官方文件 v2.09 的完整狀態碼表。**這份是測試自己抄的一份**，刻意不從
# mitake.DELIVERY_STATUS_TABLE 反推 —— 拿被測物去驗被測物，抄錯了兩邊會一起錯。
DOCUMENTED_STATUS_CODES = [
    ("0", "預約傳送中", mitake.DELIVERY_PENDING),
    ("1", "已送達業者", mitake.DELIVERY_PENDING),
    ("2", "已送達業者", mitake.DELIVERY_PENDING),
    ("3", "已送達業者", mitake.DELIVERY_PENDING),
    ("4", "已送達手機", mitake.DELIVERY_DELIVERED),
    ("5", "內容有錯誤", mitake.DELIVERY_FAILED),
    ("6", "門號有錯誤", mitake.DELIVERY_FAILED),
    ("7", "簡訊已停用", mitake.DELIVERY_FAILED),
    ("8", "逾時無送達", mitake.DELIVERY_FAILED),
    ("9", "預約已取消", mitake.DELIVERY_FAILED),
    ("*", "系統發生錯誤", mitake.DELIVERY_ERROR),
    ("a", "簡訊發送功能暫時停止", mitake.DELIVERY_ERROR),
    ("b", "簡訊發送功能暫時停止", mitake.DELIVERY_ERROR),
    ("c", "請輸入帳號", mitake.DELIVERY_ACCOUNT_ERROR),
    ("d", "請輸入密碼", mitake.DELIVERY_ACCOUNT_ERROR),
    ("e", "帳號、密碼錯誤", mitake.DELIVERY_ACCOUNT_ERROR),
    ("f", "帳號已過期", mitake.DELIVERY_ACCOUNT_ERROR),
    ("h", "帳號已被停用", mitake.DELIVERY_ACCOUNT_ERROR),
    ("k", "無效的連線位址", mitake.DELIVERY_ACCOUNT_ERROR),
    ("m", "必須變更密碼", mitake.DELIVERY_ACCOUNT_ERROR),
    ("n", "密碼已逾期", mitake.DELIVERY_ACCOUNT_ERROR),
    ("p", "無權限使用外部 HTTP 程式", mitake.DELIVERY_ACCOUNT_ERROR),
    ("r", "系統暫停服務", mitake.DELIVERY_ERROR),
    ("s", "帳務處理失敗", mitake.DELIVERY_ERROR),
    ("t", "簡訊已過期", mitake.DELIVERY_FAILED),
    ("u", "簡訊內容不得為空白", mitake.DELIVERY_FAILED),
    ("v", "無效的手機號碼", mitake.DELIVERY_FAILED),
]


def _fake_status_opener(monkeypatch, payload: bytes, calls: list | None = None):
    """把 ``_OPENER.open`` 換成回傳固定位元組的替身，並（可選）記下請求網址。"""

    def _open(*args: object, **kwargs: object):
        if calls is not None:
            # 第一個位置參數是 urllib.request.Request，帶完整 URL（含帳密）。
            calls.append(getattr(args[0], "full_url", ""))
        return _FakeResponse(payload)

    monkeypatch.setattr(mitake._OPENER, "open", _open)


@pytest.mark.parametrize(("code", "description", "category"), DOCUMENTED_STATUS_CODES)
def test_every_documented_status_code_maps_to_the_official_meaning(
    code: str, description: str, category: str
):
    """逐碼驗證：對照表是這個功能最容易抄錯的地方，一碼一個 case 釘死。

    抄錯的後果不是「顯示怪怪的」，是**把沒送到的講成送到了**（或反過來），
    而使用者對這頁的信任度是 100%（他就是為了查證才來的）。
    """
    assert mitake.describe_delivery_status(code) == (description, category)
    assert mitake.DELIVERY_STATUS_TABLE[code] == (description, category)


def test_status_table_has_exactly_the_documented_codes():
    """對照表不多不少：多出來的碼代表有人憑印象加了三竹沒定義的東西。"""
    assert set(mitake.DELIVERY_STATUS_TABLE) == {
        code for code, _, _ in DOCUMENTED_STATUS_CODES
    }


@pytest.mark.parametrize("code", ["0", "1", "2", "3"])
def test_delivered_to_carrier_is_never_reported_as_delivered_to_handset(code: str):
    """**本功能最重要的一條回歸鎖**：1–3 是「已送達業者」，不是「已送達手機」。

    1/2/3 只代表三竹把簡訊交給電信商了。若 is_delivered 誤判成 True，
    畫面會告訴使用者「對方已收到」，而他手機根本沒響 —— 這正是這個功能
    要解決的問題，做壞了等於把原本的「無從查證」升級成「被明確騙了」。
    """
    parsed = mitake.parse_status_response(f"0315772761\t{code}\t20260729143730")
    assert parsed["is_delivered"] is False
    assert parsed["category"] == mitake.DELIVERY_PENDING
    assert parsed["is_final"] is False
    assert parsed["description"] != "已送達手機"


def test_only_statuscode_4_is_delivered():
    """反向釘死：整張表裡只有 4 會讓 is_delivered 為 True。"""
    delivered = [
        code
        for code in mitake.DELIVERY_STATUS_TABLE
        if mitake.parse_status_response(f"1\t{code}\t20260729143730")["is_delivered"]
    ]
    assert delivered == [mitake.DELIVERED_STATUSCODE] == ["4"]


@pytest.mark.parametrize(
    ("code", "expected_final"),
    [("0", False), ("1", False), ("4", True), ("6", True), ("*", False), ("k", False)],
)
def test_is_final_only_for_delivered_and_failed(code: str, expected_final: bool):
    """最終狀態＝再查也不會變。系統／帳戶錯誤不算最終（稍後再查可能就有答案）。"""
    parsed = mitake.parse_status_response(f"0315772761\t{code}\t20260729143730")
    assert parsed["is_final"] is expected_final


def test_parse_status_response_happy_path():
    """Happy path：Tab 分隔三欄要完整拆出，且 raw_text 原樣保留。"""
    parsed = mitake.parse_status_response(STATUS_DELIVERED_RESPONSE)

    assert parsed["msgid"] == "0315772761"
    assert parsed["statuscode"] == "4"
    assert parsed["description"] == "已送達手機"
    assert parsed["category"] == mitake.DELIVERY_DELIVERED
    assert parsed["status_time"] == "20260729143730"
    assert parsed["is_delivered"] is True
    assert parsed["is_final"] is True
    assert parsed["raw_text"] == STATUS_DELIVERED_RESPONSE


def test_parse_status_response_tolerates_extra_whitespace_and_blank_lines():
    """欄位前後的空白、開頭空行、\\r\\n 都不該讓解析失敗（中間層改寫過的回應）。"""
    parsed = mitake.parse_status_response(
        "\r\n  0315772761 \t 4 \t 20260729143730 \r\n\r\n"
    )
    assert parsed["msgid"] == "0315772761"
    assert parsed["statuscode"] == "4"
    assert parsed["status_time"] == "20260729143730"


def test_parse_status_response_falls_back_to_whitespace_split():
    """Tab 被中間層換成空白時仍要解得出來（退一步總比整筆判壞好）。"""
    parsed = mitake.parse_status_response("0315772761 4 20260729143730")
    assert (parsed["msgid"], parsed["statuscode"]) == ("0315772761", "4")


def test_parse_status_response_without_time_field():
    """Edge case：只有兩欄（沒有狀態時間）仍可用，status_time 為 None。"""
    parsed = mitake.parse_status_response("0315772761\t4")
    assert parsed["statuscode"] == "4"
    assert parsed["status_time"] is None
    assert parsed["is_delivered"] is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \r\n \r\n",
        "0315772761",  # 欄位不足：只有 msgid
        "\t4\t20260729143730",  # msgid 是空的
        "AccountPoint=12571\r\n",  # 查餘額的 key=value 格式，不是狀態回應
    ],
)
def test_parse_status_response_refuses_to_guess(text: str):
    """格式不符一律丟例外，**不湊出一個看起來像結果的東西**。

    湊出來的狀態會被畫面當成三竹說的顯示出去，而使用者無從分辨那是真的還是我們猜的。
    另外驗 possibly_charged 為 False：這是唯讀查詢，任何失敗都沒扣點。
    """
    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.parse_status_response(text)
    assert excinfo.value.possibly_charged is False


def test_parse_status_response_rejects_non_str():
    """型別錯是輸入問題（MitakeValidationError），不是 API 問題。"""
    with pytest.raises(mitake.MitakeValidationError):
        mitake.parse_status_response(b"0315772761\t4\t20260729143730")


def test_parse_status_response_unknown_code_is_never_delivered():
    """沒收錄的碼要老實說不知道，絕不猜成「已送達」。"""
    parsed = mitake.parse_status_response("0315772761\tZ\t20260729143730")
    assert parsed["category"] == mitake.DELIVERY_UNKNOWN
    assert parsed["is_delivered"] is False
    assert parsed["is_final"] is False
    assert "未知" in parsed["description"]


def test_validate_msgid_accepts_and_strips():
    """合法 msgid：只去前後空白，內容不改寫。"""
    assert mitake.validate_msgid("0315772761") == "0315772761"
    assert mitake.validate_msgid("  0315772761 \n") == "0315772761"
    assert mitake.validate_msgid("Ab_9-0") == "Ab_9-0"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        None,
        12345,
        "0315772761&password=x",  # 想在 query string 裡多塞參數
        "0315 772761",  # 中間有空白
        "中文",
        "031577\r\n2761",  # 換行注入
        "x" * 65,  # 超過長度上限
    ],
)
def test_validate_msgid_rejects_bad_input(bad: object):
    """msgid 會被原樣拼進 query string，字元集收緊是防止改寫我們送出去的參數。"""
    with pytest.raises(mitake.MitakeValidationError):
        mitake.validate_msgid(bad)


def test_query_message_status_happy_path_uses_smquery_and_msgid(monkeypatch):
    """Happy path：查詢要打 SmQuery（唯讀免費），且帶上 msgid 參數。

    順便釘死「絕不可以改成走 SmSend」——那個端點每次呼叫都會扣點。
    """
    _use_fake_credentials(monkeypatch)
    urls: list[str] = []
    _fake_status_opener(monkeypatch, STATUS_DELIVERED_RESPONSE.encode("big5"), urls)

    status = mitake.query_message_status("0315772761")

    assert status["statuscode"] == "4"
    assert status["is_delivered"] is True
    assert len(urls) == 1
    assert "SmQuery" in urls[0]
    assert "SmSend" not in urls[0]  # ← 走錯端點就是每查一次扣一點
    assert "msgid=0315772761" in urls[0]


def test_query_message_status_pending_is_not_delivered(monkeypatch):
    """整合視角：statuscode=1 走完整條鏈路後仍必須是「還沒到手機」。"""
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(monkeypatch, STATUS_PENDING_RESPONSE.encode("big5"))

    status = mitake.query_message_status("0315772761")

    assert status["description"] == "已送達業者"
    assert status["is_delivered"] is False
    assert status["is_final"] is False


def test_query_message_status_validates_before_any_network_call(monkeypatch):
    """非法 msgid 必須在送出請求**之前**被擋下（一個位元組都不該送出去）。"""
    _use_fake_credentials(monkeypatch)
    calls: list[object] = []

    def _spy_open(*args: object, **kwargs: object):
        calls.append(args)
        raise _NetworkReached("不該送出網路請求")

    monkeypatch.setattr(mitake._OPENER, "open", _spy_open)

    with pytest.raises(mitake.MitakeValidationError):
        mitake.query_message_status("")
    with pytest.raises(mitake.MitakeValidationError):
        mitake.query_message_status("bad msgid")
    assert calls == []


def test_query_message_status_maps_key_value_auth_error(monkeypatch):
    """帳密錯時三竹回的是 key=value 格式，要走既有的 auth_failed 分類。

    上層靠 kind 決定畫面要說「去改設定」還是「稍後再試」，
    誤分類的話使用者會一直重查一個永遠不會成功的東西。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch, "statuscode=e\r\nError=帳號、密碼錯誤\r\n".encode("big5")
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status("0315772761")

    assert excinfo.value.is_auth_failed is True
    assert excinfo.value.possibly_charged is False  # 唯讀查詢，沒扣點


def test_query_message_status_maps_tab_form_account_error(monkeypatch):
    """帳戶錯誤碼若出現在 Tab 格式裡，同樣要當成設定問題丟例外。

    ``k``（IP 不在白名單）代表三竹**根本沒去查**這則簡訊。把它當成查詢結果回傳，
    畫面就會寫成「這則簡訊的狀態是：無效的連線位址」—— 那不是這則簡訊的狀態。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(monkeypatch, "0315772761\tk\t20260729143730\r\n".encode("big5"))

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status("0315772761")

    assert excinfo.value.is_ip_blocked is True
    assert excinfo.value.possibly_charged is False
    assert "無效的連線位址" in str(excinfo.value)


def test_query_message_status_returns_system_error_without_raising(monkeypatch):
    """系統類狀態碼（三竹那端暫時異常）照常回傳，讓畫面能說「稍後再查」。

    與帳戶類錯誤刻意不同調：帳戶錯誤要去改設定（丟例外），
    系統錯誤只要等（回傳結果），把後者也丟成例外會把人導去改一個沒壞的設定。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(monkeypatch, "0315772761\t*\t20260729143730\r\n".encode("big5"))

    status = mitake.query_message_status("0315772761")

    assert status["category"] == mitake.DELIVERY_ERROR
    assert status["is_delivered"] is False
    assert status["is_final"] is False


def test_query_message_status_wraps_unparseable_response(monkeypatch):
    """三竹改了格式時要明確失敗，且標明沒扣點（唯讀查詢可安全重查）。"""
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(monkeypatch, "???".encode("big5"))

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status("0315772761")
    assert excinfo.value.possibly_charged is False
