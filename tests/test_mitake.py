"""mitake 模組的離線測試。

**三個 test case 全部不碰真實 API**：不燒點數、不需要環境變數、CI 也能跑。
覆蓋的是純函式（decode_response / parse_response / count_sms_segments），
所有輸入都是實撥時逐字抄下來的真實回應樣本。
"""

import sys
from pathlib import Path

# mitake.py 在 repo 根、測試在 tests/ 子目錄，而 pytest 只會把 tests/ 放進 sys.path，
# 故手動補上根目錄，讓測試不論從哪個工作目錄執行都能 import。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mitake  # noqa: E402

# 實撥取得的真實回應樣本（逐字照抄，含 \r\n）。
SEND_SUCCESS_RESPONSE = "[1]\r\nmsgid=0313887539\r\nstatuscode=1\r\nAccountPoint=12572\r\n"
IP_BLOCKED_RESPONSE_BIG5 = "statuscode=k\r\nError=無效的連線位址\r\n".encode("big5")


def test_parse_send_success_extracts_msgid_statuscode_and_point():
    """Happy path：SmSend 成功回應要能完整拆出 msgid / statuscode / 剩餘點數。

    這是整個模組最常走的路徑，也是 Web 介面要回顯給使用者的欄位來源；
    多行格式 + 開頭的 `[1]` 批次序號是最容易解析歪掉的地方。
    """
    result = mitake.parse_response(SEND_SUCCESS_RESPONSE)

    assert result["success"] is True
    assert result["msgid"] == "0313887539"
    assert result["statuscode"] == "1"
    assert result["account_point"] == 12572
    assert result["batch_index"] == "1"
    assert result["error"] is None
    # 原始欄位要保留，日後三竹加欄位時才追查得到。
    assert result["raw_fields"]["AccountPoint"] == "12572"


def test_count_sms_segments_boundaries():
    """Edge case：計費邊界。70 字 = 1 則、71 字 = 2 則，錯一格就報錯成本。

    這支函式的輸出會直接顯示給使用者當作「這封會扣幾點」的依據，
    而點數與 App 團隊共用，低估成本的後果是 App 驗證碼發不出去。
    """
    assert mitake.count_sms_segments("") == (0, 0)
    assert mitake.count_sms_segments("字") == (1, 1)
    assert mitake.count_sms_segments("字" * 69) == (1, 69)
    assert mitake.count_sms_segments("字" * 70) == (1, 70)
    assert mitake.count_sms_segments("字" * 71) == (2, 71)
    assert mitake.count_sms_segments("字" * 140) == (2, 140)
    assert mitake.count_sms_segments("字" * 141) == (3, 141)


def test_ip_blocked_response_decodes_and_parses_as_failure():
    """Error case：IP 被擋的完整鏈路 —— Big5 位元組 → 解碼 → 解析 → 判失敗 → 分類。

    在「parse IP 被擋」與「validate_phone 拒絕非法輸入」之間選了前者，理由：
    它一次鎖住三個最容易回歸的行為 —— Big5 解碼（用 UTF-8 硬解會亂碼）、
    錯誤回應必須判為失敗（若誤判成功，Web 介面會告訴使用者「已送出」但其實沒送）、
    以及 statuscode=k 要能被分類成 ip_blocked（這是換機器部署最常見的失敗，
    上層要靠它提示「去申請白名單」而不是叫人重打帳密）。
    validate_phone 的失敗只影響單次輸入，不會造成誤報成功。
    """
    text = mitake.decode_response(IP_BLOCKED_RESPONSE_BIG5)
    assert "無效的連線位址" in text  # Big5 有解對，沒變成亂碼

    result = mitake.parse_response(text)
    assert result["success"] is False
    assert result["statuscode"] == "k"
    assert result["error"] == "無效的連線位址"
    assert result["msgid"] is None
    assert mitake.classify_statuscode(result["statuscode"]) == mitake.KIND_IP_BLOCKED
