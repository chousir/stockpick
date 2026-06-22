// 候選股頁（MVP 首頁）：可排序/篩選資料表（docs/17 §5.2）。
// 缺資料週（如 W21 無 candidates）→ 空狀態，不報錯（§2.4）。

import { type ColumnDef, type SortingFn } from "@tanstack/react-table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { InstFlowBar } from "../charts/InstFlowBar";
import { MomentumValueScatter } from "../charts/MomentumValueScatter";
import { ValuationDist } from "../charts/ValuationDist";
import { DataTable } from "../components/DataTable";
import { NarrativePanel } from "../components/NarrativePanel";
import { ApiError, getCandidates, type Row } from "../lib/api";
import { flagClass, fmtNum, isBlank, signClass } from "../lib/format";

type Kind = "id" | "text" | "num" | "signed" | "flags";

interface ColDef {
  key: string;
  label: string;
  kind: Kind;
  digits?: number;
}

// 預設欄（docs/17 §5.2，W25 完整）。舊週缺的欄整欄顯「—」。
const COLUMNS: ColDef[] = [
  { key: "stock_id", label: "股號", kind: "id" },
  { key: "name", label: "名稱", kind: "text" },
  { key: "industry", label: "產業", kind: "text" },
  { key: "theme", label: "主題", kind: "text" },
  { key: "strategy", label: "策略", kind: "text" },
  { key: "momentum_5d_pct", label: "動能5d%", kind: "signed", digits: 1 },
  { key: "ret_10d_pct", label: "報酬10d%", kind: "signed", digits: 1 },
  { key: "change_pct", label: "漲跌%", kind: "signed", digits: 2 },
  { key: "close", label: "收盤", kind: "num", digits: 2 },
  { key: "vol_ratio", label: "量比", kind: "num", digits: 2 },
  { key: "ma20_dist_pct", label: "距月線%", kind: "signed", digits: 1 },
  { key: "ma60_dist_pct", label: "距季線%", kind: "signed", digits: 1 },
  { key: "pe_ratio", label: "PE", kind: "num", digits: 1 },
  { key: "dividend_yield_pct", label: "殖利率%", kind: "num", digits: 2 },
  { key: "val_pctile", label: "估值位階", kind: "num", digits: 0 },
  { key: "rev_yoy_pct", label: "營收YoY%", kind: "signed", digits: 1 },
  { key: "inst_net_lots", label: "三大法人", kind: "signed", digits: 0 },
  { key: "foreign_net_lots", label: "外資", kind: "signed", digits: 0 },
  { key: "trust_net_lots", label: "投信", kind: "signed", digits: 0 },
  { key: "flags", label: "標籤", kind: "flags" },
];

const STRATEGIES = ["D", "E", "F", "G"] as const;

// 數值排序：缺值沉到底（asc 時 -Infinity）
const numSort: SortingFn<Row> = (a, b, id) => {
  const x = a.getValue(id);
  const y = b.getValue(id);
  const nx = isBlank(x) ? -Infinity : Number(x);
  const ny = isBlank(y) ? -Infinity : Number(y);
  return nx - ny;
};

function renderFlags(raw: unknown) {
  if (isBlank(raw)) return <span className="num-flat">—</span>;
  return (
    <>
      {String(raw)
        .split(";")
        .filter(Boolean)
        .map((f) => (
          <span key={f} className={`flag ${flagClass(f)}`}>
            {f}
          </span>
        ))}
    </>
  );
}

function buildColumns(): ColumnDef<Row, unknown>[] {
  return COLUMNS.map((col) => {
    const text = col.kind === "text" || col.kind === "id" || col.kind === "flags";
    const base: ColumnDef<Row, unknown> = {
      id: col.key,
      accessorFn: (row) => row[col.key],
      header: col.label,
      meta: { text },
      enableSorting: col.kind !== "flags",
    };
    if (col.kind === "num" || col.kind === "signed") base.sortingFn = numSort;

    base.cell = (info) => {
      const v = info.getValue();
      switch (col.kind) {
        case "id": {
          const url = info.row.original.goodinfo_url;
          const label = <span className="cell-id">{isBlank(v) ? "—" : String(v)}</span>;
          return url ? (
            <a
              className="cell-link"
              href={String(url)}
              target="_blank"
              rel="noreferrer"
              title="Goodinfo"
            >
              {label}
            </a>
          ) : (
            label
          );
        }
        case "text":
          return isBlank(v) ? "—" : String(v);
        case "flags":
          return renderFlags(v);
        case "signed":
          return <span className={signClass(v)}>{fmtNum(v, col.digits)}</span>;
        default:
          return fmtNum(v, col.digits);
      }
    };
    return base;
  });
}

interface Props {
  week: string;
}

export function Candidates({ week }: Props) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<{ status: number; msg: string } | null>(null);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState("");
  const [activeStrats, setActiveStrats] = useState<Set<string>>(new Set());
  const [cheapOnly, setCheapOnly] = useState(false);
  const [showNarrative, setShowNarrative] = useState(false);

  // 點散佈點/股號 → 開 Goodinfo（完整跳個股 detail 頁待 M-Dash 3）
  const onPick = useCallback(
    (stockId: string) => {
      const row = rows?.find((r) => String(r.stock_id) === stockId);
      const url = row?.goodinfo_url;
      if (url) window.open(String(url), "_blank", "noreferrer");
    },
    [rows],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setRows(null);
    getCandidates(week)
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

  const columns = useMemo(() => buildColumns(), []);

  const filtered = useMemo(() => {
    if (!rows) return [];
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (q) {
        const id = String(r.stock_id ?? "").toLowerCase();
        const name = String(r.name ?? "").toLowerCase();
        if (!id.includes(q) && !name.includes(q)) return false;
      }
      if (activeStrats.size > 0) {
        const strat = String(r.strategy ?? "");
        // 成員包含：D+E+F 命中 D/E/F 任一被選者（§5.2）
        const hit = [...activeStrats].some((s) => strat.includes(s));
        if (!hit) return false;
      }
      if (cheapOnly && !r.cheap_flag) return false;
      return true;
    });
  }, [rows, search, activeStrats, cheapOnly]);

  function toggleStrat(s: string) {
    setActiveStrats((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  }

  if (loading) return <div className="placeholder">載入中…</div>;

  if (error) {
    const msg = error.status === 404 ? "本週無候選股資料" : `載入失敗：${error.msg}`;
    return (
      <div className="empty-state">
        <div className="warn">⚠ {msg}</div>
        <div style={{ marginTop: 8 }}>週次 {week}</div>
      </div>
    );
  }

  return (
    <div className="cand-layout">
      <div className="cand-main">
        <div className="toolbar">
          <input
            type="text"
            placeholder="搜尋 股號/名稱…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {STRATEGIES.map((s) => (
            <button
              key={s}
              className={`chip ${activeStrats.has(s) ? "on" : ""}`}
              onClick={() => toggleStrat(s)}
            >
              {s}
            </button>
          ))}
          <button
            className={`chip ${cheapOnly ? "on" : ""}`}
            onClick={() => setCheapOnly((v) => !v)}
          >
            相對便宜
          </button>
          <button
            className={`chip ${showNarrative ? "on" : ""}`}
            onClick={() => setShowNarrative((v) => !v)}
          >
            敘事 pick.md
          </button>
          <span className="count">
            {filtered.length} / {rows?.length ?? 0} 檔
          </span>
        </div>

        <div className="chart-grid">
          <div className="chart-card">
            <h3>動能 × 估值散佈</h3>
            <MomentumValueScatter rows={filtered} onPick={onPick} />
          </div>
          <div className="chart-card">
            <h3>估值 / 殖利分布</h3>
            <ValuationDist rows={filtered} />
          </div>
        </div>
        <div className="chart-card chart-card--wide">
          <h3>法人買賣超</h3>
          <InstFlowBar rows={filtered} />
        </div>

        <DataTable columns={columns} data={filtered} />
      </div>

      {showNarrative && (
        <NarrativePanel
          week={week}
          name="pick"
          title="本週進場敘事 pick.md"
          onClose={() => setShowNarrative(false)}
        />
      )}
    </div>
  );
}
