"""web/batch_recipients.py 的純函式測試：多人（上傳名單）模式的名單解析回歸鎖。

鎖的重點：驗證規則**必須**完全透過 :func:`mitake.validate_phone`，去重必須用
「正規化後的值」而非原始字串，跳過項目必須帶正確的原因代碼——這三件事任何一個
壞掉，確認頁上顯示的「將發送 N 人／跳過 M 人」就會與實際送出的名單對不上，
而這正是本專案最不能容忍的一種落差。

全檔不碰任何 I/O、不碰網路（``mitake.validate_phone`` 是純函式，不會觸發
``tests/conftest.py`` 攔截的網路呼叫）。
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from web.batch_recipients import (  # noqa: E402
    MAX_BATCH_RECIPIENTS,
    REASON_DUPLICATE,
    REASON_INVALID_FORMAT,
    parse_batch_recipients,
)


# --------------------------------------------------------------------------- #
# 1. 正常清單（Happy Path）
# --------------------------------------------------------------------------- #


def test_parses_plain_valid_list() -> None:
    text = "0912345678\n0987654321\n0955555555\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678", "0987654321", "0955555555"]
    assert result.skipped == []


def test_valid_phones_preserve_first_seen_order() -> None:
    text = "0955555555\n0912345678\n0987654321\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0955555555", "0912345678", "0987654321"]


def test_normalizes_phone_with_separators_and_intl_prefix() -> None:
    """驗證規則完全交給 mitake.validate_phone：連字號、+886 寫法都要能正規化。"""
    text = "0912-345-678\n+886987654321\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678", "0987654321"]
    assert result.skipped == []


# --------------------------------------------------------------------------- #
# 2. 邊界案例：空行、空白、全形空白
# --------------------------------------------------------------------------- #


def test_blank_lines_are_silently_ignored_not_counted_as_skipped() -> None:
    """空行是排版產物，不是操作者填錯的內容，不該出現在「跳過」清單裡。"""
    text = "0912345678\n\n\n   \n0987654321\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678", "0987654321"]
    assert result.skipped == []


def test_empty_string_input_returns_empty_result() -> None:
    result = parse_batch_recipients("")
    assert result.valid_phones == []
    assert result.skipped == []


def test_lines_with_leading_trailing_whitespace_are_trimmed() -> None:
    text = "  0912345678  \n\t0987654321\t\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678", "0987654321"]


def test_handles_crlf_and_cr_and_lf_newlines() -> None:
    """splitlines() 天然處理三種換行慣例，不需要呼叫端事先正規化。"""
    text = "0912345678\r\n0987654321\r0955555555\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678", "0987654321", "0955555555"]


# --------------------------------------------------------------------------- #
# 3. 格式錯誤（invalid_format）
# --------------------------------------------------------------------------- #


def test_invalid_format_lines_are_skipped_with_correct_reason() -> None:
    text = "0912345678\nabc\n123\n0912\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678"]
    assert len(result.skipped) == 3
    assert all(item.reason == REASON_INVALID_FORMAT for item in result.skipped)
    assert [item.raw_line for item in result.skipped] == ["abc", "123", "0912"]


def test_all_invalid_lines_yields_empty_valid_phones() -> None:
    text = "not-a-phone\nnope\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == []
    assert len(result.skipped) == 2
    assert all(item.reason == REASON_INVALID_FORMAT for item in result.skipped)


def test_excel_header_row_style_content_is_all_skipped() -> None:
    """模擬「誤傳了帶標題列的 Excel 匯出檔」——每一行都不是合法號碼。"""
    text = "姓名,電話\n陳先生,0912345678\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == []
    assert len(result.skipped) == 2
    assert all(item.reason == REASON_INVALID_FORMAT for item in result.skipped)


# --------------------------------------------------------------------------- #
# 4. 重複（duplicate）：以正規化後的值判斷，不是原始字串
# --------------------------------------------------------------------------- #


def test_exact_duplicate_line_is_skipped_as_duplicate() -> None:
    text = "0912345678\n0912345678\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678"]
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == REASON_DUPLICATE
    assert result.skipped[0].raw_line == "0912345678"


def test_duplicate_detection_uses_normalized_value_not_raw_string() -> None:
    """同一支號碼用不同寫法出現兩次（連字號 vs 純數字）仍要判定為重複。"""
    text = "0912345678\n0912-345-678\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678"]
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == REASON_DUPLICATE
    # raw_line 保留「第二次出現時」的原始寫法，方便操作者核對是不是自己貼重了。
    assert result.skipped[0].raw_line == "0912-345-678"


def test_three_way_duplicate_only_first_counted_valid() -> None:
    text = "0912345678\n0912345678\n0912345678\n"
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678"]
    assert len(result.skipped) == 2
    assert all(item.reason == REASON_DUPLICATE for item in result.skipped)


# --------------------------------------------------------------------------- #
# 5. 混合情況（整合式：格式錯 + 重複 + 有效交錯出現）
# --------------------------------------------------------------------------- #


def test_mixed_valid_invalid_and_duplicate_lines() -> None:
    text = (
        "0912345678\n"
        "abc\n"
        "0987654321\n"
        "0912345678\n"  # 與第一行重複
        "\n"
        "0912\n"  # 格式錯
        "0955555555\n"
    )
    result = parse_batch_recipients(text)
    assert result.valid_phones == ["0912345678", "0987654321", "0955555555"]
    reasons = [(item.raw_line, item.reason) for item in result.skipped]
    assert reasons == [
        ("abc", REASON_INVALID_FORMAT),
        ("0912345678", REASON_DUPLICATE),
        ("0912", REASON_INVALID_FORMAT),
    ]


def test_does_not_enforce_max_batch_recipients_itself() -> None:
    """本函式不做「超過上限」判斷——那是 web.server 呼叫端的責任（見模組 docstring）。

    這裡只確認：即使有效號碼數超過 MAX_BATCH_RECIPIENTS，本函式仍照樣把全部解析
    出來，不會自己截斷或拋錯。
    """
    lines = [f"09{str(i).zfill(8)}" for i in range(MAX_BATCH_RECIPIENTS + 5)]
    text = "\n".join(lines)
    result = parse_batch_recipients(text)
    assert len(result.valid_phones) == MAX_BATCH_RECIPIENTS + 5
    assert result.skipped == []
