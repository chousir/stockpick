// 個股 detail 頁（docs/17 §5.4）：按 CLAUDE.md Part 3.2 報告框架卡片化。
// 不下多空結論、不報目標價（Part 3.5）；缺資料顯「未取得」不報錯。

import { useEffect, useState } from "react";
import { RangeBar } from "../components/RangeBar";
import { ApiError, getStock, type Row, type StockDetailResponse } from "../lib/api";
import { fmtNum, flagClass, isBlank, signClass } from "../lib/format";
import { stratColor } from "../charts/echartsBase";

const SOURCE_LABEL: Record<string, string> = {
  holdings: "持股",
  candidates: "候選",
  watchlist: "觀察",
};
const STRAT_NAME: Record<string, string> = {
  d: "品質領頭",
  e: "成長動能",
  f: "價值回升",
  g: "成長回檔",
};

interface Props {
  week: string;
  stockId: string | null;
  onBack: () => void;
}

// 一格標籤值（缺值顯「—」）
function Stat({ label, value, cls }: { label: string; value: React.ReactNode; cls?: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${cls ?? ""}`}>{value}</span>
    </div>
  );
}

function num(v: unknown, digits = 2, suffix = "") {
  return isBlank(v) ? <span className="num-flat">—</span> : `${fmtNum(v, digits)}${suffix}`;
}
function signed(v: unknown, digits = 2, suffix = "") {
  return <span className={signClass(v)}>{isBlank(v) ? "—" : `${fmtNum(v, digits)}${suffix}`}</span>;
}

// 單檔法人 bar：近端各窗淨買賣超，紅買綠賣，寬度 ∝ |張數|/最大值（docs/17 §5.4 卡3）。
function ChipFlowBars({ row }: { row: Row }) {
  const items = [
    { label: "三大法人 20日", v: row.inst_net_lots },
    { label: "外資 20日", v: row.foreign_net_lots },
    { label: "外資 10日", v: row.foreign_net_10d_lots },
    { label: "外資 5日", v: row.foreign_net_5d_lots },
    { label: "投信 20日", v: row.trust_net_lots },
  ].map((x) => ({ ...x, n: isBlank(x.v) ? null : Number(x.v) }));
  const maxAbs = Math.max(1, ...items.map((x) => (x.n === null ? 0 : Math.abs(x.n))));
  if (items.every((x) => x.n === null))
    return <div className="num-flat">本檔籌碼資料未取得</div>;
  return (
    <div className="flow-bars">
      {items.map((x) => (
        <div className="flow-row" key={x.label}>
          <span className="flow-label">{x.label}</span>
          <div className="flow-track">
            {x.n !== null && (
              <span
                className={`flow-fill ${x.n >= 0 ? "up" : "down"}`}
                style={{ width: `${(Math.abs(x.n) / maxAbs) * 100}%` }}
              />
            )}
          </div>
          <span className={`flow-val ${signClass(x.v)}`}>
            {x.n === null ? "—" : `${x.n >= 0 ? "買" : "賣"}${fmtNum(Math.abs(x.n), 0)}`}
          </span>
        </div>
      ))}
    </div>
  );
}

function quadClass(q: unknown): string {
  const s = String(q ?? "");
  if (s.includes("主升")) return "teal";
  if (s.includes("下一棒")) return "amber";
  if (s.includes("出貨") || s.includes("警訊")) return "alert";
  return "muted";
}

export function StockDetail({ week, stockId, onBack }: Props) {
  const [data, setData] = useState<StockDetailResponse | null>(null);
  const [error, setError] = useState<{ status: number; msg: string } | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!stockId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    getStock(week, stockId)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ApiError) setError({ status: e.status, msg: e.message });
        else setError({ status: 0, msg: String(e) });
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [week, stockId]);

  if (!stockId)
    return (
      <div className="empty-state">
        從<b> 候選股 </b>表或<b> 族群成員 </b>點選個股，即可在此鑽取。
      </div>
    );
  if (loading) return <div className="placeholder">載入中…</div>;
  if (error)
    return (
      <div className="empty-state">
        <div className="warn">⚠ {error.status === 404 ? `本週查無個股 ${stockId}` : `載入失敗：${error.msg}`}</div>
        <div style={{ marginTop: 8 }}>週次 {week}</div>
      </div>
    );
  if (!data) return null;

  const r = data.row;
  const hasRange = !isBlank(r.low_20d) || !isBlank(r.low_60d);

  return (
    <div className="detail">
      {/* 1. 頭部卡 */}
      <div className="hud-card detail-head">
        <div className="detail-id">
          <span className="cell-id detail-code">{data.stock_id}</span>
          <span className="detail-name">{String(r.name ?? "—")}</span>
          <span className="detail-tags">
            {String(r.industry ?? "—")}
            {!isBlank(r.theme) && <span className="muted"> · {String(r.theme)}</span>}
          </span>
        </div>
        <div className="detail-price">
          <span className={signClass(r.change_pct)} style={{ fontSize: 22, fontWeight: 700 }}>
            {num(r.close, 2)}
          </span>
          <span className={signClass(r.change_pct)}> {signed(r.change_pct, 2, "%")}</span>
        </div>
        <span className="shell-spacer" />
        <div className="detail-meta">
          <div className="src-badges">
            {data.sources.map((s) => (
              <span key={s} className="chip on">
                {SOURCE_LABEL[s] ?? s}
              </span>
            ))}
          </div>
          {data.goodinfo_url && (
            <a className="cell-link" href={data.goodinfo_url} target="_blank" rel="noreferrer">
              Goodinfo ↗
            </a>
          )}
          <button className="chip" onClick={onBack}>
            ← 返回候選股
          </button>
        </div>
      </div>

      <div className="detail-grid">
        {/* 2. 基本面卡 */}
        <div className="hud-card">
          <h3>基本面</h3>
          <div className="stat-grid">
            <Stat label="營收YoY%" value={signed(r.rev_yoy_pct, 1, "%")} />
            <Stat label="毛利率%" value={num(r.gross_margin_pct, 1, "%")} />
            <Stat label="單季EPS" value={num(r.eps_q, 2)} />
            <Stat label="PE" value={num(r.pe_ratio, 1)} />
            <Stat label="PB" value={num(r.pb_ratio, 2)} />
            <Stat label="殖利率%" value={num(r.dividend_yield_pct, 2, "%")} />
            <Stat label="估值位階" value={num(r.val_pctile, 0)} />
            <Stat
              label="便宜旗標"
              value={isBlank(r.cheap_flag) ? <span className="num-flat">—</span> : <span className="flag teal">{String(r.cheap_flag)}</span>}
            />
          </div>
        </div>

        {/* 3. 籌碼卡 ＋ 法人 bar */}
        <div className="hud-card">
          <h3>籌碼</h3>
          <div className="stat-grid">
            <Stat label="法人持股20日%" value={num(r.inst_pct20d, 1, "%")} />
            <Stat label="量比" value={num(r.vol_ratio, 2)} />
            <Stat label="成交量(張)" value={num(r.volume_lots_today, 0)} />
            <Stat label="成交額(百萬)" value={num(r.amount_million, 0)} />
          </div>
          <div className="chart-cap" style={{ marginTop: 8 }}>
            近端淨買賣超（張）：買超紅／賣超綠（三大法人/投信僅20日，外資另有10/5日）。
          </div>
          <ChipFlowBars row={r} />
        </div>

        {/* 4. 技術卡 */}
        <div className="hud-card">
          <h3>技術</h3>
          <div className="stat-grid">
            <Stat label="收盤" value={num(r.close, 2)} />
            <Stat label="距月線(MA20)%" value={signed(r.ma20_dist_pct, 1, "%")} />
            <Stat label="距季線(MA60)%" value={signed(r.ma60_dist_pct, 1, "%")} />
            <Stat label="MA20價" value={num(r.ma20_price, 2)} />
            <Stat label="MA60價" value={num(r.ma60_price, 2)} />
          </div>
          {hasRange ? (
            <div className="range-bars">
              <RangeBar label="20 日" low={r.low_20d} high={r.high_20d} close={r.close} ma={r.ma20_price} maLabel="MA20" />
              <RangeBar label="60 日" low={r.low_60d} high={r.high_60d} close={r.close} ma={r.ma60_price} maLabel="MA60" />
            </div>
          ) : (
            <div className="chart-cap" style={{ marginTop: 8 }}>
              本週無 20/60 日高低區間資料（reports 未含 <code>low/high_20d/60d</code>，待 W26+ 重跑）；
              技術面以上方<b>均線距離</b>呈現。reports 為每週快照，無逐日 K 線（§8）。
            </div>
          )}
        </div>

        {/* 5. 族群相對位置 */}
        <div className="hud-card">
          <h3>族群相對位置</h3>
          {data.sectors === null ? (
            <div className="num-flat">本週無族群輪動資料（sector_rotation 僅 W25 有）。</div>
          ) : data.sectors.length === 0 ? (
            <div className="num-flat">此股所屬次產業未入本週族群資金雷達。</div>
          ) : (
            <div className="stat-grid">
              {data.sectors.map((s, i) => (
                <Stat
                  key={String(s.sub_industry ?? i)}
                  label={String(s.sub_industry ?? "—")}
                  value={
                    <>
                      <span className={`flag ${quadClass(s.quadrant)}`}>{String(s.quadrant ?? "—")}</span>
                      <span className="muted"> 雷達#{fmtNum(s.radar_rank, 0)}</span>
                    </>
                  }
                />
              ))}
            </div>
          )}
          <div className="chart-cap" style={{ marginTop: 8 }}>
            本檔族群內排名（rank_in_group）：<b>{num(r.rank_in_group, 0)}</b>
          </div>
        </div>

        {/* 6. 命中策略徽章 ＋ flags */}
        <div className="hud-card detail-grid--full">
          <h3>命中策略 ／ 標籤</h3>
          <div className="badge-row">
            {data.screens.length > 0 ? (
              data.screens.map((s) => (
                <span
                  key={s}
                  className="strat-badge"
                  style={{ color: stratColor(s), borderColor: stratColor(s) }}
                >
                  {s.toUpperCase()} {STRAT_NAME[s] ?? ""}
                </span>
              ))
            ) : (
              <span className="num-flat">未命中 D/E/F/G 任一篩選（可能來自觀察清單）。</span>
            )}
          </div>
          <div className="badge-row" style={{ marginTop: 8 }}>
            {isBlank(r.flags) ? (
              <span className="num-flat">無標籤</span>
            ) : (
              String(r.flags)
                .split(";")
                .filter(Boolean)
                .map((f) => (
                  <span key={f} className={`flag ${flagClass(f)}`}>
                    {f}
                  </span>
                ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
