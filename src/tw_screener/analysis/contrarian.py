"""M-BR1 底部左側偵測揭露欄（規劃書 24）——賣壓熄火 × 基本面完好 × 貼近結構低。

四個純函式對應規格 §2 的三欄與 §4.1 的組合 tag，全部**純揭露非 gate**。

存在意義：docs/22 §2 已證 flow_turn（退潮/資金回流）三窗 CI 全跨 0、無前瞻證據，
「退潮→降級」舊讀法作廢。「賣壓熄→可逢低」直覺同屬未證實類——本模組把它做成
**可被回測證偽的欄位**，而不是再加一條沒證據的門檻（重蹈 flow_turn 覆轍是本規格
最要避免的失敗）。

**Phase 2 已否證（docs/24 §3.1・2026-07-22）**：面板 point-in-time 重建（201 週上市
宇宙）測「轉買×貼近低」聯合桶，桶 **lift（打敗當日全宇宙均值）r+20 = −2.30%**、CI95
[−3.52,−1.26]、walk-forward 1/5 為正、進攻/中性 regime 皆顯著負——三門檻全未過。故
`contrarian_base` **定案為描述 tag、永不升 gate、不寫入 picks**（同 flow_turn 進否證檔）。
四欄仍保留供人工觀察與未來複核，但舉證責任在證明有 edge。

與 grouping.near_flow_state 的分工（docs/24 §2.2）：`flow_state` 結構上是**買方旗標**
（要求 20 日淨買超 ≥ min_shares，20 日為負時回 None，天生表達不了「由賣轉買」）；
本模組的 `flow_inflection` 是**雙向**分類，20 日買賣兩側都評。兩欄並存不互相取代——
賣方鏡像併入 flow_state 會讓部分 None 變有值、連帶改動 classify_risk_kind 的
`籌碼熄火` 判定，違反 M-BR1 Phase 1「既有輸出逐位元不變」的純加法約束。
"""

from __future__ import annotations

# flow_inflection 值域（docs/24 §2.2）：買賣方向由近 5 日淨額正負決定，
# 「加速 vs 轉向/熄火」由近 5 日日均相對 20 日日均的步調決定。
FLOW_TURN_BUY = "轉買"      # 20 日賣超主導但近 5 日翻買＝賣壓熄的正訊號（M-BR1 條件之一）
FLOW_STALL = "熄火"          # 仍在賣但近端佔比極小＝賣壓幾乎停（尚未翻買）
FLOW_ACCEL_SELL = "加速賣"   # 近 5 日賣、且日均步調快於 20 日＝賣壓未熄（M-BR1 反例）
FLOW_TURN_SELL = "轉賣"      # 20 日買超主導但近 5 日翻賣（步調未加速）
FLOW_ACCEL_BUY = "加速買"    # 近 5 日買、且日均步調快於 20 日
FLOW_FLAT = "平穩"           # 方向與 20 日一致、步調無明顯變化

# base_proximity 值域（docs/24 §2.3）
BASE_AT_LOW = "在低"
BASE_NEAR_LOW = "貼低"
BASE_MID = "中段"
BASE_HIGH = "高檔"

# fundamental_health 值域（docs/24 §2.1）
FUND_STRONG = "強化"
FUND_SOLID = "穩健"
FUND_DECEL = "減速"
FUND_WORSE = "轉差"
FUND_UNKNOWN = "待查"

# contrarian_base 三條件各自的合格集合（§4.1）
_CONTRARIAN_HEALTH = frozenset({FUND_STRONG, FUND_SOLID})
_CONTRARIAN_PROXIMITY = frozenset({BASE_AT_LOW, BASE_NEAR_LOW})


def flow_inflection(
    net_20d: float | None,
    net_5d: float | None,
    min_shares: float = 1_000_000.0,
    stall_share_pct: float = 5.0,
    accel_ratio: float = 1.0,
) -> str | None:
    """M-BR1 §2.2：法人近端流向拐點（二階導）——20 日累計蓋住的近端方向與步調。

    與 near_flow_state 的差別是**雙向**：20 日賣超主導時仍分類（那正是左側佈局要看的
    區間），而 near_flow_state 在該情形回 None。判定順序：先看 20 日主導方向，再看
    近 5 日淨額正負定買賣，最後比日均步調定「加速」或「轉向/熄火/平穩」。

    Args:
        net_20d: 20 日累計淨買賣超（股）。
        net_5d: 近 5 日累計淨買賣超（股）。
        min_shares: |20 日累計| 低於此＝量太小無可揭露 → None（同 near_flow 口徑）。
        stall_share_pct: |5d| 佔 |20d| 低於此% ＝熄火（賣壓幾乎停）。
        accel_ratio: 5 日日均 / 20 日日均 超過此倍數＝加速。

    Returns:
        六值之一，或 None（資料缺／量太小）。**不得當排序主判或硬 gate**（docs/22 §2）。
    """
    # 量門檻取兩窗較大者：20 日淨額可能因買賣互抵而≈0，但近 5 日大幅單向——那正是
    # 最乾淨的拐點（群聯 20d +210 張／5d +1,687 張）。沿用 near_flow_state 的單看
    # |20d| 會把這型濾掉（該函式是買方旗標、口徑不同，見模組 docstring）。
    if net_20d is None or net_5d is None or max(abs(net_20d), abs(net_5d)) < min_shares:
        return None
    pace_20d = abs(net_20d) / 20.0
    pace_5d = abs(net_5d) / 5.0
    accelerating = pace_5d > pace_20d * accel_ratio  # pace_20d≈0 且近端有量 → True
    near_share_pct = abs(net_5d) / abs(net_20d) * 100.0 if net_20d else 100.0

    if net_20d < 0:  # 20 日賣超主導——左側佈局關心的區間
        if net_5d > 0:
            return FLOW_TURN_BUY
        if accelerating:
            return FLOW_ACCEL_SELL
        if near_share_pct < stall_share_pct:
            return FLOW_STALL
        return FLOW_FLAT
    # 20 日買超主導
    if net_5d < 0:
        return FLOW_ACCEL_SELL if accelerating else FLOW_TURN_SELL
    return FLOW_ACCEL_BUY if accelerating else FLOW_FLAT


def dist_to_low_pct(close: float | None, low: float | None) -> float | None:
    """距結構低幾 %＝(close − low)/low × 100。low ≤ 0 或缺值 → None（不臆造）。"""
    if close is None or low is None or low <= 0:
        return None
    return round((close - low) / low * 100.0, 1)


def base_proximity(
    dist_low_60d_pct: float | None,
    at_low_pct: float = 2.0,
    near_low_pct: float = 5.0,
    mid_pct: float = 20.0,
) -> str:
    """M-BR1 §2.3：與 60 日結構低的鄰近度——把「跌深」與「跌到結構」區分開。

    左側接刀必須貼近結構低才有支撐；中段回檔（如華邦電距 60 日低 +72%）接刀＝無支撐、
    不算左側佈局。缺值回空字串（不猜位階）。
    """
    if dist_low_60d_pct is None:
        return ""
    if dist_low_60d_pct <= at_low_pct:
        return BASE_AT_LOW
    if dist_low_60d_pct <= near_low_pct:
        return BASE_NEAR_LOW
    if dist_low_60d_pct <= mid_pct:
        return BASE_MID
    return BASE_HIGH


def fundamental_health(
    rev_yoy_pct: float | None,
    rev_yoy_delta: float | None = None,
    rev_yoy_delta_prev: float | None = None,
    net_margin_pct: float | None = None,
    strong_yoy_pct: float = 20.0,
    weak_yoy_pct: float = 5.0,
    decel_deep_pct: float = -30.0,
    thin_margin_pct: float = 5.0,
) -> str:
    """M-BR1 §2.1：基本面健康分類——**把營收水準與二階導拆開**。

    這欄的價值全在「拆開」：記憶體族群 YoY +621% 但加速度見頂＝`穩健/減速` 而非
    `強化`，這正是外資倒貨卻股價跌的真因（sell-the-news）；聯發科 YoY +2.8% ＝
    `減速`，即使跌深也不該被當「基本面完好」。

    只吃表內欄（rev_yoy／其二階導／淨利率），**缺 rev_yoy 一律 `待查`、不猜**。
    產業循環判讀（如 DRAM 報價）屬報告端外部查證層，不進本函式（docs/24 §7）。

    Args:
        rev_yoy_pct: 最新月營收 YoY%。None → `待查`。
        rev_yoy_delta: 本月 YoY − 上月 YoY（營收動能二階導）；None＝上月無快取。
        rev_yoy_delta_prev: 上月的 delta，供「連 2 月深負＝轉差」判定；None＝略過該條。
        net_margin_pct: 單季稅後純益率（D5 體質欄），薄利者降一級；金融/缺表 None＝不校正。
        strong_yoy_pct: YoY 達此且未降速＝強化。
        weak_yoy_pct: YoY 低於此（仍正）＝減速（低個位數成長）。
        decel_deep_pct: delta 深於此＝大幅降速。
        thin_margin_pct: 淨利率低於此＝薄利，降一級。

    Returns:
        強化／穩健／減速／轉差／待查 之一。
    """
    if rev_yoy_pct is None:
        return FUND_UNKNOWN
    if rev_yoy_pct < 0:
        return FUND_WORSE
    # 連 2 月大幅降速＝動能連續崩塌，即使 YoY 仍正也判轉差
    if (
        rev_yoy_delta is not None
        and rev_yoy_delta_prev is not None
        and rev_yoy_delta <= decel_deep_pct
        and rev_yoy_delta_prev <= decel_deep_pct
    ):
        return FUND_WORSE
    if rev_yoy_pct < weak_yoy_pct:
        grade = FUND_DECEL
    elif rev_yoy_delta is not None and rev_yoy_delta <= decel_deep_pct:
        grade = FUND_DECEL  # YoY 仍高但大幅降速
    elif rev_yoy_pct >= strong_yoy_pct and rev_yoy_delta is not None and rev_yoy_delta >= 0:
        grade = FUND_STRONG
    else:
        # 正成長但降速（未達深負）／YoY 未達強化門檻／**二階導未取得**。
        # 最後一種刻意不給 `強化`：delta 缺月時「是否加速」根本沒量到，
        # 標強化＝從缺資料推斷結論（違反誠實原則）。docs/24 §6 十銓即此型。
        grade = FUND_SOLID
    # 體質次要校正：毛利高但淨利薄 → 降一級（金融/缺表 net_margin=None 時不校正）
    if net_margin_pct is not None and net_margin_pct < thin_margin_pct:
        _demote = {FUND_STRONG: FUND_SOLID, FUND_SOLID: FUND_DECEL, FUND_DECEL: FUND_WORSE}
        grade = _demote.get(grade, grade)
    return grade


def is_contrarian_base(
    health: str | None,
    inflection: str | None,
    proximity: str | None,
) -> bool:
    """M-BR1 §4.1：三條件同時成立＝底部左側候選（**描述 tag，Phase 2 通過前不進 picks**）。

    基本面 ∈ {強化,穩健} ∧ 流向＝轉買（賣壓熄）∧ 位階 ∈ {在低,貼低}。
    負例保護（docs/24 §6 回歸測試意圖）：京元電子（20 日買超但近端加速賣）與
    聯發科（跌深但 YoY +2.8%＝減速）**必須為 False**——證明分類器沒退化成「跌深就接」。
    """
    return (
        health in _CONTRARIAN_HEALTH
        and inflection == FLOW_TURN_BUY
        and proximity in _CONTRARIAN_PROXIMITY
    )
