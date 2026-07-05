// 動能 × 估值散佈（docs/17 §7-2）。x=val_pctile（左=便宜），y=momentum_5d_pct，
// 點大小=amount_million，點色=策略；四象限參考線；點擊回呼。僅近期週有 val_pctile。
// 1A（問題1）：tooltip 補「所屬次產業輪動位置」——把 candidates.theme 對 sector_rotation
// join，讓「便宜且在漲」的點也看得出族群是流入×未漲還是流出（族群冷卻/背離）。

import type { EChartsOption } from "echarts";
import { useMemo } from "react";
import type { Row } from "../lib/api";
import { EChart } from "./EChart";
import { axisCommon, C, STRAT_COLOR, stratColor, toNum, tooltipStyle } from "./echartsBase";

interface Props {
  rows: Row[];
  onPick: (stockId: string) => void;
  // sub_industry → sector_rotation 列（quadrant/radar_rank/trend_score）；缺則不顯示族群行
  sectorByName?: Map<string, Row>;
}

interface Pt {
  value: [number, number, number];
  name: string;
  stockId: string;
  itemStyle: { color: string };
  sectors: string; // 預先組好的族群輪動 tooltip 片段（HTML）
}

// CSV quadrant 值（狀態常數）→ 中性顯示名（與 M1 報表語意一致）
const QUAD_LABEL: Record<string, string> = {
  主升續勢: "流入×已漲",
  出貨警訊: "流出×已漲",
  下一棒: "流入×未漲",
  冷卻觀望: "流出×未漲",
  位階未取得: "位階未取得",
};

function sectorLines(theme: unknown, sectorByName?: Map<string, Row>): string {
  if (!sectorByName || sectorByName.size === 0) return "";
  const tags = String(theme ?? "")
    .split("、")
    .map((t) => t.trim())
    .filter(Boolean);
  const lines: string[] = [];
  for (const tag of tags) {
    const s = sectorByName.get(tag);
    if (!s) continue;
    const quad = s.quadrant != null ? (QUAD_LABEL[String(s.quadrant)] ?? String(s.quadrant)) : "—";
    const rank = s.radar_rank != null ? `輪#${s.radar_rank}` : "";
    const trend = toNum(s.trend_score);
    const trendPart = trend != null ? `趨勢分 ${trend.toFixed(0)}` : "";
    const parts = [quad, rank, trendPart].filter(Boolean).join("・");
    lines.push(`${tag}（${parts}）`);
  }
  if (lines.length === 0) return "";
  return `<br/><span style="opacity:.75">族群：${lines.join("；")}</span>`;
}

export function MomentumValueScatter({ rows, onPick, sectorByName }: Props) {
  const pts = useMemo<Pt[]>(() => {
    const out: Pt[] = [];
    for (const r of rows) {
      const x = toNum(r.val_pctile);
      const y = toNum(r.momentum_5d_pct);
      if (x === null || y === null) continue;
      out.push({
        value: [x, y, toNum(r.amount_million) ?? 0],
        name: `${r.stock_id} ${r.name ?? ""}`,
        stockId: String(r.stock_id ?? ""),
        itemStyle: { color: stratColor(r.strategy) },
        sectors: sectorLines(r.theme, sectorByName),
      });
    }
    return out;
  }, [rows, sectorByName]);

  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 50, right: 18, top: 14, bottom: 46 },
      tooltip: {
        ...tooltipStyle,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) => {
          const d = p.data as Pt;
          return `${d.name}<br/>估值位階 ${d.value[0].toFixed(0)}｜動能5d ${d.value[1].toFixed(1)}%｜額 ${d.value[2].toFixed(0)}M${d.sectors}`;
        },
      },
      xAxis: {
        type: "value",
        name: "估值位階（左=同業便宜）",
        nameLocation: "middle",
        nameGap: 26,
        min: 0,
        max: 100,
        ...axisCommon,
      },
      yAxis: { type: "value", name: "近5日漲幅 %", ...axisCommon },
      series: [
        {
          type: "scatter",
          data: pts,
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          symbolSize: (val: any) => Math.max(6, Math.min(32, Math.sqrt(val[2] || 0) / 2.5)),
          emphasis: { focus: "self", itemStyle: { borderColor: C.hi, borderWidth: 1 } },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: C.border, type: "dashed" },
            label: { show: false },
            data: [{ xAxis: 50 }, { yAxis: 0 }],
          },
        },
      ],
    }),
    [pts],
  );

  return (
    <div>
      <div className="chart-cap">
        X＝個股在<b>所屬次產業內的估值百分位</b>（0＝同業最便宜、100＝最貴）；Y＝
        <b>近5日累計漲幅</b>（未還原除息、僅供掃描非進場依據）。點＝個股、大小＝成交額、色＝命中策略；
        hover 看<b>所屬族群輪動位置</b>。<b>左上</b>＝便宜且強勢、<b>右上</b>＝貴但強勢；虛線為位階 50 與漲幅 0。點一下可開 Goodinfo。
      </div>
      <div className="strat-legend">
        {(["D", "E", "F", "G"] as const).map((s) => (
          <span key={s}>
            <i style={{ background: STRAT_COLOR[s] }} />
            {s}
          </span>
        ))}
        <span className="muted">色＝策略</span>
      </div>
      {pts.length === 0 ? (
        <div className="empty-state">本週無估值位階資料（val_pctile 僅最新週）</div>
      ) : (
        <EChart
          option={option}
          height={320}
          onClick={(p) => {
            const d = p.data as Pt | undefined;
            if (d?.stockId) onPick(d.stockId);
          }}
        />
      )}
    </div>
  );
}
