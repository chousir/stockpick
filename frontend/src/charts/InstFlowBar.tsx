// 法人買賣超 bar（docs/17 §7-3）。買超紅、賣超綠。
// 註：candidates 層每法人別只有單一週期淨額欄，故切「法人別」（三大法人/外資/投信）；
// 完整 5/10/20 日切換待個股頁（M-Dash 3）。取 |淨額| 前 15 大。

import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import type { Row } from "../lib/api";
import { EChart } from "./EChart";
import { axisCommon, signColor, toNum, tooltipStyle } from "./echartsBase";

const METRICS = [
  { key: "inst_net_lots", label: "三大法人" },
  { key: "foreign_net_lots", label: "外資" },
  { key: "trust_net_lots", label: "投信" },
] as const;

export function InstFlowBar({ rows }: { rows: Row[] }) {
  const [metric, setMetric] = useState<string>("inst_net_lots");

  const { names, values } = useMemo(() => {
    const arr = rows
      .map((r) => ({ name: `${r.stock_id} ${r.name ?? ""}`, v: toNum(r[metric]) }))
      .filter((x): x is { name: string; v: number } => x.v !== null && x.v !== 0)
      .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
      .slice(0, 15)
      .sort((a, b) => a.v - b.v); // 遞增：正在上、負在下
    return { names: arr.map((a) => a.name), values: arr.map((a) => a.v) };
  }, [rows, metric]);

  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 104, right: 28, top: 8, bottom: 30 },
      tooltip: {
        ...tooltipStyle,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) =>
          `${p.name}<br/>${p.value >= 0 ? "買超" : "賣超"} ${Math.abs(p.value).toLocaleString()} 張`,
      },
      xAxis: { type: "value", name: "淨買賣超（張）", ...axisCommon },
      yAxis: {
        type: "category",
        data: names,
        ...axisCommon,
        axisLabel: { ...axisCommon.axisLabel, fontSize: 9 },
      },
      series: [
        {
          type: "bar",
          barWidth: "62%",
          data: values.map((v) => ({ value: v, itemStyle: { color: signColor(v) } })),
        },
      ],
    }),
    [names, values],
  );

  return (
    <div>
      <div className="chart-toggle">
        {METRICS.map((m) => (
          <button
            key={m.key}
            className={`chip ${metric === m.key ? "on" : ""}`}
            onClick={() => setMetric(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>
      {values.length === 0 ? (
        <div className="empty-state">本週無法人籌碼資料</div>
      ) : (
        <EChart option={option} height={360} />
      )}
    </div>
  );
}
