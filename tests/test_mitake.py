"""mitake 模組的離線測試。

**六個 test case 全部不碰真實 API**：不燒點數、不需要真實憑證、CI 也能跑。
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
