// 極薄 ECharts 包裝：init / setOption / ResizeObserver / dispose ＋ 點擊事件。

import * as echarts from "echarts";
import { useEffect, useRef } from "react";

export interface EChartClickParams {
  data?: unknown;
  dataIndex?: number;
  seriesIndex?: number;
  name?: string;
}

interface Props {
  option: echarts.EChartsOption;
  height?: number;
  onClick?: (params: EChartClickParams) => void;
}

export function EChart({ option, height = 300, onClick }: Props) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!elRef.current) return;
    const chart = echarts.init(elRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(elRef.current);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, true);
  }, [option]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !onClick) return;
    const handler = (params: unknown) => onClick(params as EChartClickParams);
    chart.on("click", handler);
    return () => {
      chart.off("click", handler);
    };
  }, [onClick]);

  return <div ref={elRef} style={{ width: "100%", height }} />;
}
