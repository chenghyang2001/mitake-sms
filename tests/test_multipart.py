"""web/multipart.py 的純函式測試：multipart/form-data 解析器的離線回歸鎖。

這裡鎖的是「解析失敗要拒絕、不要瞎猜」——本模組是全新程式碼，任何格式不符
（缺 boundary、段落格式錯、文字欄位非 UTF-8）都必須丟 :class:`MultipartParseError`，
不可以吞掉錯誤悄悄回傳一份不完整的結果。這一點在「檔案上傳名單去發簡訊」這個
情境下特別要命：使用者以為上傳了完整名單 A，若解析器悄悄跳過壞掉的段落，
實際解析出來的只是殘缺的 A'，簡訊就這樣少發或發錯人而沒有任何錯誤畫面提醒。

全檔不碰任何 I/O、不碰網路——純粹餵 bytes 進 :func:`parse_multipart_form_data`
並檢查回傳值或例外。
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from web.multipart import (  # noqa: E402
    MultipartParseError,
    extract_boundary,
    parse_multipart_form_data,
)

BOUNDARY = "----TestBoundary123"


def _build_body(
    parts: "list[tuple[str, bytes, str | None]]", boundary: str = BOUNDARY
) -> bytes:
    """組一份合法的 multipart body。

    ``parts`` 是 ``(name, content_bytes, filename)`` 的清單；``filename`` 為
    ``None`` 代表這是文字欄位，非 ``None`` 代表檔案欄位（會多帶一個
    ``Content-Type: text/plain`` 標頭，模擬瀏覽器實際送出的形狀）。
    """
    chunks: list[bytes] = []
    delim = f"--{boundary}".encode("ascii")
    for name, content, filename in parts:
        chunks.append(delim + b"\r\n")
        if filename is None:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        else:
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                    "Content-Type: text/plain\r\n\r\n"
                ).encode()
            )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(delim + b"--\r\n")
    return b"".join(chunks)


def _content_type(boundary: str = BOUNDARY) -> str:
    return f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------- #
# 1. extract_boundary
# --------------------------------------------------------------------------- #


def test_extract_boundary_unquoted() -> None:
    assert extract_boundary("multipart/form-data; boundary=abc123") == "abc123"


def test_extract_boundary_quoted() -> None:
    assert extract_boundary('multipart/form-data; boundary="abc 123"') == "abc 123"


def test_extract_boundary_wrong_media_type_raises() -> None:
    with pytest.raises(MultipartParseError):
        extract_boundary("application/json; boundary=abc")


def test_extract_boundary_missing_raises() -> None:
    with pytest.raises(MultipartParseError):
        extract_boundary("multipart/form-data")


def test_extract_boundary_empty_value_raises() -> None:
    with pytest.raises(MultipartParseError):
        extract_boundary("multipart/form-data; boundary=")


def test_extract_boundary_too_long_raises() -> None:
    long_boundary = "x" * 300
    with pytest.raises(MultipartParseError):
        extract_boundary(f"multipart/form-data; boundary={long_boundary}")


# --------------------------------------------------------------------------- #
# 2. 正常解析（Happy Path）
# --------------------------------------------------------------------------- #


def test_parses_single_text_field() -> None:
    body = _build_body([("phone", b"0912345678", None)])
    result = parse_multipart_form_data(body, _content_type())
    assert result.fields == {"phone": ["0912345678"]}
    assert result.files == {}


def test_parses_single_file_field() -> None:
    body = _build_body([("recipients_file", b"0912345678\n0987654321", "list.txt")])
    result = parse_multipart_form_data(body, _content_type())
    assert result.files == {"recipients_file": b"0912345678\n0987654321"}
    assert result.fields == {}


def test_parses_mixed_text_and_file_fields() -> None:
    body = _build_body(
        [
            ("send-mode", b"batch", None),
            ("body", "測試".encode("utf-8"), None),
            ("recipients_file", b"0912345678", "a.txt"),
        ]
    )
    result = parse_multipart_form_data(body, _content_type())
    assert result.fields == {"send-mode": ["batch"], "body": ["測試"]}
    assert result.files == {"recipients_file": b"0912345678"}


def test_empty_file_content_is_empty_bytes() -> None:
    """使用者選了一個空檔案：合法輸入，不是錯誤——由呼叫端決定要不要當「沒上傳」。"""
    body = _build_body([("recipients_file", b"", "empty.txt")])
    result = parse_multipart_form_data(body, _content_type())
    assert result.files == {"recipients_file": b""}


def test_empty_text_field_is_empty_string() -> None:
    body = _build_body([("body", b"", None)])
    result = parse_multipart_form_data(body, _content_type())
    assert result.fields == {"body": [""]}


def test_multiline_text_field_with_blank_line_is_preserved() -> None:
    """欄位值本身含 CRLF CRLF（多行文字含空白行）不能被誤判成段落結尾。

    ``.find()`` 找的是**第一個**「標頭／內容」分隔，只要標頭本身不含空白行，
    內容裡的空白行不會被誤認。
    """
    content = b"line1\r\n\r\nline2"
    body = _build_body([("body", content, None)])
    result = parse_multipart_form_data(body, _content_type())
    assert result.fields == {"body": ["line1\r\n\r\nline2"]}


def test_repeated_field_name_collects_multiple_values() -> None:
    body = _build_body([("tag", b"a", None), ("tag", b"b", None)])
    result = parse_multipart_form_data(body, _content_type())
    assert result.fields == {"tag": ["a", "b"]}


def test_chinese_filename_is_accepted() -> None:
    """檔名本身不影響解析結果（本模組不使用檔名值），只確認不會因中文而炸掉。"""
    body = _build_body([("recipients_file", b"0912345678", "名單.txt")])
    result = parse_multipart_form_data(body, _content_type())
    assert result.files == {"recipients_file": b"0912345678"}


# --------------------------------------------------------------------------- #
# 3. 錯誤／畸形輸入：保守拒絕，不嘗試「盡量解析」
# --------------------------------------------------------------------------- #


def test_missing_boundary_param_in_content_type_raises() -> None:
    body = _build_body([("phone", b"0912345678", None)])
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, "multipart/form-data")


def test_body_without_any_boundary_raises() -> None:
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(
            b"just some random bytes, no boundary here", _content_type()
        )


def test_missing_closing_delimiter_raises() -> None:
    """body 只有普通分隔線、缺少收尾的 ``"--boundary--"``——傳輸中斷的典型症狀。"""
    delim = f"--{BOUNDARY}".encode()
    body = (
        delim + b'\r\nContent-Disposition: form-data; name="phone"\r\n\r\n0912345678\r\n'
        + delim + b"\r\n"
    )
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, _content_type())


def test_segment_missing_header_body_separator_raises() -> None:
    delim = f"--{BOUNDARY}".encode()
    body = (
        delim + b'\r\nContent-Disposition: form-data; name="phone"\r\n'
        + delim + b"--\r\n"
    )
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, _content_type())


def test_segment_missing_content_disposition_raises() -> None:
    delim = f"--{BOUNDARY}".encode()
    body = delim + b"\r\nContent-Type: text/plain\r\n\r\nvalue\r\n" + delim + b"--\r\n"
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, _content_type())


def test_content_disposition_missing_name_raises() -> None:
    delim = f"--{BOUNDARY}".encode()
    body = (
        delim + b"\r\nContent-Disposition: form-data\r\n\r\nvalue\r\n"
        + delim + b"--\r\n"
    )
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, _content_type())


def test_non_utf8_text_field_raises() -> None:
    delim = f"--{BOUNDARY}".encode()
    body = (
        delim + b'\r\nContent-Disposition: form-data; name="body"\r\n\r\n'
        + b"\xff\xfe\x00\x01"
        + b"\r\n" + delim + b"--\r\n"
    )
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, _content_type())


def test_non_str_content_type_raises() -> None:
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(b"whatever", None)  # type: ignore[arg-type]


def test_non_bytes_body_raises() -> None:
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data("not bytes", _content_type())  # type: ignore[arg-type]


def test_segment_missing_leading_crlf_raises() -> None:
    """delimiter 之後不是緊接 CRLF（人為構造的畸形輸入）也要被拒絕，不能跳過重找。"""
    boundary_bytes = f"--{BOUNDARY}".encode()
    body = boundary_bytes + b"NOT-A-CRLF-HERE" + boundary_bytes + b"--\r\n"
    with pytest.raises(MultipartParseError):
        parse_multipart_form_data(body, _content_type())


# --------------------------------------------------------------------------- #
# 4. 冒煙測試
# --------------------------------------------------------------------------- #


def test_module_level_smoke_scenario_matches_docstring_example() -> None:
    """跑一次與 ``web/multipart.py`` 的 ``__main__`` 區塊等價的最小範例。"""
    sample = _build_body([("phone", b"0912345678", None), ("file", b"hello", "a.txt")])
    result = parse_multipart_form_data(sample, _content_type())
    assert result.fields == {"phone": ["0912345678"]}
    assert result.files == {"file": b"hello"}
