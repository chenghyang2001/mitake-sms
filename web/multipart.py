"""multipart/form-data 請求本文解析（純標準庫，最小子集實作，無 I/O）。

``http.server`` 沒有內建 multipart 解析（`cgi.FieldStorage` 已在較新 Python 版本
移除），本專案的零依賴原則又排除引入第三方套件，所以這裡自己刻一個 —— 但**刻意
只解析本功能需要的最小子集**：一般文字欄位＋單一內容型別的檔案欄位，不處理巢狀
multipart、不處理 Content-Transfer-Encoding、不處理 RFC 2231 的檔名編碼延伸語法。
這些從未在瀏覽器對本服務的實際請求中出現過，硬要支援只會讓解析器更難驗證正確性。

**解析失敗一律回錯，不嘗試「盡量解析」**：缺 boundary、段落缺
Content-Disposition、文字欄位不是合法 UTF-8，一律丟 :class:`MultipartParseError`，
由呼叫端（``web.server``）轉成 400。這是全新程式碼，錯誤時保守拒絕優於猜出一個
可能是錯的結果 —— 尤其上游是「發簡訊名單」，猜錯一個欄位的代價可能是簡訊發錯人。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "MultipartParseError",
    "ParsedMultipart",
    "extract_boundary",
    "parse_multipart_form_data",
]

# RFC 2046 對 boundary 的長度上限是 70 字元；這裡多留一點餘裕給不嚴格遵守規範的
# 客戶端，但仍要設上限 —— 不設的話一個惡意 Content-Type 標頭可以塞任意長字串，
# 讓下面的 encode／split 操作在超長字串上重複運作，形同一個免費的放大攻擊面。
_MAX_BOUNDARY_LEN = 256

# Content-Disposition 標頭裡 name="..."／filename="..." 的擷取。只支援最基本的
# 雙引號包覆語法（瀏覽器實際送出的格式），不處理跳脫雙引號或 RFC 2231 的
# filename*=UTF-8''... 延伸編碼 —— 那些場景本工具用不到，硬要支援反而讓解析器
# 更容易被構造出的邊界案例騙過。
_DISPOSITION_PARAM_PATTERN = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

_CRLF = b"\r\n"
_HEADER_BODY_SEPARATOR = b"\r\n\r\n"

# 只接受這個型別；大小寫不敏感（Content-Type 標頭本身就是大小寫不敏感的）。
_MULTIPART_MEDIA_TYPE = "multipart/form-data"


class MultipartParseError(Exception):
    """multipart/form-data 本文格式不符，無法解析。呼叫端應回應 400。"""


@dataclass(frozen=True)
class ParsedMultipart:
    """一次 multipart 請求的解析結果。

    ``fields`` 形狀比照 :func:`urllib.parse.parse_qs`
    （``Mapping[str, Sequence[str]]``），讓下游既有的 ``_first()`` 之類輔助函式
    不必為 multipart 另寫一套讀取邏輯。

    ``files`` 只回傳原始 bytes：要不要當文字解碼、用什麼編碼解碼，是呼叫端
    （依欄位語意）才知道的事，本模組不代為決定 —— 這正是「純解析、不猜測」原則
    在檔案欄位上的體現。
    """

    fields: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)


def extract_boundary(content_type: str) -> str:
    """從 ``Content-Type`` 標頭值取出 boundary 參數。

    支援加引號（``boundary="----abc"``）與不加引號（``boundary=----abc``）
    兩種寫法 —— 實測不同瀏覽器／工具產生的請求兩種都有。

    找不到合法 boundary（媒體型別不是 multipart/form-data、缺少參數、參數為空、
    參數過長）一律丟 :class:`MultipartParseError`。
    """
    if not isinstance(content_type, str):
        raise MultipartParseError(
            f"Content-Type 必須是字串，收到 {type(content_type).__name__}"
        )

    media_type, _, params_text = content_type.partition(";")
    if media_type.strip().lower() != _MULTIPART_MEDIA_TYPE:
        raise MultipartParseError(f"Content-Type 不是 multipart/form-data：{content_type!r}")

    for raw_param in params_text.split(";"):
        name, sep, value = raw_param.strip().partition("=")
        if not sep or name.strip().lower() != "boundary":
            continue
        boundary = value.strip()
        if boundary.startswith('"') and boundary.endswith('"') and len(boundary) >= 2:
            boundary = boundary[1:-1]
        if not boundary:
            raise MultipartParseError("Content-Type 的 boundary 參數為空字串")
        if len(boundary) > _MAX_BOUNDARY_LEN:
            raise MultipartParseError(
                f"Content-Type 的 boundary 過長（{len(boundary)} 字元，"
                f"上限 {_MAX_BOUNDARY_LEN}）"
            )
        return boundary

    raise MultipartParseError(f"Content-Type 缺少 boundary 參數：{content_type!r}")


def _parse_content_disposition(header_value: str) -> tuple[str | None, str | None]:
    """解析 ``Content-Disposition: form-data; name="x"; filename="y"``。

    回傳 ``(name, filename)``，兩者皆可能是 ``None``（沒有該參數）。
    只認得 ``form-data`` 型別；不是的話視為缺欄位名稱（回傳 ``(None, None)``），
    交給呼叫端依「本段落無法識別欄位名稱」的規則拒絕，而不是在這裡就猜一個。
    """
    disposition_type = header_value.split(";", 1)[0].strip().lower()
    if disposition_type != "form-data":
        return None, None

    params = dict(_DISPOSITION_PARAM_PATTERN.findall(header_value))
    return params.get("name"), params.get("filename")


def _parse_headers(raw_headers: bytes) -> dict[str, str]:
    """把一個段落的標頭 bytes 解析成 ``{標頭名稱小寫: 值}``。

    用 UTF-8（不是嚴格 ASCII）解碼：部分瀏覽器對含中文的 filename 直接送出原始
    UTF-8 位元組，不走 RFC 2231／2047 的編碼延伸語法。HTTP 標頭理論上應為 ASCII，
    但這裡放寬到 UTF-8 換來的是「中文檔名可用」，代價幾乎是零 —— 純 ASCII 內容
    用 UTF-8 解碼與用 ASCII 解碼結果逐位元組相同。
    """
    try:
        text = raw_headers.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MultipartParseError("段落標頭不是合法的 UTF-8 內容，格式不符") from exc

    headers: dict[str, str] = {}
    for line in text.split("\r\n"):
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise MultipartParseError(f"段落標頭格式不符（缺冒號）：{line!r}")
        headers[name.strip().lower()] = value.strip()
    return headers


def parse_multipart_form_data(body: bytes, content_type: str) -> ParsedMultipart:
    """解析 multipart/form-data 請求本文。

    ``body`` 是完整的 request body（已依 Content-Length 讀完，呼叫端負責大小上限）；
    ``content_type`` 是 ``Content-Type`` 標頭的完整值（含 boundary 參數）。

    任何格式不符（缺 boundary、段落缺 Content-Disposition、缺 name、文字欄位
    不是合法 UTF-8、缺結尾分隔線）都丟 :class:`MultipartParseError`，不嘗試略過
    壞掉的段落繼續解析 —— 理由見模組 docstring。

    已知限制（刻意，見模組 docstring）：本函式用「按 boundary 分割位元組字串」
    的方式實作，若二進位檔案內容中恰好出現與 boundary 完全相同的位元組序列，
    解析結果會不正確。本服務只接受純文字的手機號碼清單，這個風險在此情境下可忽略。
    """
    if not isinstance(body, (bytes, bytearray)):
        raise MultipartParseError(f"body 必須是 bytes，收到 {type(body).__name__}")

    boundary = extract_boundary(content_type)
    try:
        boundary_bytes = boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MultipartParseError(f"boundary 含有非 ASCII 字元：{boundary!r}") from exc
    delimiter = b"--" + boundary_bytes

    raw_body = bytes(body)
    segments = raw_body.split(delimiter)
    if len(segments) < 2:
        # 整份 body 裡連一次 delimiter 都沒出現 —— 不是「零欄位」，是 boundary
        # 根本沒出現在內容裡，代表 body 與 Content-Type 宣告的 boundary 對不上。
        raise MultipartParseError("找不到任何 multipart 分隔線（boundary 未出現於本文中）")

    # 頭尾各一塊：第一塊是 preamble（正常應為空字串或空白），最後一塊是最後一次
    # delimiter 之後的內容，正常應以 "--"（收尾符）開頭。中間各塊才是實際段落。
    body_segments = segments[1:-1]
    trailer = segments[-1]
    if not trailer.lstrip(b"\r\n \t").startswith(b"--"):
        raise MultipartParseError('multipart 本文缺少結尾分隔線（"--boundary--"）')

    fields: dict[str, list[str]] = {}
    files: dict[str, bytes] = {}

    for raw_segment in body_segments:
        # 每個段落緊接在 delimiter 之後，格式固定是：
        #   CRLF + headers + CRLF CRLF + content + CRLF
        # 最後那個 CRLF 屬於「下一個 delimiter 之前的分隔結構」，不是內容本身，
        # 必須去掉，否則每個檔案／文字欄位的值都會多一個尾隨換行。
        if not raw_segment.startswith(_CRLF):
            raise MultipartParseError("multipart 段落格式不符（緊接 boundary 之後缺起始換行）")
        segment = raw_segment[len(_CRLF):]
        if segment.endswith(_CRLF):
            segment = segment[: -len(_CRLF)]

        header_end = segment.find(_HEADER_BODY_SEPARATOR)
        if header_end == -1:
            raise MultipartParseError("multipart 段落格式不符（找不到標頭／內容分隔的空白行）")

        raw_headers = segment[:header_end]
        content = segment[header_end + len(_HEADER_BODY_SEPARATOR):]

        headers = _parse_headers(raw_headers)
        disposition = headers.get("content-disposition")
        if disposition is None:
            raise MultipartParseError("multipart 段落缺少 Content-Disposition 標頭")

        name, filename = _parse_content_disposition(disposition)
        if not name:
            raise MultipartParseError("multipart 段落的 Content-Disposition 缺少 name 參數")

        if filename is not None:
            # 檔案欄位：原樣回傳 bytes，編碼由呼叫端依欄位語意決定（見 docstring）。
            # 允許空內容（0 位元組）—— 那是合法的「使用者選了一個空檔案」，
            # 由呼叫端（web.server）決定要不要把它當成「沒上傳」拒收。
            files[name] = content
        else:
            try:
                text_value = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MultipartParseError(
                    f"文字欄位 {name!r} 不是合法的 UTF-8 內容"
                ) from exc
            fields.setdefault(name, []).append(text_value)

    return ParsedMultipart(fields=fields, files=files)


if __name__ == "__main__":
    # 冒煙測試：不碰任何 I/O，純粹跑一次最小範例確認模組本身載入無誤。
    _sample = (
        b'--BOUNDARY\r\nContent-Disposition: form-data; name="phone"\r\n\r\n'
        b"0912345678\r\n"
        b'--BOUNDARY\r\nContent-Disposition: form-data; name="file"; '
        b'filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n'
        b"--BOUNDARY--\r\n"
    )
    _result = parse_multipart_form_data(_sample, "multipart/form-data; boundary=BOUNDARY")
    assert _result.fields == {"phone": ["0912345678"]}
    assert _result.files == {"file": b"hello"}
    print("web/multipart.py 冒煙測試通過")
