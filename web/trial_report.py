"""體驗報告寄送（``/trial-email`` 頁「寄送體驗報告」按鈕背後的邏輯）。

這是本專案第二個會花錢／有外部副作用的入口（第一個是 :mod:`mitake` 的簡訊發送），
但成本模型完全不同：這裡走 Gmail 寄信，**不經過** :mod:`mitake` 的三竹通道，
不動用與 App 團隊共用的簡訊點數池。真正的風險是另外兩件事：

1. **寄錯資料給客戶**：報告內容抓的是 ``acfh_api``（AIHCR 場域資料庫）的即時感測
   資料，寄送前若沒重新驗證資格，可能把還在體驗中、資料不完整的報告寄出去，
   或是對多裝置客戶亂猜要寄哪一台。
2. **靜默失敗掉一封該寄的信**：客戶等的是這封信，若失敗卻不留痕跡，
   下次追查只能憑印象。

核心邏輯改寫自 ``aihcr-daily`` repo 的
``scripts/acfh_daily_report_14_days_experiencing.py``（唯讀參考，本檔未修改
該檔案），把單一用戶固定 ``hours=24`` 的每日報告，改寫成本專案「使用者主動點擊、
一次寄一份、時間窗固定 14 天」的形態。與原檔的差異：

* 時間窗改用 :data:`TRIAL_REPORT_HOURS`（``24 * 14``）取代原本的 ``hours``
  參數，且**只算一次常數**，不在呼叫處重複寫死 336 這個魔術數字。
* DB／Gmail 連線與寄送函式都改成可注入（``conn_factory`` / ``email_sender``），
  測試才能完全不碰真實 DB、不真的寄信 —— 這點原腳本沒有（它本來就是一次性排程
  腳本，靠人工 ``--dry-run`` 驗證；本檔是使用者按下去就會觸發外部副作用的
  Web 路徑，必須能被單元測試完整覆蓋）。
* 稽核落地改成獨立的 ``trial-report-audit.jsonl``（見
  :func:`default_trial_audit_path`），與 :mod:`web.audit` 的 ``send-audit.jsonl``
  分開——那是三竹簡訊的稽核，這是 Email 的稽核，兩個子系統的成本模型不同，
  混在同一份檔案只會讓事後對帳更難過濾。
* 已用天數的重新驗證改呼叫 :func:`web.templates._compute_used_days`（今天－接機日
  動態算，``today`` 可注入測試固定日期），不再信任 producer 快照的 ``used_days``
  字串——那份快照只在 producer 重新跑一次時才會更新，兩次同步之間會凍結在舊數字
  （2026-08-03 修正）。總天數仍呼叫 :func:`web.templates._parse_trial_day_count`
  讀 producer 快照的 ``days`` 欄（那是活動設定值，不是日期能推出來的）。兩邊對
  「14」「14 天」這種字串的解讀若不同步，會出現「畫面上按鈕可點，但伺服器端又
  擋下」這種使用者無法理解的落差。``web.templates`` 目前**不會**在執行期匯入
  ``web.recipients`` 或 ``web.trial_report``（只有 ``TYPE_CHECKING`` 用途），
  所以這裡反向匯入它不會造成循環匯入。

**pymysql／matplotlib 延遲匯入**：本模組是本專案唯一破例引入這兩個第三方套件
的地方（``requirements.txt`` 已記錄取捨）。但這兩個套件只在「真的要連 DB／
真的要畫圖」的函式內才 ``import``，而不是在模組頂層——這樣即使某台機器沒裝
這兩個套件，``import web.trial_report`` 本身、乃至 :mod:`web.server` 整個
零依賴的簡訊發送介面，都還是能正常載入與運作；壞掉的只會是「寄送體驗報告」
這個獨立功能本身。
"""

from __future__ import annotations

import logging
import os
import smtplib
import tempfile
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.errors import MessageError
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from web.recipients import Recipient
from web.templates import _compute_used_days, _parse_trial_day_count
from web.audit import AuditLog

__all__ = [
    "DB_HOST",
    "DB_NAME",
    "DB_PORT",
    "DB_USER",
    "DEFAULT_TRIAL_AUDIT_FILENAME",
    "ENV_GMAIL_APP_PASSWORD",
    "ENV_GMAIL_USER",
    "ENV_MYSQL_RD2_PASSWORD",
    "ENV_TRIAL_AUDIT_PATH",
    "REASON_DAYS_NOT_REACHED",
    "REASON_DB_ERROR",
    "REASON_INVALID_RECIPIENT_ID",
    "REASON_MULTIPLE_DEVICES",
    "REASON_NO_DEVICE",
    "REASON_NO_EMAIL",
    "REASON_NO_TELEMETRY",
    "REASON_SEND_ERROR",
    "TRIAL_REPORT_HOURS",
    "TrialReportConfigError",
    "TrialReportResult",
    "analyze",
    "build_pdf",
    "default_trial_audit_path",
    "fetch_telemetry",
    "fetch_user_devices",
    "fetch_user_info",
    "get_db_password",
    "get_gmail_credentials",
    "mask_email",
    "open_db_connection",
    "send_trial_report",
    "setup_cjk_font",
]

logger = logging.getLogger("mitake.web.trial_report")

# --------------------------------------------------------------------------- #
# 常數
# --------------------------------------------------------------------------- #

# 14 天 = 336 小時。定義成具名常數而非在呼叫處寫死 336，是因為「14 天體驗」
# 是這個功能存在的理由本身，改動它應該只需要改一個地方。
TRIAL_REPORT_HOURS = 24 * 14

DB_HOST = "acfh-db.cx1c1xdf6koi.ap-northeast-1.rds.amazonaws.com"
DB_PORT = 3306
DB_USER = "rd2"
DB_NAME = "acfh_api"

# DB 存 UTC naive timestamp；台灣時間固定 UTC+8（不依賴系統時區，VPS 是 UTC）。
TAIWAN_OFFSET = timedelta(hours=8)
_DB_TIME_FMT = "%Y-%m-%d %H:%M:%S"

ENV_MYSQL_RD2_PASSWORD = "MYSQL_RD2_PASSWORD"
ENV_GMAIL_USER = "GMAIL_USER"
ENV_GMAIL_APP_PASSWORD = "GMAIL_APP_PASSWORD"

# 稽核檔位置可用環境變數覆寫，預設落在 repo 的 logs/ 底下（與 web.audit 的
# send-audit.jsonl 同一個資料夾，但檔名不同、事件語意也不同，見模組 docstring）。
ENV_TRIAL_AUDIT_PATH = "MITAKE_WEB_TRIAL_AUDIT_PATH"
DEFAULT_TRIAL_AUDIT_FILENAME = "trial-report-audit.jsonl"

# 失敗原因代碼（機器可讀）。伺服器路由層（web.server.handle_send_trial_report）
# 依這個代碼決定要回 400（客戶端/資料面問題，重選對象或等體驗到期即可解決）
# 還是回 500（DB／寄信這類上游問題，不是操作者能自己修的）。
REASON_DAYS_NOT_REACHED = "days_not_reached"
REASON_INVALID_RECIPIENT_ID = "invalid_recipient_id"
REASON_NO_DEVICE = "no_device"
REASON_MULTIPLE_DEVICES = "multiple_devices"
REASON_NO_EMAIL = "no_email"
REASON_NO_TELEMETRY = "no_telemetry"
REASON_DB_ERROR = "db_error"
REASON_SEND_ERROR = "send_error"

# CJK 字型候選（依序偵測；VPS 沒裝中文字型時圖表退回英文標籤，PDF 直接跳過）。
CJK_FONT_CANDIDATES = [
    "Microsoft JhengHei",
    "Noto Sans CJK TC",
    "Noto Sans CJK SC",
    "WenQuanYi Zen Hei",
]

LABELS_ZH = {
    "pm25": "PM2.5 (μg/m³)",
    "co2": "CO2 (ppm)",
    "voc": "VOC 等級 (0-2)",
    "temp": "溫度 (°C)",
    "humi": "濕度 (%)",
    "xlabel": "台灣時間",
    "title": "{name}家 清淨機空氣品質報告（{start} ~ {end}）",
}
LABELS_EN = {
    "pm25": "PM2.5 (ug/m3)",
    "co2": "CO2 (ppm)",
    "voc": "VOC Level (0-2)",
    "temp": "Temperature (degC)",
    "humi": "Humidity (%)",
    "xlabel": "Taiwan Time (UTC+8)",
    "title": "addwii Air Quality Report {mac} ({start} ~ {end})",
}

# email 遮罩後保留的 local-part 頭幾碼。同 web.audit.mask_phone 的精神
# （稽核檔的用途是事後對帳，不是通訊錄），但方向相反：手機號碼留尾碼、
# email 留頭幾碼 + 完整網域 —— 網域不是個資（同網域的人共用），留著才看得出
# 「這是公司信箱還是私人信箱」，local-part 頭幾碼足以讓人事後認出「是哪一位」。
_EMAIL_VISIBLE_HEAD_CHARS = 2

# 遮罩用的星號**固定 3 顆，不隨帳號長度變動**——比照 web.audit.mask_phone
# 「固定留末 4 碼」那種固定格式的遮蔽風格（那邊固定的是「留幾碼」，這裡固定的是
# 「遮幾碼」，但精神一致：遮罩格式本身不該洩漏長度資訊）。若改成「星號數 = local
# 長度 - 頭幾碼」，遮罩後的長度會直接洩漏原始帳號多長（例如 "ab" 用 2 顆星、
# "chenxiaoqi" 用 8 顆星），稽核紀錄裡的遮罩值等於間接洩漏了一項可用來輔助
# 猜測／比對真實 email 的資訊，而這本應是被遮掉的東西。
_EMAIL_MASK_STARS = "***"


class TrialReportConfigError(RuntimeError):
    """環境變數缺漏（DB 密碼／Gmail 帳密未設定）。fail-fast，不用預設值靜默掩蓋。"""


@dataclass(frozen=True)
class TrialReportResult:
    """:func:`send_trial_report` 的回傳結果。

    ``reason`` 是給呼叫端（伺服器路由層）做程式化分流用的機器可讀代碼
    （``REASON_*`` 常數之一），成功時為 ``None``。``message`` 是給人看的中文說明，
    不含資料庫連線字串等內部細節。
    """

    success: bool
    recipient_id: str
    reason: str | None
    message: str
    email_masked: str | None = None
    pdf_attached: bool = False


# --------------------------------------------------------------------------- #
# 環境變數 / 憑證
# --------------------------------------------------------------------------- #


def get_db_password() -> str:
    """讀 acfh_api 唯讀帳號（``rd2``）密碼；缺少時 fail-fast。

    刻意不给預設值：預設值只會把「密碼沒設」這個錯誤延後到真的打 DB 才爆出來，
    那時錯誤訊息（連線逾時、認證失敗）比「缺環境變數」難懂得多。
    """
    password = os.environ.get(ENV_MYSQL_RD2_PASSWORD)
    if not password:
        raise TrialReportConfigError(f"缺少環境變數 {ENV_MYSQL_RD2_PASSWORD}")
    return password


def get_gmail_credentials() -> tuple[str, str]:
    """讀 Gmail 帳號與應用程式密碼；任一缺少都 fail-fast（理由同 :func:`get_db_password`）。"""
    user = os.environ.get(ENV_GMAIL_USER)
    app_password = os.environ.get(ENV_GMAIL_APP_PASSWORD)
    if not user or not app_password:
        raise TrialReportConfigError(
            f"缺少環境變數 {ENV_GMAIL_USER} 或 {ENV_GMAIL_APP_PASSWORD}"
        )
    return user, app_password


# --------------------------------------------------------------------------- #
# 資料庫（唯讀查詢；pymysql 延遲 import，見模組 docstring）
# --------------------------------------------------------------------------- #


def open_db_connection(password: str) -> Any:
    """開一條 acfh_api 唯讀連線（帳號 ``rd2``，與 aihcr-daily 用同一組帳密但各自
    讀各自的環境變數 —— 兩個 repo 互不依賴，見 doc/spec-trial-report.md）。
    """
    import pymysql  # 延遲 import，見模組 docstring

    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=password,
        database=DB_NAME,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=60,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _default_conn_factory() -> Any:
    """真正的預設 ``conn_factory``：讀密碼、開真實連線。"""
    return open_db_connection(get_db_password())


def _close_connection(conn: Any) -> None:
    """安全關閉 DB 連線。關閉失敗不影響已經算出來的結果——連線已經用完，
    關閉失敗只代表資源沒釋放乾淨，不該讓整次寄送因此被判定失敗。
    """
    try:
        conn.close()
    except Exception as exc:  # noqa: BLE001 -- 各種 driver 的關閉例外型別不一，這裡只求盡力而為
        logger.warning("體驗報告：關閉 DB 連線時發生錯誤（已忽略）：%s", exc)


def fetch_user_info(conn: Any, user_id: int) -> dict[str, Any] | None:
    """查單一用戶；回傳 ``{"name": ..., "email": ...}``，查無此人回傳 ``None``。

    ``name`` = 姓 + 名（NULL 以空字串處理），兩者皆空時退回 ``"user{id}"`` 佔位名。
    """
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT first_name, last_name, email FROM users WHERE id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    name = f"{row.get('last_name') or ''}{row.get('first_name') or ''}"
    if not name:
        name = f"user{user_id}"
    return {"name": name, "email": row.get("email")}


def fetch_user_devices(conn: Any, user_id: int) -> list[dict[str, Any]]:
    """查單一用戶的 active 裝置清單。"""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id AS device_id, mac_address, alias "
            "FROM devices WHERE user_id = %s AND status = 'active' "
            "ORDER BY id",
            (user_id,),
        )
        return list(cursor.fetchall())


def fetch_telemetry(conn: Any, device_id: int, hours: int) -> list[dict[str, Any]]:
    """查詢單一裝置過去 ``hours`` 小時感測數據（參數化查詢，禁止字串拼值）。"""
    since_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_str = since_utc.strftime(_DB_TIME_FMT)
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, pm1, pm25, pm10, temperature, humidity, "
            "voc, voc_level, co2, eco2, rssi, filter_life, created_at "
            "FROM device_telemetry "
            "WHERE device_id = %s AND created_at >= %s "
            "ORDER BY created_at ASC",
            (device_id, since_str),
        )
        return list(cursor.fetchall())


# --------------------------------------------------------------------------- #
# 共用資料處理（純函式，不碰 I/O，改寫自 aihcr-daily 同名函式）
# --------------------------------------------------------------------------- #


def collect_points(
    rows: list[dict[str, Any]], key: str, exclude_zero: bool = False
) -> list[tuple[datetime, float]]:
    """抽出 (UTC naive datetime, float 值) 列表；排除 None（可選排除 0 無效值）。"""
    points: list[tuple[datetime, float]] = []
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        value = float(value)
        if exclude_zero and value == 0:
            continue
        points.append((row["created_at"], value))
    return points


def bucket_average(
    points: list[tuple[datetime, float]], minutes: int = 10
) -> list[tuple[datetime, float]]:
    """10 分鐘桶平均平滑（純 Python 分桶，不用 pandas）。"""
    buckets: dict[datetime, list[float]] = {}
    for ts, value in points:
        key = ts.replace(
            minute=(ts.minute // minutes) * minutes, second=0, microsecond=0
        )
        buckets.setdefault(key, []).append(value)
    return sorted((k, sum(vs) / len(vs)) for k, vs in buckets.items())


def find_segments(
    points: list[tuple[datetime, float]], threshold: float, gap_minutes: int = 5
) -> list[tuple[datetime, datetime]]:
    """找出「值 > threshold」的連續區段。相鄰資料間隔超過 gap_minutes 視為中斷。"""
    segments: list[tuple[datetime, datetime]] = []
    seg_start: datetime | None = None
    prev_ts: datetime | None = None
    for ts, value in points:
        if value > threshold:
            if seg_start is None:
                seg_start = ts
            elif prev_ts is not None and (ts - prev_ts).total_seconds() > gap_minutes * 60:
                segments.append((seg_start, prev_ts))
                seg_start = ts
            prev_ts = ts
        elif seg_start is not None:
            segments.append((seg_start, prev_ts))
            seg_start = None
    if seg_start is not None and prev_ts is not None:
        segments.append((seg_start, prev_ts))
    return segments


def _compute_power_segments(
    rows: list[dict[str, Any]], gap_minutes: int = 10
) -> list[tuple[datetime, datetime]]:
    """依時間戳斷流切出「開機時段」。rows 需已按 created_at ASC 排序。"""
    if not rows:
        return []
    segments: list[tuple[datetime, datetime]] = []
    seg_start = rows[0]["created_at"]
    prev_ts = seg_start
    for row in rows[1:]:
        ts = row["created_at"]
        if (ts - prev_ts).total_seconds() > gap_minutes * 60:
            segments.append((seg_start, prev_ts))
            seg_start = ts
        prev_ts = ts
    segments.append((seg_start, prev_ts))
    return segments


def fmt_tw(dt_utc: datetime) -> str:
    """UTC naive datetime → 台灣時間字串（MM-DD HH:MM）。"""
    return (dt_utc + TAIWAN_OFFSET).strftime("%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# matplotlib（延遲 import，見模組 docstring）
# --------------------------------------------------------------------------- #


def _ensure_matplotlib() -> tuple[Any, Any, Any, Any]:
    """延遲載入 matplotlib 並鎖定 Agg backend（無 X display 的 VPS 必需），
    回傳 ``(mdates, pyplot, font_manager, PdfPages)``。

    每次呼叫都重新 ``import``：Python 的模組快取讓這只是幾次 dict 查找，
    成本可忽略，換來的是「這個檔案裡沒有任何一處在模組頂層匯入 matplotlib」。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.backends.backend_pdf import PdfPages

    return mdates, plt, font_manager, PdfPages


def setup_cjk_font() -> bool:
    """偵測 CJK 字型；找到就設 rcParams 回傳 True，否則 False（圖表退回英文標籤）。"""
    _mdates, plt, font_manager, _pdf_pages = _ensure_matplotlib()
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_FONT_CANDIDATES:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name]
            return True
    return False


def _plot_panel(ax: Any, points: list[tuple[datetime, float]], label: str, hlines: list[tuple[float, str]]) -> None:
    """畫單一子圖：折線 + 閾值虛線（hlines = [(y 值, 顏色), ...]）。"""
    if points:
        ax.plot(
            [p[0] for p in points], [p[1] for p in points], linewidth=1.2, color="#1f77b4"
        )
    for y_value, color in hlines:
        ax.axhline(y=y_value, color=color, linestyle="--", linewidth=1)
    ax.set_ylabel(label, fontsize=10)
    ax.grid(True, alpha=0.3)


def _build_chart_figure(
    rows: list[dict[str, Any]],
    user_name: str,
    mac: str,
    figsize: tuple[float, float] = (11.69, 8.27),
) -> Any:
    """建 5 面板共用 X 軸折線圖 figure（台灣時間、10 分鐘桶平均）。

    只建圖不 savefig 不 close，由呼叫端（:func:`build_pdf`）決定輸出去向。
    預設尺寸是 A4 橫式（本檔只產 PDF，不像 aihcr-daily 另外還要出直向 PNG）。
    """
    mdates, plt, _font_manager, _pdf_pages = _ensure_matplotlib()
    has_cjk = setup_cjk_font()
    plt.rcParams["axes.unicode_minus"] = False
    labels = LABELS_ZH if has_cjk else LABELS_EN

    # 矮版面（橫式）時 5 面板各分不到 1.5 吋高：縮小標題字級 + 壓縮標題保留區。
    is_compact = figsize[1] < 10

    panels = [
        ("pm25", labels["pm25"], [(15, "red")], False),
        ("co2", labels["co2"], [(800, "orange"), (1000, "red")], True),
        ("voc_level", labels["voc"], [(2, "red")], False),
        ("temperature", labels["temp"], [(28, "orange")], False),
        ("humidity", labels["humi"], [(60, "orange"), (70, "red")], False),
    ]
    fig, axes = plt.subplots(5, 1, figsize=figsize, sharex=True)
    for ax, (key, label, hlines, exclude_zero) in zip(axes, panels):
        raw_points = collect_points(rows, key, exclude_zero=exclude_zero)
        smoothed = bucket_average(raw_points)
        tw_points = [(ts + TAIWAN_OFFSET, v) for ts, v in smoothed]
        _plot_panel(ax, tw_points, label, hlines)
        if is_compact:
            ax.tick_params(labelsize=8)
            ax.yaxis.label.set_size(8)

    start_tw = fmt_tw(rows[0]["created_at"])
    end_tw = fmt_tw(rows[-1]["created_at"])
    fig.suptitle(
        labels["title"].format(name=user_name, mac=mac, start=start_tw, end=end_tw),
        fontsize=11 if is_compact else 14,
    )
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[-1].set_xlabel(labels["xlabel"], fontsize=8 if is_compact else 10)
    fig.tight_layout(rect=(0, 0, 1, 0.95 if is_compact else 0.97))
    return fig


def _sanitize_for_pdf(text: str) -> str:
    """PDF 渲染前替換 emoji：JhengHei / Noto Sans CJK 無對應 glyph 會變豆腐框。

    只影響 PDF；Email 純文字信內文保留 emoji 不動。
    """
    return (
        text.replace("✅", "[良好]")
        .replace("⚠️", "[注意]")  # ⚠ + U+FE0F 組合版
        .replace("⚠", "[注意]")  # 無 variation selector 版
        .replace("️", "")  # 移除殘留的 U+FE0F variation selector
    )


def build_pdf(
    rows: list[dict[str, Any]], report_text: str, pdf_path: str, user_name: str, mac: str
) -> bool:
    """產出 2 頁 A4 橫式 PDF：第 1 頁 5 面板折線圖、第 2 頁健康分析全文。

    回傳 ``True`` 成功產出 ／ ``False`` 無 CJK 字型時降級跳過（不產生 PDF 檔）。
    呼叫端（:func:`send_trial_report`）在 ``False`` 時改寄純文字信、不附件，
    而不是把整次寄送判定失敗——附件是加值品，缺附件不該讓報告本身寄不出去。
    """
    has_cjk = setup_cjk_font()
    if not has_cjk:
        logger.warning("體驗報告：偵測不到 CJK 字型，跳過 PDF 產出（本次信件將降級為純文字）")
        return False

    _mdates, plt, _font_manager, PdfPages = _ensure_matplotlib()
    plt.rcParams["axes.unicode_minus"] = False
    # 台灣日期由 rows 最後一筆換算（與報告資料期間一致，不受執行時刻跨日影響）。
    taiwan_date = (rows[-1]["created_at"] + TAIWAN_OFFSET).date().isoformat()
    pdf_text = _sanitize_for_pdf(report_text)

    with PdfPages(pdf_path) as pdf:
        chart_fig = _build_chart_figure(rows, user_name, mac, figsize=(11.69, 8.27))
        pdf.savefig(chart_fig)
        plt.close(chart_fig)

        text_fig = plt.figure(figsize=(11.69, 8.27))
        text_fig.text(
            0.5,
            0.94,
            f"{user_name}家 空氣品質健康分析（{taiwan_date}）",
            ha="center",
            va="top",
            fontsize=14,
            fontweight="bold",
        )
        wrapped_lines: list[str] = []
        for raw_line in pdf_text.split("\n"):
            wrapped_lines.extend(textwrap.wrap(raw_line, width=60) or [""])
        y = 0.87
        line_step = 0.026
        for line in wrapped_lines:
            if y < 0.05:
                text_fig.text(
                    0.07,
                    y,
                    "（內容過長，以下省略，完整內容見信件內文）",
                    ha="left",
                    va="top",
                    fontsize=10,
                    color="gray",
                )
                break
            text_fig.text(0.07, y, line, ha="left", va="top", fontsize=10)
            y -= line_step
        pdf.savefig(text_fig)
        plt.close(text_fig)
    return True


# --------------------------------------------------------------------------- #
# 規則式健康分析（各 section 回傳 (段落文字, 問題描述或 None)）
# --------------------------------------------------------------------------- #


def _build_overview_text(rows: list[dict[str, Any]], hours: int) -> str:
    """資料概況 + 開機時段（附在總結段內，不佔獨立編號）。"""
    count = len(rows)
    expected = hours * 60  # 感測資料約每分鐘一筆
    coverage = min(round(count / expected * 100, 1), 100.0)
    start_tw = fmt_tw(rows[0]["created_at"])
    end_tw = fmt_tw(rows[-1]["created_at"])
    lines = [
        f"   資料概況：共 {count} 筆，涵蓋率 {coverage}%，"
        f"資料期間（台灣時間）{start_tw} ~ {end_tw}"
    ]
    segments = _compute_power_segments(rows)
    total_on = sum((e - s).total_seconds() for s, e in segments) / 3600
    pct = min(round(total_on / hours * 100, 1), 100.0)
    if len(segments) == 1 and pct >= 95:
        lines.append(f"   開機時段：全天開機（開機率 {pct}%）")
    else:
        shown = segments[:6]
        seg_text = "、".join(f"{fmt_tw(s)} ~ {fmt_tw(e)}" for s, e in shown)
        suffix = "（僅列前 6 段）" if len(segments) > 6 else ""
        lines.append(
            f"   開機時段：{seg_text}{suffix}"
            f"（共 {len(segments)} 段，累計 {total_on:.1f} 小時，開機率 {pct}%）"
        )
    return "\n".join(lines)


def _section_pm25(rows: list[dict[str, Any]]) -> tuple[str, str | None]:
    points = collect_points(rows, "pm25")
    if not points:
        return "1. PM2.5：無有效資料", None
    values = [v for _, v in points]
    avg = sum(values) / len(values)
    peak = max(values)
    text = f"1. PM2.5：平均 {avg:.1f}、最大 {peak:.1f} μg/m³。"
    if peak <= 15:
        text += "✅ 優異，低於 WHO 24h 建議上限 15 μg/m³"
        return text, None
    text += "⚠️ 有超標時段，建議檢查濾網是否需更換，並留意室內污染源（烹飪、燒香、掃除揚塵）"
    return text, f"PM2.5 最大 {peak:.0f} μg/m³ 超過 WHO 上限"


def _section_co2(rows: list[dict[str, Any]]) -> tuple[str, str | None]:
    points = collect_points(rows, "co2", exclude_zero=True)  # co2=0 視為感測器無效值
    if not points:
        return "2. CO2：無有效資料（全部為 0 或缺值）", None
    values = [v for _, v in points]
    avg = sum(values) / len(values)
    peak = max(values)
    over_1000 = sum(1 for v in values if v > 1000)
    over_800 = sum(1 for v in values if v > 800)
    pct_1000 = round(over_1000 / len(values) * 100, 1)
    pct_800 = round(over_800 / len(values) * 100, 1)
    lines = [
        f"2. CO2：平均 {avg:.0f}、最大 {peak:.0f} ppm；"
        f">1000 ppm 約 {over_1000} 分鐘（{pct_1000}%）、"
        f">800 ppm 約 {over_800} 分鐘（{pct_800}%）"
    ]
    segments = find_segments(points, 1000)
    if segments:
        longest = max(segments, key=lambda s: s[1] - s[0])
        lines.append(f"   最長連續 >1000 ppm 區段：{fmt_tw(longest[0])} ~ {fmt_tw(longest[1])}")
    issue = None
    if pct_1000 >= 5:
        lines.append("   建議：睡覺留門縫 5-10 公分、起床後開窗 10 分鐘幫助換氣")
        issue = f"CO2 >1000 ppm 佔 {pct_1000}%（通風不足）"
    return "\n".join(lines), issue


def _section_voc(rows: list[dict[str, Any]]) -> tuple[str, str | None]:
    points = collect_points(rows, "voc_level")  # 0 是合法等級，不排除
    if not points:
        return "3. VOC 等級：無有效資料", None
    values = [v for _, v in points]
    total = len(values)
    level_counts: dict[int, int] = {}
    for v in values:
        level = int(v)
        level_counts[level] = level_counts.get(level, 0) + 1
    dist_text = "、".join(
        f"等級{level} 佔 {round(count / total * 100, 1)}%"
        for level, count in sorted(level_counts.items())
    )
    lines = [f"3. VOC 等級：{dist_text}"]
    segments = find_segments(points, 1)  # threshold=1 即抓等級 >= 2（偏高）
    issue = None
    if segments:
        shown = segments[:5]
        seg_text = "、".join(f"{fmt_tw(s)} ~ {fmt_tw(e)}" for s, e in shown)
        suffix = f"（僅列前 5 段，共 {len(segments)} 段）" if len(segments) > 5 else ""
        lines.append(f"   偵測到等級2（偏高）時段：{seg_text}{suffix}")
        lines.append("   建議：回想該時段活動（烹飪/清潔劑/噴霧），避免在嬰兒房直接噴灑")
        issue = f"VOC 等級2（偏高）出現 {len(segments)} 段"
    return "\n".join(lines), issue


def _section_temp(rows: list[dict[str, Any]]) -> tuple[str, str | None]:
    points = collect_points(rows, "temperature")
    if not points:
        return "4. 溫度：無有效資料", None
    values = [v for _, v in points]
    avg = sum(values) / len(values)
    pct_over = round(sum(1 for v in values if v > 28) / len(values) * 100, 1)
    text = f"4. 溫度：平均 {avg:.1f}°C，>28°C 佔比 {pct_over}%"
    if pct_over >= 20:
        text += "。提醒：嬰幼兒房建議 24~28°C"
        return text, f"溫度 >28°C 佔 {pct_over}%"
    return text, None


def _section_humidity(rows: list[dict[str, Any]]) -> tuple[str, str | None]:
    points = collect_points(rows, "humidity")
    if not points:
        return "5. 濕度：無有效資料", None
    values = [v for _, v in points]
    avg = sum(values) / len(values)
    pct_over = round(sum(1 for v in values if v > 70) / len(values) * 100, 1)
    text = f"5. 濕度：平均 {avg:.1f}%，>70% 佔比 {pct_over}%"
    if pct_over >= 20:
        text += "。建議：睡眠時段開冷氣或除濕，目標 50~60%（塵蟎黴菌是嬰幼兒過敏主因）"
        return text, f"濕度 >70% 佔 {pct_over}%"
    return text, None


def analyze(rows: list[dict[str, Any]], hours: int) -> tuple[str, str]:
    """組合規則式健康分析報告；回傳 ``(完整報告文字, 一句話總結)``。

    段落順序對齊 PDF 第 1 頁折線圖（PM2.5→CO2→VOC→溫→濕），資料概況與開機時段
    附在第 6 段總結內。**規則式**（沒有任何 AI/LLM 呼叫）——同一份輸入永遠得到
    同一份輸出，這對「寄給客戶的健康建議」而言是必要的可預測性。
    """
    sections = []
    issues = []
    for builder in (_section_pm25, _section_co2, _section_voc, _section_temp, _section_humidity):
        text, issue = builder(rows)
        sections.append(text)
        if issue:
            issues.append(issue)
    if issues:
        summary = "⚠️ 今日最需留意：" + "；".join(issues)
    else:
        summary = "✅ 各項指標均在理想範圍，整體空氣品質優異，維持現況即可"
    sections.append(f"6. 總結：{summary}\n{_build_overview_text(rows, hours)}")
    return "\n".join(sections), summary


# --------------------------------------------------------------------------- #
# Email（rec 的 email_masked 遮罩 + 收件人整理 + 真正的寄送）
# --------------------------------------------------------------------------- #


def mask_email(email: object) -> str:
    """把 email 遮成局部可辨識、局部隱藏的形式，例如
    ``chenyang@example.com`` → ``ch***@example.com``。

    只留 local-part 前 :data:`_EMAIL_VISIBLE_HEAD_CHARS` 碼 + 完整網域，中間一律補
    :data:`_EMAIL_MASK_STARS`（**固定 3 顆星，不隨帳號長度變動**——理由見該常數
    旁的說明：星號數若跟著長度變動，遮罩後的字串長度本身就洩漏了原始帳號多長）。
    網域不是個資（同網域的人共用），留著才看得出「公司信箱還是私人信箱」；
    local-part 太短（<= 頭幾碼）時**全遮**（星號數＝原始長度，這種情況本來就短，
    不構成額外的長度洩漏疑慮），避免「短帳號留 2 碼＝幾乎沒遮」的邊界案例
    （同 :func:`web.audit.mask_phone` 的處理精神）。
    """
    text = email if isinstance(email, str) else str(email)
    local, sep, domain = text.partition("@")
    if not sep:
        # 沒有 @：不是合法 email（不該發生於已驗證過的資料，這裡是防禦性處理），
        # 全遮掉比原樣寫進稽核檔安全。
        return "*" * len(text)
    if len(local) <= _EMAIL_VISIBLE_HEAD_CHARS:
        masked_local = "*" * len(local)
    else:
        masked_local = local[:_EMAIL_VISIBLE_HEAD_CHARS] + _EMAIL_MASK_STARS
    return f"{masked_local}@{domain}"


def _dedupe_bcc(staff_bcc: Sequence[str], to_list: Sequence[str]) -> list[str]:
    """整理 Bcc 名單：strip、剔除空字串、依小寫去重，且已在 To 的地址要從 Bcc 移除。

    沿用 aihcr-daily ``build_recipients()`` 的「To 優先」精神：同一地址不該
    同時出現在 To 與 Bcc，否則同一人會收到兩封（一封在 To 看得到自己、
    一封被密件副本夾帶）。
    """
    to_lowered = {addr.strip().lower() for addr in to_list if isinstance(addr, str)}
    seen: set[str] = set()
    result: list[str] = []
    for raw in staff_bcc:
        if not isinstance(raw, str):
            continue
        addr = raw.strip()
        if not addr:
            continue
        key = addr.lower()
        if key in to_lowered or key in seen:
            continue
        seen.add(key)
        result.append(addr)
    return result


def _send_real_gmail(
    subject: str,
    body: str,
    to: list[str],
    *,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
) -> bool:
    """真正的 ``email_sender`` 預設實作：``smtplib.SMTP_SSL`` 寄出。

    🔴 絕對不可補 ``msg["Bcc"] = ...``：Bcc 的實作機制是「郵件本體不含 Bcc
    header，但 SMTP 信封收件人（``sendmail`` 第二參數）含全員」。一旦寫進
    header，內部人員的 email 會原封不動印在每位客戶收到的信裡。

    🔴 ``smtp.sendmail()`` 的回傳值**必須檢查**，不能就地丟棄。它只有在「全部
    收件人都被拒」時才拋 :class:`smtplib.SMTPRecipientsRefused`；只要至少一個
    收件人成功（Bcc 固定有幾個內部信箱，幾乎必成功），它就正常回傳一個
    ``{被拒收件人: (code, msg)}`` 的 dict，**不拋例外**。這個 dict 若被忽略，
    客戶的 email（``acfh_api.users.email``，依 spec 明確「不做二次驗證」）
    只要語法合法但被 Gmail 判定無效，客戶那封信會被靜默拒收，而
    :func:`send_trial_report` 卻會回報成功、稽核記錄寫 success=True——
    客戶實際上什麼都沒收到。
    """
    bcc = bcc or []
    if not to and not bcc:
        logger.error("體驗報告：Gmail 收件人清單為空，標記失敗")
        return False
    user, app_password = get_gmail_credentials()

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for path in attachments or []:
        part = MIMEApplication(Path(path).read_bytes())
        # 檔名用 RFC2231 (utf-8, '', 檔名) 三元組，防中文檔名亂碼。
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", Path(path).name))
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, app_password)
            refused = smtp.sendmail(user, to + bcc, msg.as_string())
    except (smtplib.SMTPException, OSError, MessageError) as exc:
        # MessageError 涵蓋 msg.as_string() 的 header 語法解析錯誤（CRLF injection
        # 這類髒資料）：To 內容源自 acfh_api.users.email 這種不受控欄位，一筆含換行
        # 的髒資料就會在此拋錯。**這不涵蓋 SMTP 協定層的部分拒收**——語法合法但
        # 地址本身無效（信箱不存在、被對方伺服器判定為垃圾郵件）不會拋例外，
        # 而是反映在下面 sendmail() 的回傳值裡，兩種風險由不同的程式碼路徑處理。
        logger.error("體驗報告：Gmail 寄送失敗：%s", exc)
        return False

    if refused:
        # 至少一個收件人被拒收，但 SMTP 交握本身成功（否則會拋例外，走上面那個
        # except），所以這裡才拿得到非空 dict。客戶本人（to）若在其中，代表這封
        # 信客戶根本沒收到——即使 Bcc 內部信箱全數成功，也絕不能回報 success=True。
        logger.error("體驗報告 Gmail 部分收件人被拒收：%s", list(refused))
        if any(addr in refused for addr in to):
            return False

    logger.info("體驗報告 Gmail 已寄出（To %s 位、Bcc %s 位）", len(to), len(bcc))
    return True


# --------------------------------------------------------------------------- #
# 稽核
# --------------------------------------------------------------------------- #


def default_trial_audit_path() -> Path:
    """稽核檔預設路徑（可由 :data:`ENV_TRIAL_AUDIT_PATH` 覆寫）。

    與 :mod:`web.audit` 的 ``send-audit.jsonl`` 刻意分開存檔：那是三竹簡訊的
    稽核（花共用點數），這是 Email 的稽核（花的是 Gmail 額度、與點數池無關），
    混在同一份檔案只會讓事後對帳要先過濾 event 種類才分得開。
    """
    raw = os.environ.get(ENV_TRIAL_AUDIT_PATH)
    if raw is not None and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path(__file__).resolve().parent.parent / "logs" / DEFAULT_TRIAL_AUDIT_FILENAME


def _default_audit_log() -> AuditLog:
    return AuditLog(default_trial_audit_path())


def _fail(
    audit: AuditLog,
    recipient_id: str,
    user_id: int | None,
    reason: str,
    message: str,
    *,
    email: str | None = None,
) -> TrialReportResult:
    """寫一筆失敗稽核紀錄並組出失敗結果。email 只以遮罩形式落地（見 :func:`mask_email`）。"""
    email_masked = mask_email(email) if email else None
    # AuditLog.record 內部已把「寫入失敗」吞掉（回 bool，不拋例外）：稽核是
    # 事後追蹤用途，不該因為稽核檔本身寫不進去，就讓「這次到底寄了沒」的
    # 判斷結果被連帶打斷。
    audit.record(
        "result",
        recipient_id=recipient_id,
        user_id=user_id,
        success=False,
        reason=reason,
        email_masked=email_masked,
    )
    logger.info("體驗報告未寄送：recipient_id=%s reason=%s", recipient_id, reason)
    return TrialReportResult(
        success=False,
        recipient_id=recipient_id,
        reason=reason,
        message=message,
        email_masked=email_masked,
        pdf_attached=False,
    )


# --------------------------------------------------------------------------- #
# 對外主入口
# --------------------------------------------------------------------------- #


def send_trial_report(
    recipient: Recipient,
    *,
    conn_factory: Callable[[], Any] | None = None,
    email_sender: Callable[..., bool] | None = None,
    staff_bcc: Sequence[str] = (),
    audit_log: AuditLog | None = None,
    today: date | None = None,
) -> TrialReportResult:
    """驗證資格 → 撈 14 天 telemetry → 產報告 → 寄 Email → 留稽核紀錄。

    對應 doc/spec-trial-report.md「核心邏輯」1-9 步。所有「會擋下寄送」的情況
    都在本函式內部處理完（伺服器路由層 :meth:`web.server.SmsWebApp.
    handle_send_trial_report` 只負責反查 recipient 與轉譯 HTTP 回應），
    這樣驗證邏輯可以脫離 HTTP 層完整單元測試。

    ``conn_factory`` / ``email_sender`` 未傳時分別指向真正會連 DB／真正會
    寄信的實作；測試務必自己注入假的兩者，**絕不能讓測試碰到真實客戶資料庫
    或真的寄出 Email**（同 tests/conftest.py 對三竹 API 的封鎖精神）。

    ``audit_log`` 未傳時使用 :func:`default_trial_audit_path` 的預設位置；
    測試同樣應該注入指到 ``tmp_path`` 的實例，避免污染 repo 底下的稽核檔。

    ``today`` 只給測試注入固定日期用（傳給 :func:`web.templates._compute_used_days`
    算已用天數）；生產環境不傳，該函式會固定用 ``Asia/Taipei`` 曆日（不隨 VPS
    系統時區漂移），理由見 :func:`web.templates._compute_used_days` 的 docstring。
    """
    audit = audit_log if audit_log is not None else _default_audit_log()
    recipient_id = recipient.id

    # 1-2. 伺服器端重新驗證「已用天數 >= 天數」。**不信任前端按鈕的 disabled
    # 狀態**（devtools 可以繞過），一律用 web.templates 那份已驗證過的解析邏輯
    # 重算一次，兩邊不同步會出現「畫面能點、伺服器又擋」的落差。這裡連 producer
    # 快照的 used_days 都不再信任，只信任接機日（borrow_date）動態算——快照只在
    # producer 重新跑一次時才會更新，兩次同步之間會凍結在舊數字。
    used = _compute_used_days(recipient.borrow_date, today=today)
    total = _parse_trial_day_count(recipient.days)
    if used is None or total is None or used < total:
        return _fail(
            audit,
            recipient_id,
            None,
            REASON_DAYS_NOT_REACHED,
            "已用天數尚未達到體驗天數（或欄位無法解析），未執行任何動作，也沒有寄送任何郵件。",
        )

    # 3. id → acfh user_id。格式不符（見 Recipient.acfh_user_id）一律擋下，不猜。
    user_id = recipient.acfh_user_id
    if user_id is None:
        return _fail(
            audit,
            recipient_id,
            None,
            REASON_INVALID_RECIPIENT_ID,
            "無法從發送對象編號解析出使用者資料，未執行任何動作，也沒有寄送任何郵件。",
        )

    conn_maker = conn_factory if conn_factory is not None else _default_conn_factory
    try:
        conn = conn_maker()
    except Exception as exc:  # noqa: BLE001 -- DB 連線失敗的成因五花八門（缺密碼／逾時／認證失敗…），
        # 這裡的責任是「連不上就別繼續、留痕、回可讀訊息」，不是逐一分類底層例外。
        logger.error("體驗報告：DB 連線失敗（recipient_id=%s）：%s", recipient_id, exc)
        return _fail(audit, recipient_id, user_id, REASON_DB_ERROR, "資料庫連線失敗，未寄送任何郵件。")

    try:
        # 3（續）. 裝置數必須剛好 1；0 或 >=2 都擋下，不猜要寄哪一台。
        devices = fetch_user_devices(conn, user_id)
        if len(devices) == 0:
            return _fail(audit, recipient_id, user_id, REASON_NO_DEVICE, "查無裝置，未寄送任何郵件。")
        if len(devices) >= 2:
            return _fail(
                audit,
                recipient_id,
                user_id,
                REASON_MULTIPLE_DEVICES,
                "此客戶同時有多台裝置，暫不支援自動寄送，請手動處理。",
            )
        device_id = devices[0]["device_id"]
        mac = devices[0].get("mac_address") or ""

        # 4. email 為空 → 擋下，不寄殘缺信。
        info = fetch_user_info(conn, user_id)
        email = (info or {}).get("email")
        if not email:
            return _fail(audit, recipient_id, user_id, REASON_NO_EMAIL, "查無客戶 email，未寄送任何郵件。")
        user_name = (info or {}).get("name") or recipient.name

        # 5. 14 天內 telemetry 0 筆 → 擋下。
        rows = fetch_telemetry(conn, device_id, TRIAL_REPORT_HOURS)
        if not rows:
            return _fail(
                audit,
                recipient_id,
                user_id,
                REASON_NO_TELEMETRY,
                "近 14 天內查無感測資料，未寄送任何郵件。",
                email=email,
            )
    except Exception as exc:  # noqa: BLE001 -- 查詢層例外同樣五花八門，統一歸類為「資料庫錯誤」
        logger.error(
            "體驗報告：查詢 acfh_api 時發生錯誤（recipient_id=%s, user_id=%s）：%s",
            recipient_id,
            user_id,
            exc,
        )
        return _fail(audit, recipient_id, user_id, REASON_DB_ERROR, "查詢資料庫時發生錯誤，未寄送任何郵件。")
    finally:
        _close_connection(conn)

    # 6. 規則式健康分析（無 AI/LLM 呼叫）。
    report_text, _summary = analyze(rows, TRIAL_REPORT_HOURS)
    taiwan_date = (rows[-1]["created_at"] + TAIWAN_OFFSET).date().isoformat()
    subject = f"【addwii】{user_name} 的 14 天體驗空氣品質報告（{taiwan_date}）"

    to_list = [email]
    bcc_list = _dedupe_bcc(staff_bcc, to_list)
    sender = email_sender if email_sender is not None else _send_real_gmail

    with tempfile.TemporaryDirectory(prefix="mitake-trial-report-") as tmp_dir:
        pdf_path = Path(tmp_dir) / f"trial-report-{recipient_id}.pdf"
        try:
            pdf_attached = build_pdf(rows, report_text, str(pdf_path), user_name, mac)
        except Exception as exc:  # noqa: BLE001 -- matplotlib 底層例外不逐一列舉：PDF 是加值附件，
            # 畫不出來要降級成純文字信，不能讓整次寄送因此失敗。
            logger.warning(
                "體驗報告：PDF 產生時發生例外，降級為純文字信（recipient_id=%s）：%s",
                recipient_id,
                exc,
            )
            pdf_attached = False
        attachments = [str(pdf_path)] if pdf_attached else []

        # 7. 寄送：To=客戶本人、Bcc=固定內部清單（不寫進 Bcc header，見 _send_real_gmail）。
        try:
            sent_ok = sender(subject, report_text, to_list, bcc=bcc_list, attachments=attachments)
        except Exception as exc:  # noqa: BLE001 -- smtplib / 自訂 sender 的例外型別不固定
            logger.error("體驗報告：寄送 email 時發生例外（recipient_id=%s）：%s", recipient_id, exc)
            return _fail(
                audit,
                recipient_id,
                user_id,
                REASON_SEND_ERROR,
                "寄送郵件時發生錯誤，請查看伺服器日誌。",
                email=email,
            )

    if not sent_ok:
        return _fail(
            audit,
            recipient_id,
            user_id,
            REASON_SEND_ERROR,
            "郵件寄送失敗，請查看伺服器日誌（可能是 Gmail 帳密未設定或連線問題）。",
            email=email,
        )

    # 8-9. 留稽核紀錄（成功）+ 回傳結果頁用的訊息。
    email_masked = mask_email(email)
    audit.record(
        "result",
        recipient_id=recipient_id,
        user_id=user_id,
        success=True,
        reason=None,
        email_masked=email_masked,
        pdf_attached=pdf_attached,
    )
    logger.info(
        "體驗報告已寄送：recipient_id=%s user_id=%s pdf_attached=%s", recipient_id, user_id, pdf_attached
    )
    return TrialReportResult(
        success=True,
        recipient_id=recipient_id,
        reason=None,
        message=f"已寄送給 {email_masked}。",
        email_masked=email_masked,
        pdf_attached=pdf_attached,
    )
