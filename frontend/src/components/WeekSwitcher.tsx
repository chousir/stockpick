// 週次切換器（下拉，動態週次清單，預設 latest；docs/17 §5.1）。

interface Props {
  weeks: string[];
  value: string;
  onChange: (week: string) => void;
}

export function WeekSwitcher({ weeks, value, onChange }: Props) {
  return (
    <div className="week-switcher">
      <label htmlFor="week-select">WEEK</label>
      <select
        id="week-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {weeks.map((w) => (
          <option key={w} value={w}>
            {w}
          </option>
        ))}
      </select>
    </div>
  );
}
