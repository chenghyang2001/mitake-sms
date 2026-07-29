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
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
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

from web import templates  # noqa: E402
from web.audit import AuditLog  # noqa: E402
from web.server import (  # noqa: E402
    ACCESS_EMAIL_HEADER,
    SmsWebApp,
    TokenStore,
    _csp_header,  # 私有但刻意直測：CSP 字串是安全邊界，不該只靠端對端那一條驗
    create_server,
    make_handler,
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
) -> SmsWebApp:
    """建一個完全離線的 app：假 sender、假 status_query、假時鐘、稽核檔落在 tmp_path。"""
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
        request_id_factory=lambda: "req-test",
        require_access_email=require_access_email,
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
