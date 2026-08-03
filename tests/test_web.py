"""web/ 的離線回歸鎖：把「錯了會花錢」的行為釘死在檔案裡。

**為什麼這支測試必須存在。** `doc/spec-part2-part3.md` 早就把 `tests/test_web.py`
列為交付物，但它一直沒被寫出來；期間的驗證都是跑完即刪的臨時腳本，等於
「這批程式碼曾經被驗過一次」，而不是「被鎖住了」。而 `tests/conftest.py` 花了 49 行
論證「靠自律不夠、必須升級成機制」—— 結果全專案**唯一會花錢的模組**只有自律。

本檔鎖的是十一類「改壞了不會有人發現、但下一個使用者會多扣點」的行為：

1. `possibly_charged=True` 的失敗頁**不得有任何 `<form>`**，速率額度**不得退還**。
   （最容易壞的改法：把 `except mitake.MitakeAPIError` 併進
   `except mitake.MitakeError` —— HANDOFF §2.1 的「已知待補」正好誘導人這樣改，
   併了之後 `possibly_charged` 分流會**靜默**失效，畫面看起來一切正常。）
2. `possibly_charged=False` 的失敗頁要有重送按鈕，且額度要退還。
3. 一次性 token：同一張 token 送兩次，真正的 sender 只能被呼叫一次。
   （最容易壞的改法：把 `TokenStore.consume` 的 `pop` 改成 `get`。）
4. 確認頁顯示的號碼／內容 == sender 實際收到的號碼／內容。
   這是「二階段確認」這個設計的**全部**價值：對不上的話，確認頁只是一層假象。
5. 「三竹根本沒收到」的失敗不得寫成「三竹已明確拒絕」（會害人去後台找不存在的紀錄）。
6. 稽核寫入失敗時，成功頁不得宣稱「已寫入稽核紀錄」。
7. 稽核寫入失敗時，**「可能已扣點」頁**同樣不得宣稱已留底 —— 這頁比成功頁更嚴重：
   使用者唯一的處置是拿線索去後台查證，沒留底就連要查什麼都不知道。
8. 撞上速率上限時（``/send`` 與 ``/preview`` 兩條都算）不得把使用者打的內容丟掉。
9. CSP 的 ``script-src`` 不得退回 ``'unsafe-inline'``（那等於這道牆對腳本執行沒有作用），
   且 nonce 每個回應都必須不同。
10. 設了 ``require_access_email`` 之後，除 ``/health`` 外一律要擋；沒設時不得誤擋。
11. 速率上限要能從稽核檔回填 —— 否則一次 ``restart`` 就把當小時的預算清成零。

**測試紀律**：全檔不碰外部網路。sender 一律注入假的（`tests/conftest.py` 的 autouse
護欄另外封鎖了 `mitake._OPENER.open`，兩層互不衝突）；時間用注入時鐘推進，
不用 `sleep`；稽核檔一律寫到 pytest 的 `tmp_path`，不碰 repo 的 `logs/`；
原則上走 `SmsWebApp.route()` 這個純函式介面，不開 socket。

唯一的例外是 `test_live_server_sends_csp_matching_the_page_nonce`：它在
`127.0.0.1` 的隨機 port 上開一個真的伺服器。因為 CSP **標頭**是 HTTP 層送出的、
頁面是路由層產生的，「兩邊的 nonce 一致」這件事只有真的收一次回應才驗得到。
它仍然不連任何外部主機，sender 一樣是假的。
"""

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.message import Message
from html import escape as _html_escape
from http import HTTPStatus
from pathlib import Path

import pytest

# conftest.py 已把 repo 根塞進 sys.path，這裡再補一次是為了讓「單獨執行本檔」
# （某些 IDE 的 run-this-file 不載入 conftest 的 sys.path 調整）也能 import。
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import mitake  # noqa: E402

from web import batch_recipients  # noqa: E402
from web import templates  # noqa: E402
from web.audit import AuditLog  # noqa: E402
from web.recipients import Recipient, RecipientBook  # noqa: E402
from web.server import (  # noqa: E402
    ACCESS_EMAIL_HEADER,
    MAX_MULTIPART_BODY_BYTES,
    SmsWebApp,
    TokenStore,
    _csp_header,  # 私有但刻意直測：CSP 字串是安全邊界，不該只靠端對端那一條驗
    create_server,
    make_handler,
)
from web.trial_report import (  # noqa: E402
    REASON_DAYS_NOT_REACHED,
    REASON_DB_ERROR,
    TrialReportResult,
)

VALID_PHONE = "0912345678"
SHORT_BODY = "測試內容ABC"

# 回填測試的固定「現在」。用寫死的時刻而不是 datetime.now()：稽核檔的時間戳與
# 這個值的差就是「距今幾秒」，兩邊都固定住，測試才不會在跨小時的那一秒閃紅。
AUDIT_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))

# 實撥取得的成功回應（經 mitake.send_sms 解析後的形狀）。
SEND_OK_RESULT = {"msgid": "0313887539", "statuscode": "1", "account_point": 12572}

# 確認頁裡藏 token 的那個 hidden input。token 是 urlsafe base64，不會被 HTML 跳脫。
_TOKEN_PATTERN = re.compile(r'name="token" value="([^"]+)"')


# --------------------------------------------------------------------------- #
# 測試替身
# --------------------------------------------------------------------------- #


class FakeClock:
    """可手動推進的單調時鐘。

    用它而不是 `time.sleep`：TTL 是 600 秒、速率視窗是 3600 秒，真的睡過去
    測試就沒人跑得完，而縮短 TTL 來配合測試等於測到一組沒人在用的參數。
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class RecordingSender:
    """假的 `mitake.send_sms`：記下每次呼叫，並照設定回傳或拋出。

    **這是全檔唯一的發送出口**。它存在的意義就是讓「sender 被呼叫幾次」變成一個
    可斷言的數字 —— 重複發送這種 bug 在畫面上完全看不出來，只有計數看得出來。
    """

    def __init__(self, *, result: dict | None = None, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result if result is not None else dict(SEND_OK_RESULT)
        self._error = error

    def __call__(self, phone: str, body: str, *, max_segments: int, timeout: float) -> dict:
        self.calls.append(
            {"phone": phone, "body": body, "max_segments": max_segments, "timeout": timeout}
        )
        if self._error is not None:
            raise self._error
        return dict(self._result)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class SequencedSender:
    """假的 `mitake.send_sms`：依呼叫順序回傳／拋出不同結果。

    給批次發送「部分成功、部分失敗、部分未確認」的情境用——`RecordingSender`
    全程只有單一固定結果／例外，模擬不出批次送出迴圈裡每一筆結果不同的情況。
    """

    def __init__(self, outcomes: "list[dict | BaseException]") -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, phone: str, body: str, *, max_segments: int, timeout: float) -> dict:
        self.calls.append(
            {"phone": phone, "body": body, "max_segments": max_segments, "timeout": timeout}
        )
        outcome = self._outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class RecordingStatusQuery:
    """假的 `mitake.query_message_status`：記下每次呼叫，照設定回傳或拋出。

    投遞狀態查詢是唯讀免費的，但**一樣不准真的打三竹** —— 打多了會讓來源 IP 被
    限流，而那會連發簡訊一起壞掉（statuscode=k）。
    """

    def __init__(
        self, *, result: dict | None = None, error: BaseException | None = None
    ) -> None:
        self.calls: list[dict] = []
        self._result = result if result is not None else status_result("4")
        self._error = error

    def __call__(self, msgid: str, *, timeout: float) -> dict:
        self.calls.append({"msgid": msgid, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return dict(self._result)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FailingAuditLog(AuditLog):
    """稽核寫入永遠失敗（模擬磁碟滿／路徑不可寫）。

    覆寫最低階的 `record`，`record_attempt` / `record_result` 都會經過它。
    """

    def __init__(self, path) -> None:
        super().__init__(path, fsync=False)
        self.attempted_events: list[str] = []

    def record(self, event: str, **fields: object) -> bool:
        self.attempted_events.append(event)
        return False


class FutureMitakeError(mitake.MitakeError):
    """模擬「mitake.py 日後新增、web 層還不認得」的例外型別。

    它沒有 `possibly_charged` 屬性 —— 這正是不能寫成
    `except MitakeError as e: e.possibly_charged` 的原因。
    """


# --------------------------------------------------------------------------- #
# 共用工具
# --------------------------------------------------------------------------- #


def _form(**fields: str) -> dict[str, list[str]]:
    """把 kwargs 轉成 `parse_qs` 那種 {key: [value]} 形狀。"""
    return {key: [value] for key, value in fields.items()}


def _batch_form(body: str, **extra: str) -> dict[str, list[str]]:
    """組多人模式 `/preview` 的表單欄位（不含檔案，檔案另外用 `files=` 傳）。

    永遠帶 `send-mode=batch`，這是伺服器端分流到批次邏輯的唯一依據
    （見 `web.server.SmsWebApp.handle_preview`）。
    """
    fields = {"send-mode": "batch", "body": body}
    fields.update(extra)
    return _form(**fields)


def status_result(
    statuscode: str = "4",
    *,
    msgid: str = "0315772761",
    status_time: str = "20260729143730",
) -> dict:
    """組出一筆投遞狀態結果。

    刻意用**真的** `mitake.parse_status_response` 去解一段真實格式的回應，而不是
    手寫一個 dict：手寫的 dict 可以自由亂填（例如 statuscode=1 卻 is_delivered=True），
    測起來永遠是綠的，卻證明不了 web 層拿到真實資料時會怎麼渲染。
    這支函式不碰網路（純解析）。
    """
    return mitake.parse_status_response(f"{msgid}\t{statuscode}\t{status_time}")


def query_status(app: SmsWebApp, msgid: str | None = None, **kwargs):
    """走一次 `GET /status`（不帶 msgid 就是查詢表單頁）。"""
    query = _form(msgid=msgid) if msgid is not None else {}
    return app.route("GET", "/status", query=query, **kwargs)


def make_app(
    tmp_path: Path,
    sender: RecordingSender,
    *,
    clock: FakeClock | None = None,
    rate_limit: int = 20,
    audit_log: AuditLog | None = None,
    token_ttl_seconds: float = 600,
    token_store: TokenStore | None = None,
    require_access_email: str | None = None,
    status_query: "RecordingStatusQuery | None" = None,
    status_query_limit: int = 30,
    recipient_source: "Callable[[], RecipientBook] | None" = None,
    trial_report_sender: "Callable[..., object] | None" = None,
    staff_bcc: "tuple[str, ...] | None" = None,
    request_id_factory: "Callable[[], str] | None" = None,
) -> SmsWebApp:
    """建一個完全離線的 app：假 sender、假 status_query、假時鐘、稽核檔落在 tmp_path。

    ``recipient_source`` 預設 None：既有呼叫端不傳，走 SmsWebApp 的預設（空名單、
    手機號碼手動輸入模式），既有測試因此完全不受下拉選單功能影響。下拉相關的測試
    自己注入一個回傳記憶體內 RecipientBook 的 source。

    ``trial_report_sender`` / ``staff_bcc`` 同理：預設 None 時 SmsWebApp 會用真正的
    ``web.trial_report.send_trial_report`` 與讀 ``MITAKE_WEB_STAFF_BCC`` 環境變數，
    但**本檔任何測試都不該讓它走到那條路徑**（會真的連 acfh_api／真的寄 Gmail）——
    凡是會呼叫 ``POST /trial-email/send-report`` 的測試都必須自己傳一個假 sender。

    ``request_id_factory`` 預設 None 時維持既有的固定字串 ``"req-test"``
    （既有測試如 ``test_unconfirmed_page_never_claims_audit_when_it_failed``
    直接斷言這個字串會出現在畫面上，不能改成動態值）。批次發送的測試需要驗證
    「同一批次的每一筆 request_id 各自獨立」，才會自己傳一個會遞增的 factory。
    """
    return SmsWebApp(
        sender=sender,
        status_query=status_query,
        audit_log=(
            audit_log
            if audit_log is not None
            else AuditLog(tmp_path / "send-audit.jsonl", fsync=False)
        ),
        rate_limit=rate_limit,
        status_query_limit=status_query_limit,
        token_ttl_seconds=token_ttl_seconds,
        token_store=token_store,
        clock=clock if clock is not None else FakeClock(),
        request_id_factory=(
            request_id_factory if request_id_factory is not None else lambda: "req-test"
        ),
        require_access_email=require_access_email,
        recipient_source=recipient_source,
        trial_report_sender=trial_report_sender,
        staff_bcc=staff_bcc,
    )


def sample_recipient_book() -> RecipientBook:
    """一份記憶體內名單：1 筆 ok（可選）+ 1 筆 ambiguous + 1 筆 not_found（皆不可選）。

    刻意涵蓋三種 match_status，讓下拉測試同時驗到「可選者能選」與「不可選者灰掉但看得到」。
    ok 那筆的電話是合法的 09 開頭 10 碼（過得了 mitake.validate_phone）。
    """
    return RecipientBook(
        [
            Recipient(
                id="u46",
                name="陳筱琪",
                phone="0918123424",
                device="體驗活動14天-陳筱琪4c74",
                borrow_date="2026-07-29",
                match_status="ok",
            ),
            Recipient(
                id="u43",
                name="青蘋果",
                phone=None,
                device="體驗活動14天-青蘋果",
                borrow_date="2026-07-20",
                match_status="ambiguous",
            ),
            Recipient(
                id="loan-x",
                name="青化活動中心",
                phone=None,
                device="體驗活動14天-青化",
                borrow_date="2026-07-18",
                match_status="not_found",
            ),
        ],
        generated_at="2026-07-30T11:00:00+08:00",
    )


def access_headers(value: str | None, *, name: str = ACCESS_EMAIL_HEADER) -> Message:
    """組出帶（或不帶）Access 身分標頭的標頭物件。

    刻意用真的 `email.message.Message`（`BaseHTTPRequestHandler.headers` 的型別）
    而不是 dict：標頭名稱的大小寫由對方決定，Message 的查找不分大小寫而 dict 分，
    用 dict 測會測不到真實行為。
    """
    message = Message()
    if value is not None:
        message[name] = value
    return message


def audit_line(*, age_seconds: float, **fields: object) -> str:
    """組出一行稽核紀錄，時間戳是「`AUDIT_NOW` 之前 age_seconds 秒」。"""
    record: dict[str, object] = {
        "time": (AUDIT_NOW - timedelta(seconds=age_seconds)).isoformat(),
        "event": "result",
        "request_id": "req-old",
        "phone_masked": "******5678",
        "segments": 1,
        "chars": 5,
        "success": True,
        "possibly_charged": False,
        "msgid": "0313887539",
        "account_point": 12572,
        "error_kind": None,
        "error": None,
    }
    record.update(fields)
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def write_audit(tmp_path: Path, *lines: str) -> Path:
    """把幾行稽核紀錄寫進檔案，回傳路徑。"""
    path = tmp_path / "send-audit.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def issue_token(
    app: SmsWebApp,
    phone: str = VALID_PHONE,
    body: str = SHORT_BODY,
    headers: Message | None = None,
) -> str:
    """走一次 `/preview` 拿到確認頁的一次性 token。"""
    response = app.route("POST", "/preview", _form(phone=phone, body=body), headers=headers)
    assert response.status == HTTPStatus.OK, response.text
    match = _TOKEN_PATTERN.search(response.text)
    assert match is not None, "確認頁沒有 token，二階段確認已經壞了"
    return match.group(1)


def send_once(
    app: SmsWebApp, *, phone: str = VALID_PHONE, body: str = SHORT_BODY, **extra: str
):
    """完整走一次 preview → send。``extra`` 用來塞竄改用的多餘欄位。"""
    token = issue_token(app, phone, body)
    return app.route("POST", "/send", _form(token=token, **extra))


def api_error(
    *, possibly_charged: bool, kind: str = mitake.KIND_API, msgid: str | None = None
) -> mitake.MitakeAPIError:
    return mitake.MitakeAPIError(
        "三竹回應：statuscode=x",
        kind=kind,
        possibly_charged=possibly_charged,
        response={"msgid": msgid} if msgid is not None else None,
    )


# --------------------------------------------------------------------------- #
# 1. 基本路由（冒煙）
# --------------------------------------------------------------------------- #


def test_health_never_calls_sender(tmp_path: Path) -> None:
    """/health 不得碰任何會花錢或會打外部 API 的東西（它沒有認證，誰都打得到）。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    response = app.route("GET", "/health")

    assert response.status == HTTPStatus.OK
    assert json.loads(response.text)["status"] == "ok"
    assert sender.call_count == 0


def test_index_shows_form_and_rate_usage(tmp_path: Path) -> None:
    """表單頁要能開，且要顯示本小時用量（使用者判斷還能不能發的唯一依據）。"""
    app = make_app(tmp_path, RecordingSender(), rate_limit=7)

    response = app.route("GET", "/")

    assert response.status == HTTPStatus.OK
    assert "<form" in response.text
    assert "本小時已送出 0 / 7 則" in response.text


# --------------------------------------------------------------------------- #
# 2. 確認頁 == 實際送出的東西（這是二階段設計的全部價值）
# --------------------------------------------------------------------------- #


def test_preview_content_matches_sender_payload(tmp_path: Path) -> None:
    """確認頁上顯示的號碼／內容，必須與 sender 實際收到的**完全一致**。

    對不上的話確認頁只是一層假象：使用者確認的是 A，送出的是 B。
    這裡刻意用「需要正規化」的輸入（連字號號碼 + CRLF 換行）測，
    因為正規化正是兩邊最容易漂開的地方。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    submitted_body = "確認頁\r\n第二行"
    normalized_body = "確認頁\n第二行"

    preview = app.route("POST", "/preview", _form(phone="0912-345-678", body=submitted_body))
    assert preview.status == HTTPStatus.OK
    assert VALID_PHONE in preview.text
    assert normalized_body in preview.text
    assert "7 字 ／ 1 則" in preview.text

    token = _TOKEN_PATTERN.search(preview.text).group(1)
    sent = app.route("POST", "/send", _form(token=token))

    assert sent.status == HTTPStatus.OK
    assert sender.call_count == 1
    assert sender.calls[0]["phone"] == VALID_PHONE
    assert sender.calls[0]["body"] == normalized_body


def test_send_ignores_tampered_phone_and_body(tmp_path: Path) -> None:
    """`/send` 只認 token；表單裡另外塞的號碼／內容一律無效。

    若哪天有人「順手」把號碼與內容改成 hidden input 帶回來，使用者（或中間人）
    就能在看過確認頁**之後**換掉收件人 —— 確認頁等於白做。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    token = issue_token(app, "0987654321", "正確內容")
    response = app.route(
        "POST", "/send", _form(token=token, phone="0900000000", body="被換掉的內容")
    )

    assert response.status == HTTPStatus.OK
    assert sender.calls[-1]["phone"] == "0987654321"
    assert sender.calls[-1]["body"] == "正確內容"


def test_preview_escapes_html_in_body(tmp_path: Path) -> None:
    """使用者輸入是全專案唯一「直接組 HTML」的地方，漏跳脫就是 XSS。"""
    app = make_app(tmp_path, RecordingSender())

    response = app.route(
        "POST", "/preview", _form(phone=VALID_PHONE, body="<script>alert(1)</script>")
    )

    assert response.status == HTTPStatus.OK
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


# 兩種 XSS 形態，分開測是因為**它們的失敗訊號完全不同**：
#
# * 元素內容 breakout：靠角括號自己開一個新標籤。漏跳脫時 `<script>alert(1)` 會原樣出現。
# * 屬性 breakout：**整串不含任何角括號**，靠一個雙引號把 `value="…"` 提早關掉，
#   後面接的 `onfocus=…` 就變成 input 的真實屬性。這種形態只有含角括號的 payload
#   抓不到 —— 注意 `html.escape` **不會動** `onfocus=alert(2)` 這幾個字，跳脫前後
#   它都在字串裡；真正決定它有沒有變成屬性的，是前面那個雙引號有沒有被吃掉。
#   所以這裡禁的是 `" onfocus=`（原樣雙引號 + 事件處理器）而不是 `onfocus=`。
_XSS_PAYLOADS = [
    ("<script>alert(1)</script>", "<script>alert(1)"),
    ('" onfocus=alert(2) x="', '" onfocus='),
]

# 所有會把使用者輸入回填進 HTML 的反射點。`base_kwargs` 只放「讓該函式跑得起來」的
# 必填參數，被測欄位由測試本身塞 payload 進去。
#
# `render_notice` 的 resend_phone / resend_body 必須**成對**才會渲染重送表單
# （見 templates.render_notice 的 docstring），所以測其中一個時要把另一個補上乾淨值。
_REFLECTED_FIELDS = [
    (
        templates.render_form,
        {"max_segments": 5, "chars_per_segment": 70, "script_nonce": "n"},
        "phone",
    ),
    (
        templates.render_form,
        {"max_segments": 5, "chars_per_segment": 70, "script_nonce": "n"},
        "body",
    ),
    (templates.render_notice, {"title": "t", "heading": "h"}, "message"),
    (
        templates.render_notice,
        {"title": "t", "heading": "h", "message": "m", "resend_body": SHORT_BODY},
        "resend_phone",
    ),
    (
        templates.render_notice,
        {"title": "t", "heading": "h", "message": "m", "resend_phone": VALID_PHONE},
        "resend_body",
    ),
]


@pytest.mark.parametrize(
    ("payload", "forbidden"), _XSS_PAYLOADS, ids=["element-breakout", "attribute-breakout"]
)
@pytest.mark.parametrize(
    ("render", "base_kwargs", "field"),
    _REFLECTED_FIELDS,
    ids=[
        "render_form-phone",
        "render_form-body",
        "render_notice-message",
        "render_notice-resend_phone",
        "render_notice-resend_body",
    ],
)
def test_every_reflected_field_is_html_escaped(
    render: Callable[..., str],
    base_kwargs: dict[str, object],
    field: str,
    payload: str,
    forbidden: str,
) -> None:
    """每一個把使用者輸入回填進 HTML 的地方都必須跳脫。**這是回歸鎖，不是漏洞報告。**

    現況是安全的 —— 四個反射點目前都包著 `_e()`。這條測試存在的理由是
    「拿掉任一個 `_e()` 之後整套測試依然全綠」：`test_preview_escapes_html_in_body`
    只涵蓋 `render_preview` 一處，剩下四處
    （`render_form` 的 `value="{phone}"` 與 `<textarea>{body}`、`render_notice` 的
    `{message}`、`_hidden_resend_form` 的兩個 hidden input）等於**沒有任何測試守著**。

    其中最危險的是 `render_form` 的 `value="{phone}"`：號碼驗證**失敗**時才會走到
    這個回填路徑，所以進得去的字串正好是「沒通過驗證的任意輸入」，而且它落在 HTML
    **屬性內** —— 屬性內的注入不需要角括號就能執行。

    最容易壞的改法：有人覺得「這裡是我們自己組的字串，不會有壞東西」而順手把 `_e()`
    拿掉，或重構時漏包一個。兩種都不會被其他測試抓到。
    """
    out = render(**base_kwargs, **{field: payload})

    assert forbidden not in out, f"{field} 沒跳脫：{forbidden!r} 原樣進到 HTML"
    # 反向鎖：確認它是「被跳脫」而不是「被整段丟掉」——
    # 丟掉的話上面那條也會過，但使用者的輸入就沒回填，回填頁等於白做。
    assert _html_escape(payload) in out, f"{field} 沒有以跳脫形式回填"


# --------------------------------------------------------------------------- #
# 3. 一次性 token（重複發送 = 扣兩點、對方收到兩封）
# --------------------------------------------------------------------------- #


def test_token_is_single_use(tmp_path: Path) -> None:
    """同一張 token 送兩次，sender 只能被呼叫 **1** 次，第二次回 409。

    最容易壞的改法：`TokenStore.consume` 的 `pop` 被改成 `get`。改了之後畫面
    看起來完全正常（第二次也顯示「已送出」），只有這個計數會抓到。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    token = issue_token(app)

    first = app.route("POST", "/send", _form(token=token))
    second = app.route("POST", "/send", _form(token=token))

    assert first.status == HTTPStatus.OK
    assert second.status == HTTPStatus.CONFLICT
    assert sender.call_count == 1
    assert "<form" not in second.text


def test_expired_token_returns_410_and_never_sends(tmp_path: Path) -> None:
    """過期的確認頁不得送出（共用電腦上放到隔天還能發簡訊是真實風險）。"""
    sender = RecordingSender()
    clock = FakeClock()
    app = make_app(tmp_path, sender, clock=clock, token_ttl_seconds=600)
    token = issue_token(app)

    clock.advance(601)
    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.GONE
    assert sender.call_count == 0
    assert "<form" not in response.text


def test_unknown_token_returns_409_without_sending(tmp_path: Path) -> None:
    """亂猜的 token（含非 ASCII，使用者會在網址列亂打）不得觸發任何發送。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    for bogus in ("not-a-real-token", "中文亂打", ""):
        response = app.route("POST", "/send", _form(token=bogus))
        assert response.status == HTTPStatus.CONFLICT, bogus

    assert sender.call_count == 0


def test_send_without_token_field_does_not_send(tmp_path: Path) -> None:
    """完全沒有 token 欄位的裸 POST（curl 手打、爬蟲）不得送出。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    response = app.route("POST", "/send", {})

    assert response.status == HTTPStatus.CONFLICT
    assert sender.call_count == 0


# --------------------------------------------------------------------------- #
# 4. possibly_charged 分流（本專案代價最高的單一分支）
# --------------------------------------------------------------------------- #


def test_possibly_charged_page_has_no_form(tmp_path: Path) -> None:
    """可能已扣點 → 頁面上**不得有任何 `<form>`**，連重送的入口都不能給。"""
    sender = RecordingSender(
        error=api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED, msgid="0313887539")
    )
    app = make_app(tmp_path, sender)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "<form" not in response.text
    assert "請勿重送" in response.text
    assert "0313887539" in response.text


def test_possibly_charged_does_not_refund_quota(tmp_path: Path) -> None:
    """可能已扣點 → 速率額度**不退還**（點可能真的花掉了，退還等於放行第二次）。"""
    sender = RecordingSender(error=api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED))
    app = make_app(tmp_path, sender, rate_limit=5)

    before = app.rate_limiter.snapshot().used
    send_once(app)
    after = app.rate_limiter.snapshot().used

    assert before == 0
    assert after == 1


def test_possibly_charged_is_written_to_audit(tmp_path: Path) -> None:
    """稽核檔要留下 `possibly_charged: true` —— 對帳時「失敗」兩字不說明點扣了沒。"""
    audit_path = tmp_path / "send-audit.jsonl"
    sender = RecordingSender(error=api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED))
    app = make_app(tmp_path, sender, audit_log=AuditLog(audit_path, fsync=False))

    send_once(app)

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    result = [record for record in records if record["event"] == "result"][0]
    assert result["success"] is False
    assert result["possibly_charged"] is True


def test_safe_failure_offers_resend_form(tmp_path: Path) -> None:
    """確定沒扣點 → 要給重送按鈕（否則使用者得整段重打，那本身就是出錯來源）。"""
    sender = RecordingSender(error=api_error(possibly_charged=False))
    app = make_app(tmp_path, sender)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "<form" in response.text
    # 重送一定要重走確認頁，不能直接打 /send（那就變成一個按鈕直接花錢）。
    assert 'action="/preview"' in response.text


def test_safe_failure_refunds_quota(tmp_path: Path) -> None:
    """確定沒扣點 → 速率額度要退還，否則使用者被自己的失敗吃掉當日配額。"""
    sender = RecordingSender(error=api_error(possibly_charged=False))
    app = make_app(tmp_path, sender, rate_limit=5)

    send_once(app)

    assert app.rate_limiter.snapshot().used == 0


def test_ip_blocked_has_no_resend_button(tmp_path: Path) -> None:
    """IP 不在白名單是設定問題，重送一百次也一樣失敗 —— 給按鈕只是誘人白按。"""
    sender = RecordingSender(error=api_error(possibly_charged=False, kind=mitake.KIND_IP_BLOCKED))
    app = make_app(tmp_path, sender, rate_limit=5)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "<form" not in response.text
    assert "白名單" in response.text
    # 沒扣點，所以額度仍要退還（與「不給重送」是兩件事）。
    assert app.rate_limiter.snapshot().used == 0


def test_auth_failed_has_no_resend_button(tmp_path: Path) -> None:
    """帳密錯同上：先去改環境變數，不是重送。"""
    sender = RecordingSender(error=api_error(possibly_charged=False, kind=mitake.KIND_AUTH_FAILED))
    app = make_app(tmp_path, sender, rate_limit=5)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "<form" not in response.text
    assert app.rate_limiter.snapshot().used == 0


def test_unclassified_mitake_error_is_treated_as_possibly_charged(tmp_path: Path) -> None:
    """mitake.py 日後新增的例外型別要被**保守**當成可能已扣點。

    這條同時鎖住 except 的順序：若有人把 `except mitake.MitakeAPIError` 併進
    `except mitake.MitakeError`，上面那批 `possibly_charged=False` 的測試就會轉紅。
    """
    sender = RecordingSender(error=FutureMitakeError("日後新增的錯誤"))
    app = make_app(tmp_path, sender, rate_limit=5)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "<form" not in response.text
    assert app.rate_limiter.snapshot().used == 1


def test_unexpected_exception_is_treated_as_possibly_charged(tmp_path: Path) -> None:
    """連 bug（非 MitakeError）都要保守處理：例外可能發生在三竹已收單之後。"""
    sender = RecordingSender(error=RuntimeError("boom"))
    app = make_app(tmp_path, sender, rate_limit=5)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "<form" not in response.text
    assert "請勿重送" in response.text
    assert app.rate_limiter.snapshot().used == 1


# --------------------------------------------------------------------------- #
# 5. 樣板層的純斷言（最便宜的一條防線）
# --------------------------------------------------------------------------- #


def test_render_failed_unconfirmed_has_no_form() -> None:
    """不經任何伺服器邏輯，直接鎖「可能已扣點」的樣板不得產生 `<form>`。"""
    html = templates.render_failed_unconfirmed(
        phone=VALID_PHONE, segments=2, msgid="0313887539", message="回應無法確認"
    )

    assert "<form" not in html
    assert "請勿重送" in html


def test_render_sent_has_no_form() -> None:
    """成功頁上唯一該做的事是離開；多一個送出捷徑就多一次手滑扣點。"""
    html = templates.render_sent(
        phone=VALID_PHONE, segments=1, chars=6, msgid="0313887539", account_point=12572
    )

    assert "<form" not in html


def test_render_sent_never_claims_audit_when_it_failed() -> None:
    """`audit_ok=False` 時，成功頁不得出現「已寫入稽核紀錄」這句話。"""
    html = templates.render_sent(
        phone=VALID_PHONE,
        segments=1,
        chars=6,
        msgid="0313887539",
        account_point=12572,
        audit_ok=False,
    )

    assert "本次發送已寫入稽核紀錄" not in html
    assert "稽核紀錄寫入失敗" in html


# --------------------------------------------------------------------------- #
# 6. 稽核（R2：寫失敗時不得謊稱已留底）
# --------------------------------------------------------------------------- #


def test_success_page_claims_audit_when_it_really_wrote(tmp_path: Path) -> None:
    """正常情況要講「已寫入稽核紀錄」—— 這是下面那條反向測試的對照組。"""
    app = make_app(tmp_path, RecordingSender())

    response = send_once(app)

    assert response.status == HTTPStatus.OK
    assert "本次發送已寫入稽核紀錄" in response.text
    assert "稽核紀錄寫入失敗" not in response.text


def test_success_page_warns_when_audit_write_failed(tmp_path: Path) -> None:
    """稽核寫失敗 → 成功頁必須出現警告，且**不得**宣稱已留底。

    簡訊真的送出去了（所以仍是 200、仍顯示 msgid），但稽核檔裡查不到這一筆；
    不講出來的話，對帳的人會相信「檔案裡沒有＝沒發生過」。
    """
    audit = FailingAuditLog(tmp_path / "never-written.jsonl")
    app = make_app(tmp_path, RecordingSender(), audit_log=audit)

    response = send_once(app)

    assert response.status == HTTPStatus.OK
    assert "本次發送已寫入稽核紀錄" not in response.text
    assert "稽核紀錄寫入失敗" in response.text
    assert "0313887539" in response.text  # msgid 仍要顯示，那是唯一剩下的證據
    assert audit.attempted_events == ["attempt", "result"]
    assert not (tmp_path / "never-written.jsonl").exists()


def test_audit_file_records_attempt_then_result_without_body(tmp_path: Path) -> None:
    """一次發送寫兩筆（attempt/result）、號碼只留後四碼、內容完全不落地。

    `attempt` 有而 `result` 沒有，是「死在半路」的唯一信號，所以順序也要鎖。
    """
    audit_path = tmp_path / "send-audit.jsonl"
    app = make_app(tmp_path, RecordingSender(), audit_log=AuditLog(audit_path, fsync=False))

    send_once(app, body="這是不該落地的內容")

    raw = audit_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["event"] for record in records] == ["attempt", "result"]
    assert records[0]["phone_masked"] == "******5678"
    assert VALID_PHONE not in raw
    assert "這是不該落地的內容" not in raw
    assert records[1]["msgid"] == "0313887539"


# --------------------------------------------------------------------------- #
# 7. 速率上限（Y1：撞上限不得把使用者的內容丟掉）
# --------------------------------------------------------------------------- #


def test_send_rate_limited_keeps_content_and_offers_resend(tmp_path: Path) -> None:
    """`/send` 撞上限 → 429，但要帶回原內容與可重走確認頁的按鈕。

    token 在檢查速率**之前**就被 consume 掉了，不回填的話使用者手上什麼都不剩，
    等一小時回來要整段重打 —— 而重打長訊息本身就是打錯字、送錯人的來源。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender, rate_limit=1)
    first_token = issue_token(app, VALID_PHONE, "第一則內容")
    second_token = issue_token(app, VALID_PHONE, "第二則內容XYZ")

    assert app.route("POST", "/send", _form(token=first_token)).status == HTTPStatus.OK
    blocked = app.route("POST", "/send", _form(token=second_token))

    assert blocked.status == HTTPStatus.TOO_MANY_REQUESTS
    assert sender.call_count == 1  # 第二則沒有送出去
    assert "第二則內容XYZ" in blocked.text
    assert "<form" in blocked.text
    assert 'action="/preview"' in blocked.text  # 只能回確認頁，不能直接 /send


def test_preview_rate_limited_issues_no_token(tmp_path: Path) -> None:
    """`/preview` 撞上限就要擋下，不能發 token 讓人走到「按了才發現」。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender, rate_limit=1)
    token = issue_token(app)
    assert app.route("POST", "/send", _form(token=token)).status == HTTPStatus.OK

    response = app.route("POST", "/preview", _form(phone=VALID_PHONE, body=SHORT_BODY))

    assert response.status == HTTPStatus.TOO_MANY_REQUESTS
    assert _TOKEN_PATTERN.search(response.text) is None
    assert app.token_store.pending_count == 0


# --------------------------------------------------------------------------- #
# 8. 「為什麼沒扣點」的敘述（Y6：三竹根本沒收到 ≠ 三竹拒絕）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (mitake.MitakeValidationError("號碼格式不符"), templates.REASON_VALIDATION_BLOCKED),
        (mitake.MitakeConfigError("缺少環境變數 MITAKE_USERNAME"), templates.REASON_CONFIG_MISSING),
        (
            api_error(possibly_charged=False, kind=mitake.KIND_NETWORK),
            templates.REASON_NEVER_REACHED_MITAKE,
        ),
    ],
)
def test_never_reached_mitake_pages_do_not_claim_rejection(
    tmp_path: Path, error: BaseException, expected_reason: str
) -> None:
    """三竹根本沒收到請求的三種失敗，不得寫成「三竹已明確拒絕」。

    寫錯的代價：使用者拿著這句話去三竹後台找一筆**不存在**的紀錄，
    找不到之後最合理的推論是「那就是沒送出，我再送一次」。
    結論（沒扣點、可安全重送）不變，變的只有理由。
    """
    app = make_app(tmp_path, RecordingSender(error=error), rate_limit=5)

    response = send_once(app)

    assert "三竹已明確拒絕" not in response.text
    assert expected_reason in response.text
    assert "沒有扣點" in response.text
    assert app.rate_limiter.snapshot().used == 0


def test_mitake_rejection_page_still_says_rejected(tmp_path: Path) -> None:
    """反向鎖：三竹**真的**回了 Error 時，那句「三竹已明確拒絕」不可以被改掉。"""
    app = make_app(tmp_path, RecordingSender(error=api_error(possibly_charged=False)))

    response = send_once(app)

    assert templates.REASON_MITAKE_REJECTED in response.text


def test_config_error_page_has_no_resend_button(tmp_path: Path) -> None:
    """憑證沒設 → 要先去改 env 再重啟服務，給重送按鈕只是誘人白按。"""
    app = make_app(tmp_path, RecordingSender(error=mitake.MitakeConfigError("缺少 MITAKE_PASSWORD")))

    response = send_once(app)

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "<form" not in response.text
    assert "MITAKE_PASSWORD" in response.text


# --------------------------------------------------------------------------- #
# 9. 送出前就該被擋下的輸入（不該浪費一趟網路，更不該扣點）
# --------------------------------------------------------------------------- #


def test_invalid_phone_blocked_at_preview_and_backfilled(tmp_path: Path) -> None:
    """號碼格式錯要在 `/preview` 擋下，並把使用者剛打的內容原樣回填。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    response = app.route("POST", "/preview", _form(phone="0912", body="不該消失的內容"))

    assert response.status == HTTPStatus.BAD_REQUEST
    assert sender.call_count == 0
    assert 'value="0912"' in response.text
    assert "不該消失的內容" in response.text
    assert "未扣點" in response.text


def test_empty_body_blocked_at_preview(tmp_path: Path) -> None:
    """三竹會拒收空內容，而那趟網路來回不會告訴使用者任何有用的事。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    response = app.route("POST", "/preview", _form(phone=VALID_PHONE, body="   \n  "))

    assert response.status == HTTPStatus.BAD_REQUEST
    assert sender.call_count == 0
    assert app.token_store.pending_count == 0


def test_over_segment_limit_blocked_at_preview(tmp_path: Path) -> None:
    """超過單次則數上限要在確認頁之前擋下（這是最直接的成本護欄）。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    too_long = "字" * (mitake.CHARS_PER_SEGMENT * mitake.MAX_SEGMENTS_PER_SEND + 1)

    response = app.route("POST", "/preview", _form(phone=VALID_PHONE, body=too_long))

    assert response.status == HTTPStatus.BAD_REQUEST
    assert sender.call_count == 0
    assert app.token_store.pending_count == 0


# --------------------------------------------------------------------------- #
# 10. HTTP 層（Y2：慢速 POST 不得永久佔住 thread）
# --------------------------------------------------------------------------- #


def test_handler_sets_socket_timeout(tmp_path: Path) -> None:
    """handler 必須有 socket 逾時。

    `BaseHTTPRequestHandler.timeout` 預設是 None，`rfile.read(length)` 會無限期
    等下去：宣告 `Content-Length: 100` 卻不送 body 的連線能永久佔住一個 thread，
    而 `daemon_threads=True` 沒有 thread 上限 —— 數十條就能癱掉服務，
    且 systemd 的 `Restart=always` 救不了（process 沒死，只是不回應）。
    """
    handler_class = make_handler(make_app(tmp_path, RecordingSender()))

    assert handler_class.timeout is not None
    assert 0 < handler_class.timeout <= 60


def test_unknown_path_and_method_are_rejected(tmp_path: Path) -> None:
    """404 / 405 都只給一個回表單的連結，不給任何會觸發發送的按鈕。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    not_found = app.route("GET", "/nope")
    wrong_method = app.route("GET", "/send")

    assert not_found.status == HTTPStatus.NOT_FOUND
    assert wrong_method.status == HTTPStatus.METHOD_NOT_ALLOWED
    assert "<form" not in not_found.text
    assert "<form" not in wrong_method.text
    assert sender.call_count == 0


# --------------------------------------------------------------------------- #
# 11. 可能已扣點 + 稽核寫入失敗（本專案最壞的組合）
# --------------------------------------------------------------------------- #


def test_unconfirmed_page_never_claims_audit_when_it_failed(tmp_path: Path) -> None:
    """`possibly_charged=True` 且稽核寫失敗 → **不得**宣稱已留底，且要叫人手抄。

    這條比成功頁那條（第 6 節）更要命：成功頁至少還有 msgid 這個可查的憑據，
    而走到這裡代表「多半已扣點、且結果不明」—— 唯一的處置就是拿線索去三竹後台
    查證。稽核若也沒寫進去，使用者連要查什麼都不知道，卻被頁面告知已經留底了。
    """
    audit = FailingAuditLog(tmp_path / "never-written.jsonl")
    sender = RecordingSender(
        error=api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED)
    )
    app = make_app(tmp_path, sender, audit_log=audit)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "本次發送已寫入稽核紀錄" not in response.text
    assert "稽核紀錄寫入失敗" in response.text
    assert "請立刻手動記下" in response.text
    assert "req-test" in response.text  # request_id 是手抄時的關鍵欄位
    # 這一頁的第一鐵律不因為多了一個警告方塊就鬆動。
    assert "<form" not in response.text
    assert "請勿重送" in response.text


def test_unconfirmed_page_claims_audit_when_it_really_wrote(tmp_path: Path) -> None:
    """對照組：稽核真的寫成功時，不該冒出「請立刻手動記下」那段噪音。"""
    sender = RecordingSender(
        error=api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED)
    )
    app = make_app(tmp_path, sender)

    response = send_once(app)

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "稽核紀錄寫入失敗" not in response.text
    assert "請立刻手動記下" not in response.text


def test_render_unconfirmed_without_msgid_never_lies_about_audit() -> None:
    """樣板層直鎖：三竹連 msgid 都沒回、稽核又寫不進去時，不得說「已寫入稽核紀錄」。

    這正是原本的缺陷所在：那句話寫死在「沒有 msgid」的分支裡，與稽核是否真的
    寫成功完全無關。
    """
    html = templates.render_failed_unconfirmed(
        phone=VALID_PHONE,
        segments=2,
        msgid=None,
        message="回應無法確認",
        audit_ok=False,
        request_id="req-abc123",
    )

    assert "本次發送已寫入稽核紀錄" not in html
    assert "req-abc123" in html
    assert "<form" not in html


def test_render_unconfirmed_without_msgid_still_claims_audit_when_ok() -> None:
    """反向鎖：稽核真的寫成功時，那句「已寫入稽核紀錄」不可以被順手刪掉。"""
    html = templates.render_failed_unconfirmed(
        phone=VALID_PHONE, segments=2, msgid=None, message="回應無法確認"
    )

    assert "本次發送已寫入稽核紀錄" in html
    assert "<form" not in html


# --------------------------------------------------------------------------- #
# 12. /preview 撞上限也不得把內容丟掉（第 7 節只做了 /send）
# --------------------------------------------------------------------------- #


def test_preview_rate_limited_keeps_content_and_offers_resend(tmp_path: Path) -> None:
    """`/preview` 撞上限 → 429，但同樣要帶回原內容與可重走確認頁的按鈕。

    第 7 節只鎖了 `/send`。`/preview` 這條路徑上使用者的損失一模一樣：
    面對 429 頁時人的直覺是回表單重打，而重打長訊息本身就是打錯字、送錯人的來源。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender, rate_limit=1)
    token = issue_token(app)
    assert app.route("POST", "/send", _form(token=token)).status == HTTPStatus.OK

    blocked = app.route("POST", "/preview", _form(phone=VALID_PHONE, body="第二則內容XYZ"))

    assert blocked.status == HTTPStatus.TOO_MANY_REQUESTS
    assert "第二則內容XYZ" in blocked.text
    assert 'action="/preview"' in blocked.text  # 只能回確認頁，不能直接 /send
    assert _TOKEN_PATTERN.search(blocked.text) is None  # 但仍然不得發出 token
    assert sender.call_count == 1


# --------------------------------------------------------------------------- #
# 13. CSP nonce（Y3：'unsafe-inline' 讓 script-src 這道牆完全沒有作用）
# --------------------------------------------------------------------------- #


def test_csp_script_src_has_no_unsafe_inline(tmp_path: Path) -> None:
    """`script-src` 不得帶 `'unsafe-inline'`。

    帶了的話，被注入的 `<script>` 照樣執行 —— 這條 CSP 對「阻止腳本執行」的價值
    就是零，而那正是我們要它擋的東西。
    """
    header = _csp_header("abc123")
    script_src = header.split("script-src")[1].split(";")[0]

    assert "unsafe-inline" not in script_src
    assert "'nonce-abc123'" in script_src
    # reviewer 確認這幾條是真的有用的，不可以在改 script-src 時被一起弄丟。
    for directive in ("default-src 'none'", "form-action 'self'", "base-uri 'none'"):
        assert directive in header
    assert "frame-ancestors 'none'" in header


def test_pages_without_script_get_script_src_none(tmp_path: Path) -> None:
    """沒有腳本的頁面連 nonce 都不發，`script-src` 直接給 `'none'`（更嚴）。"""
    app = make_app(tmp_path, RecordingSender())
    preview = app.route("POST", "/preview", _form(phone=VALID_PHONE, body=SHORT_BODY))

    assert preview.nonce is None
    assert "script-src 'none'" in _csp_header(preview.nonce)


def test_form_page_nonce_is_fresh_on_every_response(tmp_path: Path) -> None:
    """nonce **每個回應都要不同**。

    重用等於把它變成一個可預測的常數：攻擊者讀一次頁面就知道該在注入的 script 上
    補哪個值，整道 CSP 就退回 `'unsafe-inline'` 的等級。
    """
    app = make_app(tmp_path, RecordingSender())

    seen = set()
    for _ in range(5):
        response = app.route("GET", "/")
        assert response.nonce
        match = re.search(r'<script nonce="([^"]+)"', response.text)
        assert match is not None, "表單頁的 <script> 沒有 nonce，即時試算會被 CSP 擋掉"
        assert match.group(1) == response.nonce  # 頁面裡的與回應帶的必須是同一個
        seen.add(response.nonce)

    assert len(seen) == 5


def test_render_form_refuses_script_without_nonce() -> None:
    """樣板層防呆：沒有 nonce 就不准輸出 `<script>`。

    靜默降級的症狀是「字數提示不會動」，沒人會聯想到 CSP，會被當成前端小 bug 放著。
    """
    with pytest.raises(ValueError):
        templates.render_form(max_segments=5, chars_per_segment=70, script_nonce="")


def test_live_server_sends_csp_matching_the_page_nonce(tmp_path: Path) -> None:
    """端對端：真正送出去的 CSP 標頭，必須與頁面裡 `<script nonce>` 完全一致。

    上面幾條都在純函式層驗，這條走真的 socket —— 標頭是在 HTTP 層送出的，
    而頁面是在路由層產生的，兩邊各產一份 nonce 是最容易犯又最難察覺的錯
    （畫面不會壞給你看，只有瀏覽器 console 會抱怨）。
    """
    app = make_app(tmp_path, RecordingSender())
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as response:
            csp = response.headers["Content-Security-Policy"]
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    match = re.search(r'<script nonce="([^"]+)"', html)
    assert match is not None
    assert f"'nonce-{match.group(1)}'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]


# --------------------------------------------------------------------------- #
# 14. 應用層存取檢查（Y4：把「安靜地被人拿去發簡訊」變成「明顯地壞掉」）
# --------------------------------------------------------------------------- #


def test_access_check_is_off_by_default(tmp_path: Path) -> None:
    """沒設就完全不擋 —— 本機 curl 測試與 `--dry-run` 驗證都不該因此變麻煩。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    assert app.required_access_email is None
    assert app.route("GET", "/").status == HTTPStatus.OK
    assert send_once(app).status == HTTPStatus.OK
    assert sender.call_count == 1


def test_access_check_allows_matching_header(tmp_path: Path) -> None:
    """標頭與設定值相符時照常放行（含實際送出）。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender, require_access_email="peter@example.com")
    headers = access_headers("peter@example.com")

    preview = app.route(
        "POST", "/preview", _form(phone=VALID_PHONE, body=SHORT_BODY), headers=headers
    )
    assert preview.status == HTTPStatus.OK
    token = _TOKEN_PATTERN.search(preview.text).group(1)

    sent = app.route("POST", "/send", _form(token=token), headers=headers)

    assert sent.status == HTTPStatus.OK
    assert sender.call_count == 1


def test_access_check_is_case_insensitive(tmp_path: Path) -> None:
    """email 大小寫與標頭名稱大小寫都不該影響判斷。

    email 的 local part 理論上區分大小寫，但沒有任何真實 IdP 這樣用；
    嚴格比對的結果只會是「設定看起來明明對卻整站 403」。標頭名稱則是由對方決定
    大小寫的，本來就必須用不分大小寫的方式查（所以 headers 用 Message 不用 dict）。
    """
    app = make_app(tmp_path, RecordingSender(), require_access_email="Peter@Example.com")

    upper = app.route("GET", "/", headers=access_headers("PETER@EXAMPLE.COM"))
    lower_name = app.route(
        "GET", "/", headers=access_headers("peter@example.com", name="cf-access-authenticated-user-email")
    )

    assert upper.status == HTTPStatus.OK
    assert lower_name.status == HTTPStatus.OK


def test_access_check_rejects_wrong_email(tmp_path: Path) -> None:
    """身分不符 → 403，且不得送出任何簡訊。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender, require_access_email="peter@example.com")
    # 先用正確身分拿到一張合法 token，確保等一下擋下來的是**身分**、不是 token。
    token = issue_token(app, headers=access_headers("peter@example.com"))

    denied = app.route(
        "POST", "/send", _form(token=token), headers=access_headers("someone-else@example.com")
    )

    assert denied.status == HTTPStatus.FORBIDDEN
    assert sender.call_count == 0
    assert "<form" not in denied.text


def test_access_check_rejects_missing_header(tmp_path: Path) -> None:
    """完全沒帶標頭 → 403。

    這正是「tunnel 已開但 Cloudflare Access 還沒設好」時外面直接打進來的樣子：
    原本會安靜地成功，現在會明顯地壞掉。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender, require_access_email="peter@example.com")

    assert app.route("GET", "/", headers=access_headers(None)).status == HTTPStatus.FORBIDDEN
    assert app.route("GET", "/").status == HTTPStatus.FORBIDDEN  # headers 根本沒傳
    assert app.route("POST", "/preview", _form(phone=VALID_PHONE, body=SHORT_BODY)).status == (
        HTTPStatus.FORBIDDEN
    )
    assert sender.call_count == 0


def test_health_is_exempt_from_access_check(tmp_path: Path) -> None:
    """`/health` 一律豁免：systemd / 監控探測不會帶 Access 標頭。

    擋掉它等於讓服務被誤判成掛掉並反覆重啟 —— 而重啟正好會清掉速率視窗。
    豁免的代價是零：這個端點只回「服務活著」，不查餘額、不碰憑證、不花錢
    （見 `SmsWebApp.handle_health` 的說明）。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender, require_access_email="peter@example.com")

    response = app.route("GET", "/health")

    assert response.status == HTTPStatus.OK
    assert json.loads(response.text)["status"] == "ok"
    assert sender.call_count == 0


def test_blank_access_email_is_treated_as_not_configured(tmp_path: Path) -> None:
    """空字串當成沒設（`Environment=MITAKE_WEB_REQUIRE_ACCESS_EMAIL=` 很容易寫成這樣）。

    若把空字串當成有效值，比對邏輯寫鬆一點就會變成「誰都放行」—— 那比不擋更糟，
    因為 log 會宣稱存取檢查已啟用。
    """
    app = make_app(tmp_path, RecordingSender(), require_access_email="   ")

    assert app.required_access_email is None
    assert app.route("GET", "/").status == HTTPStatus.OK


# --------------------------------------------------------------------------- #
# 15. 速率上限回填（Y5：一次 restart 就把當小時預算清成零）
# --------------------------------------------------------------------------- #


def test_restore_counts_recent_successful_sends(tmp_path: Path) -> None:
    """一小時視窗內的成功發送要被算回來，否則每次部署都等於偷偷放寬上限一輪。"""
    path = write_audit(
        tmp_path,
        audit_line(age_seconds=60, segments=3),
        audit_line(age_seconds=120, segments=2),
    )
    app = make_app(tmp_path, RecordingSender(), audit_log=AuditLog(path, fsync=False))

    restored = app.restore_rate_limit_from_audit(now=AUDIT_NOW)

    assert restored == 5
    assert app.rate_limiter.snapshot().used == 5


def test_restore_ignores_records_outside_the_window(tmp_path: Path) -> None:
    """超過一小時的舊紀錄不得回填（它們本來就已經離開滑動視窗了）。"""
    path = write_audit(
        tmp_path,
        audit_line(age_seconds=3_599, segments=1),
        audit_line(age_seconds=3_601, segments=4),
        audit_line(age_seconds=86_400, segments=9),
    )
    app = make_app(tmp_path, RecordingSender(), audit_log=AuditLog(path, fsync=False))

    assert app.restore_rate_limit_from_audit(now=AUDIT_NOW) == 1


def test_restore_counts_possibly_charged_as_used(tmp_path: Path) -> None:
    """`possibly_charged=true` 也算 —— 它代表請求已送到三竹、多半已經扣點。

    只算 `success=true` 的話，「結果未確認」那批會被當成沒發生，而那正是最該
    保守處理的一批。
    """
    path = write_audit(
        tmp_path,
        audit_line(age_seconds=30, segments=2, success=False, possibly_charged=True),
    )
    app = make_app(tmp_path, RecordingSender(), audit_log=AuditLog(path, fsync=False))

    assert app.restore_rate_limit_from_audit(now=AUDIT_NOW) == 2


def test_restore_ignores_safe_failures_and_attempts(tmp_path: Path) -> None:
    """確定沒扣點的失敗、以及 `attempt` 那半筆，都不佔額度。

    反過來算的話，一連串「號碼打錯」的失敗會把當小時的配額吃光。
    """
    path = write_audit(
        tmp_path,
        audit_line(age_seconds=30, segments=5, success=False, possibly_charged=False),
        audit_line(age_seconds=31, segments=5, event="attempt"),
    )
    app = make_app(tmp_path, RecordingSender(), audit_log=AuditLog(path, fsync=False))

    assert app.restore_rate_limit_from_audit(now=AUDIT_NOW) == 0
    assert app.rate_limiter.snapshot().used == 0


def test_restore_converts_wall_clock_to_monotonic_correctly(tmp_path: Path) -> None:
    """時鐘換算要正確：稽核用牆鐘、`RateLimiter` 用 monotonic，兩者不能直接相減。

    驗法：回填一筆「3500 秒前」的紀錄，然後推進**單調**時鐘 —— 再過 99 秒它應該
    還在視窗內（3599 < 3600），再過 2 秒就該掉出去。換算寫錯（例如把牆鐘秒數當成
    monotonic 絕對值）的話，這筆不是立刻消失就是永遠不過期。
    """
    path = write_audit(tmp_path, audit_line(age_seconds=3_500, segments=2))
    clock = FakeClock()
    app = make_app(
        tmp_path, RecordingSender(), clock=clock, audit_log=AuditLog(path, fsync=False)
    )

    assert app.restore_rate_limit_from_audit(now=AUDIT_NOW) == 2
    assert app.rate_limiter.snapshot().used == 2

    clock.advance(99)  # 距發生時刻 3599 秒，還在 3600 秒的視窗內
    assert app.rate_limiter.snapshot().used == 2

    clock.advance(2)  # 3601 秒，已離開視窗
    assert app.rate_limiter.snapshot().used == 0


def test_restore_survives_corrupt_lines(tmp_path: Path) -> None:
    """壞掉的行只跳過，不得讓整個回填失效（那等於退回「重啟就歸零」）。

    稽核檔可能被斷電截斷、可能被人工加過註記，一行壞掉是可預期的。
    """
    path = write_audit(
        tmp_path,
        "{ 這不是 JSON",
        "",
        json.dumps({"event": "result", "success": True}),  # 缺 segments / time
        audit_line(age_seconds=10, segments=4, time="不是時間"),
        audit_line(age_seconds=10, segments="三"),  # segments 型別錯
        audit_line(age_seconds=10, segments=True),  # bool 是 int 的子類，不可誤算成 1
        audit_line(age_seconds=10, segments=2),  # 唯一一筆好的
    )
    app = make_app(tmp_path, RecordingSender(), audit_log=AuditLog(path, fsync=False))

    assert app.restore_rate_limit_from_audit(now=AUDIT_NOW) == 2


def test_restore_without_audit_file_starts_empty(tmp_path: Path) -> None:
    """稽核檔不存在時要安靜地以空狀態啟動 —— 回填是防呆，不該讓服務起不來。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        audit_log=AuditLog(tmp_path / "does-not-exist.jsonl", fsync=False),
    )

    assert app.restore_rate_limit_from_audit(now=AUDIT_NOW) == 0
    assert app.rate_limiter.snapshot().used == 0
    # 回填完仍要能正常收送（不是進入某種半殘狀態）。
    assert app.route("GET", "/").status == HTTPStatus.OK


def test_restored_usage_actually_blocks_further_sends(tmp_path: Path) -> None:
    """回填不是只給畫面看的數字：它必須真的擋得住下一則。

    這條才是 Y5 的重點 —— 前面幾條驗的是「算對」，這條驗的是「有效」。
    """
    path = write_audit(tmp_path, audit_line(age_seconds=60, segments=2))
    sender = RecordingSender()
    app = make_app(
        tmp_path, sender, rate_limit=2, audit_log=AuditLog(path, fsync=False)
    )
    app.restore_rate_limit_from_audit(now=AUDIT_NOW)

    response = app.route("POST", "/preview", _form(phone=VALID_PHONE, body=SHORT_BODY))

    assert response.status == HTTPStatus.TOO_MANY_REQUESTS
    assert sender.call_count == 0


# --------------------------------------------------------------------------- #
# 16. token 被淘汰時的文案（Y7：講錯原因會讓人誤以為自己已經送過了）
# --------------------------------------------------------------------------- #


def test_evicted_token_message_covers_both_causes(tmp_path: Path) -> None:
    """確認頁被較新的擠掉後按送出 → 409，且文案必須同時涵蓋兩種成因。

    原本的文案只講「你按了上一頁」，但這裡的真實原因是「被新的確認頁擠掉」——
    這則簡訊**從來沒送出去過**。照原文案理解的人會以為自己已經送過而不再重發，
    於是一則該送的簡訊靜靜地消失。兩種成因在伺服器端無法區分，所以文案要都講。
    """
    sender = RecordingSender()
    clock = FakeClock()
    app = make_app(
        tmp_path,
        sender,
        clock=clock,
        token_store=TokenStore(ttl_seconds=600, max_tokens=1, clock=clock),
    )
    first_token = issue_token(app, VALID_PHONE, "第一則")
    second_token = issue_token(app, VALID_PHONE, "第二則")  # 這一張把上一張擠掉

    evicted = app.route("POST", "/send", _form(token=first_token))

    assert evicted.status == HTTPStatus.CONFLICT
    assert sender.call_count == 0
    assert "<form" not in evicted.text  # 仍然不給任何重送入口
    assert "已被較新的" in evicted.text  # (2) 被擠掉
    assert "上一頁" in evicted.text  # (1) 按了上一頁
    # 沒被擠掉的那張仍然正常可用（淘汰的是最舊的，不是全部）。
    assert app.route("POST", "/send", _form(token=second_token)).status == HTTPStatus.OK
    assert sender.calls[-1]["body"] == "第二則"


def test_reused_token_message_also_covers_both_causes(tmp_path: Path) -> None:
    """按上一頁重送的那條路徑走同一個畫面，文案同樣要兩種都講（且仍不得重發）。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    token = issue_token(app)

    assert app.route("POST", "/send", _form(token=token)).status == HTTPStatus.OK
    second = app.route("POST", "/send", _form(token=token))

    assert second.status == HTTPStatus.CONFLICT
    assert sender.call_count == 1
    assert "已被較新的" in second.text
    assert "上一頁" in second.text


# --------------------------------------------------------------------------- #
# 投遞狀態查詢（GET /status）—— 唯讀、免費、不扣點
# --------------------------------------------------------------------------- #
#
# 這一段鎖的核心只有一句話：**「已送達業者」不可以看起來像「已送達手機」。**
# 使用者會來這一頁，正是因為他手機沒響而畫面說「三竹已接收」；如果這一頁又用
# 「已送達」三個字打發他，等於把「無從查證」升級成「被明確誤導」。


def test_status_form_is_served_when_no_msgid(tmp_path: Path) -> None:
    """沒帶 msgid 就出查詢表單，而且不該去打三竹。"""
    status_query = RecordingStatusQuery()
    app = make_app(tmp_path, RecordingSender(), status_query=status_query)

    response = query_status(app)

    assert response.status == HTTPStatus.OK
    assert "查詢簡訊投遞狀態" in response.text
    assert 'name="msgid"' in response.text
    assert status_query.call_count == 0


def test_status_page_says_delivered_to_handset_only_for_code_4(tmp_path: Path) -> None:
    """Happy path：狀態碼 4 才可以說「已送達手機」，且要標明是最終狀態。"""
    status_query = RecordingStatusQuery(result=status_result("4"))
    app = make_app(tmp_path, RecordingSender(), status_query=status_query)

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.OK
    assert "已送達手機" in response.text
    assert "最終狀態" in response.text
    assert status_query.calls == [
        {"msgid": "0315772761", "timeout": pytest.approx(25.0)}
    ]


@pytest.mark.parametrize("code", ["1", "2", "3"])
def test_carrier_delivery_is_never_shown_as_delivered_to_handset(
    tmp_path: Path, code: str
) -> None:
    """**本功能最重要的一條 web 層回歸鎖。**

    狀態碼 1–3 的官方說明就是「已送達業者」。頁面必須：
    (1) 明講還沒到手機、(2) 說這不是最終狀態、可稍後再查、
    (3) 標題與說明區絕不出現「已送達手機」（那是狀態碼 4 專用的）。
    """
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(result=status_result(code)),
    )

    text = query_status(app, "0315772761").text
    # 頁尾那段固定的對照說明本來就會提到「已送達手機」（它正是在解釋兩者差別），
    # 所以只檢查它之前的內容 —— 也就是標題、狀態方塊與明細。
    above_legend = text.split("三竹的「已送達業者」")[0]

    assert "已送達業者" in above_legend
    assert "已送達手機" not in above_legend
    assert "還沒" in above_legend
    assert "不是最終狀態" in above_legend
    # 對照說明（業者 ≠ 手機）在每一頁都要出現，這是整個功能的立足點。
    assert "<strong>不代表</strong>對方收到" in text


def test_failed_status_page_says_final_and_not_delivered(tmp_path: Path) -> None:
    """失敗類（門號有錯誤）要講清楚「沒有送達」且「再查也不會變」。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(result=status_result("6")),
    )

    text = query_status(app, "0315772761").text

    assert "門號有錯誤" in text
    assert "沒有送達" in text
    assert "不會退回" in text


def test_unknown_status_code_page_refuses_to_claim_delivery(tmp_path: Path) -> None:
    """沒收錄的碼要老實說不知道，並明講「不要假設對方已經收到」。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(result=status_result("Z")),
    )

    text = query_status(app, "0315772761").text

    assert "無法辨識的狀態碼" in text
    assert "請不要假設對方已經收到" in text


def test_system_error_status_tells_user_to_retry_not_to_fix_settings(
    tmp_path: Path,
) -> None:
    """系統類狀態碼是三竹那端的事，要導向「稍後再查」而不是「去改設定」。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(result=status_result("*")),
    )

    text = query_status(app, "0315772761").text

    assert "系統發生錯誤" in text
    assert "稍後再查" in text


def test_status_page_formats_the_status_time(tmp_path: Path) -> None:
    """狀態時間要排成人看得懂的樣子，並標明是台灣時間（要拿去跟後台對帳）。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(
            result=status_result("4", status_time="20260729143730")
        ),
    )

    text = query_status(app, "0315772761").text

    assert "2026-07-29 14:37:30（台灣時間）" in text


def test_status_page_shows_unexpected_time_format_verbatim(tmp_path: Path) -> None:
    """格式不符時原樣顯示，不自己重排 —— 重排錯了就對不上三竹後台。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(
            result=status_result("4", status_time="2026/07/29")
        ),
    )

    assert "2026/07/29" in query_status(app, "0315772761").text


def test_invalid_msgid_is_rejected_before_reaching_mitake(tmp_path: Path) -> None:
    """Error case：格式就錯的 msgid 不該打出去，且要回填讓人看到自己打錯什麼。"""
    status_query = RecordingStatusQuery()
    app = make_app(tmp_path, RecordingSender(), status_query=status_query)

    response = query_status(app, "not a msgid")

    assert response.status == HTTPStatus.BAD_REQUEST
    assert status_query.call_count == 0
    assert "沒有查詢，也沒有扣點" in response.text
    assert 'value="not a msgid"' in response.text  # 原樣回填


def test_status_page_escapes_user_supplied_msgid(tmp_path: Path) -> None:
    """XSS：msgid 來自網址列，是這個功能最容易被塞東西的地方。"""
    app = make_app(tmp_path, RecordingSender(), status_query=RecordingStatusQuery())
    payload = '"><script>alert(1)</script>'

    response = query_status(app, payload)

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "<script>alert(1)</script>" not in response.text
    assert _html_escape(payload, quote=True) in response.text


def test_status_result_page_escapes_fields_echoed_from_mitake(tmp_path: Path) -> None:
    """連三竹回傳的欄位也要跳脫：它同樣是外部輸入，不因為來自上游就可信。"""
    evil = status_result("4")
    evil["msgid"] = '"><script>alert(1)</script>'
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(result=evil)
    )

    text = query_status(app, "0315772761").text

    assert "<script>alert(1)</script>" not in text


def test_status_ip_blocked_points_at_configuration_not_retry(tmp_path: Path) -> None:
    """IP 不在白名單是設定問題，重查一百次也一樣 —— 要導向申請白名單。"""
    error = mitake.MitakeAPIError(
        "三竹拒絕這次查詢：無效的連線位址",
        statuscode="k",
        kind=mitake.KIND_IP_BLOCKED,
        possibly_charged=False,
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "白名單" in response.text
    assert "未扣點" in response.text
    assert "重查不會成功" in response.text


def test_status_auth_failed_points_at_credentials(tmp_path: Path) -> None:
    """帳密錯同樣是設定問題，要指向 /etc/mitake-sms.env 而不是叫人重試。"""
    error = mitake.MitakeAPIError(
        "三竹拒絕這次查詢：帳號、密碼錯誤",
        statuscode="e",
        kind=mitake.KIND_AUTH_FAILED,
        possibly_charged=False,
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "mitake-sms.env" in response.text
    assert "未扣點" in response.text


def test_status_network_error_says_no_charge_and_safe_to_retry(tmp_path: Path) -> None:
    """一般查詢失敗：一定要講「沒有扣點」。

    這個服務的使用者已經被訓練成看到「失敗」就擔心錢。查詢是唯讀免費的，
    不講清楚只會讓人為一個免費操作提心吊膽，甚至不敢再查。
    """
    error = mitake.MitakeAPIError(
        "無法連線至三竹 API", kind=mitake.KIND_NETWORK, possibly_charged=False
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "沒有扣點" in response.text
    assert "再查一次" in response.text


def test_status_config_error_page(tmp_path: Path) -> None:
    """憑證沒設：查不了，但也沒扣點；訊息只能有變數名稱、不能有值。"""
    error = mitake.MitakeConfigError("缺少三竹憑證環境變數：MITAKE_USERNAME")
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "沒有扣點" in response.text
    assert "MITAKE_USERNAME" in response.text


def test_status_unexpected_exception_does_not_leak_internals(tmp_path: Path) -> None:
    """未預期的例外要被接住，且不把內部細節送到瀏覽器。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(error=RuntimeError("內部路徑 /home/secret")),
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "/home/secret" not in response.text
    assert "沒有扣點" in response.text


def test_status_mismatch_page_shows_both_msgids(tmp_path: Path) -> None:
    """**MUST_FIX 的 web 層鎖**：回的是別則簡訊時，兩個 msgid 都要出現在畫面上。

    修正前的實測症狀是「整頁綠色『已送達手機』，而使用者查的 0315772761 一個字
    都沒出現」。所以這裡不只驗有錯誤頁，還驗**他查的那個號碼看得到** ——
    看不到就等於在對他講另一件事，而他不會知道。
    """
    error = mitake.MitakeAPIError(
        "三竹回覆的是另一則簡訊的狀態（你查的是 0315772761，三竹回的是 9999999999）。"
        "本次查詢未扣點。請拿 msgid 到三竹後台核對，或聯絡三竹客服 02-25367777。",
        kind=mitake.KIND_MSGID_MISMATCH,
        possibly_charged=False,
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "0315772761" in response.text  # 他查的那則
    assert "9999999999" in response.text  # 三竹回的那則
    # 絕不能出現成功語氣：這一頁沒有任何「已送達」可言。
    assert "已送達手機" not in response.text


def test_status_mismatch_page_does_not_promise_that_retrying_helps(
    tmp_path: Path,
) -> None:
    """身分不符是「重查沒用」那一族，文案不可出現「稍後再查」。

    出現的話使用者會照做 —— 重整、失敗、再重整，被釘在一個永遠不會成功的迴圈裡，
    而真正該做的事（拿 msgid 去三竹後台核對、打客服）一個字都沒被講。
    """
    error = mitake.MitakeAPIError(
        "三竹回覆的是另一則簡訊的狀態（你查的是 0315772761，三竹回的是 9999999999）。",
        kind=mitake.KIND_MSGID_MISMATCH,
        possibly_charged=False,
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    text = query_status(app, "0315772761").text

    assert "稍後" not in text
    assert "三竹後台" in text
    assert "02-25367777" in text
    assert "未扣點" in text or "沒有扣點" in text


def test_status_bad_response_page_points_at_the_console_not_at_retrying(
    tmp_path: Path,
) -> None:
    """「格式解不開」同樣是重查沒用 —— 一起走不可重試的那頁。

    這條涵蓋空 msgid／欄位不足／拿到 key=value 那幾種，它們原本共用
    「查詢是唯讀操作，稍後可以安全地再查一次」的文案。
    """
    error = mitake.MitakeAPIError(
        "三竹狀態查詢回應格式不符（預期 msgid、狀態碼、狀態時間三欄，以 Tab 分隔）",
        kind=mitake.KIND_BAD_RESPONSE,
        possibly_charged=False,
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    response = query_status(app, "0315772761")

    assert response.status == HTTPStatus.BAD_GATEWAY
    assert "稍後" not in response.text
    assert "三竹後台" in response.text
    assert "02-25367777" in response.text


def test_status_network_error_still_offers_a_retry(tmp_path: Path) -> None:
    """回歸（分家的另一邊）：真正暫時性的失敗**必須**保留「稍後再查」。

    新增「重查沒用」那條路的代價若是把可重試的文案也一起改掉，就是從一種誤導換成
    另一種：網路抖一下就叫人去打三竹客服。這支確保兩邊各自留在自己的分支。
    """
    error = mitake.MitakeAPIError(
        "無法連線至三竹 API", kind=mitake.KIND_NETWORK, possibly_charged=False
    )
    app = make_app(
        tmp_path, RecordingSender(), status_query=RecordingStatusQuery(error=error)
    )

    text = query_status(app, "0315772761").text

    assert "稍後可以安全地再查一次" in text
    assert "沒有扣點" in text


def test_system_error_status_still_says_try_again_later(tmp_path: Path) -> None:
    """回歸：系統類狀態碼（`*`）是回傳值而非例外，仍然走「稍後再查」的結果頁。

    它和 bad_response 只差一個字的距離，卻是相反的建議 —— `*` 是三竹那端暫時忙，
    等一下真的會變；bad_response 是回應本身解不開，等一百年也一樣。
    """
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(result=status_result("*")),
    )

    text = query_status(app, "0315772761").text

    assert "系統發生錯誤" in text
    assert "稍後再查" in text


def test_status_future_mitake_error_is_contained(tmp_path: Path) -> None:
    """mitake.py 日後新增的例外型別要落到通用畫面，不該整個 500。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(error=FutureMitakeError("未來的錯誤")),
    )

    assert query_status(app, "0315772761").status == HTTPStatus.BAD_GATEWAY


def test_status_only_accepts_get(tmp_path: Path) -> None:
    """/status 只收 GET：查詢冪等且免費，用 POST 會讓「重新整理」跳出嚇人的重送對話框。"""
    app = make_app(tmp_path, RecordingSender(), status_query=RecordingStatusQuery())

    response = app.route("POST", "/status", _form(msgid="0315772761"))

    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED


def test_status_is_protected_by_access_email_check(tmp_path: Path) -> None:
    """設了 Access 檢查之後，/status 一樣要擋（只有 /health 豁免）。"""
    status_query = RecordingStatusQuery()
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=status_query,
        require_access_email="peter@example.com",
    )

    denied = query_status(app, "0315772761", headers=access_headers(None))
    allowed = query_status(
        app, "0315772761", headers=access_headers("peter@example.com")
    )

    assert denied.status == HTTPStatus.FORBIDDEN
    assert allowed.status == HTTPStatus.OK
    assert status_query.call_count == 1  # 被擋下的那次沒有打出去


def test_status_query_never_consumes_the_send_rate_limit(tmp_path: Path) -> None:
    """**額度不可混用**：查詢是免費的，不該吃掉「還能發幾則」的預算。

    共用一個計數器的話，查幾次投遞狀態就會害使用者發不出簡訊 ——
    免費操作擋掉付費操作，是最不該有的耦合。
    """
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(),
        rate_limit=1,
    )

    for _ in range(5):
        assert query_status(app, "0315772761").status == HTTPStatus.OK

    assert app.rate_limiter.snapshot().used == 0
    # 發送額度沒被吃掉，該送的還是送得出去。
    assert send_once(app).status == HTTPStatus.OK


def test_sending_never_consumes_the_status_query_quota(tmp_path: Path) -> None:
    """反方向也要成立：發簡訊不該吃掉查詢次數。"""
    app = make_app(tmp_path, RecordingSender(), status_query=RecordingStatusQuery())

    assert send_once(app).status == HTTPStatus.OK

    assert app.status_throttle.used == 0


def test_status_throttle_blocks_runaway_refresh(tmp_path: Path) -> None:
    """節流：擋得住放著自動重整的分頁。

    查詢不用錢，但每次都是對三竹的一個真實請求；打太密集會讓來源 IP 被限流，
    而那會連**發簡訊**一起壞掉（statuscode=k）。節流保的是發送能力。
    """
    clock = FakeClock()
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(),
        clock=clock,
        status_query_limit=2,
    )

    assert query_status(app, "0315772761").status == HTTPStatus.OK
    assert query_status(app, "0315772761").status == HTTPStatus.OK
    blocked = query_status(app, "0315772761")

    assert blocked.status == HTTPStatus.TOO_MANY_REQUESTS
    assert "不扣點" in blocked.text

    # 視窗滑過去之後要自己恢復（不需要重啟服務）。
    clock.advance(301)
    assert query_status(app, "0315772761").status == HTTPStatus.OK


def test_invalid_msgid_does_not_burn_status_quota(tmp_path: Path) -> None:
    """打錯字不該吃掉查詢額度：那次根本沒有對三竹發出任何請求。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        status_query=RecordingStatusQuery(),
        status_query_limit=1,
    )

    assert query_status(app, "bad msgid").status == HTTPStatus.BAD_REQUEST
    assert app.status_throttle.used == 0
    assert query_status(app, "0315772761").status == HTTPStatus.OK


def test_status_query_receives_the_normalized_msgid(tmp_path: Path) -> None:
    """送進 mitake 的是驗證＋去空白後的值，不是使用者的原始字串。"""
    status_query = RecordingStatusQuery()
    app = make_app(tmp_path, RecordingSender(), status_query=status_query)

    query_status(app, "  0315772761  ")

    assert status_query.calls[0]["msgid"] == "0315772761"


def test_status_pages_have_no_script_and_get_the_strictest_csp(tmp_path: Path) -> None:
    """查詢頁沒有腳本 → nonce 為 None → CSP 直接給 script-src 'none'（比 nonce 更嚴）。"""
    app = make_app(tmp_path, RecordingSender(), status_query=RecordingStatusQuery())

    form_page = query_status(app)
    result_page = query_status(app, "0315772761")

    for response in (form_page, result_page):
        assert response.nonce is None
        assert "<script" not in response.text
        assert "script-src 'none'" in _csp_header(response.nonce)


def test_sent_page_links_to_the_status_of_that_message(tmp_path: Path) -> None:
    """**這個功能最有價值的入口**：成功頁要能一鍵查剛發那則的投遞狀態。

    在此之前，使用者在成功頁看到「三竹已接收」就沒有下文了 —— 手機沒響時
    只能相信或懷疑。連結必須帶上該次的 msgid，否則他還得手抄一串數字再貼回來。
    """
    sender = RecordingSender(result={"msgid": "0313887539", "account_point": 12572})
    app = make_app(tmp_path, sender, status_query=RecordingStatusQuery())

    text = send_once(app).text

    assert 'href="/status?msgid=0313887539"' in text
    assert "不等於" in text  # 要明講「三竹已接收 ≠ 對方收到」
    # 成功頁的老規矩不變：不放任何會送出東西的元件。
    assert "<form" not in text


def test_sent_page_has_no_status_link_without_msgid(tmp_path: Path) -> None:
    """沒有 msgid 就不給一個點下去必定查無結果的死連結。"""
    sender = RecordingSender(result={"msgid": None, "account_point": 12572})
    app = make_app(tmp_path, sender, status_query=RecordingStatusQuery())

    text = send_once(app).text

    assert "/status?msgid=" not in text


def test_sent_page_url_encodes_the_msgid(tmp_path: Path) -> None:
    """msgid 進網址前要 URL-encode，進 HTML 前要跳脫（兩者都不能少）。"""
    sender = RecordingSender(result={"msgid": "a b&c", "account_point": 1})
    app = make_app(tmp_path, sender, status_query=RecordingStatusQuery())

    text = send_once(app).text

    assert 'href="/status?msgid=a%20b%26c"' in text


def test_form_page_offers_a_status_lookup_entry(tmp_path: Path) -> None:
    """表單頁也要有入口（使用者晚點回來查時，手上只有 msgid）。"""
    app = make_app(tmp_path, RecordingSender(), status_query=RecordingStatusQuery())

    text = app.route("GET", "/").text

    assert 'href="/status"' in text


def test_status_tone_table_covers_every_mitake_category() -> None:
    """把 templates 那份分類字串與 mitake 的常數釘在一起。

    templates.py 刻意不 import mitake（見該檔 `_STATUS_TONE` 的註解），代價是兩邊
    各寫一份字串。這支測試就是那份重複的鎖：任何一邊改了名字，這裡立刻紅燈，
    不會靜默漂成「查到的狀態永遠用 unknown 的樣式顯示」。
    """
    mitake_categories = {
        mitake.DELIVERY_PENDING,
        mitake.DELIVERY_DELIVERED,
        mitake.DELIVERY_FAILED,
        mitake.DELIVERY_ERROR,
        mitake.DELIVERY_ACCOUNT_ERROR,
        mitake.DELIVERY_UNKNOWN,
    }
    assert set(templates._STATUS_TONE) == mitake_categories
    # 對照表用到的每個分類都在這份清單裡（不會冒出沒人認得的第七種）。
    assert {
        category for _, category in mitake.DELIVERY_STATUS_TABLE.values()
    } <= mitake_categories


# --------------------------------------------------------------------------- #
# 18. 兩欄式版面（側欄要在每一頁、版本取自常數、/trial-email 占位頁受保護）
# --------------------------------------------------------------------------- #


def test_sidebar_appears_on_every_page(tmp_path: Path) -> None:
    """側欄（品牌 + 兩個導覽入口）必須出現在全站每一頁，而不只首頁。

    版面是套在 `_page`（所有頁面的共同外框）上的，所以只要抽三種不同流程的頁面
    ——發送表單、占位頁、投遞狀態表單——驗品牌字與兩個入口字串都在即可。
    """
    app = make_app(tmp_path, RecordingSender())

    for response in (
        app.route("GET", "/"),
        app.route("GET", "/trial-email"),
        app.route("GET", "/status"),
    ):
        assert response.status == HTTPStatus.OK, response.text
        assert "三竹簡訊工具" in response.text
        assert "發送三竹簡訊" in response.text
        assert "14天用戶體驗-郵件數據寄送" in response.text


def test_sidebar_shows_version_and_release_date(tmp_path: Path) -> None:
    """側欄的版本號與發布日要取自 `web` 套件常數（改常數就跟著動，不是各處寫死）。"""
    from web import __release_date__, __version__

    body = make_app(tmp_path, RecordingSender()).route("GET", "/").text

    assert f"v{__version__}" in body
    assert __release_date__ in body


def test_trial_email_page_is_protected_and_get_only(tmp_path: Path) -> None:
    """`/trial-email` 體驗借出頁：GET 回 200、受 Access 保護、非 GET 回 405。

    受 Access 保護這點沿用「設了 require_access_email 之後除 /health 外一律要擋」
    那套做法：不帶身分標頭連這頁都要被擋（403）—— 整個介面只要開了 Access，
    就不該有任何頁面漏在保護外。內容（表格／空名單提示）由另外的測試各自鎖，
    這裡只鎖路由與保護，故只驗頁面標題出現、不驗表格細節。
    """
    open_app = make_app(tmp_path, RecordingSender())
    page = open_app.route("GET", "/trial-email")
    assert page.status == HTTPStatus.OK
    assert "體驗借出管理" in page.text

    guarded = make_app(
        tmp_path, RecordingSender(), require_access_email="peter@example.com"
    )
    assert guarded.route("GET", "/trial-email").status == HTTPStatus.FORBIDDEN

    assert (
        open_app.route("POST", "/trial-email").status == HTTPStatus.METHOD_NOT_ALLOWED
    )


def test_live_server_passes_the_query_string_to_the_status_page(tmp_path: Path) -> None:
    """端對端：``?msgid=`` 真的有從 HTTP 層傳到路由層。

    上面所有查詢測試都直接呼叫 ``SmsWebApp.route()``，餵的是已經解析好的 query
    dict —— 那條路徑驗不到 ``_dispatch`` 有沒有真的去解析網址上的 query string。
    漏掉那一步的症狀是：不論帶什麼 msgid，畫面永遠只出查詢表單（因為 app 收到的
    是空 dict），而每一支單元測試都還是綠的。所以這條非開真的 socket 不可。

    仍然不連任何外部主機：status_query 是假的。
    """
    status_query = RecordingStatusQuery(result=status_result("4"))
    app = make_app(tmp_path, RecordingSender(), status_query=status_query)
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(
            f"http://{host}:{port}/status?msgid=0315772761", timeout=5
        ) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status_query.calls == [
        {"msgid": "0315772761", "timeout": pytest.approx(25.0)}
    ]
    assert "已送達手機" in html


# --------------------------------------------------------------------------- #
# 19. 發送對象下拉選單（Part B：id → 電話伺服器端解析、不信任前端、無名單退回手動）
# --------------------------------------------------------------------------- #


def test_recipient_dropdown_renders_with_disabled_unselectable_entries(
    tmp_path: Path,
) -> None:
    """注入名單後，表單出下拉：可選者帶遮罩電話與姓名，ambiguous / not_found 灰掉但看得到。

    「看得到但選不了」是刻意的 —— 看不到的話操作者會以為名單漏人、跑去手動硬塞號碼，
    那正是這個功能要防的（手動輸入正是打錯字、送錯人的來源）。
    """
    book = sample_recipient_book()
    app = make_app(tmp_path, RecordingSender(), recipient_source=lambda: book)

    text = app.route("GET", "/").text

    # 下拉本體 + 可選者（含遮罩電話與姓名）。
    assert '<select name="recipient_id"' in text
    assert '<option value="u46"' in text
    assert "陳筱琪" in text
    assert "0918***24" in text  # 遮罩：前 4 後 2，中間 ***
    # ambiguous / not_found：出現在畫面上（看得到姓名）但為 disabled option（選不了）。
    assert "<option disabled>" in text
    assert "青蘋果" in text
    assert "青化活動中心" in text
    # 名單新舊程度的提示。
    assert "名單同步於" in text
    # 真實號碼不可整串暴露在下拉裡（只給遮罩版）。
    assert "0918123424" not in text


def test_preview_rejects_recipient_id_and_manual_phone_together(
    tmp_path: Path,
) -> None:
    """下拉與手動輸入**兩者都有值** → 400 擋下，不猜優先序，不發 token、不送出。

    這是 doc/spec-multi-recipient-sms.md §2 的明文規定，取代了本功能加入前的舊行為
    （舊行為是「靜默信任 recipient_id、忽略 phone」）。理由：若默默選其中一個當
    優先，使用者會以為送去的是他手動打的那支、實際卻送到下拉選的人（或反過來）——
    這正是本專案要不惜代價避免的「畫面與實際不一致」。真正的防竄改改由下面
    ``test_preview_resolves_phone_from_id_when_manual_field_is_blank`` 驗證：
    手動欄位**留白**時，前端改不了送到誰。
    """
    book = sample_recipient_book()
    sender = RecordingSender()
    app = make_app(tmp_path, sender, recipient_source=lambda: book)

    response = app.route(
        "POST",
        "/preview",
        _form(recipient_id="u46", phone="0900000000", body=SHORT_BODY),
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "只擇一" in response.text
    assert _TOKEN_PATTERN.search(response.text) is None
    assert app.token_store.pending_count == 0
    assert sender.call_count == 0


def test_preview_resolves_phone_from_id_when_manual_field_is_blank(
    tmp_path: Path,
) -> None:
    """下拉模式：手動輸入欄位**留白**時，號碼由伺服器端以 id 反查（防竄改核心）。

    新版 UI 下拉與手動輸入並存，正常瀏覽器提交時手動欄位一律會帶上（即使是空字串）
    ——這裡驗證「有欄位但是空字串」與「完全沒有這個欄位」兩種提交方式都不算
    衝突，仍然照 recipient_id 正常解析並走完二階段確認。
    """
    book = sample_recipient_book()
    sender = RecordingSender()
    app = make_app(tmp_path, sender, recipient_source=lambda: book)

    preview = app.route(
        "POST",
        "/preview",
        _form(recipient_id="u46", phone="", body=SHORT_BODY),
    )

    assert preview.status == HTTPStatus.OK
    assert "0918123424" in preview.text  # 名單裡的真號碼
    assert "陳筱琪" in preview.text  # 收件人姓名
    match = _TOKEN_PATTERN.search(preview.text)
    assert match is not None, "確認頁沒有 token，下拉模式的二階段確認壞了"

    sent = app.route("POST", "/send", _form(token=match.group(1)))

    assert sent.status == HTTPStatus.OK
    assert sender.call_count == 1
    assert sender.calls[0]["phone"] == "0918123424"  # 真的送到的是伺服器端解析的號碼
    assert "已發送給 陳筱琪" in sent.text


def test_dropdown_rejects_untrusted_id_and_empty_book_falls_back_to_manual(
    tmp_path: Path,
) -> None:
    """(a) 不可選 / 未知 id 一律擋在確認頁前（不發 token、不送出）；(b) 空名單退回手動輸入。

    (a) 是防竄改的另一半：前端送 not_found / ambiguous / 根本不存在的 id 過來，
    伺服器端 book.get() 都回 None → 400，token 不發、簡訊不送。
    (b) 確保「沒設定名單」時行為與現況完全一致：仍是手動 `<input name="phone">`，
    且既有的手動 preview→send 流程照常走得完。
    """
    book = sample_recipient_book()
    sender = RecordingSender()
    app = make_app(tmp_path, sender, recipient_source=lambda: book)

    # (a) not_found / ambiguous / 未知 id 都要被擋下。
    for bad_id in ("loan-x", "u43", "does-not-exist"):
        response = app.route(
            "POST", "/preview", _form(recipient_id=bad_id, body=SHORT_BODY)
        )
        assert response.status == HTTPStatus.BAD_REQUEST, bad_id
        assert _TOKEN_PATTERN.search(response.text) is None, bad_id
        assert "重新選擇" in response.text
    assert app.token_store.pending_count == 0
    assert sender.call_count == 0

    # (b) 空名單（不注入 recipient_source）→ 手動輸入，且流程照常。
    manual_app = make_app(tmp_path, RecordingSender())
    form_text = manual_app.route("GET", "/").text
    assert '<input type="tel" id="sms-phone" name="phone"' in form_text
    assert "名單尚未同步" in form_text
    assert '<select name="recipient_id"' not in form_text
    assert send_once(manual_app).status == HTTPStatus.OK


# --------------------------------------------------------------------------- #
# 20. /trial-email 體驗借出表格（鏡像 AIHCR；唯讀、不寄信、不花錢）
# --------------------------------------------------------------------------- #


def test_trial_email_renders_table_of_trials(tmp_path: Path) -> None:
    """注入名單後，`/trial-email` 出一張表格：這七個表頭都在 + 每筆 recipient 一列。

    鏡像 AIHCR 體驗借出管理頁（設備／客戶／接機日／天數／已用天／業務／狀態）。
    驗某筆的 device/name/business/trial_status 值都出現，且**不含**電話號碼
    （此頁刻意不顯示電話欄，避免號碼被肩窺／截圖）。
    """
    book = RecipientBook(
        [
            Recipient(
                id="u46",
                name="陳筱琪",
                phone="0918123424",
                device="體驗活動14天-陳筱琪4c74",
                borrow_date="2026-07-29",
                match_status="ok",
                days="14",
                used_days="3",
                business="王小明",
                trial_status="🟢 體驗中",
            ),
            Recipient(
                id="u43",
                name="青蘋果",
                phone=None,
                device="體驗活動14天-青蘋果",
                borrow_date="2026-07-20",
                match_status="ambiguous",
                days="14",
                used_days="9",
                business="李大同",
                trial_status="🟢 體驗中",
            ),
        ],
        generated_at="2026-07-30T11:00:00+08:00",
    )
    app = make_app(tmp_path, RecordingSender(), recipient_source=lambda: book)

    text = app.route("GET", "/trial-email").text

    # 表格本體與七個表頭都在。
    assert "<table" in text
    for header in ("設備", "客戶", "接機日", "天數", "已用天", "業務", "狀態"):
        assert header in text, header
    # 某筆的 device/name/business/trial_status 值都出現（證明新四欄真的渲染出來）。
    assert "體驗活動14天-陳筱琪4c74" in text
    assert "陳筱琪" in text
    assert "王小明" in text
    assert "李大同" in text
    assert "🟢 體驗中" in text
    # 同步時間提示。
    assert "資料同步於" in text
    # 唯讀頁不顯示電話欄：真實號碼不得出現在表格裡。
    assert "0918123424" not in text


def test_trial_email_empty_book_shows_notice_not_table(tmp_path: Path) -> None:
    """空名單（producer 尚未跑、或真的沒人體驗）→ 顯示提示，**不**渲染空表格。

    預設 make_app 不注入 recipient_source＝空名單，正好覆蓋這條降級路徑。
    """
    app = make_app(tmp_path, RecordingSender())

    response = app.route("GET", "/trial-email")

    assert response.status == HTTPStatus.OK
    assert "<table" not in response.text
    assert "尚無資料" in response.text or "名單尚未同步" in response.text


def test_trial_email_escapes_cell_values(tmp_path: Path) -> None:
    """表格每一格都要過 _e —— 名單源自外部 producer，惡意值不得原樣進 HTML。

    塞一筆 business='<script>x</script>'：輸出不得含活的 `<script>`，而應是跳脫後的
    實體。這是回歸鎖（現況安全，存在意義是「拿掉那個 _e 之後這條會轉紅」）。
    """
    book = RecipientBook(
        [
            Recipient(
                id="u1",
                name="測試",
                phone=None,
                device="體驗機",
                borrow_date="2026-07-29",
                match_status="ok",
                days="14",
                used_days="1",
                business="<script>x</script>",
                trial_status="🟢 體驗中",
            ),
        ],
    )
    app = make_app(tmp_path, RecordingSender(), recipient_source=lambda: book)

    text = app.route("GET", "/trial-email").text

    assert "<script>x</script>" not in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text


def test_trial_email_send_report_button_enabled_when_used_days_reaches_total() -> None:
    """已用天數（今天－接機日動態算）== 天數 →「寄送體驗報告」按鈕要可點（無 disabled）。

    這是按鈕啟用規則的 happy path：接機日 2026-07-20、注入 today=2026-08-03，
    剛好滿 14 天，對應「已用天 >= 天數」時按鈕可點的規則。``used_days`` 欄位
    已不再影響按鈕邏輯（改用 borrow_date 動態算），刻意留空字串以證明這點。
    """
    book = RecipientBook(
        [
            Recipient(
                id="u1",
                name="測試",
                phone=None,
                device="體驗機",
                borrow_date="2026-07-20",
                match_status="ok",
                days="14",
                used_days="",
                business="王小明",
                trial_status="🟢 體驗中",
            ),
        ],
    )

    text = templates.render_trial_email(book, today=date(2026, 8, 3))

    assert "寄送體驗報告" in text
    assert "<button" in text
    # 抓出這顆按鈕的完整標籤，確認裡面沒有 disabled 屬性。
    match = re.search(r'<button type="submit" class="send-report-btn"[^>]*>', text)
    assert match is not None
    assert "disabled" not in match.group()


def test_trial_email_send_report_button_disabled_when_used_days_below_total() -> None:
    """已用天數（今天－接機日動態算）< 天數 → 按鈕要灰階不可點（含 disabled 屬性）。

    這是啟用規則的邊界案例：接機日 2026-07-20、注入 today=2026-07-25，只過了
    5 天，還沒體驗到期，不該讓人誤按去寄體驗報告。
    """
    book = RecipientBook(
        [
            Recipient(
                id="u1",
                name="測試",
                phone=None,
                device="體驗機",
                borrow_date="2026-07-20",
                match_status="ok",
                days="14",
                used_days="",
                business="王小明",
                trial_status="🟢 體驗中",
            ),
        ],
    )

    text = templates.render_trial_email(book, today=date(2026, 7, 25))

    match = re.search(r'<button type="submit" class="send-report-btn"[^>]*>', text)
    assert match is not None
    assert "disabled" in match.group()


def test_trial_email_send_report_button_handles_real_world_day_formats() -> None:
    """整合案例：線上實際資料格式（days 帶「天」字、borrow_date 格式壞掉）都要能
    正確解析、不能讓渲染整頁失敗。

    producer 從 AIHCR innerText 原樣帶下來的 ``days`` 欄位格式不保證乾淨（使用者
    截圖顯示實際格式是「14 天」而非「14」），第一筆驗證 ``_parse_trial_day_count``
    吃得下帶「天」字的 total、且 borrow_date 配合注入的 today 剛好達標。第二筆的
    ``borrow_date`` 本身格式壞掉（不是合法日期），驗證 ``_compute_used_days``
    解析失敗時保守擋下（不可點）——這是已用天數改用接機日動態算之後，還在生效的
    解析健壯性路徑。
    """
    book = RecipientBook(
        [
            Recipient(
                id="u1",
                name="陳筱琪",
                phone=None,
                device="體驗機A",
                borrow_date="2026-07-20",
                match_status="ok",
                days="14 天",
                used_days="",
                business="王小明",
                trial_status="🟢 體驗中",
            ),
            Recipient(
                id="u2",
                name="李小華",
                phone=None,
                device="體驗機B",
                borrow_date="不是日期",
                match_status="ok",
                days="14",
                used_days="",
                business="李大同",
                trial_status="🟢 體驗中",
            ),
        ],
    )

    text = templates.render_trial_email(book, today=date(2026, 8, 3))

    # 不能因為格式怪異就讓整頁渲染失敗（500 或例外）。
    assert "寄送體驗報告" in text

    buttons = re.findall(r'<button type="submit" class="send-report-btn"[^>]*>', text)
    assert len(buttons) == 2
    # 第一筆「14 天」total 解析成功，borrow_date 2026-07-20 配合 today 2026-08-03
    # 剛好滿 14 天 → 可點。
    assert "disabled" not in buttons[0]
    # 第二筆 borrow_date 格式壞掉，_compute_used_days 回 None → 保守預設不可點。
    assert "disabled" in buttons[1]


def test_trial_email_used_days_column_computed_from_borrow_date_not_snapshot() -> None:
    """已用天 = 今天－接機日動態算，不是 producer 快照裡的 used_days 字串。

    Happy path：borrow_date=2026-07-20、注入 today=2026-08-03，剛好滿 14 天。
    ``used_days`` 欄位刻意留一個明顯錯誤的舊快照值（"999"），證明「已用天」欄顯示
    的是算出來的 "14 天" 而不是快照殘留的舊值 —— 這正是本次改動要解決的問題：
    producer 快照只在重新跑一次時才更新，兩次同步之間會凍結在舊數字。
    """
    book = RecipientBook(
        [
            Recipient(
                id="u1",
                name="測試",
                phone=None,
                device="體驗機",
                borrow_date="2026-07-20",
                match_status="ok",
                days="14",
                used_days="999",  # 明顯過時的快照殘留值，不該被顯示出來
                business="王小明",
                trial_status="🟢 體驗中",
            ),
        ],
    )

    text = templates.render_trial_email(book, today=date(2026, 8, 3))

    assert "14 天" in text
    assert "999" not in text
    match = re.search(r'<button type="submit" class="send-report-btn"[^>]*>', text)
    assert match is not None
    assert "disabled" not in match.group()


def test_trial_email_used_days_shows_undetermined_when_borrow_date_unparseable() -> None:
    """Edge case：``borrow_date`` 格式壞掉 → 「已用天」欄顯示「無法判斷」、按鈕維持 disabled，
    且整頁渲染不能因此 500／拋例外。

    先驗證 :func:`web.templates._compute_used_days` 本身對壞格式回 ``None``，
    再驗證整頁渲染的行為一致。
    """
    assert templates._compute_used_days("not-a-date") is None

    book = RecipientBook(
        [
            Recipient(
                id="u1",
                name="測試",
                phone=None,
                device="體驗機",
                borrow_date="not-a-date",
                match_status="ok",
                days="14",
                used_days="",
                business="王小明",
                trial_status="🟢 體驗中",
            ),
        ],
    )

    text = templates.render_trial_email(book, today=date(2026, 8, 3))

    assert "無法判斷" in text
    match = re.search(r'<button type="submit" class="send-report-btn"[^>]*>', text)
    assert match is not None
    assert "disabled" in match.group()


def test_compute_used_days_clamps_future_borrow_date_to_zero_not_negative() -> None:
    """回歸鎖：接機日在「今天」之後（producer 髒資料的可能情況）回傳 ``0``，不回負數。

    這是刻意保留的行為（見 code review 討論）：``0`` 天在邏輯上仍然安全 ——
    必然小於任何合理的總天數，不會讓按鈕誤解鎖；代價是呼叫端無法從回傳值本身
    分辨「剛接機、0 天」與「接機日資料異常」，但這個決定不影響安全性。這支測試
    的存在意義是讓這個決定變成「有意識保留、且被測試鎖住」，而不是沒人測過的
    偶然行為 —— 未來有人想改成回傳 ``None`` 時，這裡會先轉紅提醒。
    """
    # 接機日比 today 晚 5 天 → 沒有負數的「已用 -5 天」，回傳 0。
    assert templates._compute_used_days("2026-08-08", today=date(2026, 8, 3)) == 0
    # 接機日等於 today → 剛接機當天，也是 0（與上面同一條 clamp 邏輯，非特例）。
    assert templates._compute_used_days("2026-08-03", today=date(2026, 8, 3)) == 0


# --------------------------------------------------------------------------- #
# 16. 訊息範本 radio 快選（v0.003：純前端便利功能，不動送出邏輯）
# --------------------------------------------------------------------------- #


def test_form_renders_three_message_template_radios() -> None:
    """表單頁要出三個共用 name="sms-template" 的 radio，各帶 label 文字與 value。

    三個範本（出貨／體驗結束／濾網更換）是定案需求，數量與文字都鎖住 —— 少一個或
    改名都代表 MESSAGE_TEMPLATES 被動到，這裡就該轉紅。
    """
    html = templates.render_form(max_segments=5, chars_per_segment=70, script_nonce="n")

    # 三個 radio 共用 name（天然單選互斥），且 fieldset legend 存在。
    assert html.count('name="sms-template"') == 3
    assert "訊息範本（選一個自動帶入，仍可修改）" in html
    # 三個 label 文字。
    for label in ("出貨通知", "14天體驗結束通知", "濾網更換通知"):
        assert label in html
    # 三個 value（送出時 radio 的值，純前端用途）。
    for value in ("ship", "trial_end", "filter"):
        assert f'value="{value}"' in html


def test_template_radios_escape_body_into_data_attribute() -> None:
    """範本 body 進 data-body 屬性前必須過 _e —— 否則哪天改讀外部來源就是屬性注入。

    直接餵一段惡意 body（含 <script>、雙引號、& 符號）給 helper：輸出不得含活的
    `<script>alert`（會變成頁面上真的執行的腳本），而應是跳脫後的實體。這是回歸鎖：
    現況安全（body 都包著 _e），存在的意義是「拿掉那個 _e 之後這條會轉紅」。
    """
    out = templates._template_radios(
        [{"key": "x", "label": "惡意", "body": '<script>alert(1)</script>"&'}]
    )

    # 活的 script 標籤不得原樣出現在 data-body 裡。
    assert "<script>alert" not in out
    # 角括號、雙引號、& 都要以跳脫形式存在（證明 body 過了 _e）。
    assert "&lt;script&gt;" in out
    assert "&quot;" in out
    assert "&amp;" in out


def test_template_autofill_wired_via_addeventlistener_and_single_script() -> None:
    """帶入邏輯要接上（腳本引用 sms-template）且用 addEventListener（CSP 相容，非 inline）。

    另鎖「本頁仍只有一支帶 nonce 的 <script>」：CSP 的 script-src 是 'nonce-…'，多開
    第二支未帶 nonce 的腳本會被瀏覽器擋掉，也違反本功能「只擴充既有那支腳本」的約束。
    """
    # 帶入邏輯存在，且以事件監聽（非 inline onclick）掛上。
    assert "sms-template" in templates._SEGMENT_SCRIPT
    assert "addEventListener" in templates._SEGMENT_SCRIPT

    html = templates.render_form(max_segments=5, chars_per_segment=70, script_nonce="n")
    # 全頁只有一支 <script>（就是帶 nonce 的那支），沒有新增第二支。
    assert html.count("<script") == 1
    assert '<script nonce="n">' in html


# --------------------------------------------------------------------------- #
# 21. POST /trial-email/send-report（寄送體驗報告；本檔只測路由層轉譯，
#     web.trial_report.send_trial_report 內部驗證邏輯見 tests/test_trial_report.py）
# --------------------------------------------------------------------------- #


class RecordingTrialReportSender:
    """假的 ``web.trial_report.send_trial_report``：記下呼叫參數，回傳設定好的結果。

    本檔測的是「伺服器路由層」——反查 recipient、呼叫 sender、把結果轉譯成
    HTTP 回應這三件事，不是體驗報告本身的驗證邏輯（那部分見
    tests/test_trial_report.py）。用這個假身讓兩層測試完全independent，
    互相修改不需要同步。
    """

    def __init__(self, result: TrialReportResult) -> None:
        self.calls: list[dict] = []
        self._result = result

    def __call__(self, recipient: Recipient, *, staff_bcc: tuple = ()) -> TrialReportResult:
        self.calls.append({"recipient": recipient, "staff_bcc": tuple(staff_bcc)})
        return self._result


def _trial_book_with_one_ok_recipient(recipient_id: str = "u46") -> RecipientBook:
    """一份只含 1 筆可選對象的名單，供 /trial-email/send-report 測試共用。"""
    return RecipientBook(
        [
            Recipient(
                id=recipient_id,
                name="陳筱琪",
                phone="0912345678",
                device="體驗機",
                borrow_date="2026-07-01",
                match_status="ok",
                days="14",
                used_days="14",
                business="王小明",
                trial_status="🟢 體驗中",
            ),
        ],
    )


def test_send_trial_report_get_is_method_not_allowed(tmp_path: Path) -> None:
    """GET /trial-email/send-report → 405（同其餘會觸發副作用的端點慣例，只收 POST）。"""
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=RecordingTrialReportSender(
            TrialReportResult(success=True, recipient_id="u46", reason=None, message="ok")
        ),
    )

    assert app.route("GET", "/trial-email/send-report").status == HTTPStatus.METHOD_NOT_ALLOWED


def test_send_trial_report_requires_access_email_when_configured(tmp_path: Path) -> None:
    """設了 require_access_email 時，這條路由要跟其他頁面一樣受保護（403，不是漏網之魚）。"""
    fake_sender = RecordingTrialReportSender(
        TrialReportResult(success=True, recipient_id="u46", reason=None, message="ok")
    )
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=fake_sender,
        require_access_email="ops@example.com",
    )

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u46"]}
    )

    assert response.status == HTTPStatus.FORBIDDEN
    assert fake_sender.calls == []


def test_send_trial_report_missing_recipient_id_blocks_without_calling_sender(
    tmp_path: Path,
) -> None:
    """表單沒帶 recipient_id → 400，且完全不呼叫 sender（不執行任何動作）。"""
    fake_sender = RecordingTrialReportSender(
        TrialReportResult(success=True, recipient_id="u46", reason=None, message="ok")
    )
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=fake_sender,
    )

    response = app.route("POST", "/trial-email/send-report", form={})

    assert response.status == HTTPStatus.BAD_REQUEST
    assert fake_sender.calls == []


def test_send_trial_report_unknown_recipient_id_blocks_without_calling_sender(
    tmp_path: Path,
) -> None:
    """recipient_id 查無此人（或名單已更新）→ 400，不呼叫 sender。

    這是防竄改的核心：不管前端傳什麼 recipient_id 過來，伺服器只信自己反查的結果。
    """
    fake_sender = RecordingTrialReportSender(
        TrialReportResult(success=True, recipient_id="u46", reason=None, message="ok")
    )
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=fake_sender,
    )

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u999-does-not-exist"]}
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert fake_sender.calls == []


def test_send_trial_report_success_renders_ok_page_and_calls_sender_once(
    tmp_path: Path,
) -> None:
    """成功路徑：200、畫面顯示 sender 回傳的訊息、sender 只被呼叫一次且帶對的 recipient。"""
    fake_sender = RecordingTrialReportSender(
        TrialReportResult(
            success=True,
            recipient_id="u46",
            reason=None,
            message="已寄送給 ch***@example.com。",
            email_masked="ch***@example.com",
            pdf_attached=True,
        )
    )
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=fake_sender,
        staff_bcc=("staff1@example.com", "staff2@example.com"),
    )

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u46"]}
    )

    assert response.status == HTTPStatus.OK
    assert "已寄送給 ch***@example.com" in response.text
    assert len(fake_sender.calls) == 1
    assert fake_sender.calls[0]["recipient"].id == "u46"
    assert fake_sender.calls[0]["staff_bcc"] == ("staff1@example.com", "staff2@example.com")


def test_send_trial_report_client_side_reason_renders_400(tmp_path: Path) -> None:
    """sender 回傳「資料面」失敗原因（如已用天數不足）→ 400，不是 500。"""
    fake_sender = RecordingTrialReportSender(
        TrialReportResult(
            success=False,
            recipient_id="u46",
            reason=REASON_DAYS_NOT_REACHED,
            message="已用天數尚未達到體驗天數，未寄送任何郵件。",
        )
    )
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=fake_sender,
    )

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u46"]}
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "已用天數尚未達到" in response.text


def test_send_trial_report_upstream_reason_renders_500(tmp_path: Path) -> None:
    """sender 回傳「上游」失敗原因（如 DB 連線失敗）→ 500，不是 400。

    500 頁不得洩漏內部細節（同本專案既有慣例）——這裡只確認狀態碼與訊息本身，
    訊息內容由 web.trial_report 端保證不含連線字串。
    """
    fake_sender = RecordingTrialReportSender(
        TrialReportResult(
            success=False,
            recipient_id="u46",
            reason=REASON_DB_ERROR,
            message="資料庫連線失敗，未寄送任何郵件。",
        )
    )
    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=fake_sender,
    )

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u46"]}
    )

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "資料庫連線失敗" in response.text


def test_send_trial_report_sender_raises_returns_500_without_leaking_details(
    tmp_path: Path,
) -> None:
    """sender 拋出未預期例外 → 500，畫面不得出現例外訊息本身（不洩漏內部細節）。"""

    def _boom(recipient: Recipient, *, staff_bcc: tuple = ()) -> TrialReportResult:
        raise RuntimeError("pymysql.err.OperationalError: (2003, boom secret detail)")

    app = make_app(
        tmp_path,
        RecordingSender(),
        recipient_source=_trial_book_with_one_ok_recipient,
        trial_report_sender=_boom,
    )

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u46"]}
    )

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "boom secret detail" not in response.text


def test_send_trial_report_route_revalidates_days_even_if_frontend_disabled_was_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心安全性測試：即使前端 disabled 被繞過直接送出表單，伺服器仍會重算天數擋下。

    這裡刻意**不注入假的 trial_report_sender**——用 SmsWebApp 的預設值，也就是
    真正的 ``web.trial_report.send_trial_report``，全程走到真實的驗證邏輯，才真正
    證明「伺服器端會自己重算」而不是「假 sender 剛好回傳失敗」。已用天數 < 天數
    這條分支在真正碰觸 DB **之前**就會被擋下，所以這裡不會真的連線 acfh_api，
    只把稽核檔位置導去 tmp_path，避免污染 repo 的 logs/ 目錄。

    這條路徑（``app.route`` 直接呼叫真正的 ``send_trial_report``）不像
    :func:`web.templates.render_trial_email` 或 ``send_trial_report`` 本身可以
    注入 ``today``——``web.server`` 的呼叫端刻意不傳這個參數（見改動說明），一律
    用伺服器真實日期。所以這裡改用「接機日＝今天」（用跟 ``_compute_used_days``
    伺服器端 fallback 完全相同的 ``ZoneInfo("Asia/Taipei")`` 運算式構造），已用天數
    會是 0（在極端情況下最多 1，仍遠小於 14 天），不論測試在哪一天執行都保證
    「尚未達標」，不依賴任何特定的 wall-clock 日期值。
    """
    monkeypatch.setenv(
        "MITAKE_WEB_TRIAL_AUDIT_PATH", str(tmp_path / "trial-report-audit.jsonl")
    )
    book = RecipientBook(
        [
            Recipient(
                id="u46",
                name="陳筱琪",
                phone="0912345678",
                device="體驗機",
                # 接機日＝今天：已用天數必定是 0，恆小於 14 天，不依賴測試執行當下
                # 的具體日期值（同 _compute_used_days 的伺服器端預設計算方式）。
                borrow_date=datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat(),
                match_status="ok",
                days="14",
                used_days="3",  # 已不影響驗證邏輯，僅留作展示欄位的既有寫法
                business="王小明",
                trial_status="🟢 體驗中",
            ),
        ],
    )
    app = make_app(tmp_path, RecordingSender(), recipient_source=lambda: book)

    response = app.route(
        "POST", "/trial-email/send-report", form={"recipient_id": ["u46"]}
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "天數" in response.text
    # 稽核檔案要真的落地在我們指定的 tmp_path，而不是悄悄寫進 repo 的 logs/。
    audit_path = tmp_path / "trial-report-audit.jsonl"
    assert audit_path.exists()


# --------------------------------------------------------------------------- #
# 21. 兩處各寫一份的跳過原因字串，用測試釘在一起（同 _STATUS_TONE 那套作法）
# --------------------------------------------------------------------------- #


def test_skip_reason_labels_cover_every_batch_recipients_reason() -> None:
    """`web.templates._SKIP_REASON_LABEL` 的 key 必須與
    `web.batch_recipients.REASON_INVALID_FORMAT` / `REASON_DUPLICATE` 逐字一致
    ——templates.py 執行期刻意不 import batch_recipients（會連帶 import mitake），
    兩邊各寫一份字串，靠這支測試鎖住不漂移。
    """
    assert set(templates._SKIP_REASON_LABEL.keys()) == {
        batch_recipients.REASON_INVALID_FORMAT,
        batch_recipients.REASON_DUPLICATE,
    }


# --------------------------------------------------------------------------- #
# 22. 多人（上傳名單）模式：POST /preview
# --------------------------------------------------------------------------- #


def test_batch_preview_happy_path_issues_token_and_shows_counts(tmp_path: Path) -> None:
    """正常名單（含一筆格式錯、一筆重複）→ 200，顯示正確的發送／跳過人數與總扣點。"""
    app = make_app(tmp_path, RecordingSender())
    text_content = "0912345678\n0987654321\nabc\n0912345678\n"

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.OK
    assert "2 人" in response.text  # 兩支有效號碼
    assert "跳過的 2 筆" in response.text  # abc（格式錯）+ 重複的 0912345678
    assert "2 人 × 1 則 = 2 點" in response.text
    assert _TOKEN_PATTERN.search(response.text) is not None
    assert app.token_store.pending_count == 1


def test_batch_preview_missing_file_and_missing_text_is_400(tmp_path: Path) -> None:
    """完全沒帶檔案也沒帶 recipients_text → 400，明講「請上傳名單檔案」。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    response = app.route("POST", "/preview", _batch_form(SHORT_BODY))

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "請上傳名單檔案" in response.text
    assert app.token_store.pending_count == 0
    assert sender.call_count == 0


def test_batch_preview_empty_uploaded_file_is_400(tmp_path: Path) -> None:
    """上傳了一個 0 位元組的空檔案 → 與「沒上傳」同樣視為 400。"""
    app = make_app(tmp_path, RecordingSender())

    response = app.route(
        "POST", "/preview", _batch_form(SHORT_BODY), files={"recipients_file": b""}
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "請上傳名單檔案" in response.text


def test_batch_preview_non_utf8_file_is_400(tmp_path: Path) -> None:
    """檔案不是合法 UTF-8 → 400，訊息提示存成 UTF-8 純文字檔，不嘗試硬解。"""
    app = make_app(tmp_path, RecordingSender())

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": b"\xff\xfe\x00\x01"},
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "編碼" in response.text


def test_batch_preview_no_valid_numbers_is_400(tmp_path: Path) -> None:
    """整份檔案沒有一支有效號碼（例如誤傳帶標題列的 Excel 匯出檔）→ 400。"""
    app = make_app(tmp_path, RecordingSender())
    text_content = "姓名,電話\n陳先生,0912345678\n"

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "沒有可發送的有效號碼" in response.text
    assert app.token_store.pending_count == 0


def test_batch_preview_over_max_batch_recipients_is_400(tmp_path: Path) -> None:
    """有效號碼數超過 MAX_BATCH_RECIPIENTS → 400，要求分批上傳。"""
    app = make_app(tmp_path, RecordingSender())
    lines = [f"09{str(i).zfill(8)}" for i in range(batch_recipients.MAX_BATCH_RECIPIENTS + 1)]
    text_content = "\n".join(lines)

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "不可超過" in response.text
    assert app.token_store.pending_count == 0


def test_batch_preview_exactly_at_max_batch_recipients_is_allowed(tmp_path: Path) -> None:
    """筆數剛好等於上限 → 允許（``<=`` 而非 ``<``，沿用 RateLimiter 同樣的語意）。"""
    app = make_app(tmp_path, RecordingSender(), rate_limit=10_000)
    lines = [f"09{str(i).zfill(8)}" for i in range(batch_recipients.MAX_BATCH_RECIPIENTS)]
    text_content = "\n".join(lines)

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.OK
    assert _TOKEN_PATTERN.search(response.text) is not None


def test_batch_preview_accepts_non_txt_filename_with_valid_plain_text_content(
    tmp_path: Path,
) -> None:
    """檔名是 ``.xlsx``（甚至完全沒有副檔名）但內容其實是合法的純文字號碼清單
    → 一樣正常解析、放行，不因副檔名不符而擋下。

    doc/spec-multi-recipient-sms.md 邊界條件明講：「副檔名檢查只是前端提示…
    伺服器端實際判斷依內容能否以 UTF-8 解碼、且解析出至少一個有效號碼，副檔名
    不符但內容剛好是純文字號碼清單時允許通過（不然使用者把檔名打錯就整個卡
    住，且副檔名本來就是前端可偽造的東西，不該當成安全邊界）」。

    刻意走真的 HTTP multipart 解析（而非直接呼叫 ``app.route(files=...)``）：
    ``route()`` 的 ``files`` 參數本來就只是 ``dict[str, bytes]``，從來不帶檔名——
    真正能證明「檔名被忽略」的地方，是 multipart 請求裡確實帶著一個非 ``.txt``
    的 ``filename`` 參數，仍然被正常解析。``web.multipart.parse_multipart_form_data``
    本來就只把 ``filename`` 有無當成「這是檔案欄位還是文字欄位」的判斷依據
    （見 web/multipart.py），從未把檔名值往下傳給 ``web.batch_recipients`` 或
    ``web.server``——這條測試把這個既有的架構事實，變成一條會失敗的回歸測試。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        boundary = "----NonTxtFilenameBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="send-mode"\r\n\r\nbatch\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="body"\r\n\r\n{SHORT_BODY}\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="recipients_file"; '
            'filename="list.xlsx"\r\nContent-Type: '
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
            "0912345678\r\n0987654321\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}/preview",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert "確認批次發送內容" in html
    assert "2 人" in html
    assert sender.call_count == 0  # 這裡只是確認頁，還沒送出，不該有任何發送呼叫


def test_batch_preview_empty_body_is_400(tmp_path: Path) -> None:
    app = make_app(tmp_path, RecordingSender())

    response = app.route(
        "POST",
        "/preview",
        _batch_form("   "),
        files={"recipients_file": b"0912345678\n"},
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "不可為空白" in response.text


def test_batch_preview_over_segment_limit_is_400(tmp_path: Path) -> None:
    """每人內容超過單次則數上限 → 400（成本護欄，個人／多人模式共用同一條規則）。"""
    app = make_app(tmp_path, RecordingSender())
    too_long = "字" * (mitake.CHARS_PER_SEGMENT * mitake.MAX_SEGMENTS_PER_SEND + 1)

    response = app.route(
        "POST",
        "/preview",
        _batch_form(too_long),
        files={"recipients_file": b"0912345678\n"},
    )

    assert response.status == HTTPStatus.BAD_REQUEST
    assert "超過單次上限" in response.text


def test_batch_preview_rate_limit_exceeded_is_429(tmp_path: Path) -> None:
    """N 人 × M 則超過本小時上限 → 429，訊息講清楚是「N 人 × M 則」而非只講單一數字。"""
    app = make_app(tmp_path, RecordingSender(), rate_limit=1)
    text_content = "0912345678\n0987654321\n"  # 2 人 × 1 則 = 2 則，超過上限 1 則

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.TOO_MANY_REQUESTS
    assert "超過本小時上限" in response.text
    assert "2 人" in response.text
    assert app.token_store.pending_count == 0


def test_batch_preview_rate_limit_exactly_at_boundary_is_allowed(tmp_path: Path) -> None:
    """N 人 × M 則**剛好等於**本小時上限 → 允許（``<=`` 而非 ``<``）。

    這裡鎖的是 ``RateLimiter`` 每小時速率上限本身的邊界，與上面
    ``test_batch_preview_exactly_at_max_batch_recipients_is_allowed`` 驗證的
    ``batch_recipients.MAX_BATCH_RECIPIENTS``（單次名單筆數的合理性上限）是
    **兩個不同的常數、兩個不同的檢查點**：前者是 ``RateLimiter._check_locked``
    （``self._rate.check(total_cost)``），後者是 ``_handle_preview_batch`` 裡
    對 ``len(parsed.valid_phones)`` 的直接比較，兩者不可互相取代驗證。

    沿用 ``RateLimiter._check_locked`` 既有語意（``used + cost <= self._limit``
    才放行），與個人模式 ``/preview`` 的既有邊界測試同一套判斷準則。
    """
    app = make_app(tmp_path, RecordingSender(), rate_limit=5)
    # 5 人 × 1 則（SHORT_BODY 只有 7 字，遠低於 CHARS_PER_SEGMENT）＝ 5 則，
    # 剛好打平 rate_limit=5，不多不少。
    text_content = "\n".join(f"09{str(i).zfill(8)}" for i in range(5))

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.OK
    assert "5 人" in response.text
    assert "5 人 × 1 則 = 5 點" in response.text
    assert _TOKEN_PATTERN.search(response.text) is not None
    assert app.token_store.pending_count == 1


def test_batch_preview_shows_skipped_reasons_in_chinese(tmp_path: Path) -> None:
    app = make_app(tmp_path, RecordingSender())
    text_content = "0912345678\nabc\n0912345678\n"

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY),
        files={"recipients_file": text_content.encode("utf-8")},
    )

    assert response.status == HTTPStatus.OK
    assert "格式錯誤" in response.text
    assert "重複" in response.text


def test_batch_preview_accepts_recipients_text_field_when_no_file_uploaded(
    tmp_path: Path,
) -> None:
    """沒有上傳檔案時改讀 ``recipients_text``（重送流程用的欄位）——走完全相同的解析。"""
    app = make_app(tmp_path, RecordingSender())

    response = app.route(
        "POST",
        "/preview",
        _batch_form(SHORT_BODY, recipients_text="0912345678\n0987654321"),
    )

    assert response.status == HTTPStatus.OK
    assert "2 人" in response.text
    assert _TOKEN_PATTERN.search(response.text) is not None


def test_preview_without_send_mode_field_defaults_to_single_mode(tmp_path: Path) -> None:
    """完全沒有 ``send-mode`` 欄位（例如舊版快取頁）→ 一律視為個人模式，不受批次邏輯影響。"""
    sender = RecordingSender()
    app = make_app(tmp_path, sender)

    response = app.route("POST", "/preview", _form(phone=VALID_PHONE, body=SHORT_BODY))

    assert response.status == HTTPStatus.OK
    assert _TOKEN_PATTERN.search(response.text) is not None


# --------------------------------------------------------------------------- #
# 23. 多人（上傳名單）模式：POST /send
# --------------------------------------------------------------------------- #


def _issue_batch_token(
    app: SmsWebApp, phones_text: str, body: str = SHORT_BODY
) -> str:
    """走一次多人模式 `/preview` 拿到批次確認頁的一次性 token。"""
    response = app.route(
        "POST",
        "/preview",
        _batch_form(body),
        files={"recipients_file": phones_text.encode("utf-8")},
    )
    assert response.status == HTTPStatus.OK, response.text
    match = _TOKEN_PATTERN.search(response.text)
    assert match is not None, "批次確認頁沒有 token，二階段確認已經壞了"
    return match.group(1)


def test_batch_send_all_success(tmp_path: Path) -> None:
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            {"msgid": "0313887540", "statuscode": "1", "account_point": 12571},
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "已送達三竹 2 筆" in response.text
    assert "未扣點失敗" not in response.text
    assert "未確認" not in response.text
    assert sender.call_count == 2
    assert app.rate_limiter.snapshot().used == 2  # 兩筆都成功，全部計費，不退還


def test_batch_send_mixed_not_charged_and_sent_refunds_only_that_part(
    tmp_path: Path,
) -> None:
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            api_error(possibly_charged=False, kind=mitake.KIND_API),
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "已送達三竹 1 筆" in response.text
    assert "未扣點失敗 1 筆" in response.text
    assert "未確認" not in response.text
    # 總預約 2 則，1 筆確定沒扣點 → 只退 1 則，最終計費 1 則。
    assert app.rate_limiter.snapshot().used == 1


def test_batch_send_mixed_unconfirmed_and_sent_does_not_refund(tmp_path: Path) -> None:
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED),
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "已送達三竹 1 筆" in response.text
    assert "未確認 1 筆" in response.text
    assert "未扣點失敗" not in response.text
    # 未確認的那一筆視為已消耗，不退還——與已送達的那一筆合計仍是 2 則。
    assert app.rate_limiter.snapshot().used == 2


def test_batch_send_all_three_groups_mixed(tmp_path: Path) -> None:
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            api_error(possibly_charged=False, kind=mitake.KIND_API),
            api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED),
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n0987654321\n0955555555\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "已送達三竹 1 筆" in response.text
    assert "未扣點失敗 1 筆" in response.text
    assert "未確認 1 筆" in response.text
    # 總預約 3 則，只退還「未扣點失敗」那 1 筆 → 最終計費 2 則。
    assert app.rate_limiter.snapshot().used == 2
    assert sender.call_count == 3


def _counting_id_factory() -> "Callable[[], str]":
    """回傳一個每呼叫一次就遞增的 id 產生器，給需要驗證「id 各自獨立」的測試用。

    預設的 ``make_app`` 固定回傳字串 ``"req-test"``（既有測試依賴這個固定值），
    無法用來驗證「同一批次每一筆的 request_id 互不相同」，所以另外準備一個。
    """
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"id-{counter['n']}"

    return factory


def test_batch_send_writes_shared_batch_id_to_audit(tmp_path: Path) -> None:
    """同一批次的每一筆稽核紀錄要共用同一個 batch_id，讓事後能把它們串起來；
    但每一筆的 request_id 仍各自獨立（沿用既有單筆稽核慣例）。
    """
    audit_path = tmp_path / "send-audit.jsonl"
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            {"msgid": "0313887540", "statuscode": "1", "account_point": 12571},
        ]
    )
    app = make_app(
        tmp_path,
        sender,
        audit_log=AuditLog(audit_path, fsync=False),
        rate_limit=10,
        request_id_factory=_counting_id_factory(),
    )
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    app.route("POST", "/send", _form(token=token))

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    result_records = [r for r in records if r["event"] == "result"]
    assert len(result_records) == 2
    batch_ids = {r["batch_id"] for r in result_records}
    assert len(batch_ids) == 1  # 同一批只有一個 batch_id
    assert None not in batch_ids
    # 每筆 request_id 仍各自獨立（不是共用同一個），沿用既有單筆稽核慣例。
    request_ids = {r["request_id"] for r in result_records}
    assert len(request_ids) == 2


def test_batch_send_single_mode_audit_has_no_batch_id(tmp_path: Path) -> None:
    """單筆發送的稽核紀錄 batch_id 一律是 None——確保這次擴充不影響既有單筆行為。"""
    audit_path = tmp_path / "send-audit.jsonl"
    sender = RecordingSender()
    app = make_app(tmp_path, sender, audit_log=AuditLog(audit_path, fsync=False))

    send_once(app)

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    result_records = [r for r in records if r["event"] == "result"]
    assert len(result_records) == 1
    assert result_records[0]["batch_id"] is None


def test_batch_send_shows_audit_failure_warning_with_masked_phones_and_msgid(
    tmp_path: Path,
) -> None:
    """稽核寫入全部失敗時，批次結果頁要顯示醒目警示，列出正確的遮罩號碼與 msgid。

    這是 code-reviewer MUST_FIX 1 的回歸鎖：批次模式先前完全沒有沿用單人模式
    對 ``audit_ok`` 的追蹤（見 ``_succeed`` / ``_fail_unconfirmed``），磁碟滿或
    稽核檔路徑不可寫時，畫面會顯示一片乾淨的「已送達」，操作者完全不會被提醒
    這批可能沒留底。用 ``FailingAuditLog``（既有測試替身，`record()` 一律回
    ``False``）模擬全部寫入失敗。
    """
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            api_error(possibly_charged=False, kind=mitake.KIND_API),
        ]
    )
    app = make_app(
        tmp_path,
        sender,
        audit_log=FailingAuditLog(tmp_path / "send-audit.jsonl"),
        rate_limit=10,
    )
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "稽核紀錄寫入失敗" in response.text
    assert "請立刻手動記下" in response.text
    assert "0313887539" in response.text  # 已送達那筆的 msgid 要出現在警示框裡
    assert "0912***78" in response.text  # 已送達那筆的遮罩號碼（0912345678）
    assert "0987***21" in response.text  # 未扣點失敗那筆的遮罩號碼（0987654321）
    # 仍然照常分成三組顯示——audit 失敗不該連帶影響原本的成敗分類邏輯。
    assert "已送達三竹 1 筆" in response.text
    assert "未扣點失敗 1 筆" in response.text


def test_batch_send_audit_ok_writes_no_warning_box(tmp_path: Path) -> None:
    """稽核全部寫入成功時，**不該**出現稽核警示框——避免誤報嚇壞操作者。"""
    sender = SequencedSender(
        [{"msgid": "0313887539", "statuscode": "1", "account_point": 12572}]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "稽核紀錄寫入失敗" not in response.text


def test_batch_send_ip_blocked_and_auth_failed_offer_no_resend(tmp_path: Path) -> None:
    """設定類錯誤（IP 白名單／帳密錯）不提供重送——重送一百次也一樣失敗，
    不該誘人對著整份名單白按。"""
    sender = SequencedSender(
        [
            api_error(possibly_charged=False, kind=mitake.KIND_IP_BLOCKED),
            api_error(possibly_charged=False, kind=mitake.KIND_AUTH_FAILED),
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "未扣點失敗 2 筆" in response.text
    assert "重送不會成功" in response.text
    assert "<form" not in response.text  # 沒有任何一筆可重送，不該出現重送表單
    assert "0912***78" in response.text
    assert "0987***21" in response.text


def test_batch_send_not_charged_group_offers_resend_via_preview_only(
    tmp_path: Path,
) -> None:
    """「未扣點失敗」組要有重送入口，但重送一律導回 /preview（不是直接花錢的按鈕）。"""
    sender = SequencedSender([api_error(possibly_charged=False, kind=mitake.KIND_API)])
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "<form" in response.text
    assert 'action="/preview"' in response.text
    assert 'name="send-mode" value="batch"' in response.text
    assert "0912345678" in response.text  # 重送表單帶著要重送的那支號碼


def test_batch_send_unconfirmed_only_group_has_absolutely_no_form(tmp_path: Path) -> None:
    """整批全部落在「未確認」時，這頁**不得有任何 `<form>`**——最容易改壞的鐵律。"""
    sender = SequencedSender(
        [
            api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED),
            api_error(possibly_charged=True, kind=mitake.KIND_UNCONFIRMED),
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=10)
    token = _issue_batch_token(app, "0912345678\n0987654321\n")

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.OK
    assert "<form" not in response.text
    assert "請勿對這幾筆重送" in response.text


def test_batch_send_rate_limit_exceeded_at_send_time(tmp_path: Path) -> None:
    """token 發出後、真正送出前額度被佔走 → /send 回 429，且完全不呼叫 sender。"""
    sender = SequencedSender(
        [
            {"msgid": "0313887539", "statuscode": "1", "account_point": 12572},
            {"msgid": "0313887540", "statuscode": "1", "account_point": 12571},
        ]
    )
    app = make_app(tmp_path, sender, rate_limit=3)
    token = _issue_batch_token(app, "0912345678\n0987654321\n")  # 總成本 2 則

    # 模擬「token 發出後，額度被別的請求先佔走」。
    app.rate_limiter.reserve(2)

    response = app.route("POST", "/send", _form(token=token))

    assert response.status == HTTPStatus.TOO_MANY_REQUESTS
    assert sender.call_count == 0


# --------------------------------------------------------------------------- #
# 24. 端對端：真的走 HTTP 層的 multipart 解析（不是只餵 route(files=...)）
# --------------------------------------------------------------------------- #


def test_live_server_accepts_multipart_batch_upload_end_to_end(tmp_path: Path) -> None:
    """真的用 socket 送一個 multipart POST，驗證 Content-Type 分派＋
    `web.multipart` 解析真的接到 `SmsWebApp.route()`——上面所有批次測試都直接
    呼叫 `app.route(..., files=...)`，繞過了 HTTP 層那一段。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        boundary = "----LiveTestBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="send-mode"\r\n\r\nbatch\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="body"\r\n\r\n{SHORT_BODY}\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="recipients_file"; '
            'filename="list.txt"\r\nContent-Type: text/plain\r\n\r\n'
            "0912345678\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}/preview",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert "確認批次發送內容" in html


def test_multipart_body_over_limit_is_rejected_before_parsing(tmp_path: Path) -> None:
    """multipart body 超過 MAX_MULTIPART_BODY_BYTES → 413，不解析、不進 route()。

    刻意**不**走真的 socket（同類的既有 ``MAX_REQUEST_BODY_BYTES`` 也沒有走真的
    socket 測）：對一個 ~125KB 的請求本文，用真實 HTTP 用戶端送出時，伺服器在
    讀完 Content-Length 位元組**之前**就先回應 413 並依 HTTP/1.0 慣例關閉連線，
    用戶端仍在寫入本文的那個系統呼叫可能因此提早失敗（``BrokenPipeError`` /
    ``ConnectionResetError``），使測試結果隨作業系統的 TCP 緩衝區大小而不穩定
    ——這與本測試想驗證的行為（位元組上限本身有沒有生效）無關。改成直接呼叫
    handler 層的 :meth:`_read_request_body`，用一個假的 ``rfile`` 餵資料，同樣能
    驗證「超過上限就 413、且完全不呼叫 ``web.multipart.parse_multipart_form_data``」
    ，且不受這個 race 影響。
    """
    import io

    handler_class = make_handler(make_app(tmp_path, RecordingSender()))
    handler = handler_class.__new__(handler_class)  # 繞過 socket 相關的 __init__
    boundary = "----OversizeBoundary"
    oversized_content = b"0" * (MAX_MULTIPART_BODY_BYTES + 1024)
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="recipients_file"; '
        'filename="big.txt"\r\nContent-Type: text/plain\r\n\r\n'
    ).encode("utf-8") + oversized_content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    class _FakeHeaders:
        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def get(self, name: str) -> str | None:
            return self._values.get(name)

    handler.headers = _FakeHeaders(
        {
            "Content-Length": str(len(body)),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
    )
    handler.rfile = io.BytesIO(body)

    from web.server import _RequestError

    with pytest.raises(_RequestError) as exc_info:
        handler._read_request_body()

    assert exc_info.value.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_live_server_accepts_multipart_batch_at_max_recipients_boundary(
    tmp_path: Path,
) -> None:
    """名單筆數剛好等於 ``batch_recipients.MAX_BATCH_RECIPIENTS``（500 筆）時，
    真的走 HTTP multipart 請求也要放行——不會被 ``MAX_MULTIPART_BODY_BYTES``
    這個位元組上限卡住（500 支 10 碼號碼＋換行遠小於 ``MAX_MULTIPART_BODY_BYTES``
    的 125KB）。既有的
    ``test_batch_preview_exactly_at_max_batch_recipients_is_allowed`` 只走
    ``app.route(files=...)``，跳過了真實 ``Content-Length`` 比對那一層，
    這條補上真的 socket 版本。
    """
    app = make_app(tmp_path, RecordingSender(), rate_limit=10_000)
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        boundary = "----MaxRecipientsBoundary"
        phones_text = "\n".join(
            f"09{str(i).zfill(8)}" for i in range(batch_recipients.MAX_BATCH_RECIPIENTS)
        )
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="send-mode"\r\n\r\nbatch\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="body"\r\n\r\n{SHORT_BODY}\r\n'
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="recipients_file"; '
            'filename="list.txt"\r\nContent-Type: text/plain\r\n\r\n'
            f"{phones_text}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        assert len(body) < MAX_MULTIPART_BODY_BYTES, (
            "測試前提：這份 500 筆名單的 multipart body 本來就該遠小於上限，"
            "否則這條測試驗證的就不是「筆數上限」而是「位元組上限」了。"
        )
        request = urllib.request.Request(
            f"http://{host}:{port}/preview",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert "確認批次發送內容" in html
    assert f"{batch_recipients.MAX_BATCH_RECIPIENTS} 人" in html


# --------------------------------------------------------------------------- #
# 25. HTTP 層：application/x-www-form-urlencoded 這條路徑（個人模式的既有格式）
#     ——本檔對 multipart 已有端對端＋白箱兩層驗證，但 urlencoded 這條在本次
#     擴充之前完全沒有走過真正 HTTP 層 `_read_request_body` 的自動化測試：
#     既有 100 多個測試全部直接呼叫 `app.route()` 餵現成的 dict，繞過了
#     `Content-Type` 判斷／`MAX_REQUEST_BODY_BYTES` 檢查那一層。
# --------------------------------------------------------------------------- #


def test_live_server_accepts_urlencoded_post_end_to_end(tmp_path: Path) -> None:
    """真的用 socket 送一個 ``application/x-www-form-urlencoded`` 的 POST，
    驗證 ``_read_request_body`` 的 urlencoded 分支仍然正常運作（個人模式的
    既有格式，本次擴充只是在它旁邊多加一個 multipart 分支，不該改到它）。

    這是本專案「花錢端點」對 urlencoded 這條路徑**唯一**一條走真正 HTTP 層的
    測試——其餘所有既有測試都直接呼叫 ``app.route()`` 餵現成的 dict，完全繞過
    ``Content-Type`` 判斷本身。
    """
    sender = RecordingSender()
    app = make_app(tmp_path, sender)
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        from urllib.parse import urlencode

        body = urlencode({"phone": VALID_PHONE, "body": SHORT_BODY}).encode("ascii")
        request = urllib.request.Request(
            f"http://{host}:{port}/preview",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert "確認發送內容" in html
    assert VALID_PHONE in html


def test_urlencoded_body_over_max_request_body_bytes_is_rejected(tmp_path: Path) -> None:
    """``application/x-www-form-urlencoded`` 超過既有 ``MAX_REQUEST_BODY_BYTES``
    上限 → 413，不放寬（這是個人模式原本就有的上限，本次新增
    ``MAX_MULTIPART_BODY_BYTES`` 不該影響到它）。

    與 ``test_multipart_body_over_limit_is_rejected_before_parsing`` 同一種白箱
    手法（同一個理由：真的 socket 送超大 body 有 client 寫入與 server 提早關閉
    連線的 race，見該測試 docstring），直接呼叫 handler 層的
    :meth:`_read_request_body`，用假的 ``rfile`` 餵超過上限的資料。
    """
    import io

    from web.server import MAX_REQUEST_BODY_BYTES, _RequestError

    handler_class = make_handler(make_app(tmp_path, RecordingSender()))
    handler = handler_class.__new__(handler_class)  # 繞過 socket 相關的 __init__
    oversized_body = b"body=" + b"x" * (MAX_REQUEST_BODY_BYTES + 1024)

    class _FakeHeaders:
        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def get(self, name: str) -> str | None:
            return self._values.get(name)

    handler.headers = _FakeHeaders(
        {
            "Content-Length": str(len(oversized_body)),
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    handler.rfile = io.BytesIO(oversized_body)

    with pytest.raises(_RequestError) as exc_info:
        handler._read_request_body()

    assert exc_info.value.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_urlencoded_body_within_limit_but_over_multipart_limit_still_accepted(
    tmp_path: Path,
) -> None:
    """urlencoded 的位元組上限判斷用的是 ``MAX_REQUEST_BODY_BYTES``（較小），
    不是本次新增、給 multipart 用的 ``MAX_MULTIPART_BODY_BYTES``（較大）——
    兩個上限完全獨立，不可混用同一個常數判斷。這裡構造一個大小介於兩者之間
    的 urlencoded body（超過 ``MAX_REQUEST_BODY_BYTES`` 但小於
    ``MAX_MULTIPART_BODY_BYTES``），驗證它依然被 urlencoded 分支的既有上限擋下
    （不會誤用比較寬鬆的 multipart 上限放行）。
    """
    import io

    from web.server import MAX_REQUEST_BODY_BYTES, _RequestError

    assert MAX_REQUEST_BODY_BYTES < MAX_MULTIPART_BODY_BYTES, (
        "本測試的前提：兩個上限不同且 urlencoded 的比較小，"
        "如果哪天改成一樣大，這條測試需要重新設計。"
    )

    handler_class = make_handler(make_app(tmp_path, RecordingSender()))
    handler = handler_class.__new__(handler_class)
    mid_sized_body = b"body=" + b"x" * (
        MAX_REQUEST_BODY_BYTES + (MAX_MULTIPART_BODY_BYTES - MAX_REQUEST_BODY_BYTES) // 2
    )

    class _FakeHeaders:
        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def get(self, name: str) -> str | None:
            return self._values.get(name)

    handler.headers = _FakeHeaders(
        {
            "Content-Length": str(len(mid_sized_body)),
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    handler.rfile = io.BytesIO(mid_sized_body)

    with pytest.raises(_RequestError) as exc_info:
        handler._read_request_body()

    assert exc_info.value.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
