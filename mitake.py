"""三竹簡訊（Mitake）API 核心模組。

提供「查詢餘額」與「發送簡訊」兩項能力，是後續 Web 介面與餘額告警排程的共用底層。

設計前提（違反任一項，程式寫得再正確也打不通）：

1. **IP 白名單**：三竹強制檢查來源 IP，未登記的位址一律回 ``statuscode=k`` /
   ``無效的連線位址``，與帳密是否正確**無關**。換機器部署前必須先向三竹申請。
   驗證方式是跑一次 :func:`query_balance`（唯讀、免費、不燒點數）。
2. **憑證只從環境變數讀**：``MITAKE_USERNAME`` / ``MITAKE_PASSWORD``。
   本模組所有例外訊息與 log 都只記 endpoint 名稱，**不記完整 query string**
   （帳密就在 query string 裡，記下去等同外洩）。urllib 例外物件上帶完整 URL 的
   屬性也會被清除，見 :func:`_scrub_credentials_from_exception`。
3. **回應是 Big5 編碼**：UTF-8 硬解中文會亂碼，故一律走 :func:`decode_response`。

成本模型（點數與 App 團隊共用，App 靠它發手機驗證碼）：

* :func:`send_sms` 每呼叫一次就從共用池扣點，且長內容會**倍數**扣點
  （71 字就是 2 點）。故模組層設有單次則數上限 :data:`MAX_SEGMENTS_PER_SEND`，
  貼錯一大段文字不會靜靜燒掉數百點。護欄刻意放在模組層而非 Web 層：
  Web 層由別人實作、無存取控制，且餘額告警階段共用同一個底層。
* 本模組**刻意不實作自動重試**。失敗時是否重送必須由人判讀後決定。
* 更重要的是：失敗有兩種，:class:`MitakeAPIError` 用 ``possibly_charged``
  區分它們 —— ``False`` 代表三竹明確拒絕（沒扣點，可安全重送）；
  ``True`` 代表請求已送達三竹但結果未確認（多半已扣點，**重送會扣兩次、
  對方會收到兩封**，必須去三竹後台以 msgid 查證）。上層應把這兩者渲染成
  不同畫面，不可一律顯示「發送失敗，請重試」。

只用標準庫實作（urllib），VPS 部署零外部依賴。

直接執行本檔（``python mitake.py``）只會跑純函式的自我檢查，**不會呼叫真實 API**、
不會扣點。
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = [
    "BASE_URL",
    "CHARS_PER_SEGMENT",
    "DEFAULT_TIMEOUT_SECONDS",
    "DELIVERED_STATUSCODE",
    "DELIVERY_ACCOUNT_ERROR",
    "DELIVERY_DELIVERED",
    "DELIVERY_ERROR",
    "DELIVERY_FAILED",
    "DELIVERY_FINAL_CATEGORIES",
    "DELIVERY_PENDING",
    "DELIVERY_STATUS_TABLE",
    "DELIVERY_UNKNOWN",
    "KIND_API",
    "KIND_AUTH_FAILED",
    "KIND_DECODE",
    "KIND_IP_BLOCKED",
    "KIND_NETWORK",
    "KIND_UNCONFIRMED",
    "MAX_SEGMENTS_PER_SEND",
    "MitakeAPIError",
    "MitakeConfigError",
    "MitakeError",
    "MitakeValidationError",
    "classify_statuscode",
    "count_sms_segments",
    "decode_response",
    "describe_delivery_status",
    "parse_response",
    "parse_status_response",
    "query_balance",
    "query_message_status",
    "send_sms",
    "validate_msgid",
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

# 單次發送的則數上限。內部通知的正常長度遠低於 5 則（350 字），會撞到這條線的
# 幾乎都是「整段文件貼進輸入框」的失誤 —— 而 1 萬字就是 143 點、7 萬字就是 1000 點，
# 直接從 App 驗證碼的共用池裡蒸發。要突破必須明確傳 max_segments=，不會被誤觸。
MAX_SEGMENTS_PER_SEND = 5

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
KIND_UNCONFIRMED = "unconfirmed"
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

# urllib 的例外物件會把「含帳密的完整 URL」塞在這些屬性上，raise ... from exc
# 之後它們會永久掛在 __cause__ 上，被 Sentry / Flask debugger 撈出來。
_CREDENTIAL_BEARING_EXC_ATTRS = ("url", "filename", "filename2")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """三竹 API 不該回 30x；真回了也絕不跟隨。

    跟隨重導會製造一個最貴的誤判：第一跳已送達三竹（＝已扣點），第二跳若
    DNS/連線/憑證失敗，會拋出 URLError(gaierror/ConnectionRefusedError/
    SSLCertVerificationError) —— 正好是 _request 判定「從未送達、可安全重送」
    的三種 reason，於是已扣點的請求被講成沒扣，使用者重送就扣兩次、對方收兩封。
    回 None 讓 urllib 把 30x 直接當 HTTPError 拋出，走保守的 possibly_charged 路徑。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


# --------------------------------------------------------------------------- #
# 例外階層
# --------------------------------------------------------------------------- #


class MitakeError(Exception):
    """本模組所有例外的基底，讓呼叫端可以一句 ``except MitakeError`` 收斂。"""


class MitakeConfigError(MitakeError):
    """環境變數缺漏或設定不完整（帳密沒設、值是空字串等）。"""


class MitakeValidationError(MitakeError):
    """輸入不合法（號碼格式錯、內容空白、超過則數上限、型別錯）。

    這類錯誤一律在送出網路請求**之前**丟出，保證沒有扣點。
    """


class MitakeAPIError(MitakeError):
    """呼叫三竹失敗：網路不通、回應無法解碼、或三竹回報非成功狀態。

    ``kind`` 讓上層不必比對中文字串就能分流處理：
    ``ip_blocked`` 要找三竹加白名單、``auth_failed`` 要改環境變數、
    ``network`` / ``decode`` 是連線或回應層面的問題、
    ``unconfirmed`` 是三竹收了請求但沒回可辨識的成敗。

    ``possibly_charged`` 是**重送與否的唯一依據**：

    * ``False``：三竹明確拒絕（回應帶 ``Error``）或唯讀查詢失敗 —— 沒扣點，
      修正輸入後可以安全重送。
    * ``True``：請求已送達三竹但結果無法確認（狀態碼沒見過、回應解不開、
      讀取逾時）—— 點數多半已經扣了，重送會扣第二次且對方收到兩封簡訊。
      正確處置是拿 msgid 去三竹後台查證，不是重試。
    """

    def __init__(
        self,
        message: str,
        *,
        statuscode: str | None = None,
        error_text: str | None = None,
        kind: str = KIND_API,
        possibly_charged: bool = False,
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.statuscode = statuscode
        self.error_text = error_text
        self.kind = kind
        self.possibly_charged = possibly_charged
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

    @property
    def is_unconfirmed(self) -> bool:
        """三竹收了請求，但模組無法判定這通到底送出去沒有。"""
        return self.kind == KIND_UNCONFIRMED


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

    注意：本函式不知道自己在哪個 endpoint 底下被呼叫，故 ``possibly_charged``
    留給 :func:`_request` 依 endpoint 補上（發送途中解不開回應＝很可能已扣點）。
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
    寧可回報「未確認」讓人去看，也不要謊報成功讓簡訊悄悄沒送出去；
    真正的成功碼只有實撥驗證過的 ``1``。

    但要注意 ``success is False`` **不等於「沒送出、可以重送」**：這些沒見過的
    狀態碼多半代表三竹已經收單並扣了點。把「未確認」講成「失敗」會直接誘導使用者
    重送。真正的區分在 :func:`_raise_if_failed`，它會依有無 ``Error`` 欄位決定
    ``possibly_charged``。
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

    ⚠️ 維護提醒：本函式是整個模組唯一會**改寫使用者輸入**的地方，而
    :func:`send_sms` 第一行就呼叫它。這裡的兩條 regex 若被「順手放寬」
    （例如為了支援市話把 ``^09`` 改成 ``^0``），後果不是算錯字數，是把簡訊
    送到另一個號碼 —— 扣了點，而且第三方收到內部訊息。
    tests/test_mitake.py 有對應的回歸鎖，改動這裡務必先看那支測試。
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


def _scrub_credentials_from_exception(exc: BaseException) -> None:
    """清掉 urllib 例外物件上帶完整 query string（含帳密）的屬性。

    ``HTTPError`` 會把整串請求 URL 同時塞進 ``.url`` 與 ``.filename``，
    而 ``raise ... from exc`` 會讓這個物件永久掛在 ``__cause__`` 上。
    下一階段是無存取控制的 Flask：開發期若 ``app.run(debug=True)``，
    Werkzeug 互動式 debugger 會把例外屬性與 frame locals 直接渲染在網頁上，
    共用計費帳號的密碼就會明文出現在誰都打得開的網址；Sentry 類工具預設也會擷取。

    兩個屬性都要清 —— 清 ``filename`` 不會連帶清掉 ``url``。
    """
    for attr in _CREDENTIAL_BEARING_EXC_ATTRS:
        if getattr(exc, attr, None) is not None:
            try:
                setattr(exc, attr, None)
            except (AttributeError, TypeError):
                # 屬性是唯讀 property 就跳過，不能讓清理動作本身炸掉真正的錯誤。
                pass

    # 只清屬性不夠。例外的 traceback 抓著 urllib 內部 frame，而
    # OpenerDirector.open 的 fullurl 這個 local 就是**純字串形式的完整 URL**，
    # Werkzeug debugger / Sentry 對 frame locals 做 repr() 就會明文印出密碼
    # （實測 6 個 frame 可取回明文：urlopen.url / open.fullurl / open.req /
    #  _open.req / https_open.req / do_open.req）。
    # 砍掉 traceback 才真的斷根；MitakeAPIError 自己的 traceback 不含憑證，
    # 診斷需要的 endpoint 與 reason 也已經寫進外層訊息，不會失去可追查性。
    exc.__traceback__ = None


def _fetch_raw(endpoint: str, params: dict[str, str], timeout: float) -> bytes:
    """組出帶憑證的 URL、送出 GET、回傳原始位元組。

    憑證的生命週期被刻意壓在這個函式裡，而且在送出請求前就 ``del`` 掉，
    讓這一層 frame 的 locals 在 traceback 中不含明文帳密。
    例外在這裡先被洗過再往上拋，上層只負責分類。
    """
    username, password = _get_credentials()
    query = dict(params)
    query["username"] = username
    query["password"] = password
    request = urllib.request.Request(
        urllib.parse.urljoin(BASE_URL, endpoint)
        + "?"
        + urllib.parse.urlencode(query, encoding="utf-8"),
        method="GET",
    )
    # 憑證只需要活到 Request 建好為止，之後留在 frame 裡就只是洩漏風險。
    del username, password, query

    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()
    except BaseException as exc:
        # 型別刻意放到最寬：下一行就是裸 raise，不吞、不改變任何例外的傳播，
        # 但能保證「任何」逃出這層的例外都經過憑證洗滌。
        # 若只列 (OSError, http.client.HTTPException)——HTTPError / URLError /
        # TimeoutError / SSLError 是 OSError 子類，而 IncompleteRead /
        # BadStatusLine 屬 http.client.HTTPException 且**不是** OSError
        # （實測 issubclass 為 False）——仍會漏掉 ValueError 這類第三型別，
        # 而實測那條路徑上還有 18 處明文憑證外洩點。
        _scrub_credentials_from_exception(exc)
        raise
    finally:
        # Request.full_url 一樣含帳密，別讓它留在 traceback 的 frame locals 裡。
        del request


def _request(
    endpoint: str,
    params: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """送出請求並解析回應。憑證由 :func:`_fetch_raw` 處理，本層 frame 不碰。

    所有錯誤路徑的訊息都只帶 endpoint 名稱，不帶 URL —— URL 的 query string 含帳密。

    ``possibly_charged`` 在這層依 endpoint 決定：發送途中失敗，代表 HTTP 請求
    已經送到三竹、只是回應讀不到或看不懂，點數多半已經扣了；唯讀查詢則永遠沒扣點，
    可以安全重試。
    """
    # 這個布林值是「能不能重送」的分水嶺，故在最前面算好，每條錯誤路徑都要帶上。
    charged = endpoint == ENDPOINT_SEND

    # 只記 endpoint：完整 URL 含帳密，寫進 log 等同把憑證留在磁碟上。
    logger.info("呼叫三竹 API：endpoint=%s", endpoint)

    try:
        raw = _fetch_raw(endpoint, params, timeout)
    except urllib.error.HTTPError as exc:
        raise MitakeAPIError(
            f"三竹 API 回應 HTTP {exc.code}（endpoint={endpoint}）",
            kind=KIND_NETWORK,
            possibly_charged=charged,
        ) from exc
    except urllib.error.URLError as exc:
        # DNS 解不出來、連線被拒、TLS 憑證驗不過，都發生在送出任何請求位元組**之前**，
        # 三竹根本沒收到。標成 possibly_charged=True 會逼使用者去後台查一筆不存在的
        # msgid，還把「其實可以安全重送」講成「請勿重送」。
        # 其餘 URLError（連線中途被切、寫入逾時）可能已送出部分位元組，維持保守的 True。
        never_reached_mitake = isinstance(
            exc.reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)
        )
        raise MitakeAPIError(
            f"無法連線至三竹 API（endpoint={endpoint}）：{exc.reason}",
            kind=KIND_NETWORK,
            possibly_charged=charged and not never_reached_mitake,
        ) from exc
    except TimeoutError as exc:
        raise MitakeAPIError(
            f"三竹 API 逾時超過 {timeout} 秒（endpoint={endpoint}）",
            kind=KIND_NETWORK,
            possibly_charged=charged,
        ) from exc
    except http.client.HTTPException as exc:
        # IncompleteRead / BadStatusLine 不是 OSError，不接住就整個逃出模組：
        # 上層的 except MitakeError 攔不到，也拿不到 possibly_charged。
        # 收到半截回應代表請求已送達三竹，發送情境下正是最貴的失敗模式。
        # 必須排在 except OSError 之前：RemoteDisconnected 同時是 OSError 與
        # HTTPException 的子類，排在後面會被 OSError 先接走，拿不到 unconfirmed 語意，
        # 而「伺服器收下請求後直接關閉連線」正是 unconfirmed 的教科書案例。
        # 前移不會遮蔽既有分支：HTTPError / URLError / TimeoutError 都不是 HTTPException 子類。
        raise MitakeAPIError(
            f"三竹回應在傳輸中損毀（endpoint={endpoint}）：{type(exc).__name__}: {exc}",
            kind=KIND_UNCONFIRMED if charged else KIND_NETWORK,
            possibly_charged=charged,
        ) from exc
    except OSError as exc:
        # TLS / socket 層的其他失敗（ssl.SSLError 也是 OSError）。
        raise MitakeAPIError(
            f"連線三竹 API 時發生系統層錯誤（endpoint={endpoint}）：{exc}",
            kind=KIND_NETWORK,
            possibly_charged=charged,
        ) from exc

    try:
        text = decode_response(raw)
    except MitakeAPIError as exc:
        # decode_response 不知道自己在哪個 endpoint 底下，由這裡補上扣點判斷：
        # 發送後回應解不開，等於「送出去了但看不懂結果」，絕不能當成沒送。
        exc.possibly_charged = charged
        raise

    return parse_response(text)


def _raise_if_failed(parsed: dict[str, Any], endpoint: str) -> None:
    """回應不是已驗證的成功時丟出 :class:`MitakeAPIError`，並分辨「有沒有扣點」。

    三條分流：

    1. 有 ``Error`` 欄位 → 三竹明確拒絕（IP 被擋、帳密錯…），沒收單、沒扣點。
    2. 沒有 ``Error`` 但有 ``statuscode`` 或 ``msgid`` → 三竹收下了，只是回了
       本模組沒實測過的狀態。發送情境下點數多半已經扣了，訊息必須明講「請勿重送」
       並給出 msgid 讓人去後台查證。把它講成「發送失敗」會直接誘導使用者重按，
       結果是扣兩點、對方收到兩封。
    3. 兩者皆無 → 回應完全不認得，發送情境同樣視為可能已扣點。

    第 2、3 種情況的 ``possibly_charged`` 依 endpoint 決定而非硬寫 True：
    SmQuery 是唯讀免費的，對它宣告「可能已扣點、請勿重送」會讓餘額告警排程
    在一次未知狀態後就不敢再查，反而漏掉真正的低點數告警。
    """
    if parsed["success"]:
        return

    statuscode = parsed.get("statuscode")
    error_text = parsed.get("error")
    msgid = parsed.get("msgid")
    charged = endpoint == ENDPOINT_SEND

    if error_text is not None:
        raise MitakeAPIError(
            f"三竹回報失敗（endpoint={endpoint}, statuscode={statuscode}）：{error_text}",
            statuscode=statuscode,
            error_text=error_text,
            kind=classify_statuscode(statuscode),
            possibly_charged=False,
            response=parsed,
        )

    if statuscode is not None or msgid is not None:
        raise MitakeAPIError(
            "三竹回應狀態未確認，可能已扣點，請勿重送，請至三竹後台以 msgid={} 查證"
            "（endpoint={}, statuscode={}）".format(msgid, endpoint, statuscode),
            statuscode=statuscode,
            error_text=None,
            # kind 表達的是「該怎麼辦」，而唯讀查詢的正解永遠是重試，故不給
            # unconfirmed（那語意是「別動、去後台查」）。與 _request 的
            # HTTPException 分支採同一條規則，同一件事不該在兩處拿到不同 kind。
            kind=KIND_UNCONFIRMED if charged else KIND_NETWORK,
            possibly_charged=charged,
            response=parsed,
        )

    raise MitakeAPIError(
        "三竹回應無法辨識，{}（endpoint={}）：{!r}".format(
            "可能已扣點，請勿重送" if charged else "此為唯讀查詢，未扣點",
            endpoint,
            parsed["raw_text"],
        ),
        # 同上：kind 表達「該怎麼辦」，唯讀查詢的正解永遠是重試。
        kind=KIND_UNCONFIRMED if charged else KIND_NETWORK,
        possibly_charged=charged,
        response=parsed,
    )


def query_balance(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> int:
    """查詢剩餘點數（SmQuery，免費、不扣點）。

    這也是驗證 IP 白名單是否生效的標準手段：換機器部署後先跑這支，
    通了才代表可以發簡訊。失敗一律丟 :class:`MitakeAPIError`，
    且因為是唯讀操作，``possibly_charged`` 恆為 False，可安全重試。
    """
    parsed = _request(ENDPOINT_QUERY, {}, timeout=timeout)
    _raise_if_failed(parsed, ENDPOINT_QUERY)

    account_point = parsed["account_point"]
    if account_point is None:
        raise MitakeAPIError(
            f"三竹回應未包含可解讀的 AccountPoint：{parsed['raw_text']!r}",
            kind=KIND_API,
            possibly_charged=False,
            response=parsed,
        )
    return account_point


def send_sms(
    phone: str,
    body: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_segments: int = MAX_SEGMENTS_PER_SEND,
) -> dict[str, Any]:
    """發送簡訊（SmSend）。**每次呼叫都會扣點，且點數與 App 團隊共用。**

    回傳 :func:`parse_response` 的結果再加上 ``segments`` / ``chars``
    兩個欄位，方便呼叫端記錄這次實際扣了幾點。

    ``CharsetURL=UTF-8`` 是中文不被截斷的關鍵（實測 54 字中文正常送達）：
    少了它，三竹會用預設編碼解讀 URL-encode 過的 UTF-8 位元組。

    ``max_segments`` 是防呆上限，預設 :data:`MAX_SEGMENTS_PER_SEND`。超過就在
    **發出任何網路請求之前**丟 :class:`MitakeValidationError`（故不會扣點）。
    要送超長內容必須明確傳入 ``max_segments=``，讓「我知道這會扣很多點」變成
    一個寫得出來的動作，而不是複製貼上就發生的意外。

    失敗一律丟 :class:`MitakeAPIError`。**重送前務必檢查 ``possibly_charged``**：
    為 True 代表點數多半已扣，重送會扣第二次且對方收到兩封，正確處置是拿 msgid
    去三竹後台查證。詳見模組 docstring。
    """
    dstaddr = validate_phone(phone)

    if not isinstance(body, str):
        raise MitakeValidationError(f"簡訊內容必須是字串，收到 {type(body).__name__}")
    # 先擋下空內容：三竹會拒收，送出去只是白繞一趟網路。
    if not body.strip():
        raise MitakeValidationError("簡訊內容不可為空白")

    segments, chars = count_sms_segments(body)
    if segments > max_segments:
        raise MitakeValidationError(
            f"內容 {chars} 字＝{segments} 則＝{segments} 點，超過單次上限 {max_segments} 則。"
            f"確認要送請明確傳入 max_segments。"
        )

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
# 投遞狀態查詢（SmQuery + msgid）—— 唯讀、免費、不扣點
# --------------------------------------------------------------------------- #
#
# 這一整段是**純新增**：上面的查餘額／發送流程一行都沒動。原因是 mitake.py 已部署
# 在 VPS 上，餘額告警排程每天 08:00 直接 import 它，改既有函式的風險遠高於這個功能
# 的價值。
#
# 為什麼需要這個功能：send_sms 成功只代表「三竹收下了」（statuscode=1），
# **不代表對方手機收到**。兩者之間還有一段電信商投遞，短則數秒、長則失敗。
# 在此之前，Web 介面顯示「三竹已接收」之後就沒有下文，使用者手機沒響時無從查證。
#
# 端點與查餘額**完全相同**（SmQuery），差別只在多帶一個 msgid 參數：
#
#     GET /api/mtk/SmQuery?username=X&password=Y            → AccountPoint=12571
#     GET /api/mtk/SmQuery?username=X&password=Y&msgid=NNN  → NNN<TAB>4<TAB>20260729143730
#
# 注意兩種回應的**格式完全不同**：前者是 key=value，後者是 Tab 分隔的三欄。
# 所以這裡另寫 parse_status_response，不去動 parse_response——把 Tab 格式硬塞進
# 既有解析器，等於為了一個新功能去改一支每天都在跑的函式。

# 狀態分類。分類的用途是「畫面該怎麼講、使用者接下來該做什麼」，不是原樣照抄三竹文件。
DELIVERY_PENDING = "pending"  # 還在路上，稍後再查會變
DELIVERY_DELIVERED = "delivered"  # 已送達手機，最終狀態
DELIVERY_FAILED = "failed"  # 確定沒送到，最終狀態
DELIVERY_ERROR = "error"  # 三竹系統層異常，與這則簡訊無關
DELIVERY_ACCOUNT_ERROR = "account_error"  # 帳號／IP 設定問題，查詢根本沒查成
DELIVERY_UNKNOWN = "unknown"  # 三竹回了本模組沒收錄的碼

# 狀態碼 →（中文說明, 分類）。依三竹官方文件 v2.09。
#
# ⚠️ **這張表最不該抄錯的地方是 1–3 與 4 的差別。**
# 1/2/3 是「已送達**業者**」——三竹把簡訊交給電信商了，僅此而已；
# 只有 4 是「已送達**手機**」——對方門號真的收到了。
# 把 1–3 講成「已送達手機」會讓使用者相信對方收到了而不再追，
# 而那正是這個功能要解決的問題本身。
DELIVERY_STATUS_TABLE: dict[str, tuple[str, str]] = {
    "0": ("預約傳送中", DELIVERY_PENDING),
    "1": ("已送達業者", DELIVERY_PENDING),
    "2": ("已送達業者", DELIVERY_PENDING),
    "3": ("已送達業者", DELIVERY_PENDING),
    "4": ("已送達手機", DELIVERY_DELIVERED),
    "5": ("內容有錯誤", DELIVERY_FAILED),
    "6": ("門號有錯誤", DELIVERY_FAILED),
    "7": ("簡訊已停用", DELIVERY_FAILED),
    "8": ("逾時無送達", DELIVERY_FAILED),
    "9": ("預約已取消", DELIVERY_FAILED),
    "*": ("系統發生錯誤", DELIVERY_ERROR),
    "a": ("簡訊發送功能暫時停止", DELIVERY_ERROR),
    "b": ("簡訊發送功能暫時停止", DELIVERY_ERROR),
    "c": ("請輸入帳號", DELIVERY_ACCOUNT_ERROR),
    "d": ("請輸入密碼", DELIVERY_ACCOUNT_ERROR),
    "e": ("帳號、密碼錯誤", DELIVERY_ACCOUNT_ERROR),
    "f": ("帳號已過期", DELIVERY_ACCOUNT_ERROR),
    "h": ("帳號已被停用", DELIVERY_ACCOUNT_ERROR),
    "k": ("無效的連線位址", DELIVERY_ACCOUNT_ERROR),
    "m": ("必須變更密碼", DELIVERY_ACCOUNT_ERROR),
    "n": ("密碼已逾期", DELIVERY_ACCOUNT_ERROR),
    "p": ("無權限使用外部 HTTP 程式", DELIVERY_ACCOUNT_ERROR),
    "r": ("系統暫停服務", DELIVERY_ERROR),
    "s": ("帳務處理失敗", DELIVERY_ERROR),
    "t": ("簡訊已過期", DELIVERY_FAILED),
    "u": ("簡訊內容不得為空白", DELIVERY_FAILED),
    "v": ("無效的手機號碼", DELIVERY_FAILED),
}

# 唯一代表「對方手機真的收到了」的碼。單獨拉出來當常數，是為了讓
# is_delivered 的判斷只有一個出處——這個布林值會直接決定畫面上寫「已送達手機」
# 還是「還沒送到」，不能散落在各處各判各的。
DELIVERED_STATUSCODE = "4"

# 最終狀態＝再查也不會變的分類。pending 之外的兩個「錯誤」分類刻意不算最終：
# 系統錯誤（三竹那端暫時異常）稍後再查可能就有答案了，講成最終狀態會讓人放棄追查。
DELIVERY_FINAL_CATEGORIES = frozenset({DELIVERY_DELIVERED, DELIVERY_FAILED})

# msgid 會被原樣拼進 query string，所以字元集刻意收得很緊：只收英數、底線、連字號。
# 放寬到「什麼都收」的話，使用者在網址列塞 `&password=` 之類的東西就能改寫我們送給
# 三竹的參數（urlencode 會跳脫，但這種輸入本來就不可能是合法 msgid，早點擋掉更省事）。
# 上限 64 是安全裕度：實測 msgid 是 10 碼數字。
_MSGID_PATTERN = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def validate_msgid(msgid: str) -> str:
    """驗證並正規化 msgid（只去除前後空白，不改寫內容）。

    合法回傳去空白後的字串；不合法丟 :class:`MitakeValidationError`。
    與 :func:`validate_phone` 一樣，這類錯誤一律在送出網路請求**之前**丟出。
    """
    if not isinstance(msgid, str):
        raise MitakeValidationError(f"msgid 必須是字串，收到 {type(msgid).__name__}")

    cleaned = msgid.strip()
    if not cleaned:
        raise MitakeValidationError("msgid 不可為空白")

    if not _MSGID_PATTERN.match(cleaned):
        raise MitakeValidationError(
            f"msgid 格式不符，只接受 1 至 64 個半形英數字、底線或連字號：輸入 {msgid!r}"
        )

    return cleaned


def describe_delivery_status(statuscode: str | None) -> tuple[str, str]:
    """把狀態碼翻成 ``(中文說明, 分類)``；沒收錄的碼回 ``DELIVERY_UNKNOWN``。

    **大小寫敏感**（三竹的碼本來就是小寫）。把未知碼歸成 unknown 而不是猜一個最接近
    的分類，是同一條老規矩：寧可說「不知道」讓人去查，也不要謊報「已送達」。
    """
    known = DELIVERY_STATUS_TABLE.get(statuscode) if statuscode is not None else None
    if known is None:
        return ("未知狀態碼（三竹未定義，或本模組尚未收錄）", DELIVERY_UNKNOWN)
    return known


def parse_status_response(text: str) -> dict[str, Any]:
    """解析「以 msgid 查投遞狀態」的回應（**Tab 分隔**，與 :func:`parse_response` 無關）。

    實測格式（Big5、單行、三欄）::

        0315772761	4	20260729143730
           msgid   狀態碼  狀態時間（yyyyMMddHHmmss，台灣時間）

    回傳欄位：

    ``msgid`` / ``statuscode`` / ``status_time``
        三竹回報的原始字串（第三欄缺漏時 ``status_time`` 為 None）。
    ``description`` / ``category``
        中文說明與分類，來自 :data:`DELIVERY_STATUS_TABLE`。
    ``is_delivered``
        **只有 statuscode 恰為 ``"4"`` 才是 True。** 1–3 是「已送達業者」，
        代表交給電信商了而已，對方手機不一定收到。
    ``is_final``
        是否為最終狀態（已送達手機／確定失敗）。False 代表稍後再查會變。
    ``raw_text``
        原始整段文字，供日後三竹改格式時追查。

    格式不符（空回應、欄位不足、拿到 key=value 格式）一律丟
    :class:`MitakeAPIError`（``possibly_charged`` 恆為 False —— 這是唯讀查詢）。
    刻意不「盡量湊出一個結果」：湊出來的狀態會被畫面當成真的投遞結果顯示，
    而使用者無從分辨那是三竹說的還是我們猜的。
    """
    if not isinstance(text, str):
        raise MitakeValidationError(
            f"parse_status_response 需要 str，收到 {type(text).__name__}"
        )

    # 與 parse_response 相同的換行容忍度：\r\n 是實測值，但單獨的 \r / \n 也吃。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # 取第一行有內容的。行首行尾**只去空白與換行，不去 Tab** —— Tab 是欄位分隔符，
    # 一律 strip() 的話，開頭是空 msgid 的回應（"\t4\t2026…"）會被去掉前導 Tab，
    # 整排欄位往左位移一格，狀態碼就被讀成 msgid、時間被讀成狀態碼。那個結果不會
    # 報錯，只會靜靜地顯示一個錯的狀態。
    line = next((s for s in (raw.strip(" \n") for raw in normalized.split("\n")) if s), "")
    if not line:
        raise MitakeAPIError(
            f"三竹狀態查詢回應是空的：{text!r}",
            kind=KIND_API,
            possibly_charged=False,
        )

    fields = [field.strip() for field in line.split("\t")]
    if len(fields) < 2:
        # 有些中間層會把 Tab 改寫成空白。退一步用任意空白切，總比整筆判為壞掉好。
        fields = line.split()

    if len(fields) < 2 or not fields[0] or not fields[1]:
        raise MitakeAPIError(
            "三竹狀態查詢回應格式不符（預期 msgid、狀態碼、狀態時間三欄，"
            f"以 Tab 分隔）：{text!r}",
            kind=KIND_API,
            possibly_charged=False,
        )

    msgid, statuscode = fields[0], fields[1]
    status_time = fields[2] if len(fields) > 2 and fields[2] else None
    description, category = describe_delivery_status(statuscode)

    return {
        "msgid": msgid,
        "statuscode": statuscode,
        "description": description,
        "category": category,
        "status_time": status_time,
        "is_delivered": statuscode == DELIVERED_STATUSCODE,
        "is_final": category in DELIVERY_FINAL_CATEGORIES,
        "raw_text": text,
    }


def query_message_status(
    msgid: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """查詢一則已發送簡訊的投遞狀態（SmQuery + msgid，**唯讀、免費、不扣點**）。

    走的是與 :func:`query_balance` 同一個 ``SmQuery`` 端點，所以
    :func:`_request` 算出的 ``charged`` 恆為 False —— 這條路徑上任何失敗的
    ``possibly_charged`` 都是 False，可以安全重查。**不要**把它改成走 ``SmSend``。

    回傳值即 :func:`parse_status_response` 的結果（欄位說明見該函式）。

    兩種失敗要分清楚，因為處理方式完全不同：

    * **帳戶類狀態碼**（``c``/``d``/``e``/``f``/``h``/``k``/``m``/``n``/``p``）
      代表三竹根本沒去查這則簡訊，是我們這端的帳號或 IP 白名單設定壞了。
      這種一律丟 :class:`MitakeAPIError`，``kind`` 走既有的
      :func:`classify_statuscode`（``k`` → ``ip_blocked``、``e`` → ``auth_failed``），
      讓上層沿用既有的「這是設定問題，重試沒用」畫面。
      把它當成一個查詢結果回傳，畫面就會寫成「這則簡訊的狀態是：帳號、密碼錯誤」——
      那是假的，那不是這則簡訊的狀態。
    * **系統類狀態碼**（``*``/``a``/``b``/``r``/``s``）代表三竹那端暫時異常，
      這裡**照常回傳**（``category=DELIVERY_ERROR``、``is_final=False``），
      讓畫面可以說「三竹系統忙碌，稍後再查」而不是把人導去改設定。
    """
    clean_msgid = validate_msgid(msgid)

    parsed = _request(ENDPOINT_QUERY, {"msgid": clean_msgid}, timeout=timeout)

    # 帳密／IP 錯誤時三竹回的是 key=value 格式（statuscode=e / Error=…），
    # 與正常狀態回應的 Tab 格式完全不同。交給既有的 _raise_if_failed 分類，
    # 上層拿到的 kind 與查餘額失敗時完全一致。
    if parsed["error"] is not None:
        _raise_if_failed(parsed, ENDPOINT_QUERY)

    status = parse_status_response(parsed["raw_text"])

    if status["category"] == DELIVERY_ACCOUNT_ERROR:
        raise MitakeAPIError(
            "三竹拒絕這次查詢（statuscode={}）：{}。"
            "這是帳號或連線位址的設定問題，不是這則簡訊的投遞結果。".format(
                status["statuscode"], status["description"]
            ),
            statuscode=status["statuscode"],
            error_text=status["description"],
            kind=classify_statuscode(status["statuscode"]),
            possibly_charged=False,
            response=status,
        )

    if status["msgid"] != clean_msgid:
        # 三竹正常會回傳你問的那個 msgid。對不上時仍照回（不猜、不改寫），
        # 但要留一行線索：畫面上顯示的狀態可能不是你查的那則。
        logger.warning(
            "三竹回傳的 msgid 與查詢的不符：查詢=%s 回傳=%s",
            clean_msgid,
            status["msgid"],
        )

    # 只記 msgid 與狀態碼：這兩者不是個資，也不含簡訊內容。
    logger.info(
        "查詢投遞狀態：msgid=%s statuscode=%s（%s）",
        clean_msgid,
        status["statuscode"],
        status["description"],
    )
    return status


# --------------------------------------------------------------------------- #
# 冒煙測試：只跑純函式，不碰網路、不扣點
# --------------------------------------------------------------------------- #


def _smoke_test() -> None:
    """純函式自我檢查。

    刻意**不呼叫** send_sms / query_balance，連「驗證則數上限有生效」也不放這裡：
    若上限剛好壞掉，那行 send_sms 會變成一通真實外撥，把要防的事情自己做一遍。
    則數上限的回歸鎖在 tests/test_mitake.py，那裡用替身攔住了 _OPENER.open。
    """
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
    assert count_sms_segments("字" * CHARS_PER_SEGMENT * MAX_SEGMENTS_PER_SEND)[0] == MAX_SEGMENTS_PER_SEND

    print("純函式冒煙測試全數通過（未呼叫真實 API、未扣點）")


if __name__ == "__main__":
    try:
        _smoke_test()
    except (MitakeError, AssertionError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        sys.exit(1)
