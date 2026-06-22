// 數值格式化與紅漲綠跌色彩判定（台股慣例，docs/17 §6.2）。

export function isBlank(v: unknown): boolean {
  return v === null || v === undefined || v === "";
}

export function fmtNum(v: unknown, digits = 2): string {
  if (isBlank(v)) return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

// 漲/正 → 紅(num-up)，跌/負 → 綠(num-down)，平/缺 → flat
export function signClass(v: unknown): string {
  if (isBlank(v)) return "num-flat";
  const n = Number(v);
  if (Number.isNaN(n)) return "num-flat";
  if (n > 0) return "num-up";
  if (n < 0) return "num-down";
  return "num-flat";
}

// flags chip 顏色語意（§6.2）
export function flagClass(label: string): string {
  if (label.includes("過熱") || label.includes("高PE") || label.includes("背離"))
    return "amber";
  if (label.includes("領頭") || label.includes("強勢")) return "teal";
  if (label.includes("停損") || label.includes("對作")) return "alert";
  return "muted";
}
