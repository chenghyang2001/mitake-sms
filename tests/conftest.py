"""pytest 全域設定：把「測試不准打真實三竹 API」從文件鐵律升級成機制。

HANDOFF.md §3.3 已規定「tests 必須 mock 網路呼叫」，但在此檔存在之前，那條規則
**完全靠寫測試的人自律**：只要有人新增測試時忘記 patch，測試就會真的連上三竹、
真的扣點、真的把簡訊送到某個號碼——而且測試多半還會綠燈通過，沒人會發現。

點數與 App 團隊共用（App 靠同一池發註冊驗證碼），一則 ``send_sms`` = 扣 1 點真實
點數，扣光會害 App 的註冊驗證碼發不出去。所以這裡採「預設全面封鎖」：每支測試
自動被塞入一個一被呼叫就爆炸的 ``mitake._OPENER.open`` 替身，忘記攔截的測試會
**直接紅燈**而不是靜默地花錢。要真的連外必須顯式掛 ``@pytest.mark.allow_network``。

**攔截點為什麼是 ``mitake._OPENER.open``**：模組為了禁止 redirect 已改用自建的
``_OPENER``（見 mitake.py 的 ``_fetch_raw``），不再呼叫 ``urllib.request.urlopen``。
patch 舊的 ``urlopen`` 攔不住任何東西。

**護欄鋪設的兩層**：本檔的**模組層**（import 本 conftest 的當下就生效）+ autouse
fixture（function 層，訊息帶得到 nodeid）。單靠 function 層會漏掉三個更早的時機——
子目錄 conftest 被匯入時、測試模組被 import 時的 module-level 呼叫、以及 session /
module / class scope 的 fixture（全都比 function scope 早建立）；那些時機實測會真的
做 DNS 解析並對 smsapi.mitake.com.tw:443 發起 TCP 連線。

**為什麼不鋪在 ``pytest_configure``**：那還不夠早。落在命令列參數路徑上的子目錄
conftest（``pytest tests/zzsub``）是在 ``_preparse`` 階段匯入的，早於
``pytest_configure``；實測那個時機 ``_OPENER.open`` 還是真的。父 conftest 一定早於
子 conftest 匯入，鋪在模組層才涵蓋得到 ``tests/`` 子樹底下的所有匯入期。

注意這**不是**無條件的「所有匯入期」：比本檔更早被載入的東西仍在防護之外——
實測 repo 根若放一個 ``conftest.py``，它的 module-level 呼叫發生時 ``_OPENER.open``
還是真的（``pytest tests/`` 與不帶參數的 ``pytest`` 兩種跑法皆然）；pytest 外掛
（entry-point 載入）同理。要涵蓋那些時機，本檔得比它們更早載入，做不到。

**為什麼要 ``pytest_unconfigure``**：模組層是直接賦值，沒人還原的話會殘留到同一個
process 的下一輪 pytest（pytest-watch / IDE 常駐 runner / ``pytester`` /
``pytest.main``）——下一輪重新匯入本檔時，``_REAL_OPENER_OPEN`` 會捕獲到上一輪的
封鎖版，``allow_network`` 逃生門就靜默失效了。故在 session 結束時還原。

**已知邊界（皆為實測確認，目前無低成本解）**：

1. 兩層都只鎖 ``mitake._OPENER.open`` 這一個出口。繞開 ``_OPENER`` 直接用
   ``urllib.request.urlopen`` / 裸 ``socket`` / requests 之類的第三方 HTTP client，
   這裡一律攔不到——那時要同步在此補上對應的封鎖點。
2. ``importlib.reload(mitake)`` 會重建 ``_OPENER``，兩層封鎖都還指向舊物件 →
   實測可繞過。
3. ``subprocess`` / ``multiprocessing`` 內是全新 process，實測 ``_OPENER.open``
   是真的，本檔的封鎖完全不在那裡。

換句話說：這道護欄擋的是「忘記 mock」這個高頻失誤，不是「刻意繞過」。不要把它
當成沙箱來信任。
"""

import sys
from pathlib import Path

import pytest

# conftest.py 在 tests/ 底下、mitake.py 在 repo 根，而 tests/ 沒有 __init__.py，
# pytest 只會把 tests/ 自己放進 sys.path，故手動補上根目錄，否則下面 import 會失敗。
# 用 not in 判斷是防禦「本檔被重複匯入」（rootdir 切換、importmode 差異等情境下
# conftest 可能被載入不只一次），避免同一路徑一直往 sys.path 前面疊。
# 注意：它擋不掉 tests/test_mitake.py 那份無條件 insert —— conftest 先於 test 模組
# 匯入，判斷當下路徑還不在 sys.path，所以這裡一定會 insert，test 模組接著再 insert
# 一次，repo root 實測仍會在 sys.path 出現兩次。那份 insert 在有了本檔之後已屬冗餘，
# 可另行清理（不在本檔的職責範圍內）。
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import mitake  # noqa: E402  （必須等 sys.path 補完才 import）

# marker 名稱只寫一次：錯誤訊息、註冊、fixture 判斷三處共用，改名不會各自漂移。
ALLOW_NETWORK_MARKER = "allow_network"

# 真正的 _OPENER.open，供 allow_network 測試還原用。必須在任何封鎖動作之前抓。
_REAL_OPENER_OPEN = mitake._OPENER.open


class RealMitakeAPICallBlocked(BaseException):
    """哨兵：測試在沒有攔截網路的情況下走到了真實三竹 API —— 已擋下，未送出。

    刻意繼承 ``BaseException`` 而不是 ``Exception``。mitake.py 的 ``_request``
    有一整排 ``except urllib.error.URLError`` / ``except TimeoutError`` /
    ``except http.client.HTTPException`` / ``except OSError``，專門把「看起來像
    網路錯誤」的例外翻譯成 ``MitakeAPIError``。若本哨兵繼承自那些型別（或未來
    有人加了 ``except Exception``），它就會被翻成一個看起來很正常的 API 錯誤，
    測試裡的 ``pytest.raises(mitake.MitakeAPIError)`` 反而通過——護欄等於白做。
    繼承 ``BaseException`` 保證它穿過所有 ``except Exception`` 直達 pytest。

    途中會經過 ``_fetch_raw`` 的 ``except BaseException``，但那層只洗憑證後裸
    ``raise``，不吞例外、不改訊息；副作用是 traceback 的**內層 frame 被截斷**
    （``_blocked_open`` 那層不見了，pytest 顯示的最深 frame 變成 ``_fetch_raw``
    裡的 ``raise``）。注意 ``__traceback__`` 本身不會是 ``None`` —— 裸 ``raise``
    會沿路把外層 frame 重新掛回去。既然內層 frame 不可靠，能看到的診斷資訊就
    全部寫在本例外的訊息裡，不依賴 traceback。
    """


def _make_blocked_open(nodeid: str):
    """產生綁定該測試 nodeid 的 ``_OPENER.open`` 替身（一被呼叫就爆炸）。

    簽名收 ``*args/**kwargs``：實際呼叫形式是 ``_OPENER.open(request, timeout=...)``，
    但護欄不該因為呼叫端改了參數就失效。
    """

    def _blocked_open(*args: object, **kwargs: object):
        raise RealMitakeAPICallBlocked(
            f"測試 {nodeid} 沒有攔截網路呼叫，差點真的打到三竹 API。\n"
            "已在送出前擋下：這次沒有扣點、沒有簡訊送出去。\n"
            "三竹點數與 App 團隊共用（App 靠同一池發註冊驗證碼），"
            "一則 send_sms = 扣 1 點真實點數。\n"
            "\n"
            "修法（擇一）：\n"
            "  1. 在測試裡用自己的替身覆蓋這道護欄（絕大多數情況選這個）：\n"
            "       monkeypatch.setattr(\n"
            '           mitake._OPENER, "open", lambda *a, **k: _FakeResponse(b"...")\n'
            "       )\n"
            "     攔截點是 mitake._OPENER.open，不是 urllib.request.urlopen——\n"
            "     模組為了禁止 redirect 已改走自建 _OPENER，patch 舊的 urlopen 攔不住。\n"
            f"  2. 這支測試真的必須連外 → 掛 @pytest.mark.{ALLOW_NETWORK_MARKER}，\n"
            "     但掛之前先跑 `pytest --markers` 讀該 marker 的說明，確認會不會扣點。"
        )

    return _blocked_open


# 封鎖底線鋪在「模組層」而非 pytest_configure：落在命令列參數路徑上的子目錄 conftest
# （例如 pytest tests/zzsub）是在 _preparse 階段匯入，早於 pytest_configure，鋪在
# configure 涵蓋不到 —— 實測那個時機 _OPENER.open 還是真的，module-level 呼叫會真的連外。
# 父 conftest 一定早於子 conftest 匯入，鋪在這裡才涵蓋得到 tests/ 子樹的所有匯入期。
# 但比本檔更早載入的（repo 根的 conftest.py、pytest 外掛）仍在防護外——實測確認。
mitake._OPENER.open = _make_blocked_open("<conftest / 測試模組匯入期或高階 scope fixture>")


@pytest.fixture(autouse=True)
def _block_real_mitake_api(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """（autouse）預設把 ``mitake._OPENER.open`` 換成一律拋錯的替身。

    與「測試內自己再 monkeypatch 一次」完全相容：autouse fixture 與測試函式拿到
    的是**同一個** function-scoped ``monkeypatch`` 實例，setattr 依序記錄、teardown
    以 LIFO 還原。因此執行順序是「本 fixture 先鋪護欄 → 測試自己的替身覆蓋上去
    （後設定者生效）→ 收尾時先還原成護欄、再還原成真正的 ``_OPENER.open``」，
    既有那兩支自行 patch 的測試行為不變。

    用 ``monkeypatch`` 而非手動 setattr/還原，是為了讓還原綁在 pytest 的 teardown
    上：即使測試中途拋例外，護欄也不會外洩污染後續測試。
    """
    # 顯式逃生門：掛了 marker 的測試主動把真正的 open 還原回去。
    # 不能只 return —— 本檔模組層在匯入當下就鋪了封鎖，不還原等於 marker 失效。
    # monkeypatch 會在 teardown 把封鎖版還原回來，不會外洩到後續測試。
    if request.node.get_closest_marker(ALLOW_NETWORK_MARKER) is not None:
        monkeypatch.setattr(mitake._OPENER, "open", _REAL_OPENER_OPEN)
        return

    monkeypatch.setattr(mitake._OPENER, "open", _make_blocked_open(request.node.nodeid))


def pytest_configure(config: pytest.Config) -> None:
    """註冊 ``allow_network`` marker，避免 PytestUnknownMarkWarning。

    不註冊的話 pytest 會對每個用到的地方發警告，久了大家就學會忽略警告——
    而這個 marker 恰好是「會花真錢」的那個，最不該被淹沒在雜訊裡。
    """
    config.addinivalue_line(
        "markers",
        f"{ALLOW_NETWORK_MARKER}: 解除預設的三竹 API 封鎖，讓該測試真的連外。"
        " 用之前先想清楚會不會扣點："
        "query_balance（SmQuery）是唯讀查詢、不扣點，可以安心用；"
        "send_sms（SmSend）每則扣 1 點真實點數，且點數與 App 團隊共用"
        "（App 靠同一池發註冊驗證碼），絕不可拿來當測試。"
        " 另需執行機器的 IP 在三竹白名單內，否則只會拿到 statuscode=k。"
        " 預設不該用；離線驗證請改用 monkeypatch 替身。",
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    """把模組層鋪下的封鎖還原回真正的 open。

    不還原的話，同一個 process 內跑第二次 pytest（pytest-watch / IDE 常駐 runner /
    pytester / 腳本呼叫 pytest.main）時 conftest 會被重新匯入（實測模組物件不同），
    而此時 _OPENER.open 還停在上一輪的封鎖版 —— 於是 _REAL_OPENER_OPEN 捕獲到的
    「真的 open」其實是封鎖版，下一輪的 allow_network 逃生門會靜默失效。
    """
    mitake._OPENER.open = _REAL_OPENER_OPEN
