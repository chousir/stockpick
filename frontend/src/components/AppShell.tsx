// 共用外殼：站名 ＋ 分頁導覽 ＋ 週次切換器（docs/17 §5.1）。

import type { ReactNode } from "react";
import { WeekSwitcher } from "./WeekSwitcher";

export type TabKey = "candidates" | "sectors" | "stock" | "holdings";

const TABS: { key: TabKey; label: string }[] = [
  { key: "candidates", label: "候選股" },
  { key: "sectors", label: "族群輪動" },
  { key: "stock", label: "個股" },
  { key: "holdings", label: "持股" },
];

interface Props {
  weeks: string[];
  week: string;
  onWeekChange: (week: string) => void;
  tab: TabKey;
  onTabChange: (tab: TabKey) => void;
  children: ReactNode;
}

export function AppShell({
  weeks,
  week,
  onWeekChange,
  tab,
  onTabChange,
  children,
}: Props) {
  return (
    <>
      <header className="shell-header">
        <span className="shell-title">
          <span className="accent">▌</span> 投資戰情室
        </span>
        <nav className="shell-nav">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={t.key === tab ? "active" : ""}
              onClick={() => onTabChange(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <span className="shell-spacer" />
        {weeks.length > 0 && (
          <WeekSwitcher weeks={weeks} value={week} onChange={onWeekChange} />
        )}
      </header>
      <main className="shell-body">{children}</main>
    </>
  );
}
