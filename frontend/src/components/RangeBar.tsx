// 區間條（docs/17 §5.4 卡4 / §8）：close 落在 [low, high] 的相對位置，附均線標記。
// 沒有逐日 K 線——reports 只有 N 日高低與均線；缺 low/high（目前所有週皆缺，待 W26+）
// 則降級為僅顯均線距離（由 StockDetail 卡片決定，不在此元件）。

import { fmtNum, isBlank } from "../lib/format";

interface Props {
  label: string; // 例：「20 日」
  low: unknown;
  high: unknown;
  close: unknown;
  ma: unknown; // 對應均線價（MA20/MA60）
  maLabel: string; // 例：「MA20」
}

function pct(x: number, low: number, high: number): number {
  if (high <= low) return 50;
  return Math.min(100, Math.max(0, ((x - low) / (high - low)) * 100));
}

export function RangeBar({ label, low, high, close, ma, maLabel }: Props) {
  const lo = Number(low);
  const hi = Number(high);
  const cl = Number(close);
  if (isBlank(low) || isBlank(high) || isBlank(close) || hi <= lo) {
    return (
      <div className="range-row">
        <span className="range-label">{label}高低</span>
        <span className="num-flat">區間資料未取得</span>
      </div>
    );
  }
  const closePct = pct(cl, lo, hi);
  const maPct = isBlank(ma) ? null : pct(Number(ma), lo, hi);

  return (
    <div className="range-row">
      <span className="range-label">{label}高低</span>
      <span className="range-end">{fmtNum(lo, 2)}</span>
      <div className="range-track">
        {maPct !== null && (
          <span className="range-ma" style={{ left: `${maPct}%` }} title={`${maLabel} ${fmtNum(ma, 2)}`} />
        )}
        <span className="range-dot" style={{ left: `${closePct}%` }} title={`收盤 ${fmtNum(cl, 2)}`} />
      </div>
      <span className="range-end">{fmtNum(hi, 2)}</span>
      <span className="range-now">收 {fmtNum(cl, 2)}（{fmtNum(closePct, 0)}%位）</span>
    </div>
  );
}
