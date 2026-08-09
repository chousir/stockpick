"""M4 主排序去落後化（委託書 M4）——轉折早段的三個描述欄＋「轉折埋伏」候選源 E。

**要修的病**：現行主排序鑰匙是「外資 5/10/20 三窗同買」。三窗同買是**確認**訊號——等它
成立時，法人往往已經買了一段，買到的是**建倉尾段**。M4 補的是同一批資料的**早段**讀法：
把「近端相對長窗的加速度」與「剛由賣轉買的天數」拆出來當獨立欄。

**全部純描述、非 gate**（沿 F5／M-WS5a／M-BR1 既有 pattern）：
docs/22 §2 已證 `flow_turn`（退潮/回流）三窗 CI 全跨 0、無前瞻證據；docs/24 §3.1 又證
「轉買 × 貼低」兩條件桶顯著落後宇宙。本模組的欄位**同屬未經前瞻檢驗的直覺**，存在意義是
讓「早段 vs 尾段」這個區分**可被看見、可被回測**，不是再加一條沒證據的排序主判。
`inflection_ambush` 清單只 surfacing 給人逐檔過，**不自動進 picks、不改排序、不改剔除**。
"""

from __future__ import annotations

# fundamental_health 合格集合（口徑對齊 M1.1，見 analysis/contrarian.py）
_FUND_OK = frozenset({"強化", "穩健"})


def flow_diff_5_20(net_5d: float | None, net_20d: float | None) -> float | None:
    """M4.1：近 5 日淨額 −（20 日淨額 / 4）＝近端相對長窗的**加速度**。

    口徑說明：20 日除以 4 是把它換算成「同樣 5 日長度」的平均，兩者才可相減。
      - 正且大 → 近端流入**加速**＝早段（法人剛加碼，20 日還沒追上）
      - 20 日大正但本欄為負 → **尾段**（前段買很兇、近端已在減碼）

    任一窗缺值 → None（誠實 null，不用 0 代替「沒資料」——0 的語意是「剛好打平」）。
    """
    if net_5d is None or net_20d is None:
        return None
    return round(float(net_5d) - float(net_20d) / 4.0, 1)


def margin_slim(
    margin_chg_5d_lots: float | None,
    momentum_5d_pct: float | None,
    flat_pct: float = -1.0,
) -> bool | None:
    """M4.1：融資減肥＝**融資近 5 日減** ∧ **價格近 5 日平或漲**。

    讀法（docs/11 已有的融資讀法，這裡做成欄）：融資減＋股價撐住＝散戶槓桿被洗掉但價格
    沒跌＝籌碼轉乾淨；反之融資增＋股價漲＝散戶追價（過熱風險）。

    Args:
        margin_chg_5d_lots: 融資近 5 日增減（張）；負＝減。
        momentum_5d_pct: 近 5 日漲幅%（已除息還原）。
        flat_pct: 「平」的容忍下界——跌幅在此之內仍算平（settings，預設 −1%）。

    Returns:
        True/False；任一輸入缺值 → **None**（不是 False）——上櫃股天生無融資資料，
        標 False 會讓人以為「查過了、不是減肥」，而事實是沒這筆資料。
    """
    if margin_chg_5d_lots is None or momentum_5d_pct is None:
        return None
    return float(margin_chg_5d_lots) < 0 and float(momentum_5d_pct) >= flat_pct


def is_inflection_ambush(
    fundamental_health: str | None,
    base_zone: str | None,
    dist_low_60d_pct: float | None,
    foreign_inflection_days: int | None,
    foreign_net_20d_lots: float | None,
    near_low_pct: float = 10.0,
    inflection_days_range: tuple[int, int] = (1, 5),
    small_positive_lots: float = 5000.0,
    allow_base_zone_branch: bool = True,
) -> bool:
    """M4.2 候選源 E「轉折埋伏」＝**法人剛開始買、價格還在底部**。

    四條件同時成立（委託書 M4.2）：
      1. 基本面達標（`fundamental_health ∈ {強化,穩健}`，口徑同 M1.1）
      2. `base_zone=貼底` **或** 距 60 日低 ≤ `near_low_pct`
      3. `foreign_inflection_days ∈ [lo, hi]`（剛轉買，數字大＝已買一段＝尾段）
      4. 20 日外資 ≤ 0 或**小正**（20 日已大買＝確認訊號＝尾段，正是要避開的）

    條件 2 的 `貼底` 分支量的是**距季線 ≤10%（未延伸）**、不是距低點——兩者可差很遠，
    語意陷阱與處置見 `report/inflection_ambush.py` docstring。`allow_base_zone_branch=False`
    可只留「距低」分支（回收用的一行 settings，不刪碼；同 M1.6 的回退設計）。

    與 M1 `contrarian_ready` 的分工（兩者會重疊但不等價）：
      - M1 要求 `foreign_flow_inflection=轉買` ＋ 貼結構低（≤5%）＋ **未破新低**，
        是「可小注承接」的**進場**資格。
      - 本函式放寬到「距低 ≤10%」且不要求未破新低，是**候選源**——產出清單給人逐檔過，
        判進機會層或觀察皆可，**不是進場資格**。

    Returns:
        True＝進「轉折埋伏」清單。缺值一律不合格（缺資料不放行）。
    """
    if fundamental_health not in _FUND_OK:
        return False
    at_base = (allow_base_zone_branch and base_zone == "貼底") or (
        dist_low_60d_pct is not None and float(dist_low_60d_pct) <= near_low_pct
    )
    if not at_base:
        return False
    lo, hi = inflection_days_range
    if foreign_inflection_days is None or not (lo <= int(foreign_inflection_days) <= hi):
        return False
    if foreign_net_20d_lots is None:
        return False
    return float(foreign_net_20d_lots) <= small_positive_lots
