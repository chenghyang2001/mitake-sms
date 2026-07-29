"""三竹簡訊 Web 發送介面（stdlib ``http.server``，零外部依賴）。

**這是本專案唯一會花錢的介面。** 每則簡訊扣 1 點（中文超過 70 字倍增），
點數與 App 團隊共用 —— App 靠同一池發註冊驗證碼，扣光就是害別人的功能掛掉。
所以本檔的每個決策都服從同一條原則：**寧可讓使用者多按一次，也不要讓他多花一點。**

端點::

    GET  /          表單頁（即時試算則數與扣點）
    POST /preview   確認頁（顯示解析後號碼、則數、扣點，發一張一次性 token）
    POST /send      實際發送（必須帶有效 token，用完立刻作廢）
    GET  /status    查投遞狀態（唯讀、免費、不扣點；帶 ?msgid= 才查，否則出表單）
    GET  /health    健康檢查（不需認證，只回服務活著與否）

設計重點（每一條都對應一種真金白銀的失誤）：

1. **二階段送出**：``send_sms`` 不可逆且花錢，絕不接受「一個按鈕直接送出」。
   確認頁的 token 存在伺服器端，號碼與內容不隨表單來回 —— 否則使用者能在
   看過確認頁之後改掉收件人，確認頁就白做了。
2. **一次性 token**：``/send`` 驗過就立刻從 store 移除。使用者按瀏覽器上一頁
   再送一次會拿到「此請求已處理過」，而不是第二封簡訊。比對用
   :func:`secrets.compare_digest`，避免用比對時間反推 token。
3. **失敗分兩種畫面**：依 ``MitakeAPIError.possibly_charged``
   （見 HANDOFF §2.1）。``False`` → 「沒扣點，可安全重試」＋重送按鈕；
   ``True`` → 「請勿重送，拿 msgid 去後台查證」＋**頁面上沒有任何送出表單**。
   一律顯示「發送失敗，請重試」會直接誘導重送，結果是扣兩點、對方收到兩封。
   注意 ``possibly_charged`` **只有 MitakeAPIError 有**，
   ``MitakeValidationError`` / ``MitakeConfigError`` 沒有，所以本檔分開 except，
   不寫成 ``except MitakeError as e: e.possibly_charged``（那會 AttributeError）。
4. **預設只綁 127.0.0.1**：要對外必須同時給 ``--host`` 與 ``--allow-public``，
   讓「我要把發簡訊的介面開到公網」變成一個寫得出來的動作。上線順序見 README：
   先 localhost → 再 Cloudflare Access → 最後才開 tunnel ingress。
5. **速率上限**：模組層的 ``max_segments`` 只擋單次，擋不住連按。這裡再加一層
   「每小時 N 則」的滑動視窗，且在 ``/preview`` 就擋下，不讓人走到確認頁才發現。
   視窗只活在記憶體裡，所以啟動時會從稽核檔回填（見
   :meth:`SmsWebApp.restore_rate_limit_from_audit`）—— 否則一次 ``restart``
   就把當小時的預算清成零，而部署本來就會 restart。
6. **應用層的第二道防線**：綁 loopback 只擋得住「直接連過來」的人，擋不住
   tunnel（它就是從 localhost 連進來的）。設了
   ``MITAKE_WEB_REQUIRE_ACCESS_EMAIL`` 之後，除 ``/health`` 外都會檢查
   Cloudflare Access 注入的身分標頭。**這不是真正的認證**（標頭可偽造），
   它的作用是把「Access 忘了設 / 設錯 / tunnel 先開」從「安靜地被人拿去發簡訊」
   變成「明顯地壞掉（403）」—— 詳見 :meth:`SmsWebApp._deny_without_access_email`。

執行::

    python3.12 -m web.server                 # 綁 127.0.0.1:8766
    python3.12 -m web.server --dry-run       # 只建物件不監聽（部署前的設定檢查）

環境變數：三竹憑證由 :mod:`mitake` 自己讀（``MITAKE_USERNAME`` / ``MITAKE_PASSWORD``），
本檔一律不碰、不記錄、不顯示。其餘可調參數見 :func:`build_arg_parser`。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 讓 `python3.12 web/server.py`、`python3.12 -m web.server`、pytest 三種載入方式
# 都找得到 repo 根的 mitake.py（模組零依賴，直接 import 即可）。
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import mitake  # noqa: E402  （必須等 sys.path 補完才 import）

from web import templates  # noqa: E402
from web.audit import DEFAULT_TAIL_LINES, AuditLog, mask_phone  # noqa: E402

__all__ = [
    "ACCESS_EMAIL_HEADER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_RATE_LIMIT_SEGMENTS",
    "DEFAULT_STATUS_QUERY_LIMIT",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "STATUS_QUERY_WINDOW_SECONDS",
    "PendingSend",
    "RateLimitExceededError",
    "RateLimiter",
    "RateSnapshot",
    "Response",
    "SmsWebApp",
    "StatusQueryThrottle",
    "StatusQueryThrottledError",
    "TokenExpiredError",
    "TokenStore",
    "TokenUnknownError",
    "build_arg_parser",
    "create_server",
    "main",
    "make_handler",
]

logger = logging.getLogger("mitake.web")

# --------------------------------------------------------------------------- #
# 常數
# --------------------------------------------------------------------------- #

# 預設只綁 loopback。這個值**不可以**改成 0.0.0.0：對外暴露必須是使用者顯式
# 傳入 --host 且加上 --allow-public 的結果，不能是預設值悄悄造成的。
DEFAULT_HOST = "127.0.0.1"

# 8123 / 8124 / 8765 在同一台 VPS 上已被 langgraph ch21 / ch25 / pm25-linebot 佔用。
DEFAULT_PORT = 8766

# 確認頁的有效期。太短會讓「打完字去接個電話再回來按送出」失敗（然後整段重打，
# 反而更容易出錯）；太長則讓一張沒用掉的 token 在共用電腦上放到隔天還能發簡訊。
DEFAULT_TOKEN_TTL_SECONDS = 600

# 每小時可送出的**則數**（不是次數）—— 計費單位是則，用次數當上限的話，
# 5 則的長簡訊連按 20 次就是 100 點。
DEFAULT_RATE_LIMIT_SEGMENTS = 20
RATE_LIMIT_WINDOW_SECONDS = 3600

# 投遞狀態查詢的節流：每 5 分鐘 30 次。這是**完全獨立的另一組計數**，
# 與上面的發送則數上限沒有任何往來（理由見 StatusQueryThrottle 的 docstring）。
# 30 次 / 5 分鐘對人來說綽綽有餘（平均每 10 秒一次），但擋得住放著自動重整的分頁。
DEFAULT_STATUS_QUERY_LIMIT = 30
STATUS_QUERY_WINDOW_SECONDS = 300

# 未使用的 token 上限。/preview 不消耗發送額度（它還沒花錢），所以有人不斷重整
# 確認頁時 token 會一直累積；設個上限讓記憶體用量有界，滿了就淘汰最舊的。
MAX_LIVE_TOKENS = 256

# 表單 body 的位元組上限。5 則 = 350 字，UTF-8 最多約 1 KB，再 URL-encode 撐三倍
# 也遠低於這個數；設 64 KB 是為了擋「把整份文件貼進來」造成的記憶體浪費，
# 真正的成本護欄是 max_segments。
MAX_REQUEST_BODY_BYTES = 64 * 1024

SERVER_VERSION = "MitakeWeb/1.0"

ENV_HOST = "MITAKE_WEB_HOST"
ENV_PORT = "MITAKE_WEB_PORT"
ENV_RATE_LIMIT = "MITAKE_WEB_RATE_LIMIT"
ENV_TOKEN_TTL = "MITAKE_WEB_TOKEN_TTL"
ENV_MAX_SEGMENTS = "MITAKE_WEB_MAX_SEGMENTS"
ENV_LOG_LEVEL = "MITAKE_WEB_LOG_LEVEL"
ENV_REQUIRE_ACCESS_EMAIL = "MITAKE_WEB_REQUIRE_ACCESS_EMAIL"
ENV_STATUS_QUERY_LIMIT = "MITAKE_WEB_STATUS_QUERY_LIMIT"

# Cloudflare Access 通過認證後注入的身分標頭。**這個值可以被偽造**（真正可驗的是
# Cf-Access-Jwt-Assertion 的簽章，但驗它要拉 JWKS＝破壞零外部依賴），
# 用途說明見 SmsWebApp._deny_without_access_email。
ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# CSP nonce 的位元組數。16 bytes = 128 bit，遠超過「猜中一個一次性隨機值」所需。
CSP_NONCE_BYTES = 16

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


# --------------------------------------------------------------------------- #
# 速率上限
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RateSnapshot:
    """目前視窗內的用量快照（給表單頁顯示「本小時已送 X / Y 則」用）。"""

    used: int
    limit: int
    window_seconds: int


class RateLimitExceededError(Exception):
    """超過單位時間的則數上限。``retry_after`` 為 None 代表「這筆再怎麼等都送不出去」。"""

    def __init__(
        self, message: str, *, used: int, limit: int, retry_after: float | None
    ) -> None:
        super().__init__(message)
        self.used = used
        self.limit = limit
        self.retry_after = retry_after


class RateLimiter:
    """滑動視窗的則數上限。

    用 monotonic 時鐘而非牆鐘：系統時間被 NTP 往回調（VPS 開機初期很常見）時，
    牆鐘版本會讓整個視窗的紀錄看起來「還沒發生」，上限等於失效。

    ``reserve`` / ``release`` 是成對的：發送前先扣額度，確認**沒扣點**的失敗才退還。
    順序刻意是「先扣再送」——反過來（送完成功才扣）會讓同時湧入的請求都通過檢查，
    上限形同虛設；而多扣的那次只要失敗確定沒花錢就會退回來，代價只是保守。
    """

    def __init__(
        self,
        limit: int,
        *,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError(f"速率上限必須 >= 1，收到 {limit}")
        if window_seconds < 1:
            raise ValueError(f"視窗長度必須 >= 1 秒，收到 {window_seconds}")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # 每筆是 [issued_at, cost, reservation_id]，依 issued_at 遞增（monotonic 保證）。
        self._events: list[list[float]] = []
        self._next_id = 1

    # -- 內部：全部假設呼叫端已持有 self._lock ------------------------------- #

    def _prune(self, now: float) -> None:
        self._events = [e for e in self._events if now - e[0] < self._window]

    def _used(self) -> int:
        return int(sum(event[1] for event in self._events))

    def _retry_after(self, cost: int, now: float) -> float | None:
        """還要等幾秒才塞得下 ``cost`` 則。塞不下（cost 本身就超過上限）回 None。"""
        freed = 0
        used = self._used()
        for issued_at, event_cost, _ in self._events:
            freed += int(event_cost)
            if used - freed + cost <= self._limit:
                return max(0.0, issued_at + self._window - now)
        return None

    def _check_locked(self, cost: int, now: float) -> None:
        self._prune(now)
        used = self._used()
        if used + cost <= self._limit:
            return
        retry_after = self._retry_after(cost, now)
        raise RateLimitExceededError(
            f"本小時已送出 {used} 則，再送 {cost} 則會超過上限 {self._limit} 則。",
            used=used,
            limit=self._limit,
            retry_after=retry_after,
        )

    # -- 對外 ---------------------------------------------------------------- #

    @property
    def window_seconds(self) -> int:
        return self._window

    def snapshot(self) -> RateSnapshot:
        with self._lock:
            self._prune(self._clock())
            return RateSnapshot(
                used=self._used(), limit=self._limit, window_seconds=self._window
            )

    def check(self, cost: int) -> None:
        """只檢查不佔用（``/preview`` 用）。超過就丟 :class:`RateLimitExceededError`。"""
        with self._lock:
            self._check_locked(cost, self._clock())

    def reserve(self, cost: int) -> int:
        """檢查並佔用額度，回傳可用於 :meth:`release` 的預約編號。"""
        with self._lock:
            now = self._clock()
            self._check_locked(cost, now)
            reservation_id = self._next_id
            self._next_id += 1
            self._events.append([now, float(cost), float(reservation_id)])
            return reservation_id

    def release(self, reservation_id: int) -> None:
        """退還額度。只在**確定沒扣點**的失敗路徑呼叫；不確定就不退（保守）。"""
        with self._lock:
            self._events = [e for e in self._events if int(e[2]) != reservation_id]

    def seed(self, cost: int, age_seconds: float) -> bool:
        """補進一筆「發生在 ``age_seconds`` 秒前」的用量，回傳是否採計。

        只給重啟後回填用（見 :meth:`SmsWebApp.restore_rate_limit_from_audit`）。
        刻意**不做上限檢查**：回填的是已經發生的事實，超過上限只代表接下來要等，
        不是拒絕記錄的理由 —— 拒絕記錄反而正好還原了「重啟即歸零」這個破口。
        """
        if cost < 1:
            return False
        with self._lock:
            if age_seconds < 0 or age_seconds >= self._window:
                return False
            now = self._clock()
            self._events.append(
                [now - float(age_seconds), float(cost), float(self._next_id)]
            )
            self._next_id += 1
            # _prune / _retry_after 都假設 _events 依 issued_at 遞增（正常路徑由
            # monotonic 保證）。回填是唯一會插入「過去時間」的入口，插完必須重排，
            # 否則 _retry_after 會算出偏短的等待秒數，讓人白等一輪再撞一次上限。
            self._events.sort(key=lambda event: event[0])
            return True


# --------------------------------------------------------------------------- #
# 投遞狀態查詢的節流（與發送額度完全分離）
# --------------------------------------------------------------------------- #


class StatusQueryThrottledError(Exception):
    """投遞狀態查得太頻繁。``retry_after`` 是還要等幾秒（``None`` 代表算不出來）。"""

    def __init__(
        self, message: str, *, used: int, limit: int, retry_after: float | None
    ) -> None:
        super().__init__(message)
        self.used = used
        self.limit = limit
        self.retry_after = retry_after


class StatusQueryThrottle:
    """投遞狀態查詢的滑動視窗節流器（計「次數」）。

    **刻意不重用** :class:`RateLimiter`。那個計的是**發送則數**，也就是真金白銀的
    計費單位；兩者若共用一個計數器，查一次免費的狀態就會吃掉一則簡訊的預算 ——
    使用者查了幾次投遞狀態，結果發不出簡訊，這是最不該有的耦合。反過來（發簡訊
    吃掉查詢次數）一樣荒謬。所以這裡另開一個小類別，而不是傳一個 ``cost=0``
    之類的旗標去污染那支已經在跑的程式。

    那查詢既然免費，為什麼還要節流？因為它**每次都會對三竹發一個真實的 HTTP 請求**。
    一個放著自動重整的分頁、或按住 F5 的人，就會讓我們的來源 IP 對三竹持續打點；
    三竹若因此限流或封鎖這個 IP，**連發簡訊都會一起壞掉**（statuscode=k），
    而那個帳號是與 App 團隊共用的。節流保的是發送能力，不是查詢的錢。

    與 :class:`RateLimiter` 同樣用 monotonic 時鐘（NTP 往回調不會讓限制失效），
    同樣自帶 :class:`threading.Lock`（``ThreadingHTTPServer`` 會併發呼叫）。
    """

    def __init__(
        self,
        limit: int = DEFAULT_STATUS_QUERY_LIMIT,
        *,
        window_seconds: int = STATUS_QUERY_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError(f"查詢次數上限必須 >= 1，收到 {limit}")
        if window_seconds < 1:
            raise ValueError(f"視窗長度必須 >= 1 秒，收到 {window_seconds}")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: list[float] = []

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window

    @property
    def used(self) -> int:
        """目前視窗內已用掉的次數（測試與除錯用）。"""
        with self._lock:
            now = self._clock()
            self._hits = [hit for hit in self._hits if now - hit < self._window]
            return len(self._hits)

    def acquire(self) -> None:
        """佔用一次查詢額度；超過就丟 :class:`StatusQueryThrottledError`。

        沒有 ``release``：與發送不同，查詢失敗也已經對三竹發過請求了，
        而這裡要限的正是「對三竹發了幾次請求」。退還額度等於放行重試風暴。
        """
        with self._lock:
            now = self._clock()
            self._hits = [hit for hit in self._hits if now - hit < self._window]
            if len(self._hits) >= self._limit:
                retry_after = max(0.0, self._hits[0] + self._window - now)
                raise StatusQueryThrottledError(
                    f"最近 {self._window // 60} 分鐘內已查詢 {len(self._hits)} 次，"
                    f"超過上限 {self._limit} 次。",
                    used=len(self._hits),
                    limit=self._limit,
                    retry_after=retry_after,
                )
            self._hits.append(now)


# --------------------------------------------------------------------------- #
# 一次性 token
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PendingSend:
    """確認頁背後的待送內容。存在伺服器端，不隨表單來回。"""

    phone: str  # 已經過 validate_phone 正規化的 10 碼
    body: str
    segments: int
    chars: int
    issued_at: float


class TokenError(Exception):
    """token 不可用的基底例外。"""


class TokenUnknownError(TokenError):
    """token 不存在：沒發過、已經用掉了，或是早就過期被清掉。"""


class TokenExpiredError(TokenError):
    """token 存在但已超過 TTL。"""


class TokenStore:
    """一次性 token 的保管處（記憶體內，重啟即清空 —— 這是刻意的）。

    重啟後所有確認頁失效，使用者最多重填一次；反過來把 token 持久化，
    才會出現「服務重啟後，昨天那張沒用掉的確認頁還能發簡訊」這種鬼故事。

    比對用 :func:`secrets.compare_digest` 逐一比，而不是直接 ``dict[token]``：
    dict 查找的耗時與 key 內容相關，理論上可用來一位一位試出有效 token。
    token 有 256 bit 熵、TTL 只有幾分鐘，實務上難以利用，但這行成本是零。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS,
        max_tokens: int = MAX_LIVE_TOKENS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"token TTL 必須 > 0，收到 {ttl_seconds}")
        if max_tokens < 1:
            raise ValueError(f"token 上限必須 >= 1，收到 {max_tokens}")
        self._ttl = float(ttl_seconds)
        self._max_tokens = max_tokens
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, PendingSend] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._tokens.items() if now - value.issued_at > self._ttl]
        for key in expired:
            del self._tokens[key]

    def issue(self, *, phone: str, body: str, segments: int, chars: int) -> str:
        """產生一次性 token 並記住待送內容。"""
        now = self._clock()
        # 32 bytes = 256 bit 熵。urlsafe 才能安全塞進 HTML 屬性與表單欄位。
        token = secrets.token_urlsafe(32)
        pending = PendingSend(
            phone=phone, body=body, segments=segments, chars=chars, issued_at=now
        )
        with self._lock:
            self._purge(now)
            while len(self._tokens) >= self._max_tokens:
                oldest = min(self._tokens, key=lambda key: self._tokens[key].issued_at)
                evicted = self._tokens.pop(oldest)
                # 被淘汰的那張確認頁按下送出時，看到的會是「此請求已處理過，或這張
                # 確認頁已被較新的取代」的 409 —— 而使用者當下多半想不到自己開過
                # 256 張確認頁。這行是事後唯一能把兩件事對起來的線索，所以要記到
                # 足以比對的程度（發出多久、給誰、幾則），不能只說「淘汰了一張」。
                logger.warning(
                    "待確認 token 已達上限 %s，淘汰最舊的一張："
                    "發出後 %.0f 秒、號碼 %s、%s 則、%s 字。"
                    "該確認頁按下送出只會看到 409，不會送出簡訊、不會扣點。",
                    self._max_tokens,
                    now - evicted.issued_at,
                    mask_phone(evicted.phone),
                    evicted.segments,
                    evicted.chars,
                )
            self._tokens[token] = pending
        return token

    def consume(self, token: object) -> PendingSend:
        """驗證並**立即作廢** token，回傳待送內容。

        不論成功或過期都會把它從 store 移除：留著只會讓「已經用過」與
        「還能再用一次」的界線變模糊，而模糊的那一邊就是多送一封簡訊。
        """
        now = self._clock()
        # compare_digest 對含非 ASCII 的 str 會丟 TypeError，先擋掉（使用者可以隨手
        # 在網址列塞中文）。這類輸入本來就不可能是我們發出的 token。
        if not isinstance(token, str) or not token or not token.isascii():
            raise TokenUnknownError("確認碼不存在或已作廢。")

        with self._lock:
            matched: str | None = None
            for key in self._tokens:
                if secrets.compare_digest(key, token):
                    matched = key
                    break
            if matched is None:
                self._purge(now)
                raise TokenUnknownError("確認碼不存在或已作廢。")
            pending = self._tokens.pop(matched)

        if now - pending.issued_at > self._ttl:
            raise TokenExpiredError(
                f"確認頁已超過 {int(self._ttl)} 秒未送出，為安全起見已失效。"
            )
        return pending


# --------------------------------------------------------------------------- #
# 回應物件
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Response:
    """一個完整的 HTTP 回應。刻意與 :mod:`http.server` 解耦，讓路由層可單獨測試。

    ``nonce`` 是這個回應的 CSP script nonce（沒有 ``<script>`` 的頁面為 ``None``）。
    它必須跟著回應走：產生頁面的是路由層、送出標頭的是 HTTP 層，而兩邊的值
    **一定要一模一樣**，不然不是腳本被擋掉、就是 CSP 形同虛設。讓它變成回應的
    一個欄位，是唯一能保證兩邊不會各自產生一份的做法。
    """

    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    nonce: str | None = None

    @property
    def text(self) -> str:
        """回應內容的文字形式（測試用）。"""
        return self.body.decode("utf-8", errors="replace")


def _html_response(status: int, markup: str, *, nonce: str | None = None) -> Response:
    return Response(status=int(status), body=markup.encode("utf-8"), nonce=nonce)


def _new_nonce() -> str:
    """產生一組**只給這一個回應用**的 CSP nonce。

    重用 nonce 等於把它變成一個可預測的常數：攻擊者讀一次頁面就知道該在注入的
    ``<script>`` 上補哪個值，整道 CSP 就退回 ``'unsafe-inline'`` 的等級。
    """
    return secrets.token_urlsafe(CSP_NONCE_BYTES)


def _csp_header(nonce: str | None) -> str:
    """組出這個回應的 Content-Security-Policy。

    ``script-src`` 用 nonce 而**不是** ``'unsafe-inline'``：帶 ``'unsafe-inline'``
    時，被注入的 ``<script>`` 照樣會執行 —— 也就是說，這條 CSP 對「阻止腳本執行」
    這件事的價值是零，而那正是我們要它擋的東西（頁面已全面 ``html.escape``，
    CSP 的定位本來就是「萬一漏掉一處時的第二道牆」）。

    改 nonce 的成本幾乎是零：頁面上唯一的腳本是完全靜態的即時試算（參數走
    ``data-*`` 屬性，沒有任何值被插進 ``<script>`` 裡）。沒有腳本的頁面連 nonce
    都不發，直接給 ``'none'``（更嚴）。

    ``style-src`` 維持 ``'unsafe-inline'``：樣式是一段寫死的常數，且注入 CSS 造成
    不了這裡在意的損害（發簡訊要走 form-action，那條已經鎖成 ``'self'``）。
    其餘 ``default-src 'none'`` / ``form-action 'self'`` / ``base-uri 'none'`` /
    ``frame-ancestors 'none'`` 都是實質有效的限制，維持原樣。
    """
    script_src = f"'nonce-{nonce}'" if nonce else "'none'"
    return (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src {script_src}; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


def _access_email_matches(actual: str, expected: str) -> bool:
    """大小寫不敏感地比對 Access 身分標頭。

    比對用 :func:`secrets.compare_digest`（同 token 比對的理由：不讓比對耗時洩漏
    內容）。先 ``encode`` 成 bytes 再比 —— ``compare_digest`` 對含非 ASCII 的
    ``str`` 會直接丟 ``TypeError``，而這個值是外部輸入，塞什麼進來都有可能。
    """
    left = actual.strip().casefold().encode("utf-8")
    right = expected.strip().casefold().encode("utf-8")
    return bool(right) and secrets.compare_digest(left, right)


def _first(form: Mapping[str, Sequence[str]], key: str) -> str:
    """取表單欄位的第一個值；缺欄位回空字串。

    回空字串而非 None，是因為下游一律當字串處理（``validate_phone`` 會給出
    比 ``AttributeError`` 好懂得多的錯誤訊息）。
    """
    values = form.get(key)
    if not values:
        return ""
    value = values[0]
    return value if isinstance(value, str) else str(value)


def _normalize_newlines(text: str) -> str:
    """把 CRLF / CR 統一成 LF。

    瀏覽器送出 ``<textarea>`` 時會把換行正規化成 CRLF（HTML 規範要求）。不處理的話
    使用者看到的「70 字」到伺服器端會變成 71 字以上 —— 直接多扣一點，而且確認頁上
    的數字會跟他自己數的對不起來。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _format_wait(seconds: float | None) -> str:
    """把等待秒數講成人看得懂的話。"""
    if seconds is None:
        return "這筆內容的則數本身就超過上限，等多久都送不出去，請縮短內容。"
    if seconds < 60:
        return f"請等待約 {int(seconds) + 1} 秒後再試。"
    return f"請等待約 {int(seconds // 60) + 1} 分鐘後再試。"


def _charged_segments_from_audit_line(
    line: str, reference: datetime
) -> tuple[int, float] | None:
    """從一行稽核紀錄取出 ``(則數, 距今幾秒)``；不是「已計費的 result」就回 ``None``。

    **``success=true`` 與 ``possibly_charged=true`` 都算。** 後者代表請求已經送到
    三竹、多半已經扣點，把它當成沒發生就是低估用量 —— 而低估用量正是這整段回填
    要修的東西。

    壞掉的行一律回 ``None`` 跳過（不是拋例外）：稽核檔可能被斷電截斷、可能被人
    工加過註記，一行壞掉不該讓整個回填失效 —— 那會直接退回「重啟就歸零」的原狀。
    """
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("event") != "result":
        return None
    if record.get("success") is not True and record.get("possibly_charged") is not True:
        return None

    segments = record.get("segments")
    # 排除 bool：Python 裡 True 是 int 的子類，isinstance(True, int) 為真，
    # 一個寫壞成 "segments": true 的紀錄會被當成 1 則悄悄算進去。
    if isinstance(segments, bool) or not isinstance(segments, int) or segments < 1:
        return None

    stamp_raw = record.get("time")
    if not isinstance(stamp_raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(stamp_raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        # 沒有偏移量的紀錄（人工編輯過、或未來某版忘了帶時區）當成本機時區。
        # 猜錯最多差幾小時＝被視窗濾掉，比整筆丟掉保守。
        stamp = stamp.astimezone()

    age = (reference - stamp).total_seconds()
    if age < 0:
        # 時間在未來：系統時鐘被往回調過，或紀錄被竄改。當成「剛剛才發生」是最保守
        # 的解讀（會佔用額度），把它丟掉才是危險的那一邊。
        age = 0.0
    return segments, age


# --------------------------------------------------------------------------- #
# 應用邏輯（不含任何 HTTP 細節，可直接呼叫測試）
# --------------------------------------------------------------------------- #


class SmsWebApp:
    """路由與發送流程。

    所有外部相依都可注入，這樣測試不必真的開 socket、不必真的等 TTL 過期、
    更不必真的打三竹 API：

    * ``sender``：預設 :func:`mitake.send_sms`，簽名為
      ``sender(phone, body, *, max_segments, timeout) -> dict``。
    * ``status_query``：預設 :func:`mitake.query_message_status`，簽名為
      ``status_query(msgid, *, timeout) -> dict``。唯讀、免費，但仍必須可注入 ——
      測試絕不可以真的打三竹。
    * ``clock``：預設 :func:`time.monotonic`，同時餵給 token TTL 與速率視窗。
    * ``audit_log``：預設寫 ``logs/send-audit.jsonl``。
    """

    def __init__(
        self,
        *,
        sender: Callable[..., dict[str, Any]] | None = None,
        status_query: Callable[..., dict[str, Any]] | None = None,
        audit_log: AuditLog | None = None,
        token_store: TokenStore | None = None,
        rate_limiter: RateLimiter | None = None,
        status_throttle: StatusQueryThrottle | None = None,
        max_segments: int = mitake.MAX_SEGMENTS_PER_SEND,
        rate_limit: int = DEFAULT_RATE_LIMIT_SEGMENTS,
        status_query_limit: int = DEFAULT_STATUS_QUERY_LIMIT,
        token_ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS,
        send_timeout: float = mitake.DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        request_id_factory: Callable[[], str] | None = None,
        require_access_email: str | None = None,
    ) -> None:
        if max_segments < 1:
            raise ValueError(f"單次則數上限必須 >= 1，收到 {max_segments}")
        self._sender = sender if sender is not None else mitake.send_sms
        self._status_query = (
            status_query if status_query is not None else mitake.query_message_status
        )
        self._audit = audit_log if audit_log is not None else AuditLog()
        self._tokens = (
            token_store
            if token_store is not None
            else TokenStore(ttl_seconds=token_ttl_seconds, clock=clock)
        )
        self._rate = (
            rate_limiter if rate_limiter is not None else RateLimiter(rate_limit, clock=clock)
        )
        # 與 self._rate 是兩個各自獨立的計數器，理由見 StatusQueryThrottle 的 docstring。
        self._status_throttle = (
            status_throttle
            if status_throttle is not None
            else StatusQueryThrottle(status_query_limit, clock=clock)
        )
        self._max_segments = int(max_segments)
        self._send_timeout = float(send_timeout)
        self._request_id_factory = (
            request_id_factory if request_id_factory is not None else lambda: uuid.uuid4().hex[:12]
        )
        # 空字串當成沒設：systemd 的 `Environment=MITAKE_WEB_REQUIRE_ACCESS_EMAIL=`
        # 很容易寫成這樣，而「設了一個空字串」若被當成有效值，會變成任何請求都比不過
        # → 整個介面 403 全掛（或更糟：比對邏輯寫鬆一點就變成誰都放行）。
        cleaned = (require_access_email or "").strip()
        self._required_access_email = cleaned or None

    # -- 唯讀屬性（給啟動訊息與測試用） ------------------------------------- #

    @property
    def max_segments(self) -> int:
        return self._max_segments

    @property
    def required_access_email(self) -> str | None:
        """設了就會檢查 Access 身分標頭；``None`` 代表不擋（見 ``_deny_without_access_email``）。"""
        return self._required_access_email

    @property
    def audit_path(self) -> Path:
        return self._audit.path

    @property
    def token_store(self) -> TokenStore:
        return self._tokens

    @property
    def rate_limiter(self) -> RateLimiter:
        return self._rate

    @property
    def status_throttle(self) -> StatusQueryThrottle:
        """投遞狀態查詢的節流器。與 :attr:`rate_limiter`（發送則數）互不相干。"""
        return self._status_throttle

    # -- 路由 ---------------------------------------------------------------- #

    def route(
        self,
        method: str,
        path: str,
        form: Mapping[str, Sequence[str]] | None = None,
        headers: Any = None,
        *,
        query: Mapping[str, Sequence[str]] | None = None,
    ) -> Response:
        """把 (method, path, form, headers) 對應到回應。HTTP 層只負責把參數餵進來。

        ``headers`` 只要支援 ``.get(name)`` 即可（實務上是
        :class:`email.message.Message`，它的查找不分大小寫）。給 ``None`` 等同
        「沒有任何標頭」—— 在有設 ``require_access_email`` 時那會被擋下，
        這正是我們要的預設方向。

        ``query`` 是網址上 ``?`` 之後的參數（``parse_qs`` 的形狀），**只給
        ``GET /status`` 用**。它是後加的 keyword-only 參數，既有呼叫端
        （含測試）不傳也完全不受影響。刻意與 ``form`` 分開：花錢的 ``/send``
        只吃 POST body，永遠不該從網址列取值。
        """
        form = form if form is not None else {}
        # /health 在存取檢查**之前**處理：systemd / 監控探測不會帶 Access 標頭，
        # 擋掉它等於讓服務被誤判成掛掉並反覆重啟。這個端點只回「服務活著」，
        # 不查餘額、不碰憑證、不花錢（見 handle_health），豁免的代價是零。
        if path == "/health":
            return self.handle_health() if method == "GET" else self._method_not_allowed()

        denied = self._deny_without_access_email(headers)
        if denied is not None:
            return denied

        if path == "/":
            return self.handle_index() if method == "GET" else self._method_not_allowed()
        if path == "/preview":
            return self.handle_preview(form) if method == "POST" else self._method_not_allowed()
        if path == "/send":
            return self.handle_send(form) if method == "POST" else self._method_not_allowed()
        if path == templates.STATUS_PATH:
            # 只收 GET：查詢是唯讀且冪等的，用 GET 才能收藏／重整／貼給別人，
            # 也才能讓成功頁那個入口是一個純連結而不是一顆按鈕（見 render_sent）。
            return (
                self.handle_status(query) if method == "GET" else self._method_not_allowed()
            )
        return self._not_found()

    def _deny_without_access_email(self, headers: Any) -> Response | None:
        """應用層的第二道防線：檢查 Cloudflare Access 注入的身分標頭。沒設就不擋。

        ⚠ **這不是真正的認證。** ``Cf-Access-Authenticated-User-Email`` 是一個
        純文字標頭，任何能直接連到本服務的人都可以自己加一個。真正可驗的是
        ``Cf-Access-Jwt-Assertion``（檢查 JWT 簽章），但驗它要向 Cloudflare 取
        JWKS —— 那需要 HTTP client + JWT 驗證，會直接破壞本專案的零外部依賴鐵律。

        那它為什麼還值得做？因為它**改變的是失敗模式**，不是攻擊面：

        * 在此之前，程式對「誰在存取」完全沒有意見，安全性 100% 押在 Cloudflare
          Access 上。而 ``_is_loopback`` 那道守門在正確部署下**完全不生效**——
          服務本來就綁 127.0.0.1，tunnel 也是從 localhost 連進來的。
        * 於是「Access 忘了設 / 設錯 / 先開了 tunnel 才去設」這三種很普通的失誤，
          結果都是一個對全世界開放的付費簡訊閘道，而且畫面一切正常、沒人會發現。
        * 設了這個之後，同樣的失誤會變成一個一眼看得到的 403。**壞掉會有人回報，
          安靜地被利用不會。**

        設定值為 ``None``（預設）時完全不擋，本機 ``curl`` 測試照舊可用；
        啟動時會記一筆 WARNING 提醒目前沒有應用層存取控制（見 :func:`main`）。
        """
        expected = self._required_access_email
        if expected is None:
            return None

        actual = ""
        if headers is not None:
            raw = headers.get(ACCESS_EMAIL_HEADER)
            if isinstance(raw, str):
                actual = raw

        if _access_email_matches(actual, expected):
            return None

        # 只記「缺少」或「不符」，不把標頭原值寫進 log：那是外部可控字串，
        # 照抄進 journalctl 等於讓人往日誌裡塞任意內容。
        logger.warning(
            "拒絕存取：%s 標頭%s。若這是你自己在測試，請帶上該標頭或改用 /health。",
            ACCESS_EMAIL_HEADER,
            "缺少" if not actual.strip() else "與設定值不符",
        )
        return _html_response(
            HTTPStatus.FORBIDDEN,
            templates.render_notice(
                title="沒有存取權限",
                heading="沒有存取權限",
                message=(
                    "這個服務只接受通過 Cloudflare Access 認證的身分。"
                    "本次沒有送出任何簡訊，也沒有扣點。"
                ),
                kind="error",
                hint=(
                    "若你是管理者：請確認 Cloudflare Access 已生效，"
                    f"且 {ENV_REQUIRE_ACCESS_EMAIL} 設的 email 與登入身分一致。"
                ),
            ),
        )

    # -- 各端點 -------------------------------------------------------------- #

    def handle_index(
        self,
        *,
        phone: str = "",
        body: str = "",
        error: str | None = None,
        notice: str | None = None,
        status: int = HTTPStatus.OK,
    ) -> Response:
        """表單頁。被擋下時同一頁回填原輸入 —— 讓人重打長訊息本身就是出錯來源。"""
        snapshot = self._rate.snapshot()
        # 每次進這個函式都換一組新的 nonce（它同時進頁面與 CSP 標頭，見 _csp_header）。
        # 這是全站唯一帶 <script> 的頁面，所以也是唯一需要 nonce 的地方。
        nonce = _new_nonce()
        markup = templates.render_form(
            max_segments=self._max_segments,
            chars_per_segment=mitake.CHARS_PER_SEGMENT,
            script_nonce=nonce,
            phone=phone,
            body=body,
            error=error,
            notice=notice,
            rate_used=snapshot.used,
            rate_limit=snapshot.limit,
        )
        return _html_response(status, markup, nonce=nonce)

    def handle_preview(self, form: Mapping[str, Sequence[str]]) -> Response:
        """確認頁。所有「會被擋下」的情況都在這裡擋，不留到 ``/send`` 才報錯。"""
        phone_raw = _first(form, "phone")
        body = _normalize_newlines(_first(form, "body"))

        try:
            phone = mitake.validate_phone(phone_raw)
        except mitake.MitakeValidationError as exc:
            return self.handle_index(
                phone=phone_raw,
                body=body,
                error=f"{exc}（尚未送出，未扣點）",
                status=HTTPStatus.BAD_REQUEST,
            )

        # 三竹會拒收空內容，而且那趟網路來回不會告訴使用者任何有用的事。
        if not body.strip():
            return self.handle_index(
                phone=phone_raw,
                body=body,
                error="簡訊內容不可為空白（只有空白字元也不行）。",
                status=HTTPStatus.BAD_REQUEST,
            )

        segments, chars = mitake.count_sms_segments(body)
        if segments > self._max_segments:
            return self.handle_index(
                phone=phone_raw,
                body=body,
                error=(
                    f"內容 {chars} 字＝{segments} 則＝送出會扣 {segments} 點，"
                    f"超過單次上限 {self._max_segments} 則。請縮短內容後再試。"
                ),
                status=HTTPStatus.BAD_REQUEST,
            )

        try:
            self._rate.check(segments)
        except RateLimitExceededError as exc:
            # 同 /send 的理由：撞上限時把號碼與內容帶回去。這條路徑雖然還沒發過
            # token（表單資料理論上還在瀏覽器的上一頁），但使用者面對 429 頁的
            # 直覺是回表單重打，不是按上一頁 —— 而重打長訊息本身就是打錯字、
            # 送錯人的來源。回填的號碼用正規化後的版本（它已經通過驗證）。
            return self._rate_limited_response(exc, phone=phone, body=body)

        token = self._tokens.issue(phone=phone, body=body, segments=segments, chars=chars)
        logger.info(
            "產生確認頁：號碼=%s 則數=%s 字數=%s", mask_phone(phone), segments, chars
        )
        return _html_response(
            HTTPStatus.OK,
            templates.render_preview(
                phone=phone, body=body, segments=segments, chars=chars, token=token
            ),
        )

    def handle_send(self, form: Mapping[str, Sequence[str]]) -> Response:
        """實際發送。**這是唯一會花錢的路徑。**"""
        try:
            pending = self._tokens.consume(_first(form, "token"))
        except TokenExpiredError as exc:
            return _html_response(
                HTTPStatus.GONE,
                templates.render_notice(
                    title="確認頁已過期",
                    heading="確認頁已過期",
                    message=f"{exc}簡訊沒有送出，也沒有扣點。",
                    hint="請回表單重新填寫並確認。",
                ),
            )
        except TokenUnknownError as exc:
            # 走到這裡有**兩種**成因，而它們的實際狀態完全相反：
            #   (a) 使用者按瀏覽器上一頁，回到已經送出過的確認頁再按一次 → 前一次
            #       已經送出去了，這時絕不能重發（「扣兩次點、對方收到兩封」的典型路徑）。
            #   (b) 這張確認頁被較新的擠掉了（未使用的 token 達 MAX_LIVE_TOKENS 上限，
            #       淘汰最舊的一張，見 TokenStore.issue）→ 這則**從來沒送出去過**。
            # 原本的文案只講 (a)，遇到 (b) 的人會被告知「先前那次的結果才是實際狀態」，
            # 於是誤以為自己已經送過了 —— 一則該送的簡訊就這樣沒送出去，而且沒人知道。
            # 兩種成因在伺服器端無法區分（token 都是「查無此筆」），所以文案必須同時涵蓋，
            # 並把「怎麼確定是哪一種」講清楚（看稽核紀錄，不是在這裡重按）。
            return _html_response(
                HTTPStatus.CONFLICT,
                templates.render_notice(
                    title="此請求已處理過，或這張確認頁已被取代",
                    heading="此請求已處理過，或這張確認頁已被較新的取代",
                    message=f"{exc}為避免重複發送，本次一律不送出。",
                    hint=(
                        "兩種可能：(1) 你按了瀏覽器上一頁再送出 —— 那先前那次的結果才是"
                        "實際狀態，請勿在此重按；(2) 這張確認頁已被較新的確認頁擠掉"
                        "（同時開著太多張未送出的確認頁時，最舊的會失效）—— 那這一則"
                        "從來沒有送出去過。要確認到底是哪一種，請查稽核紀錄或三竹後台；"
                        "確定沒送出再回表單重新填寫。"
                    ),
                ),
            )

        try:
            reservation = self._rate.reserve(pending.segments)
        except RateLimitExceededError as exc:
            # token 已經在上面被 consume 掉（一次性，不能還）。這裡若不把號碼與內容
            # 帶回去，使用者手上就什麼都不剩了。刻意**不**改成「先 reserve 再 consume」：
            # 那會多出一條「token 無效但額度已佔用」的必須 release 路徑，更容易漏。
            return self._rate_limited_response(exc, phone=pending.phone, body=pending.body)

        request_id = self._request_id_factory()
        masked = mask_phone(pending.phone)
        # 送出前先留底：若 process 死在「三竹已收單」與「寫結果」之間，
        # 這一筆孤兒 attempt 就是唯一的線索。
        self._audit.record_attempt(
            request_id=request_id,
            phone=pending.phone,
            segments=pending.segments,
            chars=pending.chars,
        )
        logger.info(
            "開始發送：request_id=%s 號碼=%s 則數=%s", request_id, masked, pending.segments
        )

        try:
            result = self._sender(
                pending.phone,
                pending.body,
                max_segments=self._max_segments,
                timeout=self._send_timeout,
            )
        except mitake.MitakeValidationError as exc:
            # 輸入不合法一律在送出網路請求**之前**丟出，保證沒扣點（見 mitake.py）。
            # 這類例外沒有 possibly_charged 屬性，所以單獨接。
            return self._fail_safe(
                reservation,
                request_id,
                pending,
                exc,
                kind="validation",
                status=HTTPStatus.BAD_REQUEST,
                reason=templates.REASON_VALIDATION_BLOCKED,
            )
        except mitake.MitakeConfigError as exc:
            # 憑證沒設 → 根本沒打出去，沒扣點。訊息裡只有變數名稱，不含值。
            return self._fail_safe(
                reservation,
                request_id,
                pending,
                exc,
                kind="config",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                heading="設定不完整，未扣點",
                hint="請檢查 /etc/mitake-sms.env 的 MITAKE_USERNAME / MITAKE_PASSWORD，改好後重啟 mitake-web 服務。",
                allow_resend=False,
                reason=templates.REASON_CONFIG_MISSING,
            )
        except mitake.MitakeAPIError as exc:
            return self._handle_api_error(reservation, request_id, pending, exc)
        except mitake.MitakeError as exc:
            # 這個模組日後若新增例外型別，會落到這裡。**保守當成可能已扣點**：
            # 不退還額度、不給重送按鈕。誤判成「沒扣點」的代價（多送一封）遠高於
            # 誤判成「可能扣了」的代價（多查一次後台）。
            logger.error("發送失敗（未分類的 MitakeError）：request_id=%s %s", request_id, exc)
            return self._fail_unconfirmed(
                request_id, pending, exc, kind="mitake_unknown", msgid=None
            )
        except Exception as exc:  # noqa: BLE001 — 理由見下方註解
            # 未預期的例外（bug、記憶體、第三方…）。同樣保守處理：例外可能發生在
            # 三竹已經收單之後，這時叫使用者重試就是扣第二次。
            # 只記在 log，不把 traceback 送到瀏覽器（可能含內部路徑）。
            logger.exception("發送時發生未預期錯誤：request_id=%s", request_id)
            return self._fail_unconfirmed(
                request_id, pending, exc, kind="unexpected", msgid=None
            )

        return self._succeed(request_id, pending, result)

    def handle_status(
        self, query: Mapping[str, Sequence[str]] | None = None
    ) -> Response:
        """投遞狀態查詢（``GET /status``）。**唯讀、免費、不扣點。**

        沒帶 ``msgid`` 就出查詢表單；帶了就查。這條路徑上任何一種失敗都不會扣點
        （走的是 ``SmQuery`` 端點，見 :func:`mitake.query_message_status`），
        所以每個錯誤頁都可以、也應該明講「沒有扣點」—— 這個服務的使用者已經被
        訓練成看到「失敗」就擔心錢，別讓他們為一個免費操作提心吊膽。

        **不需要 token。** 二階段確認存在的理由是「送出不可逆且花錢」，查詢兩者
        皆非。但它仍在 :meth:`_deny_without_access_email` **之後**才被路由到，
        設了 Access 檢查時一樣擋（見 :meth:`route`）。
        """
        raw_msgid = _first(query if query is not None else {}, "msgid")
        if not raw_msgid.strip():
            return _html_response(HTTPStatus.OK, templates.render_status_form())

        try:
            msgid = mitake.validate_msgid(raw_msgid)
        except mitake.MitakeValidationError as exc:
            # 回填使用者原本打的字串（樣板會 escape），讓他看得到自己哪裡打錯。
            return _html_response(
                HTTPStatus.BAD_REQUEST,
                templates.render_status_form(
                    msgid=raw_msgid, error=f"{exc}（沒有查詢，也沒有扣點）"
                ),
            )

        # 節流放在驗證**之後**：格式就錯的輸入根本不會發出請求，不該吃掉額度。
        try:
            self._status_throttle.acquire()
        except StatusQueryThrottledError as exc:
            return _html_response(
                HTTPStatus.TOO_MANY_REQUESTS,
                templates.render_notice(
                    title="查詢太頻繁",
                    heading="查詢太頻繁",
                    message=(
                        f"{exc}查詢本身不扣點，這道限制是為了避免本機 IP 對三竹打點"
                        "太密集而被限流 —— 那會連發簡訊一起壞掉。"
                    ),
                    hint=_format_wait(exc.retry_after),
                ),
            )

        try:
            status = self._status_query(msgid, timeout=self._send_timeout)
        except mitake.MitakeValidationError as exc:
            # 理論上 validate_msgid 已經擋掉了，但 status_query 是可注入的，
            # 不能假設它的驗證規則和這裡一模一樣。
            return _html_response(
                HTTPStatus.BAD_REQUEST,
                templates.render_status_form(
                    msgid=raw_msgid, error=f"{exc}（沒有扣點）"
                ),
            )
        except mitake.MitakeConfigError as exc:
            return _html_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                templates.render_notice(
                    title="設定不完整",
                    heading="設定不完整，查不了",
                    message=f"{exc}本次沒有查詢，也沒有扣點。",
                    kind="error",
                    hint=(
                        "請檢查 /etc/mitake-sms.env 的 MITAKE_USERNAME / "
                        "MITAKE_PASSWORD，改好後重啟 mitake-web 服務。"
                    ),
                ),
            )
        except mitake.MitakeAPIError as exc:
            return self._status_api_error_response(exc)
        except mitake.MitakeError as exc:
            logger.error("查詢投遞狀態失敗（未分類的 MitakeError）：msgid=%s %s", msgid, exc)
            return self._status_generic_error_response(str(exc))
        except Exception:  # noqa: BLE001 — 同 handle_send：不讓單一請求的 bug 拖垮服務
            # 只記在 log，不把 traceback 送到瀏覽器（可能含內部路徑）。
            logger.exception("查詢投遞狀態時發生未預期錯誤：msgid=%s", msgid)
            return self._status_generic_error_response(
                "伺服器內部發生錯誤。詳細原因請看 journalctl -u mitake-web。"
            )

        return _html_response(
            HTTPStatus.OK,
            templates.render_status_result(
                msgid=str(status.get("msgid") or msgid),
                statuscode=str(status.get("statuscode") or ""),
                description=str(status.get("description") or ""),
                category=str(status.get("category") or mitake.DELIVERY_UNKNOWN),
                status_time=(
                    str(status["status_time"])
                    if status.get("status_time") is not None
                    else None
                ),
                # 用 is True 而不是 bool()：這兩個布林值決定畫面上寫「已送達手機」
                # 還是「還沒到」，注入來源給了個真值字串（例如 "0"）也不該被當成已送達。
                is_delivered=status.get("is_delivered") is True,
                is_final=status.get("is_final") is True,
            ),
        )

    def _status_api_error_response(self, exc: mitake.MitakeAPIError) -> Response:
        """把查詢時的 :class:`mitake.MitakeAPIError` 分成四種畫面。

        分類的唯一用途是**告訴使用者下一步該做什麼**，四種答案彼此互斥：

        =========================  ==========================================
        kind                       下一步
        =========================  ==========================================
        ``ip_blocked``             寄信給三竹加白名單（重查沒用）
        ``auth_failed``            改 ``/etc/mitake-sms.env``（重查沒用）
        ``msgid_mismatch``         拿 msgid 去三竹後台核對（重查沒用）
        ``bad_response``           同上，回應解不開（重查沒用）
        其餘（``network`` 等）      稍後再查一次（**只有這種**重查才有意義）
        =========================  ==========================================

        「重查沒用」的四種若共用「稍後再查」的文案，使用者會照做、會失敗、會再照做 ——
        那句話把他釘在一個永遠不會成功的迴圈裡，而真正該做的事一個字都沒說。

        分流依據沿用發送路徑那套 ``kind``，不另外比對中文字串。
        """
        kind = str(getattr(exc, "kind", mitake.KIND_API))
        if getattr(exc, "possibly_charged", False):
            # SmQuery 是唯讀端點，這裡永遠不該為真。真的出現代表 mitake.py 那邊
            # 有人把查詢改成走 SmSend 了 —— 那是會扣點的，必須看得見。
            logger.error(
                "查詢路徑上出現 possibly_charged=True，這代表查詢端點被改成會扣點的：%s",
                exc,
            )

        if kind == mitake.KIND_IP_BLOCKED:
            return _html_response(
                HTTPStatus.BAD_GATEWAY,
                templates.render_notice(
                    title="查不到投遞狀態",
                    heading="這台機器的 IP 不在三竹白名單，查不了（未扣點）",
                    message=f"{exc}",
                    kind="error",
                    hint=(
                        "這是設定問題不是暫時故障，重查不會成功。"
                        "請寄 service@mitake.com.tw 申請把本機外網 IP 加入白名單。"
                    ),
                ),
            )
        if kind == mitake.KIND_AUTH_FAILED:
            return _html_response(
                HTTPStatus.BAD_GATEWAY,
                templates.render_notice(
                    title="查不到投遞狀態",
                    heading="三竹帳號或密碼錯誤，查不了（未扣點）",
                    message=f"{exc}",
                    kind="error",
                    hint=(
                        "這是設定問題不是暫時故障，重查不會成功。"
                        "請檢查 /etc/mitake-sms.env 的憑證，改好後重啟 mitake-web 服務。"
                    ),
                ),
            )
        if kind == mitake.KIND_MSGID_MISMATCH:
            # 三竹回的是別則簡訊的狀態。exc 的訊息已經同時帶著「你查的」與
            # 「三竹回的」兩個 msgid —— 那兩個數字並排放，是這一頁唯一有用的資訊，
            # 使用者一眼就看得出「這不是我那則」。
            return self._status_unretryable_error_response(
                heading="查詢無效：三竹回的是另一則簡訊（未扣點）",
                message=str(exc),
            )
        if kind == mitake.KIND_BAD_RESPONSE:
            return self._status_unretryable_error_response(
                heading="三竹的回應解不開，無法判讀狀態（未扣點）",
                message=str(exc),
            )
        return self._status_generic_error_response(str(exc))

    def _status_unretryable_error_response(
        self, *, heading: str, message: str
    ) -> Response:
        """「三竹有回應，但那份回應不能當答案」的畫面 —— **不可出現「稍後再查」**。

        涵蓋兩種：回的是別則簡訊（``msgid_mismatch``）、回應格式解不開
        （``bad_response``）。兩者的共通點是**時間解決不了**：連線是通的，壞的是
        回應內容本身，一秒後再查會拿到同一份東西。

        和 :meth:`_status_generic_error_response` 分家的理由就只有這一句文案：
        原本兩者共用「查詢是唯讀操作，稍後可以安全地再查一次」，於是格式問題被講成
        暫時故障，使用者會反覆重整到放棄，而「拿 msgid 去三竹後台核對」這句真正的
        出路被稀釋在後半段。
        """
        return _html_response(
            HTTPStatus.BAD_GATEWAY,
            templates.render_notice(
                title="查詢無效",
                heading=heading,
                message=(
                    f"{message} 這次查詢沒有扣點，簡訊本身的狀態不受影響"
                    "（查詢是唯讀操作，不會動到那則簡訊）。"
                ),
                kind="error",
                hint=(
                    "重查不會得到不同的結果 —— 問題出在三竹回的內容，不是暫時的連線故障。"
                    "請拿 msgid 到三竹後台核對，或聯絡三竹客服 02-25367777。"
                ),
            ),
        )

    def _status_generic_error_response(self, message: str) -> Response:
        """查詢失敗但**真的可以再試**的統一畫面。一定要講「沒有扣點」。

        給的是暫時性失敗：網路不通、三竹系統忙、未預期的內部錯誤。
        「回應解不開」「回的是別則簡訊」那兩種請走
        :meth:`_status_unretryable_error_response` —— 對它們說「稍後再查」是騙人的。
        """
        return _html_response(
            HTTPStatus.BAD_GATEWAY,
            templates.render_notice(
                title="查不到投遞狀態",
                heading="查不到投遞狀態（未扣點）",
                message=f"{message} 這次查詢沒有扣點，簡訊本身的狀態不受影響。",
                kind="error",
                hint=(
                    "查詢是唯讀操作，稍後可以安全地再查一次。"
                    "若一直查不到，請拿 msgid 到三竹後台核對。"
                ),
            ),
        )

    def handle_health(self) -> Response:
        """健康檢查：只回「服務活著」，不查餘額、不碰憑證。

        刻意**不**在這裡呼叫 ``query_balance``：它雖然免費，但這個端點不需要認證，
        任何打得到 port 的人都能反覆觸發，等於把對外 API 的呼叫頻率交給別人決定；
        餘額本身也是不該外流的營運資訊。餘額監控由第二部分的排程負責。
        """
        payload = {
            "status": "ok",
            "service": "mitake-web",
            "version": SERVER_VERSION,
            "time": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return Response(
            status=HTTPStatus.OK, body=body, content_type="application/json; charset=utf-8"
        )

    # -- 啟動時的狀態回填 ---------------------------------------------------- #

    def restore_rate_limit_from_audit(
        self,
        *,
        now: datetime | None = None,
        max_lines: int = DEFAULT_TAIL_LINES,
    ) -> int:
        """從稽核檔回填速率上限的已用量，回傳補回去的**則數**。

        為什麼需要：:class:`RateLimiter` 只活在記憶體裡，一次 ``systemctl restart``
        就把當小時的預算清成零。這不是可被遠端誘發的漏洞（找不到能讓 process 死掉
        的路徑），但**是日常維運的真實破口**：``git pull && restart`` 是每次部署都
        會做的事，OOM 也不需要誰動手 —— 等於每次部署都偷偷把上限重設一次。

        兩個刻意的選擇：

        1. ``success=true`` 與 ``possibly_charged=true`` **都算**（見
           :func:`_charged_segments_from_audit_line`）。
        2. **時鐘換算**：:class:`RateLimiter` 用 :func:`time.monotonic`（重啟後歸零，
           與上一輪的數值沒有任何可比性），稽核檔用帶時區的牆鐘。所以這裡先用牆鐘
           算出每筆「距今幾秒」，再交給 :meth:`RateLimiter.seed` 以**現在的**
           monotonic 為基準往回推。兩種時鐘的數值絕不能直接相減。

        任何讀不到／解析不了的情況都只記 WARNING 並以空狀態啟動：回填是防呆，
        讓服務起不來的代價遠高於少擋幾則。
        """
        reference = now if now is not None else datetime.now(timezone.utc).astimezone()
        window = self._rate.window_seconds
        restored = 0
        try:
            for line in self._audit.tail_lines(max_lines):
                parsed = _charged_segments_from_audit_line(line, reference)
                if parsed is None:
                    continue
                segments, age = parsed
                if age >= window:
                    continue  # 超出視窗的舊紀錄，本來就不該再佔用額度
                if self._rate.seed(segments, age):
                    restored += segments
        except Exception:  # noqa: BLE001 — 理由見 docstring 最後一段
            logger.warning(
                "從稽核檔回填速率上限失敗，改以空狀態啟動"
                "（本小時的已用量會從 0 起算，上限等於被放寬一輪）。",
                exc_info=True,
            )
            return restored

        if restored:
            logger.info(
                "已從稽核檔回填本小時已送出 %s 則（避免重啟把每小時上限歸零）。", restored
            )
        return restored

    # -- 結果處理 ------------------------------------------------------------ #

    def _succeed(
        self, request_id: str, pending: PendingSend, result: Mapping[str, Any]
    ) -> Response:
        msgid = result.get("msgid") if isinstance(result, Mapping) else None
        account_point = result.get("account_point") if isinstance(result, Mapping) else None
        audit_ok = self._audit.record_result(
            request_id=request_id,
            phone=pending.phone,
            segments=pending.segments,
            chars=pending.chars,
            success=True,
            msgid=msgid,
            account_point=account_point,
        )
        # 稽核寫失敗時，這行就是「到底扣了沒」的唯一剩餘證據 —— 必須拉到 ERROR，
        # 否則 --log-level WARNING 會連它一起關掉，孤兒 attempt 就真的無從判讀。
        (logger.info if audit_ok else logger.error)(
            "發送成功：request_id=%s msgid=%s 剩餘點數=%s 稽核已留底=%s",
            request_id,
            msgid,
            account_point,
            audit_ok,
        )
        return _html_response(
            HTTPStatus.OK,
            templates.render_sent(
                phone=pending.phone,
                segments=pending.segments,
                chars=pending.chars,
                msgid=str(msgid) if msgid is not None else None,
                account_point=account_point if isinstance(account_point, int) else None,
                audit_ok=audit_ok,
            ),
        )

    def _fail_safe(
        self,
        reservation: int,
        request_id: str,
        pending: PendingSend,
        exc: BaseException,
        *,
        kind: str,
        status: int,
        heading: str = "發送失敗，未扣點",
        hint: str | None = None,
        allow_resend: bool = True,
        reason: str | None = None,
    ) -> Response:
        """確定沒扣點的失敗：退還速率額度，並給（多數情況下的）重送按鈕。

        ``reason`` 是「為什麼沒扣點」那句話，``None`` 表示沿用樣板預設的
        「三竹已明確拒絕」。**只有三竹真的回了 Error 的路徑可以用預設值**：
        輸入驗證、憑證未設、連線層失敗這三種，三竹根本沒收到請求，
        寫成「三竹已明確拒絕」會害人拿著一筆不存在的紀錄去三竹後台找。
        """
        self._rate.release(reservation)
        self._audit.record_result(
            request_id=request_id,
            phone=pending.phone,
            segments=pending.segments,
            chars=pending.chars,
            success=False,
            possibly_charged=False,
            error_kind=kind,
            error_message=str(exc),
        )
        logger.warning(
            "發送失敗（未扣點）：request_id=%s kind=%s %s", request_id, kind, exc
        )
        return _html_response(
            status,
            templates.render_failed_safe(
                phone=pending.phone,
                body=pending.body,
                message=str(exc),
                heading=heading,
                hint=hint,
                allow_resend=allow_resend,
                reason=reason,
            ),
        )

    def _fail_unconfirmed(
        self,
        request_id: str,
        pending: PendingSend,
        exc: BaseException,
        *,
        kind: str,
        msgid: str | None,
    ) -> Response:
        """可能已扣點：**不退還額度、不給重送按鈕**，要使用者拿 msgid 去後台查證。

        ``record_result`` 的回傳值一定要接住。這條路徑上「稽核有沒有寫成功」比
        成功頁那條更關鍵：走到這裡代表點多半已經扣了、而結果不明，使用者唯一的
        處置就是拿線索去三竹後台查證 —— 稽核若也沒寫進去，他連要查什麼都不知道，
        卻還被頁面告知「本次發送已寫入稽核紀錄」。
        """
        audit_ok = self._audit.record_result(
            request_id=request_id,
            phone=pending.phone,
            segments=pending.segments,
            chars=pending.chars,
            success=False,
            msgid=msgid,
            possibly_charged=True,
            error_kind=kind,
            error_message=str(exc),
        )
        logger.error(
            "發送結果未確認（可能已扣點）：request_id=%s kind=%s msgid=%s 稽核已留底=%s %s",
            request_id,
            kind,
            msgid,
            audit_ok,
            exc,
        )
        if not audit_ok:
            # 額外一行、講白話：這是最壞的組合（可能已扣點 + 查無紀錄）。
            # 上面那行的重點是錯誤本身，容易被當成又一筆發送失敗滑過去。
            logger.error(
                "可能已扣點但稽核紀錄寫入失敗：request_id=%s 號碼=%s 則數=%s。"
                "稽核檔中查不到這一筆，請立刻以此 request_id 與時間人工補記。",
                request_id,
                mask_phone(pending.phone),
                pending.segments,
            )
        return _html_response(
            HTTPStatus.BAD_GATEWAY,
            templates.render_failed_unconfirmed(
                phone=pending.phone,
                segments=pending.segments,
                msgid=msgid,
                message=str(exc),
                audit_ok=audit_ok,
                request_id=request_id,
            ),
        )

    def _handle_api_error(
        self,
        reservation: int,
        request_id: str,
        pending: PendingSend,
        exc: mitake.MitakeAPIError,
    ) -> Response:
        """依 ``possibly_charged`` 分成兩種完全不同的畫面。這是本檔最貴的分支。"""
        # 用 getattr 而非直接取屬性：這個欄位只保證存在於 MitakeAPIError，
        # 未來若有人把這裡改成接更廣的型別，缺屬性時要保守地當「可能已扣點」，
        # 而不是 AttributeError 直接 500（500 頁沒有「請勿重送」的警告）。
        possibly_charged = bool(getattr(exc, "possibly_charged", True))
        kind = str(getattr(exc, "kind", mitake.KIND_API))
        response = getattr(exc, "response", None)
        msgid = response.get("msgid") if isinstance(response, Mapping) else None

        if possibly_charged:
            return self._fail_unconfirmed(
                request_id,
                pending,
                exc,
                kind=kind,
                msgid=str(msgid) if msgid is not None else None,
            )

        # 以下都是三竹明確拒絕＝沒扣點。但「能不能靠重送解決」還要看 kind：
        # IP 不在白名單、帳密錯，都是設定問題，重送一百次也一樣失敗。
        if kind == mitake.KIND_IP_BLOCKED:
            return self._fail_safe(
                reservation,
                request_id,
                pending,
                exc,
                kind=kind,
                status=HTTPStatus.BAD_GATEWAY,
                heading="這台機器的 IP 不在三竹白名單，未扣點",
                hint=(
                    "這是設定問題不是暫時故障，重送不會成功。"
                    "請寄 service@mitake.com.tw 申請把本機外網 IP 加入白名單。"
                ),
                allow_resend=False,
            )
        if kind == mitake.KIND_AUTH_FAILED:
            return self._fail_safe(
                reservation,
                request_id,
                pending,
                exc,
                kind=kind,
                status=HTTPStatus.BAD_GATEWAY,
                heading="三竹帳號或密碼錯誤，未扣點",
                hint=(
                    "這是設定問題不是暫時故障，重送不會成功。"
                    "請檢查 /etc/mitake-sms.env 的憑證，改好後重啟 mitake-web 服務。"
                ),
                allow_resend=False,
            )
        # 落到這裡的還有兩種截然不同的東西：三竹回了 Error（真的拒絕），以及
        # kind=network 且 possibly_charged=False（見 mitake.send_sms 的
        # never_reached_mitake —— DNS 失敗、連線被拒，三竹那端什麼都沒發生）。
        # 兩者都沒扣點、都可安全重送，但「該去哪裡查」完全不同，敘述不能共用。
        if kind == mitake.KIND_NETWORK:
            return self._fail_safe(
                reservation,
                request_id,
                pending,
                exc,
                kind=kind,
                status=HTTPStatus.BAD_GATEWAY,
                heading="連不上三竹，未扣點",
                hint="這是本機到三竹的連線問題（DNS 或網路），沒有扣點，恢復後可以安全重送。",
                reason=templates.REASON_NEVER_REACHED_MITAKE,
            )
        return self._fail_safe(
            reservation,
            request_id,
            pending,
            exc,
            kind=kind,
            status=HTTPStatus.BAD_GATEWAY,
            hint="三竹明確拒絕了這次請求，沒有扣點，修正後可以安全重送。",
        )

    # -- 共用的小回應 -------------------------------------------------------- #

    def _rate_limited_response(
        self,
        exc: RateLimitExceededError,
        *,
        phone: str | None = None,
        body: str | None = None,
    ) -> Response:
        """撞上速率上限的 429 頁。

        ``phone`` / ``body`` 有給時會多一個帶回原內容的按鈕（導向 ``/preview``，
        不會直接送出）。``/send`` 這條路徑一定要給：token 在檢查速率之前就已經被
        consume 掉了，不回填的話使用者等一小時回來得把整段訊息重打 —— 而重打長訊息
        本身就是打錯字、送錯人的來源（同 :meth:`handle_index` 的理由）。
        """
        return _html_response(
            HTTPStatus.TOO_MANY_REQUESTS,
            templates.render_notice(
                title="超過發送上限",
                heading="超過發送上限",
                message=f"{exc}本次未送出，也沒有扣點。",
                hint=_format_wait(exc.retry_after),
                resend_phone=phone,
                resend_body=body,
                resend_label="額度恢復後，用原內容重新確認",
            ),
        )

    def _not_found(self) -> Response:
        return _html_response(
            HTTPStatus.NOT_FOUND,
            templates.render_notice(
                title="找不到頁面",
                heading="找不到頁面",
                message="這個網址不存在。",
                kind="error",
            ),
        )

    def _method_not_allowed(self) -> Response:
        return _html_response(
            HTTPStatus.METHOD_NOT_ALLOWED,
            templates.render_notice(
                title="不支援的請求方式",
                heading="不支援的請求方式",
                message="這個網址不接受此種請求方式。",
                kind="error",
            ),
        )


# --------------------------------------------------------------------------- #
# HTTP 層
# --------------------------------------------------------------------------- #


class _RequestError(Exception):
    """請求本身就不合法（缺 Content-Length、body 太大、Content-Type 不對）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def make_handler(app: SmsWebApp) -> type[BaseHTTPRequestHandler]:
    """產生綁定該 ``app`` 的 handler 類別。

    與 :func:`create_server` 分開，是為了讓測試能只建 handler（或用 port 0 開一個
    臨時 server）而不必真的佔用 8766。
    """

    class MitakeWebRequestHandler(BaseHTTPRequestHandler):
        server_version = SERVER_VERSION
        # 預設會在 Server 標頭附上 Python 版本；對外服務沒必要主動報出可攻擊面。
        sys_version = ""
        # 刻意維持 HTTP/1.0（不設 protocol_version）：每個回應後關閉連線。
        # 走 keep-alive 的話，我們在「還沒讀完 request body 就回 4xx」的路徑上
        # 會讓連線殘留未讀位元組，下一個請求解析就會錯亂。這個服務的流量是個位數
        # req/min，省不下的那點連線成本毫無意義。
        #
        # socket 讀寫逾時。BaseHTTPRequestHandler 預設是 None，
        # 而 self.rfile.read(length) 在 blocking socket 上會**無限期**等下去：
        # 宣告 Content-Length: 100 卻不送 body 的連線能永久佔住一個 thread，
        # ThreadingHTTPServer + daemon_threads 沒有 thread 上限，數十條就癱掉服務，
        # 而 systemd 的 Restart=always 救不了（process 沒死，只是不回應）。
        # StreamRequestHandler.setup() 會把這個值 settimeout 到 connection 上。
        # 只影響 socket I/O：send_sms 那 25 秒是對三竹的**另一條**連線，
        # 期間本連線沒有任何讀寫，不會被這個值誤斷。
        timeout = 30
        _app = app

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 規定的名稱
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                parts = urlparse(self.path)
                path = parts.path
                # 網址上的 query 只餵給 GET /status（唯讀查詢）。花錢的 /send 一律
                # 只吃 POST body —— 不讓任何會扣點的東西從網址列取得參數。
                query = (
                    parse_qs(parts.query, keep_blank_values=True) if parts.query else {}
                )
                form = self._read_form() if method == "POST" else {}
                # self.headers 是 email.message.Message，.get() 不分大小寫 ——
                # 標頭名稱的大小寫由對方決定，不能自己用 dict 查。
                response = self._app.route(
                    method, path, form, headers=self.headers, query=query
                )
            except _RequestError as exc:
                response = _html_response(
                    exc.status,
                    templates.render_notice(
                        title="請求無法處理",
                        heading="請求無法處理",
                        message=str(exc),
                        kind="error",
                    ),
                )
            except Exception:  # noqa: BLE001 — 不讓單一請求的 bug 拖垮整個服務
                # 只記在 log，回給瀏覽器的頁面不含 traceback（可能含內部路徑）。
                logger.exception("處理請求時發生未預期錯誤：%s %s", method, self.path)
                response = _html_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    templates.render_notice(
                        title="伺服器錯誤",
                        heading="伺服器錯誤",
                        message="伺服器內部發生錯誤，這次沒有送出簡訊。",
                        kind="error",
                        hint="詳細原因請看 journalctl -u mitake-web。",
                    ),
                )
            self._respond(response)

        def _read_form(self) -> dict[str, list[str]]:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise _RequestError(
                    HTTPStatus.LENGTH_REQUIRED, "POST 請求缺少 Content-Length 標頭。"
                )
            try:
                length = int(raw_length)
            except ValueError:
                raise _RequestError(
                    HTTPStatus.BAD_REQUEST, "Content-Length 不是合法的數字。"
                ) from None
            if length < 0:
                raise _RequestError(HTTPStatus.BAD_REQUEST, "Content-Length 不可為負數。")
            if length > MAX_REQUEST_BODY_BYTES:
                raise _RequestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"表單內容超過 {MAX_REQUEST_BODY_BYTES} 位元組上限。",
                )

            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != _FORM_CONTENT_TYPE:
                raise _RequestError(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    f"只接受 {_FORM_CONTENT_TYPE} 格式的表單。",
                )

            raw = self.rfile.read(length)
            # errors="replace"：壞掉的位元組不該讓請求 500，讓它變成一個過不了
            # validate_phone 的字串，走正常的「號碼格式錯」流程即可。
            text = raw.decode("utf-8", errors="replace")
            return parse_qs(text, keep_blank_values=True)

        def _respond(self, response: Response) -> None:
            body = response.body
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            # no-store：確認頁帶著一次性 token，不該被瀏覽器或中間層留下來。
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            # 縱深防禦：頁面已全面 html.escape，CSP 是萬一漏掉一處時的第二道牆。
            # nonce 取自 response 本身（不是在這裡另外產生一個）—— 頁面裡的
            # <script nonce="…"> 與這行標頭必須完全一致，兩邊各產一份必然會漂開。
            self.send_header("Content-Security-Policy", _csp_header(response.nonce))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """把預設印到 stderr 的存取紀錄改走 logging，讓 journalctl 有結構可看。"""
            logger.info("%s %s", self.address_string(), format % args)

    return MitakeWebRequestHandler


def create_server(
    app: SmsWebApp, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    """建立（並綁定）伺服器，但**不**開始服務 —— ``serve_forever`` 由呼叫端決定。

    用 :class:`~http.server.ThreadingHTTPServer` 而非單執行緒的 ``HTTPServer``：
    一次發送最久會佔住 25 秒（``mitake.DEFAULT_TIMEOUT_SECONDS``），單執行緒的話
    這 25 秒內連 ``/health`` 都不會回應，systemd/監控會誤判服務掛掉並重啟 ——
    而重啟正好發生在「三竹可能已收單」的當下，是最不該中斷的時刻。
    代價是共享狀態必須自己上鎖：:class:`TokenStore`、:class:`RateLimiter`、
    :class:`~web.audit.AuditLog` 三者內部都有 :class:`threading.Lock`。

    測試要臨時開一個不佔用固定 port 的伺服器，傳 ``port=0`` 即可。
    """
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env_int(name: str, fallback: int) -> int:
    """讀取整數環境變數。格式錯就丟 :class:`ValueError`，**不靜默用預設值**。

    靜默 fallback 在這裡特別危險：``MITAKE_WEB_RATE_LIMIT=２０``（全形）會讓
    「每小時 20 則」悄悄變回內建預設，而使用者以為自己設好了。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"環境變數 {name} 必須是整數，實際是 {raw.strip()!r}") from None


def build_arg_parser() -> argparse.ArgumentParser:
    """建立命令列參數剖析器（預設值可由環境變數覆寫）。"""
    parser = argparse.ArgumentParser(
        prog="mitake-web",
        description="三竹簡訊 Web 發送介面（每則扣 1 點，點數與 App 團隊共用）",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(ENV_HOST) or DEFAULT_HOST,
        help=f"綁定位址（預設 {DEFAULT_HOST}；非 loopback 需同時加 --allow-public）",
    )
    parser.add_argument(
        "--port", type=int, default=_env_int(ENV_PORT, DEFAULT_PORT), help=f"綁定埠號（預設 {DEFAULT_PORT}）"
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=_env_int(ENV_MAX_SEGMENTS, mitake.MAX_SEGMENTS_PER_SEND),
        help=f"單次發送的則數上限（預設 {mitake.MAX_SEGMENTS_PER_SEND}）",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=_env_int(ENV_RATE_LIMIT, DEFAULT_RATE_LIMIT_SEGMENTS),
        help=f"每小時可送出的則數上限（預設 {DEFAULT_RATE_LIMIT_SEGMENTS}）",
    )
    parser.add_argument(
        "--status-query-limit",
        type=int,
        default=_env_int(ENV_STATUS_QUERY_LIMIT, DEFAULT_STATUS_QUERY_LIMIT),
        help=(
            f"每 {STATUS_QUERY_WINDOW_SECONDS // 60} 分鐘可查詢投遞狀態的次數上限"
            f"（預設 {DEFAULT_STATUS_QUERY_LIMIT}）。查詢不扣點，此限制是為了避免"
            "本機 IP 對三竹打點太密集而被限流。"
        ),
    )
    parser.add_argument(
        "--token-ttl",
        type=int,
        default=_env_int(ENV_TOKEN_TTL, DEFAULT_TOKEN_TTL_SECONDS),
        help=f"確認頁的有效秒數（預設 {DEFAULT_TOKEN_TTL_SECONDS}）",
    )
    parser.add_argument(
        "--audit-path",
        default=None,
        help="稽核檔路徑（預設讀 MITAKE_WEB_AUDIT_PATH，再預設 logs/send-audit.jsonl）",
    )
    parser.add_argument(
        "--allow-public",
        action="store_true",
        help="允許綁定非 loopback 位址。發簡訊會扣共用點數，請先設好 Cloudflare Access 再開。",
    )
    parser.add_argument(
        "--require-access-email",
        default=os.environ.get(ENV_REQUIRE_ACCESS_EMAIL) or None,
        help=(
            f"只接受 {ACCESS_EMAIL_HEADER} 標頭等於此 email 的請求"
            f"（預設讀 {ENV_REQUIRE_ACCESS_EMAIL}；不設＝完全不擋，/health 一律豁免）。"
            "注意這不是真正的認證（標頭可偽造），它的作用是讓 Cloudflare Access "
            "沒設好時明顯壞掉（403），而不是安靜地被人拿去發簡訊。"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只建立設定與物件、不開始監聽（部署前檢查用，不會扣點）",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get(ENV_LOG_LEVEL) or "INFO",
        help="logging 等級（預設 INFO）",
    )
    return parser


def _is_loopback(host: str) -> bool:
    """判斷綁定位址是否只在本機可達。無法解析的主機名一律視為**不是** loopback。"""
    candidate = host.strip()
    if candidate.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # 主機名（非 IP 字面值）無法在這裡安全判定，保守當成對外，要求顯式 --allow-public。
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 進入點。回傳值即 process exit code。"""
    try:
        parser = build_arg_parser()
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not _is_loopback(args.host) and not args.allow_public:
        print(
            f"錯誤：拒絕綁定非 loopback 位址 {args.host!r}。\n"
            "這個介面每送一則簡訊就扣共用點數池 1 點（App 團隊靠同一池發註冊驗證碼），"
            "對外暴露前必須先設好 Cloudflare Zero Trust Access。\n"
            "確認已設好認證後，再加上 --allow-public 重跑。",
            file=sys.stderr,
        )
        return 2

    for name, value, minimum in (
        ("--max-segments", args.max_segments, 1),
        ("--rate-limit", args.rate_limit, 1),
        ("--status-query-limit", args.status_query_limit, 1),
        ("--token-ttl", args.token_ttl, 1),
    ):
        if value < minimum:
            print(f"錯誤：{name} 必須 >= {minimum}，收到 {value}", file=sys.stderr)
            return 2
    if not 1 <= args.port <= 65535:
        print(f"錯誤：--port 必須介於 1..65535，收到 {args.port}", file=sys.stderr)
        return 2

    app = SmsWebApp(
        audit_log=AuditLog(args.audit_path) if args.audit_path else None,
        max_segments=args.max_segments,
        rate_limit=args.rate_limit,
        status_query_limit=args.status_query_limit,
        token_ttl_seconds=args.token_ttl,
        require_access_email=args.require_access_email,
    )

    logger.info(
        "設定：單次上限 %s 則、每小時上限 %s 則、確認頁 %s 秒、"
        "投遞狀態查詢上限 %s 次／%s 秒、稽核檔 %s",
        app.max_segments,
        args.rate_limit,
        args.token_ttl,
        args.status_query_limit,
        STATUS_QUERY_WINDOW_SECONDS,
        app.audit_path,
    )

    # 這條 WARNING 是刻意的噪音：沒有應用層存取控制時，整個服務的安全性 100% 押在
    # Cloudflare Access 上，而 Access 忘了設／設錯／tunnel 先開都不會有任何徵兆。
    # 讓它每次啟動都在 journalctl 裡講一次，至少「沒設」這件事是看得見的。
    if app.required_access_email:
        logger.info(
            "已啟用應用層存取檢查：除 /health 外，只接受 %s 標頭符合設定值的請求。",
            ACCESS_EMAIL_HEADER,
        )
    else:
        logger.warning(
            "目前無應用層存取控制：任何連得到 %s:%s 的人都能發簡訊（每則從共用點數池扣 1 點）。"
            "對外提供前請設 %s（或 --require-access-email），並確認 Cloudflare Access 已生效。",
            args.host,
            args.port,
            ENV_REQUIRE_ACCESS_EMAIL,
        )

    # 在開始接受請求**之前**回填：晚一步的話，重啟後的頭幾則會用到歸零的額度。
    app.restore_rate_limit_from_audit()

    if args.dry_run:
        logger.info("--dry-run：初始化完成，未開始監聽，未發送任何簡訊。")
        return 0

    try:
        server = create_server(app, args.host, args.port)
    except OSError as exc:
        print(f"錯誤：無法綁定 {args.host}:{args.port} —— {exc}", file=sys.stderr)
        return 1

    logger.info("mitake-web 已啟動：http://%s:%s/", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中斷訊號，正在關閉…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
