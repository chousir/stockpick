"""M-BR1 底部左側偵測揭露欄（規劃書 24 §6 驗收）。

ground-truth 錨點＝2026-07-20 真實資料（docs/24 §6 表）。核心測試意圖：**京元電子
（20 日買超但近端加速賣）與聯發科（跌深但基本面停滯）必須被排除在 contrarian_base
之外**——這兩個負例證明分類器沒有退化成「跌深就接」。
"""

from tw_screener.analysis.contrarian import (
    base_proximity,
    dist_to_low_pct,
    flow_inflection,
    fundamental_health,
    is_contrarian_base,
)

K = 1000.0  # 張 → 股（規格表以張計，函式吃股）


class TestFlowInflection:
    def test_20d_sell_5d_buy_is_turn_buy(self):
        """十銓 20d -3,324／5d +1,002＝賣壓翻買（M-BR1 正訊號）。"""
        assert flow_inflection(-3324 * K, 1002 * K) == "轉買"

    def test_adata_turn_buy(self):
        """威剛 20d -6,447／5d +392。"""
        assert flow_inflection(-6447 * K, 392 * K) == "轉買"

    def test_phison_accel_buy(self):
        """群聯 20d +210／5d +1,687：20 日淨額因買賣互抵而極小，近端才是真訊號。

        量門檻取兩窗較大者，故本檔不被 min_lots 濾掉（單看 |20d| 會誤判為量小）。
        """
        assert flow_inflection(210 * K, 1687 * K) == "加速買"

    def test_volume_gate_uses_larger_window(self):
        """兩窗皆小才判無可揭露；任一窗夠大就評。"""
        assert flow_inflection(-500 * K, -300 * K) is None      # 兩窗皆 < 1000 張
        assert flow_inflection(-500 * K, 1200 * K) == "轉買"     # 近端夠大 → 仍評

    def test_kyec_20d_buy_but_near_accel_sell(self):
        """京元 20d +41,263／5d -23,442——20 日買超蓋住近端倒貨，**絕不可判賣壓熄**。"""
        assert flow_inflection(41263 * K, -23442 * K) == "加速賣"

    def test_mediatek_accel_sell(self):
        """聯發科 20d -6,853／5d -2,007：日均 401 > 343＝賣壓仍在加速。"""
        assert flow_inflection(-6853 * K, -2007 * K) == "加速賣"

    def test_winbond_decelerating_sell_is_not_accel(self):
        """華邦電 20d -305,441／5d -24,885：日均 4,977 << 15,272＝賣壓明顯減速。

        docs/24 §6 表把本檔標為「加速賣」，與規格 §2.2 自訂的加速定義
        （|5d 日均| > |20d 日均|）矛盾——依**公式**判為平穩（減速但仍在賣、
        佔比 8.1% 未達熄火門檻）。contrarian_base=FALSE 的結論不受影響。
        """
        assert flow_inflection(-305441 * K, -24885 * K) == "平穩"

    def test_stall_when_selling_nearly_stopped(self):
        assert flow_inflection(-100000 * K, -2000 * K) == "熄火"

    def test_none_when_missing_or_too_small(self):
        assert flow_inflection(None, 100 * K) is None
        assert flow_inflection(-3324 * K, None) is None


class TestBaseProximity:
    def test_zones(self):
        assert base_proximity(0.0) == "在低"
        assert base_proximity(2.0) == "在低"
        assert base_proximity(3.0) == "貼低"       # 群聯
        assert base_proximity(5.7) == "中段"       # 京元
        assert base_proximity(22.0) == "高檔"      # 晶豪科 +22%
        assert base_proximity(72.0) == "高檔"      # 華邦電：中段回檔≠跌到結構
        assert base_proximity(None) == ""

    def test_dist_to_low_pct(self):
        assert dist_to_low_pct(110.0, 100.0) == 10.0
        assert dist_to_low_pct(100.0, 100.0) == 0.0
        assert dist_to_low_pct(None, 100.0) is None
        assert dist_to_low_pct(110.0, None) is None
        assert dist_to_low_pct(110.0, 0.0) is None  # 除以零防護


class TestFundamentalHealth:
    def test_missing_yoy_is_unknown(self):
        """威剛/群聯 YoY 無快取 → 待查，**不猜**。"""
        assert fundamental_health(None) == "待查"

    def test_high_yoy_accelerating_is_strong(self):
        """晶豪科 YoY +328% 且未降速。"""
        assert fundamental_health(328.0, 10.0) == "強化"

    def test_high_yoy_but_delta_unmeasured_is_solid_not_strong(self):
        """十銓 YoY +35% 但上月無快取 → 穩健，**不得標強化**。

        「是否加速」沒量到就不能宣稱加速（誠實原則）。docs/24 §6 表亦期望穩健。
        """
        assert fundamental_health(35.0, None) == "穩健"

    def test_high_yoy_mild_decel_is_solid(self):
        """記憶體型：YoY 高但加速度見頂＝穩健，非強化（sell-the-news 真因）。"""
        assert fundamental_health(190.0, -20.0) == "穩健"

    def test_high_yoy_deep_decel_is_decel(self):
        assert fundamental_health(190.0, -80.0) == "減速"

    def test_mediatek_low_yoy_is_decel(self):
        """聯發科 YoY +2.8%＝低個位數，即使跌深也不算基本面完好。"""
        assert fundamental_health(2.8, None) == "減速"

    def test_negative_yoy_is_worse(self):
        assert fundamental_health(-12.0) == "轉差"

    def test_two_consecutive_deep_decel_is_worse(self):
        """連 2 月深負二階導＝動能連續崩塌，YoY 仍正也判轉差。"""
        assert fundamental_health(120.0, -50.0, -40.0) == "轉差"

    def test_thin_net_margin_demotes_one_grade(self):
        """毛利高但淨利薄 → 降一級（強化→穩健）。"""
        assert fundamental_health(328.0, 10.0, None, 2.0) == "穩健"

    def test_none_net_margin_does_not_demote(self):
        """金融/缺表股 net_margin=None → 不校正（誠實 null，不當薄利罰）。"""
        assert fundamental_health(328.0, 10.0, None, None) == "強化"


class TestContrarianBase:
    def test_teamgroup_true(self):
        """十銓：轉買＋穩健＋在低＝三條件成立。"""
        assert is_contrarian_base("穩健", "轉買", "在低") is True

    def test_kyec_false_near_term_accel_sell(self):
        """京元：位階貼低但近端加速賣 → FALSE（負例保護）。"""
        assert is_contrarian_base("穩健", "加速賣", "貼低") is False

    def test_mediatek_false_fundamentals_stalled(self):
        """聯發科：即使假設賣壓熄且貼低，基本面減速 → FALSE（負例保護）。"""
        assert is_contrarian_base("減速", "轉買", "在低") is False

    def test_winbond_false_mid_range(self):
        """華邦電：基本面穩健但未貼結構低（中段回檔＝無支撐）→ FALSE。"""
        assert is_contrarian_base("穩健", "轉買", "中段") is False

    def test_unknown_health_false(self):
        """待查不等於完好——缺 YoY 不得自動放行。"""
        assert is_contrarian_base("待查", "轉買", "在低") is False

    def test_null_inflection_false(self):
        assert is_contrarian_base("強化", None, "在低") is False


# ── 委託書 M1.1 防接刀補強（2026-08-08 裁決 A 人工解禁）──────────────────


def test_trailing_buy_streak_days_counts_from_latest():
    """由最新日往回數連續買超天數；最新日非買超＝0；空序列＝None（不記 0）。"""
    from tw_screener.analysis.contrarian import trailing_buy_streak_days

    assert trailing_buy_streak_days([-500, -300, 100, 200, 300]) == 3
    assert trailing_buy_streak_days([100, 200, -50]) == 0      # 最新日在賣
    assert trailing_buy_streak_days([100, 200, 300]) == 3
    assert trailing_buy_streak_days([-100, 0, 200]) == 1       # 0 不算買超
    assert trailing_buy_streak_days([100, None, 300]) == 1     # None 視為中斷、不猜
    assert trailing_buy_streak_days([]) is None


def test_contrarian_entry_ready_requires_streak_and_no_new_low():
    """三條件 tag 成立仍不夠——連買天數不足或正在破新低者一律不合格。"""
    from tw_screener.analysis.contrarian import contrarian_entry_ready

    # 合格：tag True ＋ 連買 2 日 ＋ 距 60 日低 +3.2%（未破新低）
    assert contrarian_entry_ready(True, 2, 3.2) is True
    # 單日翻買就接刀 → 擋下
    assert contrarian_entry_ready(True, 1, 3.2) is False
    # 當日收盤就是 60 日新低（dist=0，low_60d 窗含當日）→ 仍在破底、擋下
    assert contrarian_entry_ready(True, 3, 0.0) is False
    # 法人資料缺（streak=None）→ 缺資料不放行
    assert contrarian_entry_ready(True, None, 3.2) is False
    # 位階不明 → 不放行
    assert contrarian_entry_ready(True, 3, None) is False
    # tag 本身不成立 → 前面條件再好也不合格
    assert contrarian_entry_ready(False, 5, 3.2) is False


def test_contrarian_entry_ready_thresholds_are_configurable():
    """門檻走 settings（鐵律 5），不寫死在程式裡。"""
    from tw_screener.analysis.contrarian import contrarian_entry_ready

    assert contrarian_entry_ready(True, 2, 3.2, min_buy_streak_days=3) is False
    assert contrarian_entry_ready(True, 3, 3.2, min_buy_streak_days=3) is True
    assert contrarian_entry_ready(True, 3, 0.5, new_low_eps_pct=1.0) is False
    assert contrarian_entry_ready(True, 3, 1.5, new_low_eps_pct=1.0) is True
