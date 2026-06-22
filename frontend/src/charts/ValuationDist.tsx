// 估值/殖利分布（docs/17 §7-4）。val_pctile 或 dividend_yield_pct 直方圖，
// 標 cheap_flag 群落數。僅 W25 有 val_pctile / dividend_yield_pct。

import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import type { Row } from "../lib/api";
import { EChart } from "./EChart";
import { axisCommon, C, toNum, tooltipStyle } from "./echartsBase";

const METRICS = [
  { key: "val_pctile", label: "估值位階", step: 10, max: 100 as number | null },
  { key: "dividend_yield_pct", label: "殖利率%", step: 1, max: null as number | null },
];

export function ValuationDist({ rows }: { rows: Row[] }) {
  const [metric, setMetric] = useState<string>("val_pctile");
  const cfg = METRICS.find((m) => m.key === metric) ?? METRICS[0];

  const { labels, counts, cheapCount } = useMemo(() => {
    const vals = rows
      .map((r) => toNum(r[metric]))
      .filter((v): v is number => v !== null);
    const cheap = rows.filter((r) => Boolean(r.cheap_flag)).length;
    if (vals.length === 0) return { labels: [], counts: [], cheapCount: cheap };

    const step = cfg.step;
    const maxV = cfg.max ?? Math.max(step, Math.ceil(Math.max(...vals) / step) * step);
    const nb = Math.max(1, Math.round(maxV / step));
    const c = new Array<number>(nb).fill(0);
    for (const v of vals) {
      let idx = Math.floor(v / step);
      if (idx < 0) idx = 0;
      if (idx >= nb) idx = nb - 1;
      c[idx] += 1;
    }
    const l = c.map((_, i) => `${i * step}`);
    return { labels: l, counts: c, cheapCount: cheap };
  }, [rows, metric, cfg]);

  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 40, right: 16, top: 12, bottom: 28 },
      tooltip: { ...tooltipStyle, trigger: "axis" },
      xAxis: {
        type: "category",
        data: labels,
        name: cfg.label,
        ...axisCommon,
      },
      yAxis: { type: "value", name: "檔數", ...axisCommon },
      series: [
        {
          type: "bar",
          data: counts,
          barWidth: "82%",
          itemStyle: { color: C.accent, opacity: 0.85 },
        },
      ],
    }),
    [labels, counts, cfg],
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
        {cheapCount > 0 && <span className="count">相對便宜 {cheapCount} 檔</span>}
      </div>
      {counts.length === 0 ? (
        <div className="empty-state">本週無{cfg.label}資料（僅最新週）</div>
      ) : (
        <EChart option={option} height={300} />
      )}
    </div>
  );
}
