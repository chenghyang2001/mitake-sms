"""發送對象名單（consumer 側，純標準庫，零外部依賴）。

名單本身由**另一支 producer**產出一份 JSON 檔（本模組不做 producer，只讀）。
本模組是這整條「發送對象下拉選單」功能的資料層：把 JSON 解析成
:class:`RecipientBook`，供 :mod:`web.templates` 渲染下拉、供 :mod:`web.server`
在 ``/preview`` 用 id 反查真實電話。

**為什麼 consumer 這麼防禦性。** 這個名單餵進的是整個專案唯一會花錢的介面
（每則簡訊扣與 App 團隊共用的點數）。所以兩條鐵律：

1. **降級不 crash**：名單檔不存在、JSON 壞掉、結構不符，一律回
   :meth:`RecipientBook.empty`，讓表單退回「手動輸入號碼」的現況行為 ——
   絕不能因為名單這個「加值功能」讀不到，就把整個花錢工具弄掛。
2. **id 反查只認可選者**：:meth:`RecipientBook.get` 只在該 id 為「ok 且有電話」
   時回傳，未知 id / 非 ok 的 id 一律 ``None``。這是伺服器端防竄改的關鍵 ——
   前端送什麼 id 過來都無所謂，查不到可選對象就擋下，不會誤發到髒資料號碼。

只用標準庫（``json`` / ``logging`` / ``pathlib`` / ``dataclasses``），
與 :mod:`mitake`、其餘 :mod:`web` 子模組一致，VPS 部署零外部依賴。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Recipient", "RecipientBook", "load_recipients", "parse_acfh_user_id"]

logger = logging.getLogger("mitake.web.recipients")

# 唯一「可被選取發送」的 match_status。producer 端保證只有這個狀態才會帶電話；
# 寫成常數而非散落各處的字面字串，是為了讓「什麼才算可選」在一個地方看得完。
_SELECTABLE_STATUS = "ok"

# match_status 缺漏或型別錯時的保守預設：一律當成「不可選」。寧可少發（使用者
# 看得到但選不了、去問 producer），也不要把一筆狀態不明的資料當成 ok 而誤發簡訊。
_FALLBACK_STATUS = "not_found"

# Recipient.id 的前綴（tools/build_recipients.py 產生時固定用 id_prefix="u"，
# 例如 "u46" 對應 acfh_api.users.id=46）。這裡只是**目前**的慣例，不是這個類別
# 強制的格式——見 parse_acfh_user_id 的 docstring：格式不符一律回 None，不假設。
_ACFH_USER_ID_PREFIX = "u"


@dataclass(frozen=True)
class Recipient:
    """一筆發送對象。``phone`` 為 ``None`` 代表 producer 沒能替它配到電話。

    frozen 是刻意的：名單載入後就是一份唯讀快照，不該有任何路徑改到它，
    避免「渲染時看到的」與「反查時拿到的」在併發下漂開。
    """

    id: str
    name: str
    phone: str | None
    device: str
    borrow_date: str
    match_status: str
    # 以下四欄只給 /trial-email 的體驗借出表格顯示，不參與 is_selectable / get() 反查。
    # 帶預設值放在既有欄位之後，是 frozen dataclass 新增欄位的相容做法 —— 既有以位置或
    # 關鍵字建構 Recipient 的地方（測試、_parse_recipient）不必全部補這四個。
    # 用 trial_status 而非 status，避免與 match_status 混淆；它對應 JSON 的 "status" 鍵。
    days: str = ""
    used_days: str = ""
    business: str = ""
    trial_status: str = ""

    @property
    def is_selectable(self) -> bool:
        """能不能被選來發送：必須是 ok 狀態**且**真的有電話。

        兩個條件缺一不可 —— producer 理論上保證「ok 才有 phone」，但這裡不信任
        那個保證（producer 是另一支程式，可能有 bug），自己再驗一次 phone 非空。
        """
        return self.match_status == _SELECTABLE_STATUS and bool(self.phone)

    @property
    def acfh_user_id(self) -> int | None:
        """從 :attr:`id`（例如 ``"u46"``）解析出 acfh_api 的 ``users.id``（int）。

        給 :mod:`web.trial_report` 用：體驗報告要去 acfh_api 查裝置與 email，
        必須先知道這是哪個 user_id。解析失敗（見 :func:`parse_acfh_user_id`）
        回傳 ``None``，**不拋例外**——呼叫端（伺服器路由）不該因為一筆格式怪異的
        id 就整支請求 500，而是把它當成「查無此人」擋下。
        """
        return parse_acfh_user_id(self.id)


class RecipientBook:
    """一份發送對象名單（唯讀快照）。

    ``generated_at`` 是 producer 標記的產出時間（可能為 ``None``），只拿來在畫面上
    告訴操作者「這份名單多新」，不參與任何邏輯判斷。
    """

    def __init__(
        self, recipients: list[Recipient], *, generated_at: str | None = None
    ) -> None:
        self._recipients: list[Recipient] = list(recipients)
        self.generated_at = generated_at
        # 先建好 id → Recipient 索引，讓 get() 是 O(1)：/preview 每次送出都會反查一次，
        # 名單長度可能上百筆，逐筆線性掃描沒有必要。重複 id 時後者覆蓋前者
        # （producer 端不該產出重複 id，真的重複也只影響那一筆，不值得為此拒收整份）。
        self._by_id: dict[str, Recipient] = {rec.id: rec for rec in self._recipients}

    @classmethod
    def empty(cls) -> "RecipientBook":
        """空名單。名單檔缺失／壞掉時的降級目標，也是「沒設定名單」的預設值。"""
        return cls([], generated_at=None)

    def all(self) -> list[Recipient]:
        """全部對象（含不可選的）—— 給渲染用，不可選者要顯示成灰掉的選項。"""
        return list(self._recipients)

    def selectable(self) -> list[Recipient]:
        """只回可選者（ok 且有電話）。"""
        return [rec for rec in self._recipients if rec.is_selectable]

    def get(self, recipient_id: str) -> Recipient | None:
        """以 id 反查對象，**只在該 id 為可選時回傳**，否則 ``None``。

        這是防竄改的核心：``/preview`` 拿前端送來的 recipient_id 進來查，未知 id、
        或指向 ambiguous / not_found 的 id，一律得到 ``None`` → 呼叫端擋下。
        前端無從讓一個「不可選」的對象變成「可發送」。
        """
        rec = self._by_id.get(recipient_id)
        if rec is None or not rec.is_selectable:
            return None
        return rec

    def is_empty(self) -> bool:
        """名單是否為空。空名單 → 表單維持手動輸入（與現況完全一致）。"""
        return not self._recipients


def parse_acfh_user_id(recipient_id: str) -> int | None:
    """把 ``"u46"`` 這種 Recipient id 解析成 acfh_api 的 ``users.id``（``46``）。

    **格式不保證乾淨**：``tools/build_recipients.py`` 目前固定用
    ``id_prefix="u"`` 產生這個欄位，但這只是「目前」的 producer 行為 —— 未來
    有人改了 prefix、或有人手動塞測試資料，格式都可能不符。所以這裡不假設，
    只信兩件事：開頭是 :data:`_ACFH_USER_ID_PREFIX`、其餘是純數字；不符合就
    回傳 ``None``，**絕不拋例外**（呼叫端把它當成「無法解析」擋下即可，
    不該因為一筆髒 id 就讓整支請求崩潰）。

    非字串輸入（型別錯）同樣回 ``None``，不假設呼叫端一定傳對型別。
    """
    if not isinstance(recipient_id, str):
        return None
    if not recipient_id.startswith(_ACFH_USER_ID_PREFIX):
        return None
    digits = recipient_id[len(_ACFH_USER_ID_PREFIX):]
    if not digits.isdigit():
        return None
    return int(digits)


def _parse_recipient(entry: dict[str, object]) -> Recipient | None:
    """把 JSON 裡的一筆物件轉成 :class:`Recipient`；欄位缺到無法辨識就回 ``None``。

    id 與 name 是「識別」與「顯示」的最低需求，缺任一這筆就無意義，直接丟棄
    （回 None 讓呼叫端跳過），而不是硬塞空字串進去 —— 一個沒有名字的選項對操作者
    毫無意義。其餘欄位型別不對時給安全預設，不讓單筆髒資料害整份名單解析失敗。
    """
    recipient_id = entry.get("id")
    name = entry.get("name")
    if not isinstance(recipient_id, str) or not recipient_id:
        return None
    if not isinstance(name, str) or not name:
        return None

    phone = entry.get("phone")
    # 只接受非空字串當電話；null / 空字串 / 型別錯都收斂成 None（＝沒有電話）。
    if not isinstance(phone, str) or not phone:
        phone = None

    match_status = entry.get("match_status")
    if not isinstance(match_status, str) or not match_status:
        match_status = _FALLBACK_STATUS

    device = entry.get("device")
    device = device if isinstance(device, str) else ""
    borrow_date = entry.get("borrow_date")
    borrow_date = borrow_date if isinstance(borrow_date, str) else ""

    # 以下四欄純供 /trial-email 表格顯示，沿用「型別不對就當空字串」的降級風格，
    # 缺漏一律預設 ""（不影響 is_selectable / get() 反查，發送對象下拉完全不受影響）。
    # JSON 的 "status" 鍵對到 dataclass 的 trial_status（避免與 match_status 混淆）。
    days = entry.get("days")
    days = days if isinstance(days, str) else ""
    used_days = entry.get("used_days")
    used_days = used_days if isinstance(used_days, str) else ""
    business = entry.get("business")
    business = business if isinstance(business, str) else ""
    trial_status = entry.get("status")
    trial_status = trial_status if isinstance(trial_status, str) else ""

    return Recipient(
        id=recipient_id,
        name=name,
        phone=phone,
        device=device,
        borrow_date=borrow_date,
        match_status=match_status,
        days=days,
        used_days=used_days,
        business=business,
        trial_status=trial_status,
    )


def load_recipients(path: str | Path) -> RecipientBook:
    """讀取並解析名單 JSON 檔，回傳 :class:`RecipientBook`。

    **任何讀不到／解析不了的情況都降級成 :meth:`RecipientBook.empty`，不拋例外。**
    這個名單是加值功能，花錢的發送介面不能因為它壞掉就掛：

    * 檔案不存在 → 靜默回 empty（producer 還沒跑很正常，不值得記 warning 洗版）。
    * 其他 I/O 錯誤 / JSON 壞掉 / 結構不符 → 記一筆 WARNING 後回 empty，
      讓維運看得到「名單沒生效」，但服務照常以手動輸入模式運作。

    用具體例外類型（不裸 ``except``）：``FileNotFoundError`` 與其餘 ``OSError``
    要分開處理（前者是預期狀態、後者才是異常），``json.JSONDecodeError`` 是
    ``ValueError`` 的子類，用 ``ValueError`` 接。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        # 預期內的狀態：producer 尚未產出名單。降級成手動輸入，不記 warning。
        return RecipientBook.empty()
    except OSError as exc:
        # 權限不足、路徑是目錄、磁碟錯誤等 —— 這些才是該讓維運看到的異常。
        logger.warning("讀取名單檔失敗（%s），改以空名單降級（表單維持手動輸入）：%s", target, exc)
        return RecipientBook.empty()

    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("名單檔 JSON 解析失敗（%s），改以空名單降級：%s", target, exc)
        return RecipientBook.empty()

    if not isinstance(data, dict):
        logger.warning("名單檔頂層結構不符（應為物件），改以空名單降級：%s", target)
        return RecipientBook.empty()

    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str):
        generated_at = None

    raw_recipients = data.get("recipients")
    if not isinstance(raw_recipients, list):
        logger.warning("名單檔缺少 recipients 陣列，改以空名單降級：%s", target)
        return RecipientBook.empty()

    recipients: list[Recipient] = []
    for entry in raw_recipients:
        if not isinstance(entry, dict):
            continue  # 單筆不是物件就跳過，不讓一筆髒資料拖垮整份名單
        parsed = _parse_recipient(entry)
        if parsed is not None:
            recipients.append(parsed)

    return RecipientBook(recipients, generated_at=generated_at)
