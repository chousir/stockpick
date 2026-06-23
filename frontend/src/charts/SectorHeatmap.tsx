// 族群資金熱力圖（docs/17 §7-1）。列＝次產業（依 radar_rank），欄＝資金 z 因子，
// 色＝z-score（teal=流出↔amber=流入，與紅漲綠跌分離，§6.2）。
// y 軸標籤帶 quadrant 色點 ＋ entry/confirm 圖示（⚑進場觸發、✓確認）。
// 僅近期週（有 sector_rotation.csv）可繪，其餘週由頁面顯空狀態。

import type { EChartsOption } from "echarts";
import { useMemo } from "react";
import type { Row } from "../lib/api";
import { axisCommon, C, MONO, toNum, tooltipStyle } from "./echartsBase";
import { EChart } from "./EChart";

// 熱力圖欄位（皆為 z-score，同一色階口徑）。依 總量｜外資｜投信 分區、各區內 5d/20d。
// 註：目前 reports 僅 5d/20d；10d 待分析層補窗後加入（flow_breadth 0..1 另留在輪動表）。
const COLS: { key: string; group: string; win: string }[] = [
  { key: "net_flow_5d_z", group: "總量", win: "5d" },
  { key: "net_flow_20d_z", group: "總量", win: "20d" },
  { key: "foreign_flow_5d_z", group: "外資", win: "5d" },
  { key: "foreign_flow_20d_z", group: "外資", win: "20d" },
  { key: "trust_flow_5d_z", group: "投信", win: "5d" },
  { key: "trust_flow_20d_z", group: "投信", win: "20d" },
];

// 群組分隔線：群組變換的「欄邊界」索引（總量→外資＝2、外資→投信＝4）。
// 畫在疊加的隱形 value 軸（0..COLS.length）上——category 軸的 markLine 數值會被 ECharts
// 四捨五入到最近欄位中心（量過 1.5→欄2 中心），落不到欄間；value 軸線性對映、不會被進位。
const GROUP_BOUNDARIES = COLS.reduce<number[]>((acc, c, i) => {
  if (i > 0 && c.group !== COLS[i - 1].group) acc.push(i);
  return acc;
}, []);

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
      grid: { left: 150, right: 150, top: 46, bottom: 16 },
      tooltip: {
        ...tooltipStyle,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) => {
          const [ci, ri, z] = p.data.value as [number, number, number];
          const c = COLS[ci];
          return `${ordered[ri].sub_industry}<br/>${c.group}近${c.win} z＝${z.toFixed(2)}（${
            z > 0 ? "資金流入" : z < 0 ? "資金流出" : "中性"
          }）`;
        },
      },
      xAxis: [
        {
          type: "category",
          position: "top",
          data: COLS.map((c) => `${c.group}|${c.win}`),
          ...axisCommon,
          axisTick: { show: false },
          axisLabel: {
            ...axisCommon.axisLabel,
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            formatter: (value: string) => {
              const [g, w] = value.split("|");
              return `{g|${g}}\n{w|${w}}`;
            },
            rich: {
              g: { color: C.hi, fontFamily: MONO, fontSize: 11, fontWeight: "bold", lineHeight: 14 },
              w: { color: C.muted, fontFamily: MONO, fontSize: 10, lineHeight: 13 },
            },
          },
          splitLine: { show: false },
        },
        // 疊加隱形 value 軸（0..欄數）：僅供群組分隔線定位，線性對映欄邊界、不被進位
        { type: "value", min: 0, max: COLS.length, position: "top", show: false },
      ],
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
        precision: 1,
        calculable: true,
        orient: "vertical",
        right: 8,
        top: "middle",
        itemHeight: 150,
        itemWidth: 16,
        text: ["流入 +3", "-3 流出"],
        textStyle: { color: C.text, fontFamily: MONO, fontSize: 11 },
        // 中段用實色（非透明）讓每格都有底，避免近零格「看不見」
        inRange: { color: [C.accent, "#243049", C.warn] },
        dimension: 2,
        seriesIndex: 0,
      },
      series: [
        {
          type: "heatmap",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: cells,
          label: {
            show: true,
            fontFamily: MONO,
            fontSize: 11,
            fontWeight: "bold",
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            formatter: (p: any) => (p.data.value[2] as number).toFixed(1),
          },
          itemStyle: { borderColor: C.bg, borderWidth: 2 },
          emphasis: { itemStyle: { borderColor: C.hi, borderWidth: 1 } },
        },
        {
          // 群組分隔線載體：綁隱形 value 軸（xAxisIndex 1），markLine 落在欄邊界、不被進位
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 0,
          data: [],
          silent: true,
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { color: C.text, width: 2, type: "solid", opacity: 0.9 },
            data: GROUP_BOUNDARIES.map((x) => ({ xAxis: x })),
          },
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
        列＝<b>次產業</b>（依資金雷達排名）、欄分 <b>總量｜外資｜投信</b> 三區（分隔線區隔）、
        各區內 <b>近5d／近20d</b>，格值＝<b>資金淨流入 z 分數</b>（目前僅 5d/20d，10d 待分析層補窗）；
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
