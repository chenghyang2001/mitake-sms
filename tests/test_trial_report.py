"""web/trial_report.py 的離線回歸鎖。

**這支測試絕對不能真的連 DB、也絕對不能真的寄信。** ``send_trial_report`` 會查
acfh_api（AIHCR 場域資料庫，內含真實客戶資料）並透過 Gmail 寄信給真實客戶
email——這兩件事一旦在測試裡不小心觸發，後果比 tests/conftest.py 防的「不小心
花三竹點數」更嚴重（那頂多燒點數，這裡是把測試資料寄給真人）。所以全檔規則：

1. ``conn_factory`` 一律注入假的 :class:`FakeConnection`（純記憶體，`.cursor()`
   回傳依 SQL 關鍵字分流的假游標），**不 monkeypatch pymysql**。
2. ``email_sender`` 一律注入 :class:`RecordingEmailSender`（只記錄呼叫參數，
   不真的連 smtp.gmail.com）。
3. ``audit_log`` 一律注入指到 ``tmp_path`` 的 :class:`~web.audit.AuditLog`，
   不寫進 repo 的 ``logs/``。
4. matplotlib 相關函式（``analyze`` / ``build_pdf``）本身是純函式或只寫暫存檔，
   可以直接呼叫真正的實作；唯一會依賴「這台機器有沒有裝 CJK 字型」的地方是
   :func:`web.trial_report.setup_cjk_font`，凡是要斷言 PDF 一定產得出來／
   一定產不出來的測試，都直接 monkeypatch 這個函式，不依賴機器上實際裝了什麼字型
   （不同開發機／CI 機器字型不一樣，靠環境猜結果只會做出不穩定的測試）。

COMPLEXITY: complex（本檔覆蓋 doc/spec-trial-report.md 的核心邏輯 1-9 步 +
與 aihcr-daily 對齊的規則式分析／PDF／收件人整理邏輯），依鐵律至少 20+ 測試案例。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from web.audit import AuditLog  # noqa: E402
from web.recipients import Recipient  # noqa: E402
from web import trial_report  # noqa: E402
from web.trial_report import (  # noqa: E402
    REASON_DAYS_NOT_REACHED,
    REASON_DB_ERROR,
    REASON_INVALID_RECIPIENT_ID,
    REASON_MULTIPLE_DEVICES,
    REASON_NO_DEVICE,
    REASON_NO_EMAIL,
    REASON_NO_TELEMETRY,
    REASON_SEND_ERROR,
    TRIAL_REPORT_HOURS,
    mask_email,
    send_trial_report,
)

# --------------------------------------------------------------------------- #
# 測試替身
# --------------------------------------------------------------------------- #


class _FakeCursor:
    """依 SQL 關鍵字分流回傳資料的假游標。支援 `with conn.cursor() as cursor:` 語法。"""

    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._pending: str | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._conn.executed.append((sql, params))
        if self._conn.raise_on_execute is not None:
            raise self._conn.raise_on_execute
        if "FROM devices" in sql:
            self._pending = "devices"
        elif "FROM users" in sql:
            self._pending = "user"
        elif "FROM device_telemetry" in sql:
            self._pending = "telemetry"
        else:
            raise AssertionError(f"FakeConnection 未預期的 SQL：{sql!r}")

    def fetchall(self) -> list:
        if self._pending == "devices":
            return list(self._conn.devices)
        if self._pending == "telemetry":
            return list(self._conn.telemetry_rows)
        raise AssertionError(f"fetchall() 用在不支援的查詢類型：{self._pending!r}")

    def fetchone(self) -> dict | None:
        if self._pending == "user":
            return self._conn.user_row
        raise AssertionError(f"fetchone() 用在不支援的查詢類型：{self._pending!r}")


class FakeConnection:
    """完全在記憶體內運作的假 DB 連線，供 ``conn_factory`` 注入。"""

    def __init__(
        self,
        *,
        devices: list[dict] | None = None,
        user_row: dict | None = None,
        telemetry_rows: list[dict] | None = None,
        raise_on_execute: BaseException | None = None,
    ) -> None:
        self.devices = devices if devices is not None else []
        self.user_row = user_row
        self.telemetry_rows = telemetry_rows if telemetry_rows is not None else []
        self.raise_on_execute = raise_on_execute
        self.executed: list[tuple] = []
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class RecordingEmailSender:
    """假的 email_sender：記錄每次呼叫參數，依設定回傳或拋出。"""

    def __init__(self, *, result: bool = True, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result
        self._error = error

    def __call__(self, subject, body, to, *, bcc=None, attachments=None) -> bool:
        self.calls.append(
            {
                "subject": subject,
                "body": body,
                "to": list(to),
                "bcc": list(bcc or []),
                "attachments": list(attachments or []),
            }
        )
        if self._error is not None:
            raise self._error
        return self._result


class FakeSMTPClient:
    """假的 ``smtplib.SMTP_SSL`` 客戶端：模擬 context manager + login()/sendmail()。

    用來驗證 :func:`web.trial_report._send_real_gmail` 有沒有正確處理
    ``sendmail()`` 的回傳值——真正的 smtplib 只有在**全部**收件人都被拒時才拋
    ``SMTPRecipientsRefused``，只要有一個成功就正常 return 一個
    ``{被拒地址: (code, msg)}`` 的 dict，不拋例外。這支假身讓測試能在完全不連
    smtp.gmail.com 的情況下模擬「部分收件人被拒收」這種只會反映在回傳值裡、
    不會反映在例外裡的情境。
    """

    def __init__(self, *, refused: dict | None = None) -> None:
        self.refused = dict(refused) if refused else {}
        self.login_calls: list[tuple] = []
        self.sendmail_calls: list[tuple] = []

    def __enter__(self) -> "FakeSMTPClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def login(self, user: str, password: str) -> None:
        self.login_calls.append((user, password))

    def sendmail(self, from_addr: str, to_addrs: list, msg: str) -> dict:
        self.sendmail_calls.append((from_addr, list(to_addrs), msg))
        return dict(self.refused)


def _make_recipient(
    *,
    recipient_id: str = "u46",
    days: str = "14",
    used_days: str = "14",
    name: str = "陳筱琪",
    borrow_date: str = "2026-07-01",
) -> Recipient:
    """一筆已達體驗天數、id 格式正常的 Recipient（多數測試的基準情境）。

    ``used_days`` 現在只是 Recipient 上的展示欄位，不再影響 ``send_trial_report``
    的天數驗證邏輯（改用 ``borrow_date`` 動態算，見 ``web.templates._compute_used_days``）
    ——呼叫端若要控制驗證結果，改傳 ``borrow_date`` 並搭配 ``send_trial_report(...,
    today=...)`` 顯式注入固定日期，不要再依賴 ``used_days``。
    """
    return Recipient(
        id=recipient_id,
        name=name,
        phone="0912345678",
        device="體驗機",
        borrow_date=borrow_date,
        match_status="ok",
        days=days,
        used_days=used_days,
        business="王小明",
        trial_status="🟢 體驗中",
    )


def _make_telemetry_rows(n: int = 60, start: datetime | None = None) -> list[dict]:
    """產生 n 筆假感測資料（每分鐘一筆），涵蓋 analyze()/build_pdf() 用到的所有欄位。"""
    base = start or datetime(2026, 7, 1, 0, 0, 0)
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": i,
                "pm1": 5.0,
                "pm25": 8.0 + (i % 5),
                "pm10": 10.0,
                "temperature": 25.0,
                "humidity": 55.0,
                "voc": 100.0,
                "voc_level": 0,
                "co2": 600.0,
                "eco2": 500.0,
                "rssi": -60,
                "filter_life": 90,
                "created_at": base + timedelta(minutes=i),
            }
        )
    return rows


def _make_devices(*, mac: str = "AA:BB:CC:DD:EE:FF") -> list[dict]:
    return [{"device_id": 101, "mac_address": mac, "alias": "客廳"}]


def _make_audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "trial-report-audit.jsonl", fsync=False)


def _read_audit_records(audit: AuditLog) -> list[dict]:
    import json

    if not audit.path.exists():
        return []
    return [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 1. Happy path
# --------------------------------------------------------------------------- #


def test_success_happy_path_sends_email_with_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """單裝置、有 email、有 14 天資料、PDF 產得出來 → 成功，email_sender 被呼叫一次。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: True)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "筱琪", "last_name": "陳", "email": "chenxiaoqi@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender()
    audit = _make_audit(tmp_path)

    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        staff_bcc=("staff@example.com",),
        audit_log=audit,
        # 顯式注入固定日期（借機日 2026-07-01 起算 19 天，確定 >= 14 天達標）——
        # 不依賴執行測試當下的真實 wall-clock 日期，即使 today 剛好落在合理值,
        # 也不該讓測試結果隨執行日期漂移。
        today=date(2026, 7, 20),
    )

    assert result.success is True
    assert result.pdf_attached is True
    assert conn.closed is True
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["to"] == ["chenxiaoqi@example.com"]
    assert call["bcc"] == ["staff@example.com"]
    assert len(call["attachments"]) == 1
    assert call["attachments"][0].endswith(".pdf")


def test_success_recipient_id_passed_to_default_hours_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """驗證確實用 TRIAL_REPORT_HOURS（14 天=336 小時）查 telemetry，不是寫死別的數字。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    assert TRIAL_REPORT_HOURS == 336
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "筱琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
        today=date(2026, 7, 20),  # 借機日 2026-07-01 起算 19 天，確定 >= 14 天達標
    )
    _sql, params = next((s, p) for s, p in conn.executed if "FROM device_telemetry" in s)
    _device_id, since_str = params
    since_dt = datetime.strptime(since_str, "%Y-%m-%d %H:%M:%S")
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    delta_hours = (now_utc_naive - since_dt).total_seconds() / 3600
    # 允許幾分鐘誤差（測試執行耗時），核心是確認查詢窗口確實是 336 小時而非
    # 24 小時（aihcr-daily 原本每日報告的窗口）或其他寫死的數字。
    assert abs(delta_hours - TRIAL_REPORT_HOURS) < 0.1


# --------------------------------------------------------------------------- #
# 2. 已用天數 < 天數（伺服器端重新驗證擋下）
# --------------------------------------------------------------------------- #


def test_blocks_when_days_not_reached_without_touching_db(
    tmp_path: Path,
) -> None:
    """已用天數（今天－接機日動態算）< 天數 → 擋下，且完全不呼叫 conn_factory（不碰 DB）。

    ``used_days`` 欄位已不再影響驗證邏輯，改用 borrow_date（預設 2026-07-01）
    搭配注入 today=2026-07-04（3 天）製造「未達標」情境。
    """
    conn_factory_called = []

    def _conn_factory():
        conn_factory_called.append(True)
        raise AssertionError("不該呼叫 conn_factory")

    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(days="14"),
        conn_factory=_conn_factory,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
        today=date(2026, 7, 4),
    )

    assert result.success is False
    assert result.reason == REASON_DAYS_NOT_REACHED
    assert conn_factory_called == []
    assert sender.calls == []


def test_blocks_when_day_fields_unparseable(tmp_path: Path) -> None:
    """days 解析不出數字、borrow_date 格式壞掉（真實世界髒資料）→ 保守擋下，不猜。

    ``used_days`` 已不再是「已用天數」的來源，改讓 borrow_date 本身格式跑掉
    （不是合法 ``YYYY-MM-DD``），驗證 ``_compute_used_days`` 解析失敗一樣被擋下。
    """
    result = send_trial_report(
        _make_recipient(days="", borrow_date="不是日期"),
        conn_factory=lambda: (_ for _ in ()).throw(AssertionError("不該呼叫")),
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_DAYS_NOT_REACHED


def test_days_reval_handles_real_world_format_with_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真實世界格式「14 天」的 total 也要能正確判定已達標並繼續往下走。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    result = send_trial_report(
        _make_recipient(days="14 天"),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
        today=date(2026, 7, 20),  # 借機日 2026-07-01 起算 19 天，確定 >= 14 天達標
    )
    assert result.success is True


def test_same_recipient_gating_flips_purely_with_injected_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration：同一筆 recipient（固定 borrow_date），光是換 `today` 注入值，
    寄送資格判斷就跟著變 —— 這正是本次改動要解決的問題。

    舊邏輯（讀 producer 快照的 used_days 字串）無論「今天」是哪天，判斷結果都
    不會變，因為快照只在 producer 重新跑一次時才更新。改成「今天－接機日」動態算
    之後，同一筆資料在不同的「今天」底下應該給出不同的資格判斷：
    * today 落在達標之後 → 確實往下走到 DB/email 邏輯（不被 REASON_DAYS_NOT_REACHED 擋下）。
    * today 落在達標之前 → 確實被 REASON_DAYS_NOT_REACHED 擋下、且完全不呼叫 conn_factory。
    """
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    recipient = _make_recipient(days="14", borrow_date="2026-07-01")

    # 情境一：today 落在達標之後（起算 19 天）→ 確實走到 DB/email 邏輯。
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender_reached = RecordingEmailSender()
    result_reached = send_trial_report(
        recipient,
        conn_factory=lambda: conn,
        email_sender=sender_reached,
        audit_log=_make_audit(tmp_path),
        today=date(2026, 7, 20),
    )
    assert result_reached.success is True
    assert result_reached.reason is None
    assert len(sender_reached.calls) == 1

    # 情境二：同一筆 recipient，today 改落在達標之前（起算 3 天）→ 擋下，不碰 DB。
    conn_factory_called: list[bool] = []

    def _conn_factory_not_reached():
        conn_factory_called.append(True)
        raise AssertionError("未達標時不該呼叫 conn_factory")

    sender_not_reached = RecordingEmailSender()
    result_not_reached = send_trial_report(
        recipient,
        conn_factory=_conn_factory_not_reached,
        email_sender=sender_not_reached,
        audit_log=_make_audit(tmp_path),
        today=date(2026, 7, 4),
    )
    assert result_not_reached.success is False
    assert result_not_reached.reason == REASON_DAYS_NOT_REACHED
    assert conn_factory_called == []
    assert sender_not_reached.calls == []


# --------------------------------------------------------------------------- #
# 3. 裝置數 = 0
# --------------------------------------------------------------------------- #


def test_blocks_when_zero_devices(tmp_path: Path) -> None:
    conn = FakeConnection(devices=[])
    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_NO_DEVICE
    assert sender.calls == []
    assert conn.closed is True


# --------------------------------------------------------------------------- #
# 4. 裝置數 >= 2（多裝置）
# --------------------------------------------------------------------------- #


def test_blocks_when_multiple_devices_with_clear_reason(tmp_path: Path) -> None:
    conn = FakeConnection(
        devices=[
            {"device_id": 1, "mac_address": "AA", "alias": "一樓教室"},
            {"device_id": 2, "mac_address": "BB", "alias": "二樓教室"},
        ]
    )
    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_MULTIPLE_DEVICES
    assert "多裝置" in result.message or "手動" in result.message
    assert sender.calls == []


# --------------------------------------------------------------------------- #
# 5. email 為空
# --------------------------------------------------------------------------- #


def test_blocks_when_email_is_empty(tmp_path: Path) -> None:
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": None},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_NO_EMAIL
    assert sender.calls == []


def test_blocks_when_user_row_missing_entirely(tmp_path: Path) -> None:
    """fetch_user_info 查無此人（回傳 None）時同樣視為沒有 email，不 crash。"""
    conn = FakeConnection(devices=_make_devices(), user_row=None, telemetry_rows=_make_telemetry_rows())
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_NO_EMAIL


# --------------------------------------------------------------------------- #
# 6. 14 天內 telemetry 0 筆
# --------------------------------------------------------------------------- #


def test_blocks_when_no_telemetry(tmp_path: Path) -> None:
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=[],
    )
    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_NO_TELEMETRY
    assert sender.calls == []
    # email 已經查到，稽核仍要記下遮罩後的 email（見稽核相關測試），
    # 但這裡先確認不因為有 email 就繼續往下寄信。
    assert result.email_masked == mask_email("a@example.com")


# --------------------------------------------------------------------------- #
# 7. PDF 產不出來（CJK 字型偵測失敗）→ 降級成純文字信，仍要寄出去
# --------------------------------------------------------------------------- #


def test_downgrades_to_plain_text_when_cjk_font_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is True
    assert result.pdf_attached is False
    assert len(sender.calls) == 1
    assert sender.calls[0]["attachments"] == []


# --------------------------------------------------------------------------- #
# 8. 稽核記錄（成功／失敗皆要寫入，email 不可明碼）
# --------------------------------------------------------------------------- #


def test_audit_record_written_on_success_without_plaintext_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "chenxiaoqi@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    audit = _make_audit(tmp_path)
    send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(),
        audit_log=audit,
    )
    records = _read_audit_records(audit)
    assert len(records) == 1
    assert records[0]["success"] is True
    raw_text = audit.path.read_text(encoding="utf-8")
    assert "chenxiaoqi@example.com" not in raw_text
    assert "ch***@example.com" in raw_text


def test_audit_record_written_on_failure(tmp_path: Path) -> None:
    """借機日 2026-07-01 起算 1 天（注入 today=2026-07-02），未達標 → 擋下留稽核。"""
    audit = _make_audit(tmp_path)
    send_trial_report(
        _make_recipient(days="14"),
        conn_factory=lambda: (_ for _ in ()).throw(AssertionError("不該呼叫")),
        email_sender=RecordingEmailSender(),
        audit_log=audit,
        today=date(2026, 7, 2),
    )
    records = _read_audit_records(audit)
    assert len(records) == 1
    assert records[0]["success"] is False
    assert records[0]["reason"] == REASON_DAYS_NOT_REACHED


def test_audit_log_survives_missing_parent_directory(tmp_path: Path) -> None:
    """稽核檔目錄不存在時（AuditLog 內部會自動 mkdir）不 crash，且結果照常回傳。"""
    audit = AuditLog(tmp_path / "not-yet-created" / "trial-report-audit.jsonl", fsync=False)
    result = send_trial_report(
        _make_recipient(days="14"),
        conn_factory=lambda: (_ for _ in ()).throw(AssertionError("不該呼叫")),
        email_sender=RecordingEmailSender(),
        audit_log=audit,
        today=date(2026, 7, 2),
    )
    assert result.success is False
    assert audit.path.exists()


# --------------------------------------------------------------------------- #
# 9. Recipient.id 解析 acfh user_id 失敗
# --------------------------------------------------------------------------- #


def test_blocks_when_recipient_id_unparseable_without_touching_db(tmp_path: Path) -> None:
    conn_factory_called = []

    def _conn_factory():
        conn_factory_called.append(True)
        raise AssertionError("不該呼叫 conn_factory")

    result = send_trial_report(
        _make_recipient(recipient_id="not-a-valid-id"),
        conn_factory=_conn_factory,
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_INVALID_RECIPIENT_ID
    assert conn_factory_called == []


# --------------------------------------------------------------------------- #
# 邊界／整合案例
# --------------------------------------------------------------------------- #


def test_db_connection_error_returns_db_error_reason(tmp_path: Path) -> None:
    def _boom():
        raise ConnectionError("connect timed out")

    sender = RecordingEmailSender()
    result = send_trial_report(
        _make_recipient(),
        conn_factory=_boom,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_DB_ERROR
    assert sender.calls == []


def test_db_query_error_after_connection_still_closes_connection(tmp_path: Path) -> None:
    conn = FakeConnection(raise_on_execute=RuntimeError("table locked"))
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_DB_ERROR
    assert conn.closed is True


def test_email_sender_exception_returns_send_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender(error=OSError("smtp connection reset"))
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_SEND_ERROR


def test_email_sender_returns_false_is_treated_as_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """email_sender 沒有拋例外、只是回傳 False（例如 SMTP 帳密錯）也要算失敗。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(result=False),
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is False
    assert result.reason == REASON_SEND_ERROR


# --------------------------------------------------------------------------- #
# _send_real_gmail：sendmail() 回傳值必須被檢查（code-reviewer MUST_FIX）
# --------------------------------------------------------------------------- #


def test_send_real_gmail_returns_false_when_customer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """smtp.sendmail() 沒拋例外、只是在回傳的 dict 裡把客戶本人列為被拒收 →
    _send_real_gmail 必須回傳 False，不能因為「SMTP 交握沒出錯」就當成功。

    這是 code-reviewer 的 MUST_FIX：Python 官方行為是只有全部收件人都被拒才會
    拋 SMTPRecipientsRefused，只要 Bcc（固定有內部信箱）成功，sendmail() 就正常
    return 一個 {被拒地址: (code, msg)} 的 dict，過去這個 dict 被完全丟棄。
    """
    monkeypatch.setattr(
        trial_report, "get_gmail_credentials", lambda: ("bot@example.com", "app-pw")
    )
    fake_client = FakeSMTPClient(refused={"customer@example.com": (550, b"Mailbox unavailable")})
    monkeypatch.setattr(trial_report.smtplib, "SMTP_SSL", lambda *a, **k: fake_client)

    ok = trial_report._send_real_gmail(
        "subject", "body", ["customer@example.com"], bcc=["staff@example.com"], attachments=[]
    )

    assert ok is False
    # 確實嘗試過寄送（不是提早 return 而完全沒呼叫 sendmail）。
    assert len(fake_client.sendmail_calls) == 1


def test_send_real_gmail_returns_true_when_only_bcc_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有 Bcc（內部信箱）被拒收、客戶本人成功 → 仍視為成功。

    這是本次修復刻意選擇的行為：客戶本人（to）收到報告才是這個功能存在的理由，
    內部密件副本收不到是次要問題（已經用 logger.error 留痕，見 _send_real_gmail），
    不該讓一個內部信箱打錯字就讓客戶端看到「寄送失敗」。
    """
    monkeypatch.setattr(
        trial_report, "get_gmail_credentials", lambda: ("bot@example.com", "app-pw")
    )
    fake_client = FakeSMTPClient(refused={"staff@example.com": (550, b"Mailbox unavailable")})
    monkeypatch.setattr(trial_report.smtplib, "SMTP_SSL", lambda *a, **k: fake_client)

    ok = trial_report._send_real_gmail(
        "subject", "body", ["customer@example.com"], bcc=["staff@example.com"], attachments=[]
    )

    assert ok is True


def test_send_real_gmail_returns_true_when_nothing_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendmail() 回傳空 dict（沒有任何人被拒收）→ 正常成功，不受本次修復影響。"""
    monkeypatch.setattr(
        trial_report, "get_gmail_credentials", lambda: ("bot@example.com", "app-pw")
    )
    fake_client = FakeSMTPClient(refused={})
    monkeypatch.setattr(trial_report.smtplib, "SMTP_SSL", lambda *a, **k: fake_client)

    ok = trial_report._send_real_gmail(
        "subject", "body", ["customer@example.com"], bcc=(), attachments=[]
    )

    assert ok is True


def test_send_trial_report_end_to_end_fails_when_default_sender_refuses_customer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整合測試：走 send_trial_report 的真正預設 email_sender（_send_real_gmail），
    確認客戶被拒收時整條路徑最終回報失敗，而不是在某一層被吞掉變成成功。
    """
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    monkeypatch.setattr(
        trial_report, "get_gmail_credentials", lambda: ("bot@example.com", "app-pw")
    )
    fake_client = FakeSMTPClient(refused={"a@example.com": (550, b"no such user")})
    monkeypatch.setattr(trial_report.smtplib, "SMTP_SSL", lambda *a, **k: fake_client)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )

    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=None,  # 不注入假 sender，故意走真正的 _send_real_gmail
        audit_log=_make_audit(tmp_path),
    )

    assert result.success is False
    assert result.reason == REASON_SEND_ERROR
    assert len(fake_client.sendmail_calls) == 1


def test_empty_staff_bcc_sends_only_to_customer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender()
    send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        staff_bcc=(),
        audit_log=_make_audit(tmp_path),
    )
    assert sender.calls[0]["to"] == ["a@example.com"]
    assert sender.calls[0]["bcc"] == []


def test_multiple_staff_bcc_all_included(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender()
    staff = ("peter@addwii.com", "kerwin@addwii.com", "nicole@addwii.com")
    send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        staff_bcc=staff,
        audit_log=_make_audit(tmp_path),
    )
    assert sender.calls[0]["bcc"] == list(staff)


def test_staff_bcc_equal_to_customer_email_is_deduped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bcc 名單裡剛好等於客戶本人 email（不分大小寫）時要去重，不寄兩封給同一人。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(),
    )
    sender = RecordingEmailSender()
    send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=sender,
        staff_bcc=("A@EXAMPLE.COM", "staff@example.com"),
        audit_log=_make_audit(tmp_path),
    )
    assert sender.calls[0]["to"] == ["a@example.com"]
    assert sender.calls[0]["bcc"] == ["staff@example.com"]


def test_large_telemetry_volume_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """幾千筆 telemetry 資料（14 天、每分鐘一筆的量級）分析與寄送都要能跑完，不 crash。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: False)
    conn = FakeConnection(
        devices=_make_devices(),
        user_row={"first_name": "琪", "last_name": "陳", "email": "a@example.com"},
        telemetry_rows=_make_telemetry_rows(n=4000),
    )
    result = send_trial_report(
        _make_recipient(),
        conn_factory=lambda: conn,
        email_sender=RecordingEmailSender(),
        audit_log=_make_audit(tmp_path),
    )
    assert result.success is True


def test_analyze_and_build_pdf_do_not_raise_on_normal_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyze()/build_pdf() 搬過來後對正常資料要能跑完、產出非空文字與真的 PDF 檔。"""
    monkeypatch.setattr(trial_report, "setup_cjk_font", lambda: True)
    rows = _make_telemetry_rows()
    report_text, summary = trial_report.analyze(rows, TRIAL_REPORT_HOURS)
    assert report_text
    assert summary
    assert "1. PM2.5" in report_text

    pdf_path = tmp_path / "out.pdf"
    ok = trial_report.build_pdf(rows, report_text, str(pdf_path), "測試用戶", "AA:BB:CC")
    assert ok is True
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


def test_mask_email_normal_local_part() -> None:
    assert mask_email("chenxiaoqi@example.com") == "ch***@example.com"


def test_mask_email_short_local_part_is_fully_masked() -> None:
    assert mask_email("ab@example.com") == "**@example.com"
    assert mask_email("a@example.com") == "*@example.com"


def test_mask_email_defensive_on_missing_at_sign() -> None:
    """防禦性案例：不含 @ 的髒輸入（不該發生於已驗證資料）也不能 crash，且要全遮。"""
    assert mask_email("not-an-email") == "*" * len("not-an-email")


def test_recipient_acfh_user_id_valid_and_invalid_formats() -> None:
    assert Recipient(
        id="u46", name="x", phone=None, device="", borrow_date="", match_status="ok"
    ).acfh_user_id == 46
    for bad_id in ("46", "user46", "u", "u4a", "", "U46"):
        assert (
            Recipient(id=bad_id, name="x", phone=None, device="", borrow_date="", match_status="ok").acfh_user_id
            is None
        )
