// 法人買賣超 bar（docs/17 §7-3）。買超紅、賣超綠，取 |淨額| 前 15。
// 期間明確標示：三大法人/投信＝近20日累計；外資另有近10/5日（資料只有外資有近端窗）。
// 完整逐日切換待個股頁（M-Dash 3）。

import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import type { Row } from "../lib/api";
import { EChart } from "./EChart";
import { axisCommon, signColor, toNum, tooltipStyle } from "./echartsBase";

const PARTIES = [
  { key: "inst", label: "三大法人" },
  { key: "foreign", label: "外資" },
  { key: "trust", label: "投信" },
];

// 各法人別可用期間 → CSV 欄位（base 欄皆為近 20 日累計）
const PERIODS: Record<string, { days: string; col: string }[]> = {
  inst: [{ days: "20日", col: "inst_net_lots" }],
  foreign: [
    { days: "20日", col: "foreign_net_lots" },
    { days: "10日", col: "foreign_net_10d_lots" },
    { days: "5日", col: "foreign_net_5d_lots" },
  ],
  trust: [{ days: "20日", col: "trust_net_lots" }],
};

export function InstFlowBar({ rows }: { rows: Row[] }) {
  const [party, setParty] = useState<string>("inst");
  const [days, setDays] = useState<string>("20日");

  const periods = PERIODS[party];
  const active = periods.find((p) => p.days === days) ?? periods[0];
  const partyLabel = PARTIES.find((p) => p.key === party)?.label ?? "";

  function pickParty(p: string) {
    setParty(p);
    // 換法人別時，若新法人別沒有目前期間，回退到 20 日
    if (!PERIODS[p].some((x) => x.days === days)) setDays("20日");
  }

  const { names, values } = useMemo(() => {
    const arr = rows
      .map((r) => ({ name: `${r.stock_id} ${r.name ?? ""}`, v: toNum(r[active.col]) }))
      .filter((x): x is { name: string; v: number } => x.v !== null && x.v !== 0)
      .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
      .slice(0, 15)
      .sort((a, b) => a.v - b.v); // 遞增：正在上、負在下
    return { names: arr.map((a) => a.name), values: arr.map((a) => a.v) };
  }, [rows, active.col]);

  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 104, right: 28, top: 8, bottom: 30 },
      tooltip: {
        ...tooltipStyle,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (p: any) =>
          `${p.name}<br/>${partyLabel}近${active.days} ${p.value >= 0 ? "買超" : "賣超"} ${Math.abs(
            p.value,
          ).toLocaleString()} 張`,
      },
      xAxis: { type: "value", name: `近${active.days}淨買賣超（張）`, ...axisCommon },
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
    [names, values, active.days, partyLabel],
  );

  return (
    <div>
      <div className="chart-toggle">
        {PARTIES.map((p) => (
          <button
            key={p.key}
            className={`chip ${party === p.key ? "on" : ""}`}
            onClick={() => pickParty(p.key)}
          >
            {p.label}
          </button>
        ))}
        {periods.length > 1 && (
          <>
            <span className="muted">｜期間</span>
            {periods.map((p) => (
              <button
                key={p.days}
                className={`chip ${active.days === p.days ? "on" : ""}`}
                onClick={() => setDays(p.days)}
              >
                {p.days}
              </button>
            ))}
          </>
        )}
      </div>
      <div className="chart-cap">
        {partyLabel} <b>近{active.days}累計淨買賣超（張）</b>，買超紅／賣超綠，取 |淨額| 前 15。
        {party !== "foreign" && "（三大法人/投信資料僅近20日；外資可切近10/5日）"}
      </div>
      {values.length === 0 ? (
        <div className="empty-state">本週無{partyLabel}近{active.days}籌碼資料</div>
      ) : (
        <EChart option={option} height={360} />
      )}
    </div>
  );
}
