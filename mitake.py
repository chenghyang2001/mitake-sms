"""三竹簡訊（Mitake）API 核心模組。

提供「查詢餘額」與「發送簡訊」兩項能力，是後續 Web 介面與餘額告警排程的共用底層。

設計前提（違反任一項，程式寫得再正確也打不通）：

1. **IP 白名單**：三竹強制檢查來源 IP，未登記的位址一律回 ``statuscode=k`` /
   ``無效的連線位址``，與帳密是否正確**無關**。換機器部署前必須先向三竹申請。
   驗證方式是跑一次 :func:`query_balance`（唯讀、免費、不燒點數）。
2. **憑證只從環境變數讀**：``MITAKE_USERNAME`` / ``MITAKE_PASSWORD``。
   本模組所有例外訊息與 log 都只記 endpoint 名稱，**不記完整 query string**
   （帳密就在 query string 裡，記下去等同外洩）。
3. **回應是 Big5 編碼**：UTF-8 硬解中文會亂碼，故一律走 :func:`decode_response`。

成本警告：點數與 App 團隊共用（App 靠它發手機驗證碼）。:func:`send_sms` 每呼叫一次
就從共用池扣點，且本模組**刻意不實作自動重試**——失敗時是否重送必須由人判讀
statuscode 後決定。自動重試會把「其實已送出、只是回應碼沒見過」的情況重複扣款，
而簡訊沒送到的損失遠小於 App 驗證碼因點數見底而發不出去。

只用標準庫實作（urllib），VPS 部署零外部依賴。

直接執行本檔（``python mitake.py``）只會跑純函式的自我檢查，**不會呼叫真實 API**、
不會扣點。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = [
    "BASE_URL",
    "CHARS_PER_SEGMENT",
    "DEFAULT_TIMEOUT_SECONDS",
    "KIND_API",
    "KIND_AUTH_FAILED",
    "KIND_DECODE",
    "KIND_IP_BLOCKED",
    "KIND_NETWORK",
    "MitakeAPIError",
    "MitakeConfigError",
    "MitakeError",
    "MitakeValidationError",
    "classify_statuscode",
    "count_sms_segments",
    "decode_response",
    "parse_response",
    "query_balance",
    "send_sms",
    "validate_phone",
]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常數
# --------------------------------------------------------------------------- #

BASE_URL = "https://smsapi.mitake.com.tw/api/mtk/"
ENDPOINT_QUERY = "SmQuery"
ENDPOINT_SEND = "SmSend"

ENV_USERNAME = "MITAKE_USERNAME"
ENV_PASSWORD = "MITAKE_PASSWORD"

# 25 秒：三竹正常回應在 1 秒內，設這麼長只是為了容忍 VPS 偶發的網路抖動。
# 不設無限等待，否則 Web 介面的請求執行緒會被卡死。
DEFAULT_TIMEOUT_SECONDS = 25.0

# 中文簡訊走 UCS-2，每則上限 70 字。純英數其實可到 160 字，但本工具是內部中文用途，
# 一律以 70 計 —— 成本估算寧可高估也不低估（低估會讓人誤以為還有點數可用）。
CHARS_PER_SEGMENT = 70

# 唯一經實撥驗證的成功碼。其餘碼一律不認定為成功，理由見 parse_response docstring。
SUCCESS_STATUSCODE = "1"

# 實測過的兩個錯誤碼，用來讓上層區分「IP 被擋」與「帳密錯」——兩者的處理方式
# 完全不同（前者要打電話給三竹加白名單，後者要改環境變數）。
STATUSCODE_IP_BLOCKED = "k"
STATUSCODE_AUTH_FAILED = "e"

# MitakeAPIError.kind 的取值，讓呼叫端不必比對中文錯誤字串就能分流。
KIND_IP_BLOCKED = "ip_blocked"
KIND_AUTH_FAILED = "auth_failed"
KIND_NETWORK = "network"
KIND_DECODE = "decode"
KIND_API = "api"

# 解碼順序：三竹回應實測是 Big5。cp950 是 Big5 的超集，放最後當保險；
# utf-8 夾在中間是為了容忍未來三竹改版（若已改 UTF-8，big5 多半會先解碼失敗）。
_RESPONSE_ENCODINGS = ("big5", "utf-8", "cp950")

# 使用者手輸的號碼常夾雜半形/全形的空白、連字號與括號，先清掉再驗證。
_PHONE_SEPARATOR_PATTERN = re.compile(r"[\s\-‐-―－()（）]")
_TW_MOBILE_PATTERN = re.compile(r"^09\d{8}$")
# 只有「886 + 9 開頭的 9 碼」才視為國碼寫法：台灣手機一律 09 開頭，
# 不存在其他以 886 起頭卻合法的 10 碼號碼，故此規則無歧義。
_TW_INTL_PATTERN = re.compile(r"^\+?886(9\d{8})$")


# --------------------------------------------------------------------------- #
# 例外階層
# --------------------------------------------------------------------------- #


class MitakeError(Exception):
    """本模組所有例外的基底，讓呼叫端可以一句 ``except MitakeError`` 收斂。"""


class MitakeConfigError(MitakeError):
    """環境變數缺漏或設定不完整（帳密沒設、值是空字串等）。"""


class MitakeValidationError(MitakeError):
    """輸入不合法（號碼格式錯、內容空白、型別錯）。這類錯誤不會送出請求、不扣點。"""


class MitakeAPIError(MitakeError):
    """呼叫三竹失敗：網路不通、回應無法解碼、或三竹回報錯誤碼。

    ``kind`` 讓上層不必比對中文字串就能分流處理：
    ``ip_blocked`` 要找三竹加白名單、``auth_failed`` 要改環境變數、
    ``network`` 可以重試（唯讀查詢才可以，發送不行）。
    """

    def __init__(
        self,
        message: str,
        *,
        statuscode: str | None = None,
        error_text: str | None = None,
        kind: str = KIND_API,
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.statuscode = statuscode
        self.error_text = error_text
        self.kind = kind
        self.response = response

    @property
    def is_ip_blocked(self) -> bool:
        """來源 IP 不在三竹白名單。"""
        return self.kind == KIND_IP_BLOCKED

    @property
    def is_auth_failed(self) -> bool:
        """帳號或密碼錯誤。"""
        return self.kind == KIND_AUTH_FAILED

    @property
    def is_network_error(self) -> bool:
        """連不到三竹（DNS/TLS/逾時/HTTP 錯誤）。"""
        return self.kind == KIND_NETWORK


# --------------------------------------------------------------------------- #
# 純函式：憑證、解碼、解析、驗證、計費
# --------------------------------------------------------------------------- #


def _get_credentials() -> tuple[str, str]:
    """從環境變數取出三竹帳密。

    缺任一項就丟 :class:`MitakeConfigError` 並指名缺哪個變數。刻意不提供預設值：
    用預設值兜底只會把錯誤延後到「打了 API 才發現帳密是空的」，那時已經多繞一圈。

    回傳值不做 strip（密碼理論上可能含前後空白），空白判斷只用於「是否算缺漏」。
    例外訊息只出現變數**名稱**，絕不含值。
    """
    credentials: dict[str, str] = {}
    missing: list[str] = []
    for name in (ENV_USERNAME, ENV_PASSWORD):
        value = os.environ.get(name)
        if value is None or not value.strip():
            missing.append(name)
        else:
            credentials[name] = value

    if missing:
        raise MitakeConfigError(
            "缺少三竹憑證環境變數：{}。請參考 .env.example 設定"
            "（本機放 .env，VPS 放 /etc/mitake-sms.env 權限 600）。".format("、".join(missing))
        )

    return credentials[ENV_USERNAME], credentials[ENV_PASSWORD]


def decode_response(raw: bytes) -> str:
    """把三竹回應的位元組解成文字，依序嘗試 big5 → utf-8 → cp950。

    三種編碼全部失敗時**丟出** :class:`MitakeAPIError`（而非回傳 repr）。理由：
    解不開的回應對下游毫無意義，若回傳 repr，:func:`parse_response` 只會產出一個
    「看起來像正常失敗」的結果，把「三竹改了編碼」這種需要人介入的問題偽裝成
    普通的發送失敗。丟例外才會讓它浮上來；例外訊息內附前 200 個原始位元組的
    repr，保留診斷所需的線索。
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise MitakeValidationError(f"decode_response 需要 bytes，收到 {type(raw).__name__}")

    data = bytes(raw)
    for encoding in _RESPONSE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise MitakeAPIError(
        "三竹回應無法以 {} 任一編碼解讀，原始位元組（前 200）：{!r}".format(
            "/".join(_RESPONSE_ENCODINGS), data[:200]
        ),
        kind=KIND_DECODE,
    )


def _to_int_or_none(value: str | None) -> int | None:
    """把 AccountPoint 之類的欄位轉成 int，轉不動就回 None（原始字串仍留在 raw_fields）。"""
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse_response(text: str) -> dict[str, Any]:
    """把三竹回應文字解析成結構化的 dict。

    可處理的四種實測格式::

        AccountPoint=12571                                      # SmQuery 成功
        [1] / msgid=... / statuscode=1 / AccountPoint=...       # SmSend 成功
        statuscode=k / Error=無效的連線位址                       # IP 不在白名單
        statuscode=e / Error=帳號、密碼錯誤                       # 帳密錯

    回傳欄位：

    ``success``
        是否判定為成功。判定順序：有 ``Error`` 即失敗 → 否則看 ``statuscode``
        是否為 ``"1"`` → 否則有 ``AccountPoint`` 即算成功（SmQuery 樣態）。
    ``statuscode`` / ``msgid`` / ``error``
        原始字串（缺漏為 None）。
    ``account_point``
        轉成 int 的剩餘點數（轉不動為 None）。
    ``batch_index``
        ``[1]`` 這種批次序號的內容，SmQuery 回應沒有這行故為 None。
    ``raw_fields`` / ``raw_text``
        原始 key/value 與原始整段文字，供日後出現沒見過的欄位時追查。

    **未實測過的 statuscode 一律不判成功**（例如三竹文件提到的 ``0`` 預約中）。
    寧可回報「未確認成功」讓人去看，也不要謊報成功讓簡訊悄悄沒送出去；
    真正的成功碼只有實撥驗證過的 ``1``。反過來說，呼叫端**不可**因為
    ``success is False`` 就自動重送——那會重複扣點，見模組 docstring。
    """
    if not isinstance(text, str):
        raise MitakeValidationError(f"parse_response 需要 str，收到 {type(text).__name__}")

    raw_fields: dict[str, str] = {}
    batch_index: str | None = None

    # \r\n 是實測的換行，但一併容忍單獨的 \r 或 \n，避免 proxy 改寫換行就解析失敗。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            batch_index = line[1:-1]
            continue
        if "=" not in line:
            continue
        # 只切第一個 "="：錯誤訊息或簡訊內容本身可能含 "="。
        key, _, value = line.partition("=")
        raw_fields[key.strip()] = value.strip()

    lookup = {key.lower(): value for key, value in raw_fields.items()}
    statuscode = lookup.get("statuscode") or None
    msgid = lookup.get("msgid") or None
    error = lookup.get("error") or None
    account_point = _to_int_or_none(lookup.get("accountpoint"))

    if error is not None:
        success = False
    elif statuscode is not None:
        success = statuscode == SUCCESS_STATUSCODE
    elif account_point is not None:
        success = True
    else:
        success = False

    return {
        "success": success,
        "statuscode": statuscode,
        "msgid": msgid,
        "error": error,
        "account_point": account_point,
        "batch_index": batch_index,
        "raw_fields": raw_fields,
        "raw_text": text,
    }


def classify_statuscode(statuscode: str | None) -> str:
    """把 statuscode 對應到 ``KIND_*``，讓上層不必比對中文錯誤字串。"""
    if statuscode == STATUSCODE_IP_BLOCKED:
        return KIND_IP_BLOCKED
    if statuscode == STATUSCODE_AUTH_FAILED:
        return KIND_AUTH_FAILED
    return KIND_API


def validate_phone(phone: str) -> str:
    """驗證台灣手機號碼並正規化成三竹 ``dstaddr`` 可用的 10 碼格式。

    會先移除半形/全形的空白、連字號與括號，所以 ``0910-869-893``、
    ``0910 869 893`` 都會被接受。

    **+886 的處理決定：轉換，不是放行。** ``+886910869893`` / ``886910869893``
    一律改寫成 ``0910869893``。理由：三竹的 dstaddr 只有 ``09xxxxxxxx`` 這種寫法
    經過實撥驗證，``+886`` 是否被接受從未實測。把未驗證的格式直接送出去，最壞情況是
    「扣了點但沒送達」——這是所有失敗模式裡最貴的一種。在本地轉成已知可行的形式，
    成本是零、風險是零。轉換條件限縮在「886 之後恰為 9 開頭的 9 碼」，因為台灣手機
    一律 09 開頭，不存在其他以 886 起頭卻合法的號碼，故此規則不會誤判。

    合法回傳正規化後的 10 碼字串；不合法丟 :class:`MitakeValidationError` 並說明原因。
    """
    if not isinstance(phone, str):
        raise MitakeValidationError(f"手機號碼必須是字串，收到 {type(phone).__name__}")

    cleaned = _PHONE_SEPARATOR_PATTERN.sub("", phone)
    if not cleaned:
        raise MitakeValidationError("手機號碼不可為空白")

    international = _TW_INTL_PATTERN.match(cleaned)
    if international:
        cleaned = "0" + international.group(1)

    if not _TW_MOBILE_PATTERN.match(cleaned):
        raise MitakeValidationError(
            f"手機號碼格式不符，需為 09 開頭共 10 碼：輸入 {phone!r}，正規化後 {cleaned!r}"
        )

    return cleaned


def count_sms_segments(body: str) -> tuple[int, int]:
    """回傳 ``(則數, 字數)``，給使用者在按下發送前看清楚會扣幾點。

    中文簡訊 70 字 = 1 則 = 1 點，超過即倍增（71 字 = 2 則 = 2 點）。

    空字串回 ``(0, 0)`` 而非 ``(1, 0)``：空內容三竹會直接拒收，報 1 則會讓成本
    預估憑空多出一點。

    已知限制：字數以 Python 的字元數（code point）計。BMP 以外的字元（多數 emoji）
    在簡訊的 UCS-2 計數下可能算 2 單位，此處未做該換算——因為本工具是中文內部用途，
    且該行為未經實測，不憑猜測寫進計費邏輯。若日後要送 emoji，需先實撥驗證。
    """
    if not isinstance(body, str):
        raise MitakeValidationError(f"簡訊內容必須是字串，收到 {type(body).__name__}")

    chars = len(body)
    if chars == 0:
        return (0, 0)

    # 無條件進位，避免浮點誤差（-(-a // b) 是整數版的 ceil）。
    segments = -(-chars // CHARS_PER_SEGMENT)
    return (segments, chars)


# --------------------------------------------------------------------------- #
# 網路層
# --------------------------------------------------------------------------- #


def _request(
    endpoint: str,
    params: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """組 URL、送出 GET、解碼並解析回應。

    帳密由本函式統一補上，呼叫端不需要（也不應該）碰到憑證。
    任何錯誤路徑的訊息都只帶 endpoint 名稱，不帶 URL——URL 的 query string 含帳密。
    """
    username, password = _get_credentials()

    query = dict(params)
    query["username"] = username
    query["password"] = password
    url = urllib.parse.urljoin(BASE_URL, endpoint) + "?" + urllib.parse.urlencode(
        query, encoding="utf-8"
    )

    # 只記 endpoint：完整 URL 含帳密，寫進 log 等同把憑證留在磁碟上。
    logger.info("呼叫三竹 API：endpoint=%s", endpoint)

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise MitakeAPIError(
            f"三竹 API 回應 HTTP {exc.code}（endpoint={endpoint}）", kind=KIND_NETWORK
        ) from exc
    except urllib.error.URLError as exc:
        raise MitakeAPIError(
            f"無法連線至三竹 API（endpoint={endpoint}）：{exc.reason}", kind=KIND_NETWORK
        ) from exc
    except TimeoutError as exc:
        raise MitakeAPIError(
            f"三竹 API 逾時超過 {timeout} 秒（endpoint={endpoint}）", kind=KIND_NETWORK
        ) from exc
    except OSError as exc:
        # TLS / socket 層的其他失敗（ssl.SSLError 也是 OSError）。
        raise MitakeAPIError(
            f"連線三竹 API 時發生系統層錯誤（endpoint={endpoint}）：{exc}", kind=KIND_NETWORK
        ) from exc

    return parse_response(decode_response(raw))


def _raise_if_failed(parsed: dict[str, Any], endpoint: str) -> None:
    """回應判定為失敗時，丟出帶 statuscode 與分類的 :class:`MitakeAPIError`。"""
    if parsed["success"]:
        return

    statuscode = parsed.get("statuscode")
    error_text = parsed.get("error")
    raise MitakeAPIError(
        "三竹回報失敗（endpoint={}, statuscode={}）：{}".format(
            endpoint, statuscode, error_text or "三竹未附錯誤說明"
        ),
        statuscode=statuscode,
        error_text=error_text,
        kind=classify_statuscode(statuscode),
        response=parsed,
    )


def query_balance(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> int:
    """查詢剩餘點數（SmQuery，免費、不扣點）。

    這也是驗證 IP 白名單是否生效的標準手段：換機器部署後先跑這支，
    通了才代表可以發簡訊。失敗一律丟 :class:`MitakeAPIError`。
    """
    parsed = _request(ENDPOINT_QUERY, {}, timeout=timeout)
    _raise_if_failed(parsed, ENDPOINT_QUERY)

    account_point = parsed["account_point"]
    if account_point is None:
        raise MitakeAPIError(
            f"三竹回應未包含可解讀的 AccountPoint：{parsed['raw_text']!r}",
            kind=KIND_API,
            response=parsed,
        )
    return account_point


def send_sms(
    phone: str,
    body: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """發送簡訊（SmSend）。**每次呼叫都會扣點，且點數與 App 團隊共用。**

    回傳 :func:`parse_response` 的結果再加上 ``segments`` / ``chars``
    兩個欄位，方便呼叫端記錄這次實際扣了幾點。

    ``CharsetURL=UTF-8`` 是中文不被截斷的關鍵（實測 54 字中文正常送達）：
    少了它，三竹會用預設編碼解讀 URL-encode 過的 UTF-8 位元組。

    失敗一律丟 :class:`MitakeAPIError`；呼叫端**不可**自動重送，理由見模組 docstring。
    """
    dstaddr = validate_phone(phone)

    if not isinstance(body, str):
        raise MitakeValidationError(f"簡訊內容必須是字串，收到 {type(body).__name__}")
    # 先擋下空內容：三竹會拒收，送出去只是白繞一趟網路。
    if not body.strip():
        raise MitakeValidationError("簡訊內容不可為空白")

    segments, chars = count_sms_segments(body)

    parsed = _request(
        ENDPOINT_SEND,
        {"dstaddr": dstaddr, "smbody": body, "CharsetURL": "UTF-8"},
        timeout=timeout,
    )
    _raise_if_failed(parsed, ENDPOINT_SEND)

    result = dict(parsed)
    result["segments"] = segments
    result["chars"] = chars

    # 不記號碼與內容：前者是個資，後者可能含驗證碼。
    logger.info(
        "簡訊已送出：msgid=%s，則數=%s，剩餘點數=%s",
        result.get("msgid"),
        segments,
        result.get("account_point"),
    )
    return result


# --------------------------------------------------------------------------- #
# 冒煙測試：只跑純函式，不碰網路、不扣點
# --------------------------------------------------------------------------- #


def _smoke_test() -> None:
    """純函式自我檢查。刻意不呼叫 query_balance / send_sms，避免誤觸真實 API。"""
    assert parse_response("AccountPoint=12571\r\n")["account_point"] == 12571

    sent = parse_response("[1]\r\nmsgid=0313887539\r\nstatuscode=1\r\nAccountPoint=12572\r\n")
    assert sent["success"] is True and sent["msgid"] == "0313887539"

    blocked = parse_response(decode_response("statuscode=k\r\nError=無效的連線位址\r\n".encode("big5")))
    assert blocked["success"] is False
    assert classify_statuscode(blocked["statuscode"]) == KIND_IP_BLOCKED

    assert validate_phone("0910-869-893") == "0910869893"
    assert validate_phone("+886910869893") == "0910869893"
    assert count_sms_segments("字" * 70) == (1, 70)
    assert count_sms_segments("字" * 71) == (2, 71)

    print("純函式冒煙測試全數通過（未呼叫真實 API、未扣點）")


if __name__ == "__main__":
    try:
        _smoke_test()
    except (MitakeError, AssertionError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        sys.exit(1)
