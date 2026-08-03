"""多人（上傳名單）模式的手機號碼清單解析（純函式，無 I/O）。

三竹沒有批次發送 API，「多人模式」本質上是把同一則內容迴圈呼叫 N 次
``mitake.send_sms``（見 ``web.server`` 的送出迴圈）。在真的花錢之前，這裡先把
上傳檔案裡「哪些號碼真的會被拿去發送」與「哪些被跳過、為什麼」算清楚，讓確認頁
能把完整結果攤在操作者面前 —— 這是本專案一貫的原則：不猜、不吞、讓人在按下送出
前看得到全貌（見 doc/spec-multi-recipient-sms.md §3）。

驗證規則完全交給 :func:`mitake.validate_phone`，本檔不重寫任何格式判斷 ——
避免兩處驗證邏輯日後各自漂移，導致「這裡說合法、那裡說不合法」的不一致。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# 本模組依賴 mitake.validate_phone()，須確保 repo 根在 sys.path 上 —— 與
# web/server.py 同一套防禦寫法：本模組可能被單獨 import（測試、未來的獨立工具），
# 不能假設呼叫端一定先跑過 web/server.py 那段 sys.path 補丁。
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import mitake  # noqa: E402  （必須等 sys.path 補完才 import）

__all__ = [
    "MAX_BATCH_RECIPIENTS",
    "REASON_DUPLICATE",
    "REASON_INVALID_FORMAT",
    "BatchParseResult",
    "SkippedRecipient",
    "parse_batch_recipients",
]

# 單次名單的合理性上限。**不是**速率上限（那個在 web.server 用「則數」計算，
# 見 RateLimiter）——這條只是防止單一 token／稽核紀錄無限膨脹的護欄。500 筆足以
# 覆蓋正常的體驗名單規模，超過的話多半是誤傳了不該傳的檔案，要求分批比默默處理
# 一份巨大清單更安全（也更容易在畫面上核對完整名單）。
MAX_BATCH_RECIPIENTS = 500

# 跳過原因的代碼。字串值本身就是給人看的識別碼（不是流水號），供
# web.templates 對應成中文說明；改動這兩個值務必同步檢查
# tests/test_web.py 裡把兩邊釘在一起的測試（同 web.templates._STATUS_TONE
# 那套「兩處各寫一份、用測試鎖住不漂移」的作法）。
REASON_INVALID_FORMAT = "invalid_format"
REASON_DUPLICATE = "duplicate"


@dataclass(frozen=True)
class SkippedRecipient:
    """一筆被跳過的名單項目。

    ``raw_line`` 保留去除前後空白後的原始文字，讓操作者能回頭核對原始檔案 ——
    要不要遮罩顯示是呼叫端（``web.templates``）的責任，本檔是純解析層，不做任何
    畫面相關的決策。
    """

    raw_line: str
    reason: str


@dataclass(frozen=True)
class BatchParseResult:
    """名單解析結果：有效且去重後的號碼，以及被跳過的項目。

    ``valid_phones`` 依檔案中第一次出現的順序排列，且都已經過
    :func:`mitake.validate_phone` 正規化（10 碼、09 開頭）。
    """

    valid_phones: list[str]
    skipped: list[SkippedRecipient]


def parse_batch_recipients(text: str) -> BatchParseResult:
    """把上傳的 .txt 內容解析成「有效號碼」與「跳過項目」兩份清單。

    步驟（對照 doc/spec-multi-recipient-sms.md §3）：

    1. 依行分割（``str.splitlines()`` 天然處理 CRLF／CR／LF 三種換行慣例），
       每行去除前後空白，空行直接忽略（不計入 skipped —— 空行是檔案排版的自然
       產物，不是操作者填錯的內容，不需要在確認頁上被當成一條「錯誤」列出來）。
    2. 逐行呼叫 :func:`mitake.validate_phone`；格式不符（非 09 開頭、長度不對、
       夾雜非數字字元等）一律歸入 :data:`REASON_INVALID_FORMAT`。
    3. 通過驗證的號碼以**正規化後的值**去重：同一支號碼第二次起出現，歸入
       :data:`REASON_DUPLICATE`（原始行內容仍保留在 ``raw_line``，方便操作者
       核對是不是自己貼重了，或名單本身就有重複收件人）。

    本函式**不做**「有效號碼數為 0」或「超過 :data:`MAX_BATCH_RECIPIENTS` 上限」
    的擋下判斷 —— 那是 HTTP 語意（400、對應的錯誤文案），屬於 ``web.server``
    呼叫端的責任；本檔只負責把「檔案裡實際發生了什麼」算成事實，不代為決定
    這份事實該不該被接受。
    """
    valid_phones: list[str] = []
    skipped: list[SkippedRecipient] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            normalized = mitake.validate_phone(line)
        except mitake.MitakeValidationError:
            skipped.append(SkippedRecipient(raw_line=line, reason=REASON_INVALID_FORMAT))
            continue

        if normalized in seen:
            skipped.append(SkippedRecipient(raw_line=line, reason=REASON_DUPLICATE))
            continue

        seen.add(normalized)
        valid_phones.append(normalized)

    return BatchParseResult(valid_phones=valid_phones, skipped=skipped)


if __name__ == "__main__":
    # 冒煙測試：純函式、無 I/O，跑一次確認模組載入與基本邏輯正確。
    _sample = "0912345678\n0912-345-678\n\nabc\n0987654321\n"
    _result = parse_batch_recipients(_sample)
    assert _result.valid_phones == ["0912345678", "0987654321"], _result.valid_phones
    assert len(_result.skipped) == 2, _result.skipped
    assert _result.skipped[0].reason == REASON_DUPLICATE
    assert _result.skipped[1].reason == REASON_INVALID_FORMAT
    print("web/batch_recipients.py 冒煙測試通過")
