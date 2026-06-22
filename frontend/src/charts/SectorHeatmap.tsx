// 族群資金熱力圖（docs/17 §7-1）。列＝次產業（依 radar_rank），欄＝資金 z 因子，
// 色＝z-score（teal=流出↔amber=流入，與紅漲綠跌分離，§6.2）。
// y 軸標籤帶 quadrant 色點 ＋ entry/confirm 圖示（⚑進場觸發、✓確認）。
// 僅有 sector_rotation.csv 的週（目前 W24/W25）可繪，其餘週由頁面顯空狀態。

import type { EChartsOption } from "echarts";
import { useMemo } from "react";
import type { Row } from "../lib/api";
import { axisCommon, C, MONO, toNum, tooltipStyle } from "./echartsBase";
import { EChart } from "./EChart";

// 熱力圖欄位（皆為 z-score，同一色階口徑）。flow_breadth 為 0..1 不同尺度，留在輪動表。
const COLS: { key: string; label: string }[] = [
  { key: "net_flow_5d_z", label: "總量5d" },
  { key: "net_flow_20d_z", label: "總量20d" },
  { key: "foreign_flow_5d_z", label: "外資5d" },
  { key: "foreign_flow_20d_z", label: "外資20d" },
  { key: "trust_flow_5d_z", label: "投信5d" },
  { key: "trust_flow_20d_z", label: "投信20d" },
];

// quadrant → 色點 rich 樣式索引（主升續勢 teal／下一棒 amber／冷卻觀望 muted／出貨警訊 紅）
const QUADRANTS = ["主升續勢", "下一棒", "冷卻觀望", "出貨警訊"];
const QUAD_COLOR = [C.accent, C.warn, C.muted, C.up];

interface Cell {
  value: [number, number, number]; // [colIdx, rowIdx, z]
  label: { color: string };
}

export function SectorHeatmap({ rows }: { rows: Row[] }) {
  // 依 radar_rank 升冪；rank 1 在最上（yAxis inverse）
  const ordered = useMemo(
    () =>
      [...rows].sort(
        (a, b) => (toNum(a.radar_rank) ?? 1e9) - (toNum(b.radar_rank) ?? 1e9),
      ),
    [rows],
  );

  const yLabels = useMemo(
    () => ordered.map((r) => `${toNum(r.radar_rank) ?? "–"} ${r.sub_industry ?? "—"}`),
    [ordered],
  );

  // 每列的 quadrant 樣式索引 ＋ 觸發圖示（給 axisLabel.formatter 用）
  const rowMeta = useMemo(
    () =>
      ordered.map((r) => {
        const q = QUADRANTS.indexOf(String(r.quadrant ?? ""));
        const icons =
          (r.entry_triggered ? " ⚑" : "") + (r.confirm_triggered ? "✓" : "");
        return { q: q < 0 ? 2 : q, icons };
      }),
    [ordered],
  );

  const cells = useMemo<Cell[]>(() => {
    const out: Cell[] = [];
    ordered.forEach((r, ri) => {
      COLS.forEach((c, ci) => {
        const z = toNum(r[c.key]);
        if (z === null) return;
        out.push({
          value: [ci, ri, z],
          // 亮格（|z| 大）用深色字、暗格用淺色字，維持可讀
          label: { color: Math.abs(z) >= 0.8 ? "#08111a" : C.muted },
        });
      });
    });
    return out;
  }, [ordered]);

  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 150, right: 16, top: 28, bottom: 16 },
      tooltip: {
        ...tooltipStyle,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) => {
          const [ci, ri, z] = p.data.value as [number, number, number];
          return `${ordered[ri].sub_industry}<br/>${COLS[ci].label} z＝${z.toFixed(2)}（${
            z > 0 ? "資金流入" : z < 0 ? "資金流出" : "中性"
          }）`;
        },
      },
      xAxis: {
        type: "category",
        position: "top",
        data: COLS.map((c) => c.label),
        ...axisCommon,
        axisLabel: { ...axisCommon.axisLabel, fontSize: 10 },
        splitLine: { show: false },
      },
      yAxis: {
        type: "category",
        inverse: true,
        data: yLabels,
        ...axisCommon,
        axisLabel: {
          ...axisCommon.axisLabel,
          fontSize: 10,
          align: "left",
          margin: 142,
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          formatter: (value: string, index: number) => {
            const m = rowMeta[index] ?? { q: 2, icons: "" };
            return `{q${m.q}|●} ${value}${m.icons}`;
          },
          rich: {
            q0: { color: QUAD_COLOR[0] },
            q1: { color: QUAD_COLOR[1] },
            q2: { color: QUAD_COLOR[2] },
            q3: { color: QUAD_COLOR[3] },
          },
        },
        splitLine: { show: false },
      },
      visualMap: {
        min: -3,
        max: 3,
        calculable: true,
        orient: "horizontal",
        right: 16,
        top: 0,
        itemHeight: 80,
        itemWidth: 12,
        text: ["流入", "流出"],
        textStyle: { color: C.muted, fontFamily: MONO, fontSize: 10 },
        inRange: { color: [C.accent, "rgba(17,24,38,0.35)", C.warn] },
        dimension: 2,
        seriesIndex: 0,
      },
      series: [
        {
          type: "heatmap",
          data: cells,
          label: {
            show: true,
            fontFamily: MONO,
            fontSize: 9,
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            formatter: (p: any) => (p.data.value[2] as number).toFixed(1),
          },
          itemStyle: { borderColor: C.bg, borderWidth: 1 },
          emphasis: { itemStyle: { borderColor: C.hi, borderWidth: 1 } },
        },
      ],
    }),
    [cells, yLabels, ordered, rowMeta],
  );

  if (ordered.length === 0)
    return <div className="empty-state">本週無族群輪動資料（sector_rotation 僅近期週）</div>;

  return (
    <div>
      <div className="chart-cap">
        列＝<b>次產業</b>（依資金雷達排名）、欄＝各窗<b>資金淨流入 z 分數</b>；
        色階 <span style={{ color: C.warn }}>琥珀＝流入</span>／
        <span style={{ color: C.accent }}>teal＝流出</span>（與漲跌紅綠分離）。
        左側色點＝象限（<span style={{ color: QUAD_COLOR[0] }}>●主升續勢</span>
        <span style={{ color: QUAD_COLOR[1] }}>●下一棒</span>
        <span style={{ color: QUAD_COLOR[2] }}>●冷卻觀望</span>
        <span style={{ color: QUAD_COLOR[3] }}>●出貨警訊</span>）；⚑＝進場觸發、✓＝確認。
      </div>
      <EChart option={option} height={Math.max(300, ordered.length * 22 + 60)} />
    </div>
  );
}
