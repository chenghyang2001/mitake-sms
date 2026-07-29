"""發送稽核留底：把「誰在何時發了幾則、扣了幾點、是否可能已扣點」寫成檔案。

為什麼一定要有這層：發簡訊是**不可逆且花共用點數**的動作。事後要回答
「上週三那 6 點是誰燒的」「這筆 msgid 到底有沒有送出去」，只能靠留底；
`send_sms` 的回傳值一離開 process 就沒了。

三個刻意的設計：

1. **號碼只留後四碼**（:func:`mask_phone`）。稽核檔的用途是對帳，不是通訊錄；
   完整手機號碼是個資，落地一份就多一份外洩面。訊息內容則**完全不寫入**
   ——它可能含驗證碼或內部資訊，而對帳根本不需要它。
2. **每次發送寫兩筆**：送出前先寫 ``attempt``、拿到結果再寫 ``result``。
   只寫結果的話，process 若在「三竹已收單」與「寫檔」之間被 kill（VPS OOM、
   systemd restart），就會留下一筆扣了點卻查無紀錄的幽靈簡訊 —— 而那正是
   最需要留底的情境。``attempt`` 有、``result`` 沒有，本身就是一個清楚的信號。
3. **寫檔失敗不會打斷發送流程**（:meth:`AuditLog.record` 回傳 bool 而非拋例外）。
   簡訊已經送出去了，這時候讓 Web 回一個 500 錯誤頁，只會誘導使用者重按送出
   ——為了「記帳失敗」而多扣一點、多送一封，代價比記不到帳更高。失敗改走
   logger.error，讓它在 journalctl 留下痕跡。

4. **可以被回讀**（:meth:`AuditLog.tail_lines`）。稽核檔同時是速率上限在重啟後
   唯一的記憶來源 —— ``RateLimiter`` 只活在記憶體裡，一次 ``systemctl restart``
   就把當小時的預算清成零。回讀同樣是 best-effort（讀不到只回空 list），
   因為它是防呆，不該讓服務起不來。

檔案格式是 JSON Lines（一行一筆），append-only：純文字可 grep、可 tail，
不需要任何額外工具，也不會因為程式中途死掉而毀掉整個檔案。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "DEFAULT_AUDIT_FILENAME",
    "DEFAULT_TAIL_LINES",
    "ENV_AUDIT_PATH",
    "AuditLog",
    "default_audit_path",
    "mask_phone",
]

logger = logging.getLogger(__name__)

# 稽核檔位置可用環境變數覆寫（VPS 上建議指到 /var/log/mitake-sms/ 之類的持久位置，
# 見 deploy/mitake-web.service 的註解）。沒設就落在 repo 的 logs/ 底下。
ENV_AUDIT_PATH = "MITAKE_WEB_AUDIT_PATH"
DEFAULT_AUDIT_FILENAME = "send-audit.jsonl"

# 錯誤訊息可能很長（三竹的原始回應會被塞進去），截斷避免單行爆掉整個檔案。
_MAX_ERROR_CHARS = 500

# 遮罩後保留的末幾碼。四碼足以在對帳時認出「這是我發給誰的」，又不構成完整號碼。
_VISIBLE_TAIL_DIGITS = 4

# 重啟時回讀多少行來回填速率上限（見 web.server 的 restore_rate_limit_from_audit）。
# 一則簡訊寫兩行（attempt + result），每小時上限預設 20 則＝40 行；抓 500 行是留給
# 「上限被調大」與「夾雜失敗紀錄」的餘裕。
DEFAULT_TAIL_LINES = 500

# 回讀時最多從檔尾往回抓多少位元組。稽核檔是 append-only、會長到數十 MB，
# 為了看最後幾十行而整檔載入記憶體並不划算（而且那發生在服務啟動的關鍵路徑上）。
_TAIL_MAX_BYTES = 512 * 1024


def default_audit_path() -> Path:
    """回傳稽核檔的預設路徑（可由 :data:`ENV_AUDIT_PATH` 覆寫）。

    不用 ``os.environ.get(name, fallback)`` 的單行寫法：環境變數被設成空字串
    （systemd 的 ``Environment=MITAKE_WEB_AUDIT_PATH=`` 很容易寫成這樣）時，
    單行寫法會回傳 ``Path("")``，一路等到真的要寫檔才炸；這裡把空字串當成沒設。
    """
    raw = os.environ.get(ENV_AUDIT_PATH)
    if raw is not None and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path(__file__).resolve().parent.parent / "logs" / DEFAULT_AUDIT_FILENAME


def mask_phone(phone: object) -> str:
    """把手機號碼遮成只剩後四碼，例如 ``0910869893`` → ``******9893``。

    短於或等於四碼的輸入（多半是使用者亂打的字串）**全部遮掉**：若照原樣保留，
    等於把一個沒被驗證過的短輸入原封不動寫進稽核檔，反而洩漏更多。
    """
    text = phone if isinstance(phone, str) else str(phone)
    text = text.strip()
    if len(text) <= _VISIBLE_TAIL_DIGITS:
        return "*" * len(text)
    return "*" * (len(text) - _VISIBLE_TAIL_DIGITS) + text[-_VISIBLE_TAIL_DIGITS:]


def _local_now() -> datetime:
    """帶時區的現在時間。

    ``astimezone()`` 讓 isoformat 一定帶 ``+08:00`` 這種偏移量。VPS 系統時區是 UTC、
    人在台灣看報表 —— 沒有偏移量的時間戳在對帳時會變成「這是幾點？」的猜謎。
    """
    return datetime.now(timezone.utc).astimezone()


def _truncate(value: object, limit: int = _MAX_ERROR_CHARS) -> str | None:
    """把任意值轉成有長度上限的字串；``None`` 維持 ``None``（欄位缺值與空字串是兩件事）。"""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截斷）"


class AuditLog:
    """把發送紀錄以 JSON Lines 追加寫入檔案。

    ``now`` 與 ``path`` 都可注入，測試不必碰真實時間或真實路徑。
    內部有鎖：伺服器跑在 :class:`~http.server.ThreadingHTTPServer` 上，
    兩個請求同時寫同一個檔會交錯出壞掉的行。
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        fsync: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else default_audit_path()
        self._now = now if now is not None else _local_now
        # 預設 fsync：稽核的價值全在「機器突然斷電/被 kill 時那一筆還在不在」。
        # 每則簡訊才寫兩行，這點 I/O 成本相對於一則簡訊的金額完全可以忽略。
        # 測試可關掉以免拖慢（tmp_path 上的 fsync 在 Windows 特別慢）。
        self._fsync = fsync
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 低階寫入
    # ------------------------------------------------------------------ #

    def record(self, event: str, **fields: Any) -> bool:
        """寫入一筆紀錄，回傳是否成功。**任何 I/O 失敗都不會往外拋。**

        理由見模組 docstring：這個函式的呼叫點在「簡訊已經送出去之後」，
        在那裡拋例外會把使用者導向錯誤頁，而錯誤頁會誘導重送 —— 為了記不到帳
        再扣一點、再送一封。
        """
        record: dict[str, Any] = {"time": self._now().isoformat(), "event": event}
        # 值為 None 的欄位一律保留：對帳時「msgid 是 null」與「根本沒有 msgid 這個欄位」
        # 代表不同意義（前者是三竹沒給，後者是程式沒寫），不能靠省略欄位來表達。
        record.update(fields)

        try:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            # 有人塞了不可序列化的值進來。降級成 repr 也要把這筆留下來，
            # 因為「發生過一次發送」這件事比欄位漂不漂亮重要得多。
            logger.error("稽核紀錄無法序列化（event=%s）：%s", event, exc)
            line = json.dumps(
                {"time": record.get("time"), "event": event, "raw": repr(fields)},
                ensure_ascii=False,
                sort_keys=True,
            )

        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    if self._fsync:
                        os.fsync(handle.fileno())
            return True
        except OSError as exc:
            # 只記路徑與錯誤，不重印整筆紀錄：紀錄裡沒有憑證，但也沒必要讓
            # journalctl 變成第二份稽核檔。
            logger.error("稽核紀錄寫入失敗（path=%s）：%s", self.path, exc)
            return False

    # ------------------------------------------------------------------ #
    # 低階讀取（只給「重啟後回填速率上限」用）
    # ------------------------------------------------------------------ #

    def tail_lines(
        self, max_lines: int = DEFAULT_TAIL_LINES, *, max_bytes: int = _TAIL_MAX_BYTES
    ) -> list[str]:
        """回傳稽核檔最後 ``max_lines`` 行。**讀不到就回空 list，不拋例外。**

        與 :meth:`record` 同一個理由：呼叫端是服務啟動流程，這裡拋例外會讓
        「稽核檔不見了」升級成「發簡訊介面起不來」。回填是防呆，不是必要條件。

        從檔尾 ``seek`` 回讀固定位元組而非整檔讀入 —— 這個檔只增不減，且這段
        程式跑在啟動的關鍵路徑上。
        """
        if max_lines < 1:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                start = max(0, handle.tell() - max_bytes)
                handle.seek(start)
                raw = handle.read()
        except OSError as exc:
            logger.warning("讀取稽核檔失敗（path=%s）：%s", self.path, exc)
            return []

        # errors="replace"：斷電可能在檔尾留下半個 UTF-8 字元，不該讓整個回填失效。
        lines = raw.decode("utf-8", errors="replace").splitlines()
        # 從檔案中間切進去時，第一行幾乎一定是半行。丟掉它比留著讓它 JSON 解析失敗
        # 更誠實（後者在 log 裡看起來像「稽核檔壞了」，但其實是我們自己切的）。
        if start > 0 and lines:
            lines = lines[1:]
        return lines[-max_lines:]

    # ------------------------------------------------------------------ #
    # 語意化介面（呼叫端只用這兩個）
    # ------------------------------------------------------------------ #

    def record_attempt(
        self, *, request_id: str, phone: str, segments: int, chars: int
    ) -> bool:
        """送出前的留底。有 attempt 卻沒有對應 result＝那次發送死在半路，需人工查證。"""
        return self.record(
            "attempt",
            request_id=request_id,
            phone_masked=mask_phone(phone),
            segments=segments,
            chars=chars,
        )

    def record_result(
        self,
        *,
        request_id: str,
        phone: str,
        segments: int,
        chars: int,
        success: bool,
        msgid: str | None = None,
        account_point: int | None = None,
        possibly_charged: bool = False,
        error_kind: str | None = None,
        error_message: object = None,
    ) -> bool:
        """送出後的留底。

        ``possibly_charged`` 一定要寫進去：日後對帳時，「失敗」這兩個字沒有告訴你
        點數扣了沒，只有這個欄位有。
        """
        return self.record(
            "result",
            request_id=request_id,
            phone_masked=mask_phone(phone),
            segments=segments,
            chars=chars,
            success=bool(success),
            msgid=msgid,
            account_point=account_point,
            possibly_charged=bool(possibly_charged),
            error_kind=error_kind,
            error=_truncate(error_message),
        )
