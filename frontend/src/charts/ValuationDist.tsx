// 估值/殖利分布（docs/17 §7-4）。val_pctile 或 dividend_yield_pct 直方圖。
// hover 每根長條列出該區間的個股清單，標 cheap_flag 群落數。僅 W25 有相關欄。

import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import type { Row } from "../lib/api";
import { EChart } from "./EChart";
import { axisCommon, C, toNum, tooltipStyle } from "./echartsBase";

const METRICS = [
  { key: "val_pctile", label: "估值位階", step: 10, max: 100 as number | null, unit: "" },
  { key: "dividend_yield_pct", label: "殖利率", step: 1, max: null as number | null, unit: "%" },
];

interface Bin {
  lo: number;
  hi: number;
  stocks: string[];
}

export function ValuationDist({ rows }: { rows: Row[] }) {
  const [metric, setMetric] = useState<string>("val_pctile");
  const cfg = METRICS.find((m) => m.key === metric) ?? METRICS[0];

  const { bins, cheapCount } = useMemo(() => {
    const items = rows
      .map((r) => ({ v: toNum(r[metric]), label: `${r.stock_id} ${r.name ?? ""}` }))
      .filter((x): x is { v: number; label: string } => x.v !== null);
    const cheap = rows.filter((r) => Boolean(r.cheap_flag)).length;
    if (items.length === 0) return { bins: [] as Bin[], cheapCount: cheap };

    const step = cfg.step;
    const maxV = cfg.max ?? Math.max(step, Math.ceil(Math.max(...items.map((i) => i.v)) / step) * step);
    const nb = Math.max(1, Math.round(maxV / step));
    const b: Bin[] = Array.from({ length: nb }, (_, i) => ({
      lo: i * step,
      hi: (i + 1) * step,
      stocks: [],
    }));
    for (const it of items) {
      let idx = Math.floor(it.v / step);
      if (idx < 0) idx = 0;
      if (idx >= nb) idx = nb - 1;
      b[idx].stocks.push(it.label);
    }
    return { bins: b, cheapCount: cheap };
  }, [rows, metric, cfg]);

  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 40, right: 16, top: 12, bottom: 30 },
      tooltip: {
        ...tooltipStyle,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) => {
          const bin = bins[p.dataIndex as number];
          if (!bin) return "";
          const head = `${cfg.label} ${bin.lo}–${bin.hi}${cfg.unit}：${bin.stocks.length} 檔`;
          const list = bin.stocks.slice(0, 18).join("<br/>");
          const more = bin.stocks.length > 18 ? `<br/>…等 ${bin.stocks.length} 檔` : "";
          return `${head}<hr style="border-color:${C.border};margin:4px 0"/>${list}${more}`;
        },
      },
      xAxis: {
        type: "category",
        data: bins.map((b) => `${b.lo}`),
        name: `${cfg.label}${cfg.unit ? `(${cfg.unit})` : ""}`,
        ...axisCommon,
      },
      yAxis: { type: "value", name: "檔數", ...axisCommon },
      series: [
        {
          type: "bar",
          data: bins.map((b) => b.stocks.length),
          barWidth: "82%",
          itemStyle: { color: C.accent, opacity: 0.85 },
          emphasis: { itemStyle: { opacity: 1, color: C.hi } },
        },
      ],
    }),
    [bins, cfg],
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
      <div className="chart-cap">
        {metric === "val_pctile" ? (
          <>
            <b>估值位階</b>＝個股在所屬次產業內的估值百分位（PE 為主、虧損股用 PB）；
            <b>0＝同業最便宜、100＝最貴</b>。長條＝落在各區間的候選股檔數。
          </>
        ) : (
          <>
            <b>殖利率</b>＝官方近12月現金股利 / 股價（%）。長條＝各殖利率區間的候選股檔數。
          </>
        )}
        <b> hover 長條看該區間有哪些股票。</b>
      </div>
      {bins.length === 0 ? (
        <div className="empty-state">本週無{cfg.label}資料（僅最新週）</div>
      ) : (
        <EChart option={option} height={300} />
      )}
    </div>
  );
}
