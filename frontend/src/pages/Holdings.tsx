// 持股損益頁（docs/17 §5.5）：損益表 ＋ 停損距離(MA60)燈號 ＋ 除權息提醒。
// 隱私遮罩（§5.1）：買價/報酬/市值前端打碼（API 仍回明文）；收盤/停損距離為公開行情不遮。
// 缺資料週（無 holdings）→ 空狀態，不報錯（§2.4）。此頁資料永不進 git（reports/* gitignore）。

import { useEffect, useState } from "react";
import { ApiError, getHoldings, type Row } from "../lib/api";
import { fmtNum, isBlank, signClass } from "../lib/format";

// 停損為「絕對 MA60 價」（CLAUDE.md）。距季線 ≤ 此 % 視為逼近（前端顯示門檻）。
const NEAR_STOP_PCT = 5;

interface Props {
  week: string;
  privacy: boolean;
  onStock: (stockId: string) => void;
}

// 遮罩：隱私開啟時把私密欄位（買價/報酬/市值）打成 ●●●
function Masked({ privacy, children }: { privacy: boolean; children: React.ReactNode }) {
  if (privacy) return <span className="masked">●●●</span>;
  return <>{children}</>;
}

// 停損距離：close 相對 MA60 價的緩衝%，附燈號（破季線=alert／逼近=amber／安全=muted）。
function stopLoss(close: unknown, ma60: unknown): { pct: number | null; label: string; cls: string } {
  if (isBlank(close) || isBlank(ma60)) return { pct: null, label: "—", cls: "muted" };
  const c = Number(close);
  const m = Number(ma60);
  if (Number.isNaN(c) || Number.isNaN(m) || m === 0) return { pct: null, label: "—", cls: "muted" };
  const pct = ((c - m) / m) * 100;
  if (pct <= 0) return { pct, label: "已破季線", cls: "alert" };
  if (pct <= NEAR_STOP_PCT) return { pct, label: "逼近", cls: "amber" };
  return { pct, label: "安全", cls: "muted" };
}

export function Holdings({ week, privacy, onStock }: Props) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<{ status: number; msg: string } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setRows(null);
    getHoldings(week)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ApiError) setError({ status: e.status, msg: e.message });
        else setError({ status: 0, msg: String(e) });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [week]);

  if (loading) return <div className="placeholder">載入中…</div>;

  if (error) {
    const msg = error.status === 404 ? "本週無持股資料" : `載入失敗：${error.msg}`;
    return (
      <div className="empty-state">
        <div className="warn">⚠ {msg}</div>
        <div style={{ marginTop: 8 }}>週次 {week}</div>
      </div>
    );
  }

  const data = rows ?? [];
  if (data.length === 0)
    return <div className="empty-state">本週持股清單為空。</div>;

  const totalMv = data.reduce(
    (sum, r) => sum + (isBlank(r.market_value_k) ? 0 : Number(r.market_value_k)),
    0,
  );

  return (
    <div className="holdings">
      <div className="holdings-summary">
        <span className="count">{data.length} 檔持股</span>
        <span className="muted">
          總市值(千)：{" "}
          <Masked privacy={privacy}>
            <b>{fmtNum(totalMv, 0)}</b>
          </Masked>
        </span>
        <span className="shell-spacer" />
        {privacy && <span className="masked-note">🔒 隱私遮罩中（買價/報酬/市值已打碼）</span>}
      </div>

      <div className="table-wrap">
        <table className="hud">
          <thead>
            <tr>
              <th className="text">股號</th>
              <th className="text">名稱</th>
              <th className="text">產業</th>
              <th>買價</th>
              <th>收盤</th>
              <th>報酬%</th>
              <th>市值(千)</th>
              <th className="text">停損距離(MA60)</th>
              <th className="text">除權息</th>
            </tr>
          </thead>
          <tbody>
            {data.map((r) => {
              const sl = stopLoss(r.close, r.ma60_price);
              const exDiv = !isBlank(r.ex_div_cash) && Number(r.ex_div_cash) > 0;
              const id = String(r.stock_id ?? "");
              return (
                <tr key={id || String(r.name)}>
                  <td className="text">
                    {isBlank(r.stock_id) ? (
                      <span className="cell-id">—</span>
                    ) : (
                      <button
                        className="cell-link cell-id-btn"
                        onClick={() => onStock(id)}
                        title="個股鑽取"
                      >
                        {id}
                      </button>
                    )}
                  </td>
                  <td className="text">{isBlank(r.name) ? "—" : String(r.name)}</td>
                  <td className="text muted">{isBlank(r.industry) ? "—" : String(r.industry)}</td>
                  <td>
                    <Masked privacy={privacy}>{fmtNum(r.buy_price, 2)}</Masked>
                  </td>
                  <td>{fmtNum(r.close, 2)}</td>
                  <td>
                    <Masked privacy={privacy}>
                      <span className={signClass(r.return_pct)}>{fmtNum(r.return_pct, 1)}</span>
                    </Masked>
                  </td>
                  <td>
                    <Masked privacy={privacy}>{fmtNum(r.market_value_k, 0)}</Masked>
                  </td>
                  <td className="text">
                    <span className={`flag ${sl.cls}`}>{sl.label}</span>
                    {sl.pct !== null && (
                      <span className="muted">
                        {" "}
                        {sl.pct >= 0 ? "+" : ""}
                        {fmtNum(sl.pct, 1)}%
                      </span>
                    )}
                  </td>
                  <td className="text">
                    {exDiv ? (
                      <span className="flag amber">
                        配息 {fmtNum(r.ex_div_cash, 2)}
                        {!isBlank(r.div_addback_pct) && Number(r.div_addback_pct) !== 0 && (
                          <> · 還原 {fmtNum(r.div_addback_pct, 1)}%</>
                        )}
                      </span>
                    ) : (
                      <span className="num-flat">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="chart-cap" style={{ marginTop: 10 }}>
        停損為<b>絕對 MA60 價</b>（收盤跌破即出場）：<span className="flag alert">已破季線</span>{" "}
        收盤 ≤ MA60；<span className="flag amber">逼近</span> 緩衝 ≤ {NEAR_STOP_PCT}%；
        <span className="flag muted">安全</span> 緩衝 &gt; {NEAR_STOP_PCT}%。除權息以持股自身{" "}
        <code>ex_div_cash</code> ＞ 0 標示。報酬／市值套台股紅賺綠賠。
      </div>
    </div>
  );
}
