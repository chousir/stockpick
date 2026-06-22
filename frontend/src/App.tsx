import { useEffect, useState } from "react";
import { AppShell, type TabKey } from "./components/AppShell";
import { Candidates } from "./pages/Candidates";
import { getWeeks } from "./lib/api";

// 後續 milestone 才實作的頁面（docs/17 §9）
const PENDING: Record<Exclude<TabKey, "candidates">, string> = {
  sectors: "族群輪動（M-Dash 2）",
  stock: "個股鑽取（M-Dash 3）",
  holdings: "持股損益（M-Dash 4）",
};

export default function App() {
  const [weeks, setWeeks] = useState<string[]>([]);
  const [week, setWeek] = useState<string>("");
  const [tab, setTab] = useState<TabKey>("candidates");
  const [bootError, setBootError] = useState<string | null>(null);

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
        <Candidates week={week} />
      ) : (
        <div className="placeholder">「{PENDING[tab]}」尚未實作。</div>
      )}
    </AppShell>
  );
}
