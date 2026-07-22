"""contrarian 效度編排（M-BR1 Phase 2；自 cli.py 薄殼呼叫）。

面板 → point-in-time flow_inflection×base_proximity 聯合桶 → 焦點桶 forward alpha
lift＋block-bootstrap CI＋regime 切片＋walk-forward → §1 三叉硬門檻裁決 →
research/contrarian_efficacy/。CLI 只保留參數解析（同 laggard_grid_runner 慣例）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import typer
from rich.console import Console

console = Console()


def run_contrarian_efficacy(settings: Path, out_dir: Path | None) -> None:
    """M-BR1 Phase 2：賣壓熄（轉買）× 貼近結構低聯合桶的前瞻報酬檢驗。

    唯一問題（docs/24 §3 開工三行）：兩條件同時成立的桶，前瞻 lift（打敗同日全宇宙
    均值）是否顯著為正且跨 regime 一致？裁決＝§1 硬門檻（lift 的 block-bootstrap CI
    下界 > 0 ＋ ≥wf_min_frac walk-forward 段為正 ＋ 跨 ≥2 regime 且含防禦為正）。
    三缺一 → contrarian_base 維持描述欄（否證亦如實記錄）。
    """
    import yaml

    from tw_screener.backtest import contrarian_efficacy as ce
    from tw_screener.backtest import factor_lab as lab
    from tw_screener.backtest.regime_slice import block_len_for_horizon
    from tw_screener.backtest.rotation_efficacy import weekly_snapshot_dates

    with open(settings) as f:
        cfg = yaml.safe_load(f)
    cec = cfg.get("backtest", {}).get("contrarian_efficacy", {})
    horizons = tuple(int(h) for h in cec.get("horizons_td", [5, 10, 20, 40]))
    focal_h = int(cec.get("focal_horizon", 20))
    n_boot = int(cec.get("n_boot", 1000))
    n_splits = int(cec.get("n_splits", 5))
    min_train_frac = float(cec.get("min_train_frac", 0.4))
    wf_min_frac = float(cec.get("wf_min_frac", 0.8))
    weekly = bool(cec.get("weekly_snapshot", True))
    panel_path = Path(
        cfg.get("backtest", {}).get("factor_lab", {}).get(
            "panel_path", "research/panel/panel.parquet"
        )
    )
    out = out_dir or Path(cec.get("output_dir", "research/contrarian_efficacy"))

    # Phase 1 描述欄門檻直接重用（settings.contrarian_base；同一口徑，避免漂移）
    cb = cfg.get("contrarian_base", {})
    thresholds = {
        "min_shares": float(cb.get("min_lots", 1000)) * 1000.0,  # 張 → 股
        "stall_share_pct": float(cb.get("stall_share_pct", 5)),
        "accel_ratio": float(cb.get("accel_ratio", 1.0)),
        "at_low_pct": float(cb.get("at_low_pct", 2.0)),
        "near_low_pct": float(cb.get("near_low_pct", 5.0)),
        "mid_pct": float(cb.get("mid_pct", 20.0)),
    }

    if not panel_path.exists():
        console.print(f"[red]無面板 {panel_path}——先跑 make build-panel[/red]")
        raise typer.Exit(1)
    panel = pl.read_parquet(panel_path)
    if panel.is_empty():
        console.print("[red]面板為空[/red]")
        raise typer.Exit(1)

    console.print("[bold]重建 point-in-time flow_inflection×base_proximity 聯合桶...[/bold]")
    sig = ce.contrarian_signal_panel(
        panel,
        min_shares=thresholds["min_shares"],
        stall_share_pct=thresholds["stall_share_pct"],
        accel_ratio=thresholds["accel_ratio"],
        at_low_pct=thresholds["at_low_pct"],
        near_low_pct=thresholds["near_low_pct"],
        mid_pct=thresholds["mid_pct"],
    )
    if weekly:
        wk = set(weekly_snapshot_dates(sig["date"].unique().to_list()))
        sig = sig.filter(pl.col("date").is_in(list(wk)))
    # lift{h}＝alpha{h} − 當日全宇宙均值：正確零假設（alpha 用中位、右偏 → 桶均值 vs 0 有偏）
    sig = ce.add_universe_lift(sig, horizons=horizons)
    n_focal = sig.filter(pl.col(ce.FOCAL_LABEL)).height
    n_weeks = sig["date"].n_unique()
    console.print(
        f"  週快照 {n_weeks} 週；焦點桶（轉買×貼近低）命中 {n_focal} 個 股×週。"
    )

    lift = ce.contrarian_lift_table(sig, horizons=horizons, n_boot=n_boot)
    if lift.is_empty():
        console.print("[red]lift 表為空——面板缺 alpha{h} 欄？[/red]")
        raise typer.Exit(1)
    decomp = ce.contrarian_decomp_grid(sig, horizon=focal_h, n_boot=n_boot)

    block_len = block_len_for_horizon(focal_h, weekly=weekly)
    emb = block_len
    # walk-forward＋全樣本 CI 皆以 lift 為量尺（打敗宇宙才算 edge）
    wf, wf_pos, wf_valid = ce.contrarian_walk_forward(
        sig, horizon=focal_h, n_splits=n_splits,
        min_train_frac=min_train_frac, embargo_td=emb, metric="lift",
    )
    full_ci = ce.contrarian_full_ci(
        sig, horizon=focal_h, block_len=block_len, n_boot=n_boot, metric="lift"
    )

    # ── regime 切片（焦點桶 lift per-date 序列・moving-block CI・§1 含空頭硬化）──
    has_regime = "regime" in sig.columns and not sig["regime"].drop_nulls().is_empty()
    if has_regime:
        focal = sig.filter(pl.col(ce.FOCAL_LABEL))
        rslices = lab.regime_mean_slices(
            focal, f"lift{focal_h}", block_len=block_len, n_boot=n_boot
        )
        # regime_alignment_verdict 僅作報表對照（不強制含空頭）；硬門檻由 gate 自判防禦
        regime_verdict, same_reg, present = lab.regime_alignment_verdict(
            rslices, 1, value_col="mean"
        )
    else:
        rslices = pl.DataFrame()
        regime_verdict, same_reg, present = "regime 樣本不足（無可判切片）", [], 0

    passed, gate_str = ce.contrarian_gate_verdict(
        full_ci, wf_pos, wf_valid, rslices, wf_min_frac=wf_min_frac
    )

    # ── 落地：CSV + MD ────────────────────────────────────────────────────────
    tag = date.today().strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    lift.write_csv(out / f"contrarian_lift_{tag}.csv")
    if not decomp.is_empty():
        decomp.write_csv(out / f"contrarian_decomp_{tag}.csv")
    if not wf.is_empty():
        wf.write_csv(out / f"contrarian_walkforward_{tag}.csv")
    if not rslices.is_empty():
        rslices.write_csv(out / f"contrarian_regime_{tag}.csv")

    md = _render_md(
        sig=sig, lift=lift, decomp=decomp, wf=wf, rslices=rslices,
        focal_h=focal_h, n_boot=n_boot, block_len=block_len, full_ci=full_ci,
        wf_pos=wf_pos, wf_valid=wf_valid, regime_verdict=regime_verdict,
        same_reg=same_reg, present=present, gate_str=gate_str, thresholds=thresholds,
    )
    md_path = out / f"contrarian_efficacy_{tag}.md"
    md_path.write_text(md, encoding="utf-8")
    console.print(f"[green]M-BR1 Phase 2 報告 → {md_path}[/green]")
    console.print(f"[bold]裁決：{gate_str}[/bold]")


def _c(v: object, suf: str = "", nd: int = 2) -> str:
    return f"{float(v):+.{nd}f}{suf}" if isinstance(v, (int, float)) else "—"


def _render_md(  # noqa: PLR0913, PLR0915 — 純字串拼裝報表，扁平易讀優於過度拆分
    *,
    sig: pl.DataFrame,
    lift: pl.DataFrame,
    decomp: pl.DataFrame,
    wf: pl.DataFrame,
    rslices: pl.DataFrame,
    focal_h: int,
    n_boot: int,
    block_len: int,
    full_ci: tuple[float | None, float | None, float | None, int],
    wf_pos: int,
    wf_valid: int,
    regime_verdict: str,
    same_reg: list[str],
    present: int,
    gate_str: str,
    thresholds: dict[str, float],
) -> str:
    from tw_screener.backtest import factor_lab as lab

    thr = thresholds
    span = f"{sig['date'].min()!s}~{sig['date'].max()!s}"
    reg_dist = "—"
    if "regime" in sig.columns:
        reg_dist = "、".join(
            f"{reg} n={int(sig.filter(pl.col('regime') == reg).height)}"
            for reg in lab.REGIME_LABELS
        )

    lines = [
        "# M-BR1 底部左側偵測 Phase 2 因子檢驗（規劃書 24 §3）",
        "",
        f"- 產出日：{date.today()}；面板週快照 {sig['date'].n_unique()} 週；"
        f"樣本期間 {span}。",
        "- 唯一問題：「賣壓熄（flow_inflection=轉買）× 貼近結構低（base_proximity∈"
        "{在低,貼低}）」聯合桶，前瞻 **lift（打敗同日全宇宙均值）** 是否顯著為正且跨 "
        "regime 一致？",
        f"- 口徑：net_20d/5d＝日 foreign_net 滾動和；dist_low_60d＝close 對 60 日"
        f"滾動低；門檻沿 settings.contrarian_base（min_lots={int(thr['min_shares'] / 1000)}"
        f"、貼低≤{thr['near_low_pct']:.0f}%）。**量尺＝lift{{h}}＝alpha{{h}} − 當日全宇宙"
        "alpha 均值**（alpha=r−當日中位、報酬右偏 → 全宇宙均值 > 0，故桶均值 vs 0 有偏；"
        "lift 零假設才是 0）。",
        "- **升 gate 唯一開關（§1 硬門檻，寫死）**：lift 的 block-bootstrap CI 下界 > 0 ＋ "
        "≥4/5 walk-forward 段 lift 為正 ＋ 跨 ≥2 regime **且含防禦（2022 空頭）** 為正。"
        "三缺一 → 描述欄。",
        "",
        f"## 裁決：{gate_str}",
        "",
        f"- 焦點桶全樣本 lift（r+{focal_h}）：**{_c(full_ci[0], '%')}**"
        f"（block-bootstrap CI95 "
        f"[{_c(full_ci[1])}, {_c(full_ci[2])}]・n_dates={full_ci[3]}）。",
        "",
    ]

    # 焦點桶 vs 全體 pooled lift
    lines += [
        "## 焦點桶 vs 全體 pooled lift（各前瞻窗）",
        "",
        "| 窗 | 桶 | n | mean | CI95 | median | win | 前半 | 後半 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in lift.iter_rows(named=True):
        ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
        win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
        lines.append(
            f"| r+{r['horizon']} | {r['bucket']} | {r['n']} | {_c(r['mean'], '%')} "
            f"| {ci} | {_c(r['median'], '%')} | {win} | {_c(r['mean_h1'], '%')} "
            f"| {_c(r['mean_h2'], '%')} |"
        )
    lines += [
        "",
        "> **桶 mean 直接與（全體）比即見 lift**：桶 alpha 顯著低於全體＝負 lift＝落後宇宙。"
        "pooled CI＝iid bootstrap，相鄰週窗重疊時偏樂觀，僅作方向/效應量參考；升 gate 的 "
        "CI 判準走上方 lift 的 block-bootstrap ＋ walk-forward ＋ regime 切片。",
        "",
    ]

    # flow×base 分解格
    if not decomp.is_empty():
        lines += [
            f"## flow_inflection × base_proximity 分解格（r+{focal_h}・全 cell）",
            "",
            "| flow | base | n | mean | CI95 | win |",
            "|---|---|---|---|---|---|",
        ]
        for r in decomp.iter_rows(named=True):
            ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
            lines.append(
                f"| {r['flow']} | {r['base']} | {r['n']} | {_c(r['mean'], '%')} "
                f"| {ci} | {win} |"
            )
        lines += ["", "> 看焦點桶（轉買×在低/貼低）的 lift 是否真來自交互作用、"
                  "而非單臂（如 base 貼低本身有 lift、與 flow 無關）。", ""]

    # walk-forward（lift 為量尺；為正＝該段打敗宇宙）
    lines += [
        f"## walk-forward（r+{focal_h} lift・expanding＋embargo；§1「≥4/5 段為正」）",
        "",
        f"- lift 為正段數：**{wf_pos}/{wf_valid}**（打敗宇宙的段數；mean 欄＝該段 lift）。",
        "",
        "| 段 | 測試起 | 測試迄 | n | n_dates | lift |",
        "|---|---|---|---|---|---|",
    ]
    for r in wf.iter_rows(named=True):
        lines.append(
            f"| {r['split_id']} | {r['test_start']} | {r['test_end']} | {r['n']} "
            f"| {r['n_dates']} | {_c(r['mean'], '%')} |"
        )
    lines.append("")

    # regime 切片（lift 為量尺；§1 含空頭硬化＝防禦須可判且正）
    lines += [
        f"## regime 切片（焦點桶 lift・r+{focal_h}・moving-block CI・§1 含 2022 空頭硬化）",
        "",
        f"- 對照 regime_alignment_verdict（不強制含空頭）：**{regime_verdict}**"
        f"（可判且正切片 {len(same_reg)}/{present}"
        f"：{'、'.join(same_reg) if same_reg else '無'}）。**硬門檻另須防禦片可判且正**。",
        "",
    ]
    if rslices.is_empty():
        lines += ["> regime 標籤未產（面板 regime 欄缺席或全 null）——先跑 "
                  "make regime-history 再 make build-panel。", ""]
    else:
        lines += [
            "| regime | n | n_dates | lift | bs_CI95 | 樣本 |",
            "|---|---|---|---|---|---|",
        ]
        for r in rslices.iter_rows(named=True):
            ci = f"[{_c(r['ci_lo'])}, {_c(r['ci_hi'])}]" if r["ci_lo"] is not None else "—"
            flag = "樣本不足" if r["thin"] else "可判"
            lines.append(
                f"| {r['regime']} | {r['n']} | {r['n_dates']} | {_c(r['mean'], '%')} "
                f"| {ci} | {flag} |"
            )
        lines.append("")

    lines += lab.inference_footer(
        sample_span=span,
        regime_dist=reg_dist,
        method_desc=(
            f"焦點桶 per-date mean 序列 moving-block bootstrap（block={block_len}"
            f"・B={n_boot}・seed=42）；pooled 為 iid bootstrap 僅供對照（docs/22 §7.2）；"
            "walk-forward expanding＋embargo 防前瞻窗跨界"
        ),
        membership_desc=(
            "面板普通股宇宙（4 位數字濾網）；歷史段 TPEX 上櫃回查為 no-op → "
            "實質為上市宇宙檢驗（OTC 2022-24 永久斷供，如實揭露）"
        ),
    )
    return "\n".join(lines)
