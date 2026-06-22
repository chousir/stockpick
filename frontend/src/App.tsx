import { useCallback, useEffect, useState } from "react";
import { AppShell, type TabKey } from "./components/AppShell";
import { Candidates } from "./pages/Candidates";
import { Sectors } from "./pages/Sectors";
import { StockDetail } from "./pages/StockDetail";
import { getWeeks } from "./lib/api";

export default function App() {
  const [weeks, setWeeks] = useState<string[]>([]);
  const [week, setWeek] = useState<string>("");
  const [tab, setTab] = useState<TabKey>("candidates");
  const [stock, setStock] = useState<string | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);

  // 點候選股/族群成員 → 鑽取個股頁（docs/17 §5.2/§5.3）
  const goStock = useCallback((stockId: string) => {
    setStock(stockId);
    setTab("stock");
  }, []);

  useEffect(() => {
    getWeeks()
      .then((r) => {
        setWeeks(r.weeks);
        setWeek(r.latest ?? r.weeks[r.weeks.length - 1] ?? "");
      })
      .catch((e: unknown) => setBootError(String(e)));
  }, []);

  if (bootError)
    return (
      <div className="empty-state" style={{ margin: 40 }}>
        <div className="warn">⚠ 無法連線後端 API</div>
        <div style={{ marginTop: 8 }}>{bootError}</div>
      </div>
    );

  if (!week) return <div className="placeholder">載入週次…</div>;

  return (
    <AppShell
      weeks={weeks}
      week={week}
      onWeekChange={setWeek}
      tab={tab}
      onTabChange={setTab}
    >
      {tab === "candidates" ? (
        <Candidates week={week} onStock={goStock} />
      ) : tab === "sectors" ? (
        <Sectors week={week} onStock={goStock} />
      ) : tab === "stock" ? (
        <StockDetail week={week} stockId={stock} onBack={() => setTab("candidates")} />
      ) : (
        <div className="placeholder">「持股損益（M-Dash 4）」尚未實作。</div>
      )}
    </AppShell>
  );
}
