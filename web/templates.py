"""HTML 產生（純字串拼接，不引入樣板引擎 —— 沿用整個專案的零依賴原則）。

**本檔最重要的一條規則：任何進入 HTML 的使用者輸入都必須先過** :func:`html.escape`。
這裡是全專案唯一「拿使用者輸入直接組 HTML」的地方，漏一個就是 XSS。
使用者輸入只有兩個來源（手機號碼、簡訊內容），兩者都會原樣回填到表單與確認頁，
所以每個插值點都用 :func:`_e` 包起來 —— 用短別名是為了讓「哪裡沒包」一眼看得出來。

第二條規則：**失敗頁分兩種，且只有一種可以有重送按鈕**。依
``MitakeAPIError.possibly_charged``（見 HANDOFF §2.1）：

* ``False`` → :func:`render_failed_safe`，明確說「沒扣點，可安全重試」＋重送按鈕。
* ``True``  → :func:`render_failed_unconfirmed`，明確說「請勿重送」，
  **頁面上不存在任何送出表單**，並把 msgid 放大讓人拿去三竹後台查證。

把兩者都渲染成「發送失敗，請重試」是這個專案代價最高的單一錯誤：
使用者一按重送就是扣兩點、對方收到兩封。

排版刻意樸素：這是內部工具，可讀性與「按錯按鈕的機率」比美觀重要。
"""

from __future__ import annotations

from html import escape as _e
from urllib.parse import quote as _q

__all__ = [
    "REASON_CONFIG_MISSING",
    "REASON_MITAKE_REJECTED",
    "REASON_NEVER_REACHED_MITAKE",
    "REASON_VALIDATION_BLOCKED",
    "STATUS_PATH",
    "render_failed_safe",
    "render_failed_unconfirmed",
    "render_form",
    "render_notice",
    "render_preview",
    "render_sent",
    "render_status_form",
    "render_status_result",
    "status_query_url",
]

# 三竹後台網址只出現在「請勿重送、去查證」的畫面上，用具名常數避免各處抄錯。
_MITAKE_CONSOLE_URL = "https://smsapi.mitake.com.tw/"

# 投遞狀態查詢頁的路徑。樣板與路由層共用同一個常數，改路徑時不會只改到一邊
# （只改一邊的症狀是成功頁那個連結變成 404 —— 而那正是這個功能唯一的入口）。
STATUS_PATH = "/status"

# 「為什麼沒扣點」的四種說法。四種的**結論相同**（沒扣點、可安全重送），差別在
# 三竹到底有沒有收到這次請求 —— 這件事決定了使用者接下來該去哪裡查、該找誰修。
# 寫成具名常數而非散落在呼叫端，是為了讓「哪些情況三竹其實沒收到」在一個地方看得完。
REASON_MITAKE_REJECTED = "三竹已明確拒絕，這次沒有扣點，也沒有簡訊送出。"
REASON_VALIDATION_BLOCKED = (
    "內容在本機就被擋下，請求從未送到三竹，這次沒有扣點，也沒有簡訊送出。"
)
REASON_CONFIG_MISSING = (
    "三竹憑證沒有設定，請求連組都沒組出來、從未送到三竹，這次沒有扣點，也沒有簡訊送出。"
)
REASON_NEVER_REACHED_MITAKE = (
    "連線沒有建立起來，請求從未送到三竹，這次沒有扣點，也沒有簡訊送出。"
)

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.5rem 1rem; background: #f5f6f8; color: #1c1f23;
  font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif; line-height: 1.6; }
main { max-width: 40rem; margin: 0 auto; background: #fff; border-radius: .75rem;
  padding: 1.25rem 1.5rem 1.75rem; box-shadow: 0 1px 4px rgba(0,0,0,.12); }
h1 { font-size: 1.2rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 1.25rem 0 .5rem; }
p { margin: .5rem 0; }
label { display: block; font-weight: 700; margin: 1rem 0 .25rem; }
input[type=tel], textarea { width: 100%; padding: .6rem; font-size: 1rem; font-family: inherit;
  border: 1px solid #c6cbd1; border-radius: .4rem; background: #fff; color: inherit; }
textarea { min-height: 8.5rem; resize: vertical; }
button { margin-top: 1.1rem; padding: .65rem 1.4rem; font-size: 1rem; font-weight: 700;
  border: 0; border-radius: .4rem; background: #1a56b5; color: #fff; cursor: pointer; }
button.danger { background: #b3261e; }
button.secondary { background: #5a6068; }
.cost { margin-top: .4rem; font-size: .95rem; color: #4a5058; }
.cost.over { color: #b3261e; font-weight: 700; }
.box { margin: 1rem 0; padding: .8rem 1rem; border-left: 5px solid #8a9099;
  border-radius: .4rem; background: #eef0f3; }
.box.ok { border-color: #0a7f3f; background: #e8f6ee; }
.box.error { border-color: #b3261e; background: #fdecea; }
.box.danger { border-color: #b3261e; background: #fdecea; font-weight: 700; }
.box.warn { border-color: #b26a00; background: #fdf3e3; }
.box h2 { margin-top: 0; }
dl { margin: .75rem 0; display: grid; grid-template-columns: 8rem 1fr; gap: .35rem .75rem; }
dt { font-weight: 700; color: #4a5058; }
dd { margin: 0; word-break: break-all; }
.msgid { font-family: ui-monospace, Consolas, monospace; font-size: 1.1rem; font-weight: 700;
  background: #fff; padding: .15rem .4rem; border-radius: .25rem; }
.preview-body { white-space: pre-wrap; word-break: break-word; background: #f5f6f8;
  border: 1px solid #dde1e6; border-radius: .4rem; padding: .7rem; }
.muted { color: #5a6068; font-size: .9rem; }
.back { display: inline-block; margin-top: 1.25rem; color: #1a56b5; }
form.inline { display: inline; }
"""

# 即時試算則數的腳本。刻意寫成完全靜態（參數走 data-* 屬性），
# 這樣就不必把任何值插進 <script> 裡 —— 少一個 XSS 破口。
#
# 用 Array.from(value).length 而非 value.length：後者算的是 UTF-16 code unit，
# 中文沒差但 emoji 會多算一倍，畫面上顯示的扣點數就會跟 mitake.count_sms_segments
# （算 Python 的 code point）對不起來。畫面試算只是提示，真正的計費以伺服器端為準，
# 但兩邊對不上會讓人不敢相信確認頁上的數字。
_SEGMENT_SCRIPT = """
(function () {
  var input = document.getElementById("sms-body");
  var out = document.getElementById("sms-cost");
  if (!input || !out) { return; }
  var per = parseInt(input.dataset.perSegment, 10) || 70;
  var max = parseInt(input.dataset.maxSegments, 10) || 5;
  function update() {
    var chars = Array.from(input.value).length;
    var segments = chars === 0 ? 0 : Math.ceil(chars / per);
    out.textContent = chars + " 字 = " + segments + " 則 = 預估扣 " + segments + " 點"
      + (segments > max ? "（超過單次上限 " + max + " 則，送出會被擋下）" : "");
    out.className = segments > max ? "cost over" : "cost";
  }
  input.addEventListener("input", update);
  update();
})();
"""


def _page(title: str, body_html: str, *, script: str = "", script_nonce: str = "") -> str:
    """組出完整 HTML 文件。``title`` 一律跳脫；``body_html`` 由呼叫端負責已跳脫。

    ``noindex`` 是防呆：這個服務未來會掛上 Cloudflare Tunnel，萬一 Access 設定
    出包而被公開，至少不要被搜尋引擎收錄成「免費簡訊發送器」。

    有 ``script`` 就**必須**有 ``script_nonce``：CSP 的 ``script-src`` 已經從
    ``'unsafe-inline'`` 改成 ``'nonce-…'``（見 ``web.server._csp_header``），
    少了 nonce 的 ``<script>`` 會被瀏覽器擋掉、即時試算靜靜失效。這裡直接丟
    ``ValueError`` 而不是靜默降級 —— 靜默降級的症狀（「字數提示不會動」）沒人會
    聯想到 CSP，會被當成前端小 bug 放著。
    """
    if script and not script_nonce:
        raise ValueError("有 script 就必須提供 script_nonce（否則會被 CSP 擋下）")
    script_block = f'<script nonce="{_e(script_nonce)}">{script}</script>\n' if script else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-TW">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{_e(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        "<main>\n"
        f"{body_html}\n"
        "</main>\n"
        f"{script_block}"
        "</body>\n"
        "</html>\n"
    )


def _back_link(text: str = "← 回到發送表單") -> str:
    return f'<a class="back" href="/">{_e(text)}</a>\n'


def _box(kind: str, heading: str, body_html: str) -> str:
    """提示方塊。``heading`` 會被跳脫，``body_html`` 由呼叫端負責。"""
    return f'<div class="box {_e(kind)}"><h2>{_e(heading)}</h2>\n{body_html}</div>\n'


def _cost_line(segments: int, chars: int) -> str:
    return (
        f"<p><strong>{chars} 字 = {segments} 則 = 送出後扣 {segments} 點</strong>"
        "（點數與 App 團隊共用）</p>\n"
    )


def _hidden_resend_form(phone: str, body: str, label: str) -> str:
    """重送按鈕：導回 ``/preview`` 而不是直接 ``/send``。

    重送**一定要重走確認頁**，否則就變成「一個按鈕直接花錢」，
    整個二階段設計等於白做；同時也讓重送重新取得一次性 token。
    """
    return (
        '<form method="post" action="/preview">\n'
        f'<input type="hidden" name="phone" value="{_e(phone)}">\n'
        f'<input type="hidden" name="body" value="{_e(body)}">\n'
        f"<button type=\"submit\">{_e(label)}</button>\n"
        "</form>\n"
    )


def status_query_url(msgid: str) -> str:
    """組出「查這則 msgid 的投遞狀態」的網址。

    回傳值**尚未 HTML 跳脫**（只做了 URL-encode），放進 HTML 屬性前仍要過
    :func:`_e` —— 與本檔其他插值點同一條規則，不為它開特例。
    """
    return f"{STATUS_PATH}?msgid={_q(msgid, safe='')}"


def _format_status_time(raw: str | None) -> str:
    """把 ``20260729143730`` 排成 ``2026-07-29 14:37:30``。

    格式不符時**原樣回傳**，不猜、不補零：這個欄位是給人拿去跟三竹後台對帳的，
    自己重排過的時間對不上就是災難。
    """
    if raw is None:
        return "（三竹未回傳）"
    text = raw.strip()
    if not text:
        return "（三竹未回傳）"
    # isascii() 是必要的：全形數字的 isdigit() 也是 True，切出來會是一串怪東西。
    if len(text) == 14 and text.isascii() and text.isdigit():
        return (
            f"{text[0:4]}-{text[4:6]}-{text[6:8]} "
            f"{text[8:10]}:{text[10:12]}:{text[12:14]}（台灣時間）"
        )
    return text


# 分類 →（方塊樣式, 標題）。key 必須與 ``mitake.DELIVERY_*`` 的值完全一致。
#
# 這裡**刻意不 import mitake**：web/__init__.py 已說明本套件的匯入順序有其脆弱性
# （repo root 是由 web.server 補進 sys.path 的），樣板層為了幾個字串常數去建立那條
# 相依不划算。代價是兩邊各寫一份字串，所以另外用一支測試把兩邊釘在一起
# （tests/test_web.py::test_status_tone_table_covers_every_mitake_category），
# 任何一邊改了名字都會紅燈，不會靜默漂開。
_STATUS_TONE: dict[str, tuple[str, str]] = {
    "delivered": ("ok", "已送達手機"),
    "pending": ("warn", "還沒送到手機（處理中）"),
    "failed": ("error", "沒有送達（最終狀態）"),
    "error": ("warn", "三竹系統異常，這次查不到結果"),
    "account_error": ("error", "查詢沒有完成：帳號或連線位址設定有問題"),
    "unknown": ("warn", "三竹回了無法辨識的狀態碼"),
}

# 每一頁結果都要出現的對照說明。這個功能存在的理由就是「已送達業者 ≠ 已送達手機」，
# 所以這句話不做成可選項 —— 不論查出什麼狀態都印。
_STATUS_LEGEND = (
    '<p class="muted">三竹的「已送達業者」（狀態碼 1–3）只代表簡訊交給電信商了，'
    "<strong>不代表</strong>對方收到；"
    "只有「已送達手機」（狀態碼 4）才代表對方門號真的收到。</p>\n"
)


# --------------------------------------------------------------------------- #
# 各頁面
# --------------------------------------------------------------------------- #


def render_form(
    *,
    max_segments: int,
    chars_per_segment: int,
    script_nonce: str,
    phone: str = "",
    body: str = "",
    error: str | None = None,
    notice: str | None = None,
    rate_used: int | None = None,
    rate_limit: int | None = None,
) -> str:
    """表單頁（``GET /``，以及各種被擋下時的回填頁）。

    ``phone`` / ``body`` 是使用者上次填的內容 —— 被擋下時原樣回填，
    否則使用者得整段重打，而重打長訊息本身就是打錯字、送錯人的來源。

    ``script_nonce`` 沒有預設值，是刻意的：這是全站唯一帶 ``<script>`` 的頁面，
    而該 nonce 必須與同一個回應的 CSP 標頭一致（且每個回應都要換一組新的），
    所以只有產生回應的那一層能決定它，樣板不該自己編一個。
    """
    parts = ["<h1>三竹簡訊發送</h1>\n"]
    parts.append(
        '<p class="muted">每則扣 1 點，點數與 App 團隊共用（App 靠同一池發註冊驗證碼）。'
        "送出前會先出確認頁。</p>\n"
    )

    if error:
        parts.append(_box("error", "無法送出", f"<p>{_e(error)}</p>\n"))
    if notice:
        parts.append(_box("warn", "提醒", f"<p>{_e(notice)}</p>\n"))

    if rate_used is not None and rate_limit is not None:
        parts.append(
            f'<p class="muted">本小時已送出 {rate_used} / {rate_limit} 則。</p>\n'
        )

    parts.append('<form method="post" action="/preview" accept-charset="UTF-8">\n')
    parts.append('<label for="sms-phone">手機號碼</label>\n')
    parts.append(
        '<input type="tel" id="sms-phone" name="phone" autocomplete="off" '
        'inputmode="numeric" placeholder="09xxxxxxxx" '
        f'value="{_e(phone)}">\n'
    )
    parts.append('<label for="sms-body">簡訊內容</label>\n')
    parts.append(
        f'<textarea id="sms-body" name="body" data-per-segment="{int(chars_per_segment)}" '
        f'data-max-segments="{int(max_segments)}">{_e(body)}</textarea>\n'
    )
    parts.append('<p class="cost" id="sms-cost"></p>\n')
    parts.append(
        f'<p class="muted">中文每 {int(chars_per_segment)} 字算 1 則，'
        f"單次最多 {int(max_segments)} 則。</p>\n"
    )
    parts.append('<button type="submit">下一步：確認內容</button>\n')
    parts.append("</form>\n")
    # 查投遞狀態是唯讀、免費的操作，放在表單頁只是入口；真正有價值的入口在成功頁
    # （那裡帶得到剛發那則的 msgid，見 render_sent）。
    parts.append(
        f'<p class="muted"><a href="{STATUS_PATH}">查詢先前發送的投遞狀態'
        "（免費，不扣點）</a></p>\n"
    )

    return _page(
        "三竹簡訊發送", "".join(parts), script=_SEGMENT_SCRIPT, script_nonce=script_nonce
    )


def render_preview(
    *, phone: str, body: str, segments: int, chars: int, token: str
) -> str:
    """確認頁（``POST /preview`` 的回應）。按下這頁的按鈕才會真的花錢。

    表單裡**只帶 token**，不帶號碼與內容 —— 兩者留在伺服器端的 token 內容裡。
    若把它們也放進 hidden input，使用者（或中間人）就能在確認頁之後改掉收件人，
    確認頁上顯示的東西與實際送出的東西可以不一樣，這頁的意義就消失了。
    """
    parts = ["<h1>確認發送內容</h1>\n"]
    parts.append(
        _box(
            "warn",
            "按下送出就會扣點且無法取消",
            "<p>請確認號碼與內容無誤。三竹沒有「收回」功能。</p>\n",
        )
    )
    parts.append("<dl>\n")
    parts.append(f"<dt>收件號碼</dt><dd>{_e(phone)}</dd>\n")
    parts.append(f"<dt>字數／則數</dt><dd>{chars} 字 ／ {segments} 則</dd>\n")
    parts.append(f"<dt>將扣點數</dt><dd><strong>{segments} 點</strong></dd>\n")
    parts.append("</dl>\n")
    parts.append("<h2>簡訊內容</h2>\n")
    parts.append(f'<div class="preview-body">{_e(body)}</div>\n')

    parts.append('<form method="post" action="/send" accept-charset="UTF-8">\n')
    parts.append(f'<input type="hidden" name="token" value="{_e(token)}">\n')
    parts.append(
        f'<button type="submit" class="danger">確定送出（扣 {segments} 點）</button>\n'
    )
    parts.append("</form>\n")
    parts.append(_back_link("← 改一下再送"))

    return _page("確認發送內容", "".join(parts))


def render_sent(
    *,
    phone: str,
    segments: int,
    chars: int,
    msgid: str | None,
    account_point: int | None,
    audit_ok: bool = True,
) -> str:
    """成功頁（``POST /send`` 成功）。

    刻意**不放任何送出按鈕**：使用者在成功頁上唯一該做的事是離開。
    多一個「再送一次」的捷徑，就多一個手滑扣點的機會。

    但**有 msgid 時會多一個「查詢投遞狀態」的連結**（有 msgid 才給，沒有就不給一個
    點下去必定查無結果的死連結）。這是整個投遞查詢功能最有價值的入口：
    「三竹已接收」只代表三竹收下了，離「對方手機收到」還有一段電信商投遞。
    在此之前，使用者在這一頁就沒有下文了，手機沒響時只能猜。
    連結是 ``<a>`` 而不是按鈕或表單 —— 這一頁的規矩是「不放任何會送出東西的元件」，
    而查詢是唯讀、免費的 GET，不破壞那條規矩。
    """
    parts = ["<h1>已送出</h1>\n"]
    parts.append(
        _box("ok", "三竹已接收", f"<p>{_e(phone)}，{chars} 字／{segments} 則，扣 {segments} 點。</p>\n")
    )
    parts.append("<dl>\n")
    parts.append(
        f'<dt>msgid</dt><dd><span class="msgid">{_e(msgid) if msgid else "（三竹未回傳）"}</span></dd>\n'
    )
    if account_point is not None:
        parts.append(f"<dt>剩餘點數</dt><dd>{account_point}</dd>\n")
    parts.append("</dl>\n")
    if msgid:
        parts.append(
            _box(
                "warn",
                "「三竹已接收」不等於「對方收到」",
                "<p>上面代表三竹收下了這則簡訊，接下來還要經過電信商投遞才會到對方手機，"
                "通常數秒到數分鐘。</p>\n"
                f'<p><a href="{_e(status_query_url(msgid))}">'
                "查詢這則的投遞狀態（免費、不扣點，可以重複查）</a></p>\n",
            )
        )
    if audit_ok:
        parts.append(
            '<p class="muted">msgid 是日後查證這封簡訊的唯一依據，建議一併記下。'
            "本次發送已寫入稽核紀錄。</p>\n"
        )
    else:
        # 稽核是事後唯一能回答「這一點是誰燒的」的東西。寫不進去時必須講出來，
        # 否則對帳的人會相信檔案裡沒有＝沒發生過。
        parts.append(
            _box(
                "warn",
                "簡訊已送出，但稽核紀錄寫入失敗",
                "<p><strong>請立刻手動記下上方 msgid</strong>——這次發送沒有留底，"
                "日後對帳在稽核檔中查不到這一筆。</p>\n"
                "<p>原因請看 <code>journalctl -u mitake-web</code>（多半是磁碟滿或"
                "稽核檔路徑不可寫）。</p>\n",
            )
        )
    parts.append(_back_link("← 再發一則（會重新確認）"))

    return _page("已送出", "".join(parts))


def render_failed_safe(
    *,
    phone: str,
    body: str,
    message: str,
    heading: str = "發送失敗，未扣點",
    hint: str | None = None,
    allow_resend: bool = True,
    reason: str | None = None,
) -> str:
    """失敗頁 A：``possibly_charged=False``，也就是**確定沒扣點**。

    這種情況（號碼格式錯、三竹明確拒絕、憑證沒設）重送是安全且正確的，
    所以提供重送按鈕；不提供的話使用者得整段重打，反而更容易出錯。

    ``allow_resend=False`` 留給「重送也不會成功」的設定類錯誤（IP 不在白名單、
    帳密錯）：那些要先去改設定，給重送按鈕只是誘人白按。

    ``reason`` 是「為什麼沒扣點」那一句。預設值假設三竹**收到了**請求並回絕，
    但本頁另外三個呼叫端（輸入驗證、憑證未設、連線層失敗）三竹根本沒收到請求——
    對那些情況寫「三竹已明確拒絕」是假的，日後有人照這句話去三竹後台找紀錄會撲空。
    結論（沒扣點、可安全重送）不變，變的只有理由。
    """
    parts = ["<h1>發送失敗</h1>\n"]
    reason_text = reason if reason is not None else REASON_MITAKE_REJECTED
    inner = f"<p>{_e(message)}</p>\n<p><strong>{_e(reason_text)}</strong></p>\n"
    if hint:
        inner += f"<p>{_e(hint)}</p>\n"
    parts.append(_box("error", heading, inner))

    if allow_resend:
        parts.append(_hidden_resend_form(phone, body, "修正後重新確認"))
    parts.append(_back_link())

    return _page("發送失敗", "".join(parts))


def render_failed_unconfirmed(
    *,
    phone: str,
    segments: int,
    msgid: str | None,
    message: str,
    audit_ok: bool = True,
    request_id: str | None = None,
) -> str:
    """失敗頁 B：``possibly_charged=True``，也就是**多半已經扣了點**。

    這頁**絕對不可以有任何送出或重送按鈕**（連 ``<form>`` 都不放）。
    請求已經送到三竹、只是結果讀不回來；重送等於扣第二次點、對方收到兩封。
    正確處置是拿 msgid 去三竹後台查證。

    ``audit_ok=False``（稽核寫入失敗）在**這一頁**比在成功頁更嚴重：成功頁至少
    還有 msgid 這個可查的憑據，而走到這裡代表「多半已扣點、且結果不明」——
    此時唯一的處置就是拿線索去三竹後台查證，稽核若也沒寫進去，使用者連要查什麼
    都不知道。所以這裡不只是「不宣稱已留底」，而是要主動叫他當場手抄。
    """
    parts = ["<h1>狀態未確認</h1>\n"]
    inner = (
        "<p><strong>請勿重送。</strong>請求已經送到三竹，只是回應無法確認，"
        f"這 {segments} 點很可能已經扣掉、簡訊也可能已經送達 {_e(phone)}。</p>\n"
        f"<p>{_e(message)}</p>\n"
    )
    parts.append(_box("danger", "可能已扣點，請勿重送", inner))

    parts.append("<h2>接下來要做的事</h2>\n")
    parts.append("<dl>\n")
    parts.append(
        f'<dt>msgid</dt><dd><span class="msgid">{_e(msgid) if msgid else "（三竹未回傳 msgid）"}</span></dd>\n'
    )
    parts.append("</dl>\n")
    parts.append(
        f'<p>請到 <a href="{_MITAKE_CONSOLE_URL}" rel="noreferrer noopener" target="_blank">三竹後台</a>'
        "以上述 msgid 查詢這筆的實際狀態。確認真的沒送出，再回表單重發。</p>\n"
    )
    if not audit_ok:
        # 這是全站最壞的組合：可能已扣點 + 沒有留底。稽核檔是事後唯一能回答
        # 「這一點是誰燒的、到底送出去沒有」的東西；它沒寫成功時，唯一還來得及
        # 保住線索的時機就是使用者現在還盯著這一頁的這幾秒。
        manual = (
            "<p><strong>請立刻手動記下下列資訊</strong>——這次<strong>可能已經扣點</strong>，"
            "而且<strong>沒有留底</strong>：稽核檔裡查不到這一筆，日後對帳只剩你手抄的這份。</p>\n"
        )
        if request_id:
            manual += f'<p>request_id：<span class="msgid">{_e(request_id)}</span></p>\n'
        manual += (
            "<p>另外請記下：<strong>現在的日期與時間（到分）</strong>、上方的 msgid、"
            f"收件號碼 {_e(phone)}、以及這則簡訊的內容。</p>\n"
            "<p>原因請看 <code>journalctl -u mitake-web</code>"
            "（多半是磁碟滿或稽核檔路徑不可寫）。</p>\n"
        )
        parts.append(_box("danger", "而且這次沒有留底：稽核紀錄寫入失敗", manual))

    if not msgid:
        tail = (
            "本次發送已寫入稽核紀錄。"
            if audit_ok
            else "而且本次發送沒有留底（見上方警告），請務必手抄時間與號碼。"
        )
        parts.append(
            '<p class="muted">這次三竹連 msgid 都沒回傳，請改以「發送時間 + 收件號碼」'
            f"在後台比對；{tail}</p>\n"
        )
    parts.append(_back_link("← 回表單（確認查證後再發）"))

    return _page("狀態未確認", "".join(parts))


def render_notice(
    *,
    title: str,
    heading: str,
    message: str,
    kind: str = "warn",
    hint: str | None = None,
    back_text: str = "← 回到發送表單",
    resend_phone: str | None = None,
    resend_body: str | None = None,
    resend_label: str = "帶著剛才的內容回確認頁",
) -> str:
    """通用提示頁：token 失效／過期、速率上限、404、405 等。

    這些情況共通的特性是「什麼都沒發生、也沒扣點」，所以預設只給一個回表單的連結，
    不給任何會觸發發送的按鈕 —— 特別是「token 已作廢」那條：
    使用者按上一頁再送一次時會走到這裡，這時**絕不能**幫他重發。

    ``resend_phone`` / ``resend_body`` 兩者**都**給時才會多出一個回填按鈕。它導向
    ``/preview``（不是 ``/send``），所以按下去只會重走確認頁、不會直接花錢；用途是
    讓撞上速率上限的人不必把整段訊息重打一次 —— 重打長訊息本身就是打錯字、送錯人
    的來源。給 token 失效／404 那類頁面時**不要**傳，那些情況沒有「剛才的內容」可談。
    """
    body = f"<p>{_e(message)}</p>\n"
    if hint:
        body += f'<p class="muted">{_e(hint)}</p>\n'
    parts = [f"<h1>{_e(title)}</h1>\n", _box(kind, heading, body)]
    if resend_phone is not None and resend_body is not None:
        parts.append(_hidden_resend_form(resend_phone, resend_body, resend_label))
    parts.append(_back_link(back_text))
    return _page(title, "".join(parts))


# --------------------------------------------------------------------------- #
# 投遞狀態查詢（唯讀、免費、不扣點）
# --------------------------------------------------------------------------- #


def render_status_form(
    *, msgid: str = "", error: str | None = None, notice: str | None = None
) -> str:
    """查詢表單頁（``GET /status``，沒帶 msgid 時）。

    表單用 ``method="get"``：查詢是唯讀且冪等的，用 GET 才能讓結果頁的網址可以
    收藏、可以重新整理、可以貼給別人 —— 而重新整理一個 POST 結果頁會跳出
    「要重新送出表單嗎」，在這個專案裡那個對話框本身就是誤導（使用者已經被訓練成
    「重送＝可能多扣點」）。查詢不花錢，不需要二階段確認。

    這一頁**沒有任何 ``<script>``**，所以不收 nonce（見 ``web.server._csp_header``：
    沒有腳本的頁面直接給 ``script-src 'none'``，比帶 nonce 更嚴）。
    """
    parts = ["<h1>查詢簡訊投遞狀態</h1>\n"]
    parts.append(
        '<p class="muted">用發送成功時拿到的 msgid 查這則簡訊到底有沒有到對方手機。'
        "查詢是唯讀操作，免費、不扣點，可以重複查。</p>\n"
    )

    if error:
        parts.append(_box("error", "查詢失敗", f"<p>{_e(error)}</p>\n"))
    if notice:
        parts.append(_box("warn", "提醒", f"<p>{_e(notice)}</p>\n"))

    parts.append(
        f'<form method="get" action="{STATUS_PATH}" accept-charset="UTF-8">\n'
    )
    parts.append('<label for="status-msgid">msgid</label>\n')
    parts.append(
        '<input type="tel" id="status-msgid" name="msgid" autocomplete="off" '
        f'inputmode="numeric" placeholder="0315772761" value="{_e(msgid)}">\n'
    )
    parts.append('<button type="submit">查詢投遞狀態</button>\n')
    parts.append("</form>\n")
    parts.append(_STATUS_LEGEND)
    parts.append(_back_link())

    return _page("查詢簡訊投遞狀態", "".join(parts))


def render_status_result(
    *,
    msgid: str,
    statuscode: str,
    description: str,
    category: str,
    status_time: str | None = None,
    is_delivered: bool = False,
    is_final: bool = False,
) -> str:
    """查詢結果頁。

    **這一頁最重要的一件事：不可以讓「已送達業者」看起來像「已送達手機」。**
    狀態碼 1–3 的中文說明就是「已送達業者」，單獨顯示這五個字，多數人會讀成
    「送到了」。所以處理中的狀態一律附上「這不是最終狀態、對方不一定收到、稍後
    可以再查」的白話說明，且整頁只有 ``is_delivered``（狀態碼 4）才用綠色的
    成功樣式。

    參數直接取自 :func:`mitake.parse_status_response` 的回傳欄位。
    ``category`` 是字串（``mitake.DELIVERY_*`` 的值），對應表見 :data:`_STATUS_TONE`。
    """
    kind, heading = _STATUS_TONE.get(category, _STATUS_TONE["unknown"])

    if is_delivered:
        detail = (
            "<p>三竹回報這則簡訊<strong>已經送達對方手機</strong>（狀態碼 4）。"
            "這是最終狀態，不會再變。</p>\n"
        )
    elif category == "pending":
        detail = (
            f"<p>三竹目前回報「{_e(description)}」，也就是簡訊已經交給電信商，"
            "<strong>但還沒有確認對方手機收到</strong>。</p>\n"
            "<p><strong>這不是最終狀態。</strong>投遞通常在數秒到數分鐘內完成，"
            "稍後回到這一頁重新查詢即可（查詢免費、不扣點）。</p>\n"
        )
    elif category == "failed":
        detail = (
            f"<p>三竹回報這則簡訊<strong>沒有送達</strong>：{_e(description)}。</p>\n"
            "<p>這是最終狀態，再查也不會改變。已經扣掉的點數不會退回。"
            "確認號碼與內容後，可以回表單重新發送（會再扣一次點）。</p>\n"
        )
    elif category == "error":
        detail = (
            f"<p>三竹回報系統層異常：{_e(description)}。</p>\n"
            "<p>這不是這則簡訊的投遞結果，而是三竹那端暫時的狀況。"
            "請稍後再查一次；若持續如此，請聯絡三竹。</p>\n"
        )
    elif category == "account_error":
        detail = (
            f"<p>三竹沒有去查這則簡訊，而是回報設定問題：{_e(description)}。</p>\n"
            "<p>請先修正帳號設定或 IP 白名單，再回來查詢。</p>\n"
        )
    else:
        detail = (
            f"<p>三竹回了狀態碼「{_e(statuscode)}」，本工具尚未收錄它的意義。</p>\n"
            "<p>在查明之前，<strong>請不要假設對方已經收到</strong>。"
            "可以拿下方的 msgid 到三竹後台核對。</p>\n"
        )

    parts = ["<h1>投遞狀態</h1>\n", _box(kind, heading, detail)]

    parts.append("<dl>\n")
    parts.append(f'<dt>msgid</dt><dd><span class="msgid">{_e(msgid)}</span></dd>\n')
    parts.append(f"<dt>狀態碼</dt><dd>{_e(statuscode)}</dd>\n")
    parts.append(f"<dt>狀態說明</dt><dd>{_e(description)}</dd>\n")
    parts.append(f"<dt>狀態時間</dt><dd>{_e(_format_status_time(status_time))}</dd>\n")
    parts.append(
        "<dt>是否最終狀態</dt><dd>{}</dd>\n".format(
            "是（不會再變）" if is_final else "否（稍後再查可能會變）"
        )
    )
    parts.append("</dl>\n")

    if not is_final:
        # 重查連結而不是自動刷新：自動刷新會在無人看著的分頁裡一直打三竹的 API。
        parts.append(
            f'<p><a href="{_e(status_query_url(msgid))}">重新查詢這則（免費、不扣點）</a></p>\n'
        )

    parts.append(_STATUS_LEGEND)
    parts.append(
        f'<p class="muted"><a href="{STATUS_PATH}">查詢另一則 msgid</a></p>\n'
    )
    parts.append(_back_link())

    return _page("投遞狀態", "".join(parts))
