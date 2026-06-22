// HUD 圖表共用：色票（對齊 §6.1，canvas 需硬值）、紅漲綠跌、軸樣式。

export const C = {
  bg: "#0b0f17",
  panel: "#111826",
  border: "#1c2738",
  accent: "#18e0c8",
  warn: "#ffae00",
  text: "#9fb3c8",
  muted: "#5d6b7e",
  hi: "#e6f0f7",
  up: "#ff4d5e", // 漲/買超（紅）
  down: "#2bd576", // 跌/賣超（綠）
  flat: "#9fb3c8",
};

export const MONO = "'JetBrains Mono','IBM Plex Mono',monospace";

// 策略首字母配色（D/E/F/G）
export const STRAT_COLOR: Record<string, string> = {
  D: "#18e0c8",
  E: "#ffae00",
  F: "#6aa3ff",
  G: "#c98bff",
};

export const stratColor = (strategy: unknown): string => {
  const first = String(strategy ?? "").charAt(0).toUpperCase();
  return STRAT_COLOR[first] ?? C.accent;
};

// 紅漲綠跌：>0 紅、<0 綠（§6.2）
export const signColor = (v: number): string =>
  v > 0 ? C.up : v < 0 ? C.down : C.flat;

export const tooltipStyle = {
  backgroundColor: C.panel,
  borderColor: C.border,
  borderWidth: 1,
  textStyle: { color: C.hi, fontFamily: MONO, fontSize: 11 },
};

export const axisCommon = {
  axisLine: { lineStyle: { color: C.border } },
  axisTick: { lineStyle: { color: C.border } },
  axisLabel: { color: C.muted, fontFamily: MONO, fontSize: 10 },
  splitLine: { lineStyle: { color: "rgba(28,39,56,0.45)" } },
  nameTextStyle: { color: C.muted, fontFamily: MONO, fontSize: 10 },
};

export const toNum = (v: unknown): number | null =>
  v === null || v === undefined || v === "" || Number.isNaN(Number(v))
    ? null
    : Number(v);
