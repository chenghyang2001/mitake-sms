# tools/ 是「內網 producer 工具」套件，與核心零依賴服務（repo 根的 mitake.py / web/）
# 完全獨立：本套件可用 pymysql + playwright，但絕不 import web/ 或 mitake.py，
# 也不被它們 import。放成 package 只為了讓測試能以 `from tools.build_recipients import ...`
# 匯入純函式做離線驗證（pytest 需要一個可 import 的套件路徑）。
