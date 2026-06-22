import { useCallback, useEffect, useState } from "react";
import { AppShell, type TabKey } from "./components/AppShell";
import { Candidates } from "./pages/Candidates";
import { Holdings } from "./pages/Holdings";
import { Sectors } from "./pages/Sectors";
import { StockDetail } from "./pages/StockDetail";
import { getWeeks } from "./lib/api";

const PRIVACY_KEY = "warroom.privacy";

export default function App() {
  const [weeks, setWeeks] = useState<string[]>([]);
  const [week, setWeek] = useState<string>("");
  const [tab, setTab] = useState<TabKey>("candidates");
  const [stock, setStock] = useState<string | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  // 隱私遮罩：持久化到 localStorage，重新整理仍維持（截圖友善，docs/17 §5.1）
  const [privacy, setPrivacy] = useState<boolean>(
    () => localStorage.getItem(PRIVACY_KEY) === "1",
  );

  useEffect(() => {
    localStorage.setItem(PRIVACY_KEY, privacy ? "1" : "0");
  }, [privacy]);

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
      privacy={privacy}
      onPrivacyChange={setPrivacy}
    >
      {tab === "candidates" ? (
        <Candidates week={week} onStock={goStock} />
      ) : tab === "sectors" ? (
        <Sectors week={week} onStock={goStock} />
      ) : tab === "stock" ? (
        <StockDetail week={week} stockId={stock} onBack={() => setTab("candidates")} />
      ) : (
        <Holdings week={week} privacy={privacy} onStock={goStock} />
      )}
    </AppShell>
  );
}
