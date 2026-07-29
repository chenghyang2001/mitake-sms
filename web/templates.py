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

__all__ = [
    "REASON_CONFIG_MISSING",
    "REASON_MITAKE_REJECTED",
    "REASON_NEVER_REACHED_MITAKE",
    "REASON_VALIDATION_BLOCKED",
    "render_failed_safe",
    "render_failed_unconfirmed",
    "render_form",
    "render_notice",
    "render_preview",
    "render_sent",
]

# 三竹後台網址只出現在「請勿重送、去查證」的畫面上，用具名常數避免各處抄錯。
_MITAKE_CONSOLE_URL = "https://smsapi.mitake.com.tw/"

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
