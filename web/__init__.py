"""三竹簡訊 Web 發送介面套件（第三部分）。

本套件是整個專案**唯一會花錢**的入口：每送出一則簡訊就從與 App 團隊共用的
點數池扣 1 點（長內容倍數扣），扣光會直接害 App 發不出註冊驗證碼。所以這裡的
每個設計決策都以「不要讓人不小心多花錢、也不要讓人因誤解而重複發送」為最高優先。

模組分工：

``web.audit``
    發送稽核留底（誰在何時發了幾則、msgid 是什麼、是否可能已扣點）。
``web.templates``
    HTML 產生。使用者輸入一律 ``html.escape`` 後才進頁面。
``web.server``
    路由與發送流程（二階段送出、一次性 token、速率上限、失敗分兩種畫面）。

與 :mod:`mitake` 一樣**只用標準庫**，VPS 部署零外部依賴。

本檔刻意不做任何 import：``web.server`` 會先把 repo root 補進 ``sys.path`` 才
``import mitake``，若這裡先 import 子模組，反而會讓匯入順序變得脆弱。
"""

# 版本資訊的**單一真相來源**。側欄（web.templates._sidebar）與測試都從這裡取值，
# 而不是各自寫死字串 —— 改版號只改這一處，全站與回歸測試會同步跟著動。
# 這裡放純字串常數不違反「本檔不 import 子模組」的原則（見上方 docstring）：
# 脆弱的是匯入順序，不是模組層常數。
__version__ = "0.002"
__release_date__ = "2026-07-30"

__all__ = ["audit", "recipients", "server", "templates", "__version__", "__release_date__"]
