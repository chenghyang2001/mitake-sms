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
    """最小可用的 ``_OPENER.open`` 回傳替身（需支援 context manager 與 read）。

    **有狀態**：`read(n)` 會從目前位置往後給、讀完回 `b""`，和真的
    `http.client.HTTPResponse` 一樣。這不是為了好看 —— `mitake._read_capped`
    是「一直讀到回空為止」的迴圈，若替身每次都把整段 payload 再給一次，迴圈永遠
    讀不到結尾，測試會拿到一段被重複串接的假資料（而且看起來很像成功）。

    `amt=None` 代表「全部讀完」，與標準檔案物件語意一致。
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._pos = 0

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            chunk = self._payload[self._pos :]
        else:
            chunk = self._payload[self._pos : self._pos + max(0, amt)]
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _ShortReadResponse(_FakeResponse):
    """每次 `read()` 最多只給 `chunk_size` 個位元組的替身。

    存在的理由：`read(n)` 的契約是「**最多** n 個位元組」。若 `_read_capped` 寫成
    單次 `read(limit)` 就收工，遇到會短讀的檔案物件時會**靜默截斷**回應 ——
    而截斷後的 `AccountPoi` 會被解析成「查不到餘額」，正是每天 08:00 那個餘額
    告警排程最不該收到的假訊號（它會發出一則不存在的低點數警報）。
    """

    def __init__(self, payload: bytes, *, chunk_size: int = 3) -> None:
        super().__init__(payload)
        self._chunk_size = chunk_size

    def read(self, amt: int | None = None) -> bytes:
        limit = self._chunk_size if amt is None else min(amt, self._chunk_size)
        return super().read(limit)


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


# --------------------------------------------------------------------------- #
# 身分驗證：三竹回的必須是「你問的那則」
# --------------------------------------------------------------------------- #
#
# 這一段鎖的是 mitake.py 區段鐵律的第二半（第一半「格式異常拋錯」在上面）。
# 第一版把身分不符寫成 logger.warning 就放行，實測後果：查 0315772761、三竹回
# 9999999999，畫面整頁綠色「已送達手機」，而使用者查的 msgid 一個字都沒出現。
# 反向更貴：那則其實已送達、畫面卻說「沒送到可以重發」→ 多扣一點 + 對方收到兩封。

FOREIGN_MSGID = "9999999999"
QUERIED_MSGID = "0315772761"


def test_foreign_msgid_is_rejected_instead_of_shown_as_this_messages_status(monkeypatch):
    """**MUST_FIX 的主鎖**：三竹回別則簡訊的狀態時必須拋錯，不可回傳。

    刻意用 statuscode=4（已送達手機）當樣本 —— 那是最貴的誤報：畫面會用綠色的
    成功樣式說「對方已收到」，使用者於是不再追，而他真正問的那則可能根本沒送到。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch, f"{FOREIGN_MSGID}\t4\t20260729143730\r\n".encode("big5")
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status(QUERIED_MSGID)

    assert excinfo.value.kind == mitake.KIND_MSGID_MISMATCH
    # 查詢走唯讀免費的 SmQuery，從來不扣點。寫成 True 會讓畫面說「請勿重送」，
    # 而重查一次根本沒有代價。
    assert excinfo.value.possibly_charged is False


def test_foreign_msgid_never_produces_a_delivered_result(monkeypatch):
    """反向釘死：那條路徑上**不可能**產出 is_delivered=True 的回傳值。

    與上一支的差別在斷言對象：上一支驗「有拋錯」，這支驗「沒有任何結果溜出來」。
    若日後有人把 raise 改回 warning，這裡的 result 會被指派，斷言就抓得到。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch, f"{FOREIGN_MSGID}\t4\t20260729143730\r\n".encode("big5")
    )

    result = "沒有回傳值"
    try:
        result = mitake.query_message_status(QUERIED_MSGID)
    except mitake.MitakeAPIError:
        pass

    assert result == "沒有回傳值"


def test_mismatch_error_names_both_msgids(monkeypatch):
    """錯誤訊息必須**同時**帶查詢的與回傳的 msgid。

    只講一個等於沒講：使用者無從判斷「這是我那則嗎」。兩個數字並排放，
    他一眼就看得出不是自己那則，也才知道要拿哪個號碼去三竹後台核對。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch, f"{FOREIGN_MSGID}\t4\t20260729143730\r\n".encode("big5")
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status(QUERIED_MSGID)

    message = str(excinfo.value)
    assert QUERIED_MSGID in message
    assert FOREIGN_MSGID in message


@pytest.mark.parametrize("code", ["0", "1", "4", "6", "8", "*", "Z"])
def test_mismatch_is_rejected_regardless_of_the_status_code(monkeypatch, code: str):
    """身分不符時，**不論那則的狀態碼是什麼**都要拒絕。

    包含 ``*``（系統錯誤）與 ``Z``（未知碼）—— 那兩種在 msgid 相符時是照常回傳的，
    所以這裡確認拒絕的理由是「身分」而不是「狀態」。
    帳戶類的碼（k/e…）不在此列：它們在更前面就以設定問題拋出了，是另一條路徑。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch, f"{FOREIGN_MSGID}\t{code}\t20260729143730\r\n".encode("big5")
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status(QUERIED_MSGID)
    assert excinfo.value.kind == mitake.KIND_MSGID_MISMATCH


def test_multiline_response_with_a_foreign_first_line_is_rejected(monkeypatch):
    """多行回應且第一行是別則 —— 不可靜默取第一行當答案。

    parse_status_response 只讀第一個非空行。若身分不檢查，這種回應會讓畫面顯示
    第一行那則的狀態，而使用者問的那則（第二行）明明就在同一份回應裡。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch,
        (
            f"{FOREIGN_MSGID}\t8\t20260729143730\r\n"
            f"{QUERIED_MSGID}\t4\t20260729143731\r\n"
        ).encode("big5"),
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status(QUERIED_MSGID)
    assert excinfo.value.kind == mitake.KIND_MSGID_MISMATCH


def test_matching_msgid_path_is_unchanged(monkeypatch):
    """回歸：msgid 相符時一切照舊 —— 身分檢查不可波及正常路徑。"""
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(monkeypatch, STATUS_DELIVERED_RESPONSE.encode("big5"))

    status = mitake.query_message_status(QUERIED_MSGID)

    assert status["msgid"] == QUERIED_MSGID
    assert status["statuscode"] == "4"
    assert status["is_delivered"] is True
    assert status["is_final"] is True


def test_surrounding_whitespace_in_the_query_is_not_a_mismatch(monkeypatch):
    """Edge case：使用者貼上時多了空白，正規化後相符就不算身分不符。

    validate_msgid 會去掉前後空白，比對用的必須是正規化後的值 —— 拿原始輸入去比，
    每個複製貼上的人都會撞到一個「三竹回了別則簡訊」的假警報。
    """
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(monkeypatch, STATUS_DELIVERED_RESPONSE.encode("big5"))

    status = mitake.query_message_status(f"  {QUERIED_MSGID}  ")

    assert status["msgid"] == QUERIED_MSGID
    assert status["is_delivered"] is True


# --------------------------------------------------------------------------- #
# 回應大小上限（_fetch_raw）—— 最需要小心的回歸面
# --------------------------------------------------------------------------- #
#
# _fetch_raw 是 query_balance / send_sms / query_message_status 三者共用的底層，
# 而 query_balance 每天 08:00 被 n8n2vps-hub 的餘額告警排程呼叫。加上限的前提是
# 「正常大小的回應逐位元組不變」，故這一段的回歸鎖比新行為的鎖還多。


def _fake_opener_with(monkeypatch, response_factory):
    """把 ``_OPENER.open`` 換成「每次呼叫都產生一個新 response 物件」的替身。

    每次都新建是必要的：response 有讀取位置，共用同一個物件的話第二次呼叫會從
    上次讀完的位置繼續，拿到空回應。
    """
    monkeypatch.setattr(mitake._OPENER, "open", lambda *a, **k: response_factory())


@pytest.mark.parametrize(
    "size",
    [0, 1, 19, 1024, mitake.MAX_RESPONSE_BYTES - 1, mitake.MAX_RESPONSE_BYTES],
)
def test_response_at_or_under_the_cap_is_returned_byte_for_byte(monkeypatch, size: int):
    """**最重要的回歸鎖**：沒超過上限的回應，拿到的位元組必須與加上限之前完全相同。

    含邊界值 MAX_RESPONSE_BYTES 本身（剛好等於上限＝仍然合法）。少一個位元組、
    多一個位元組都算失敗 —— 靜默截斷的回應解析出來會是「查不到餘額」，
    而那個排程收到的會是一則不存在的低點數警報。
    """
    _use_fake_credentials(monkeypatch)
    payload = b"A" * size
    _fake_opener_with(monkeypatch, lambda: _FakeResponse(payload))

    assert mitake._fetch_raw(mitake.ENDPOINT_QUERY, {}, 5.0) == payload


def test_read_capped_reassembles_short_reads():
    """``read(n)`` 允許短讀，``_read_capped`` 必須把碎片拼回完整內容。

    單次 ``read(limit)`` 的寫法在這裡只會拿到前 3 個位元組，
    而那正是「靜默截斷」這個最壞情況的縮影。
    """
    payload = "AccountPoint=12571\r\n".encode("big5")
    assert (
        mitake._read_capped(_ShortReadResponse(payload, chunk_size=3), 65_537) == payload
    )


def test_query_balance_still_works_through_the_capped_reader(monkeypatch):
    """整合回歸：每天 08:00 那條路徑（query_balance）必須完全不受影響。"""
    _use_fake_credentials(monkeypatch)
    _fake_opener_with(
        monkeypatch, lambda: _FakeResponse("AccountPoint=12571\r\n".encode("big5"))
    )

    assert mitake.query_balance() == 12571


def test_query_balance_survives_a_short_reading_connection(monkeypatch):
    """整合回歸（短讀版）：連線一次只給幾個位元組時，餘額仍要正確讀出來。"""
    _use_fake_credentials(monkeypatch)
    _fake_opener_with(
        monkeypatch,
        lambda: _ShortReadResponse(
            "AccountPoint=12571\r\n".encode("big5"), chunk_size=4
        ),
    )

    assert mitake.query_balance() == 12571


def test_oversized_response_is_rejected_not_truncated(monkeypatch):
    """超過上限就拋錯，**不截斷後照常解析**。

    截頭去尾的一大坨 HTML（代理器錯誤頁、DNS 被劫持）解出來的任何「狀態」
    都是憑空捏造的，而畫面會把它當成三竹說的顯示出去。
    """
    _use_fake_credentials(monkeypatch)
    oversized = b"<html>" + b"x" * mitake.MAX_RESPONSE_BYTES
    _fake_opener_with(monkeypatch, lambda: _FakeResponse(oversized))

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake._fetch_raw(mitake.ENDPOINT_QUERY, {}, 5.0)

    assert excinfo.value.kind == mitake.KIND_BAD_RESPONSE
    assert excinfo.value.possibly_charged is False  # 唯讀查詢，沒扣點


def test_oversized_response_error_message_stays_small(monkeypatch):
    """錯誤訊息本身不可以變成新的問題：20 萬字的回應不該產生 20 萬字的錯誤頁。"""
    _use_fake_credentials(monkeypatch)
    _fake_opener_with(monkeypatch, lambda: _FakeResponse(b"x" * 200_000))

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake._fetch_raw(mitake.ENDPOINT_QUERY, {}, 5.0)

    assert len(str(excinfo.value)) < 500


def test_oversized_response_on_the_send_path_is_flagged_possibly_charged(monkeypatch):
    """發送路徑上拿到超大回應 ＝ 請求已送達三竹但看不懂回應 → 別重送。

    與查詢路徑刻意不同調：查詢免費（possibly_charged=False、可安全重查），
    發送已經花了錢（True、要去後台以 msgid 查證）。搞反任一邊都會讓人多扣一次點。
    """
    _use_fake_credentials(monkeypatch)
    _fake_opener_with(monkeypatch, lambda: _FakeResponse(b"x" * 200_000))

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake._fetch_raw(mitake.ENDPOINT_SEND, {}, 5.0)

    assert excinfo.value.possibly_charged is True
    assert excinfo.value.kind == mitake.KIND_UNCONFIRMED


def test_max_response_bytes_is_64_kib():
    """上限值本身釘死：改動它等於改動所有三竹回應的接受範圍，要有人明確決定。"""
    assert mitake.MAX_RESPONSE_BYTES == 64 * 1024


def test_long_unparseable_response_is_truncated_in_the_error_message():
    """解析失敗時，錯誤訊息裡的回應原文要截斷並明講「已截斷」。

    不講的話，看到訊息的人會以為三竹只回了那 200 個字，往錯的方向排查。
    """
    text = "x" * 5_000

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.parse_status_response(text)

    message = str(excinfo.value)
    assert "已截斷" in message
    assert "5000" in message  # 原文長度要講出來
    assert len(message) < 500


def test_short_unparseable_response_is_shown_verbatim():
    """回歸：短回應的錯誤訊息一字不改 —— 截斷邏輯不可波及正常大小的診斷資訊。"""
    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.parse_status_response("???")

    message = str(excinfo.value)
    assert "'???'" in message
    assert "已截斷" not in message


# --------------------------------------------------------------------------- #
# 狀態碼大小寫正規化
# --------------------------------------------------------------------------- #
#
# 實測 `K` 會落到「無法辨識的狀態碼」，把「IP 不在白名單」這個純設定問題講成
# 「這則簡訊狀態不明」—— 使用者去追一則根本沒問題的簡訊，而該做的事（寄信給三竹）
# 沒人去做。方向雖保守（畫面仍寫「請不要假設對方已經收到」），但誤導成本是實的。


@pytest.mark.parametrize(
    ("upper", "lower"), [("K", "k"), ("E", "e"), ("A", "a"), ("V", "v"), ("T", "t")]
)
def test_uppercase_status_codes_map_to_the_same_meaning(upper: str, lower: str):
    """大寫變體查表結果必須與小寫完全相同（說明與分類都是）。"""
    assert mitake.describe_delivery_status(upper) == mitake.describe_delivery_status(
        lower
    )


def test_status_code_normalization_does_not_invent_meanings():
    """回歸：正規化只接住大小寫變體，**不會**把真的未知碼猜成某個已知碼。"""
    assert mitake.describe_delivery_status("Z")[1] == mitake.DELIVERY_UNKNOWN
    assert mitake.describe_delivery_status("zz")[1] == mitake.DELIVERY_UNKNOWN
    assert mitake.describe_delivery_status(None)[1] == mitake.DELIVERY_UNKNOWN


def test_original_statuscode_is_never_rewritten_by_normalization():
    """**正規化只用於查表**：回傳給呼叫端的 statuscode 必須是三竹回的原值。

    改寫了的話，日後想核對「三竹到底回了什麼」會核對到我們自己改過的版本，
    log 與稽核就失去了對帳能力。
    """
    parsed = mitake.parse_status_response(f"{QUERIED_MSGID}\tK\t20260729143730")

    assert parsed["statuscode"] == "K"  # 原值，不是 "k"
    assert parsed["category"] == mitake.DELIVERY_ACCOUNT_ERROR
    assert parsed["description"] == "無效的連線位址"
    assert parsed["is_delivered"] is False


@pytest.mark.parametrize(
    ("code", "expected_kind"),
    [
        ("k", mitake.KIND_IP_BLOCKED),
        ("K", mitake.KIND_IP_BLOCKED),
        ("e", mitake.KIND_AUTH_FAILED),
        ("E", mitake.KIND_AUTH_FAILED),
        ("1", mitake.KIND_API),
        ("z", mitake.KIND_API),
        (None, mitake.KIND_API),
    ],
)
def test_classify_statuscode_normalizes_too(code, expected_kind: str):
    """classify_statuscode 必須與 describe_delivery_status 同步正規化。

    只修其中一邊會製造新的矛盾：``K`` 被 describe 判成「帳戶設定問題」而拋例外，
    kind 卻是 api → 畫面顯示的是「稍後再查」的一般錯誤頁，而不是「去申請 IP
    白名單」那頁。使用者照著重查一百次也不會成功。
    """
    assert mitake.classify_statuscode(code) == expected_kind


def test_uppercase_account_error_reaches_the_ip_blocked_branch(monkeypatch):
    """整合：Tab 格式裡的大寫 ``K`` 要一路走到 ip_blocked，畫面才會導向申請白名單。"""
    _use_fake_credentials(monkeypatch)
    _fake_status_opener(
        monkeypatch, f"{QUERIED_MSGID}\tK\t20260729143730\r\n".encode("big5")
    )

    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.query_message_status(QUERIED_MSGID)

    assert excinfo.value.is_ip_blocked is True
    assert excinfo.value.possibly_charged is False
    assert excinfo.value.statuscode == "K"  # 原值仍保留給稽核


@pytest.mark.parametrize("text", ["", "0315772761", "AccountPoint=12571\r\n"])
def test_format_errors_are_bad_response_not_generic_api(text: str):
    """格式解不開屬於 bad_response（重試無用），不是可重試的一般 api 錯誤。

    kind 決定畫面文案：講成可重試，使用者會反覆重整到放棄，
    而「拿 msgid 去三竹後台核對」這句真正的出路一直沒被講清楚。
    """
    with pytest.raises(mitake.MitakeAPIError) as excinfo:
        mitake.parse_status_response(text)
    assert excinfo.value.kind == mitake.KIND_BAD_RESPONSE
    assert excinfo.value.possibly_charged is False
