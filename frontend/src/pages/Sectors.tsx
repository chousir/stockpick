// 族群輪動頁（docs/17 §5.3）：族群資金熱力圖 ＋ 輪動表（成員展開）＋ 主題強度排行
// ＋ 敘事分頁（sector_rotation.md / group_analysis.md）。
// sector_rotation 僅近期週有（W24/W25）；缺資料週顯空狀態，不報錯（§2.4）。

import { type ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { SectorHeatmap } from "../charts/SectorHeatmap";
import { DataTable } from "../components/DataTable";
import { NarrativePanel } from "../components/NarrativePanel";
import { ApiError, getCandidates, getSectors, getThemes, type Row } from "../lib/api";
import { fmtNum, isBlank, signClass } from "../lib/format";

interface Props {
  week: string;
}

type Load<T> = { data: T | null; status: number | null; loading: boolean };

function useTable(week: string, fetcher: (w: string) => Promise<Row[]>): Load<Row[]> {
  const [state, setState] = useState<Load<Row[]>>({
    data: null,
    status: null,
    loading: true,
  });
  useEffect(() => {
    let cancelled = false;
    setState({ data: null, status: null, loading: true });
    fetcher(week)
      .then((data) => !cancelled && setState({ data, status: 200, loading: false }))
      .catch((e: unknown) => {
        if (cancelled) return;
        const status = e instanceof ApiError ? e.status : 0;
        setState({ data: null, status, loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [week, fetcher]);
  return state;
}

// 象限色語意（§6.2）
function quadClass(q: unknown): string {
  const s = String(q ?? "");
  if (s.includes("主升")) return "teal";
  if (s.includes("下一棒")) return "amber";
  if (s.includes("出貨") || s.includes("警訊")) return "alert";
  return "muted";
}

// rank_delta 箭頭：上升（正）紅、下降（負）綠、不變顯「持平」（§5.3）
function RankDelta({ v }: { v: unknown }) {
  if (isBlank(v)) return <span className="num-flat">—</span>;
  const n = Number(v);
  if (Number.isNaN(n)) return <span className="num-flat">—</span>;
  if (n === 0) return <span className="muted">持平</span>;
  return (
    <span className={n > 0 ? "num-up" : "num-down"}>
      {n > 0 ? "▲" : "▼"}
      {Math.abs(n)}
    </span>
  );
}

// 成員展開內容：本週入選候選股 chip（連 Goodinfo）。universe＝族群成員總數（reports/ 無逐檔清單）。
function MemberChips({ members, universe }: { members: Row[]; universe?: unknown }) {
  const total = isBlank(universe) ? null : Number(universe);
  return (
    <>
      <div className="member-note">
        本週入選候選股 <b>{members.length}</b> 檔
        {total !== null && total > members.length && (
          <>
            （族群成員共 {total} 檔，其餘未入選任何策略篩選、reports 無其價量資料）
          </>
        )}
      </div>
      {members.length === 0 ? (
        <span className="muted">本週候選股無此族群成員。</span>
      ) : (
        <div className="member-chips">
          {members.map((m) => {
            const url = m.goodinfo_url ? String(m.goodinfo_url) : null;
            const label = (
              <span>
                <b>{String(m.stock_id ?? "")}</b> {String(m.name ?? "")}
                <span className={signClass(m.momentum_5d_pct)}>
                  {" "}
                  {fmtNum(m.momentum_5d_pct, 1)}%
                </span>
              </span>
            );
            return url ? (
              <a
                key={String(m.stock_id)}
                className="member-chip"
                href={url}
                target="_blank"
                rel="noreferrer"
                title="Goodinfo"
                onClick={(e) => e.stopPropagation()}
              >
                {label}
              </a>
            ) : (
              <span key={String(m.stock_id)} className="member-chip">
                {label}
              </span>
            );
          })}
        </div>
      )}
    </>
  );
}

// theme_strength 表欄（lead_score 預設排序、rank_delta 箭頭，§5.3）
function themeColumns(): ColumnDef<Row, unknown>[] {
  const num = (key: string, header: string, digits: number): ColumnDef<Row, unknown> => ({
    id: key,
    accessorFn: (r) => r[key],
    header,
    sortingFn: (a, b, id) => {
      const x = isBlank(a.getValue(id)) ? -Infinity : Number(a.getValue(id));
      const y = isBlank(b.getValue(id)) ? -Infinity : Number(b.getValue(id));
      return x - y;
    },
    cell: (info) => fmtNum(info.getValue(), digits),
  });
  return [
    { id: "radar_rank", accessorFn: (r) => r.radar_rank, header: "排名", cell: (i) => fmtNum(i.getValue(), 0) },
    {
      id: "theme",
      accessorFn: (r) => r.theme,
      header: "主題/次產業",
      meta: { text: true },
      cell: (i) => (isBlank(i.getValue()) ? "—" : String(i.getValue())),
    },
    {
      id: "kind",
      accessorFn: (r) => r.kind,
      header: "類別",
      meta: { text: true },
      enableSorting: false,
      cell: (i) => (isBlank(i.getValue()) ? "—" : String(i.getValue())),
    },
    num("lead_score", "領先分", 1),
    num("score", "強度分", 1),
    {
      ...num("momentum_5d", "動能5d%", 1),
      cell: (i) => <span className={signClass(i.getValue())}>{fmtNum(i.getValue(), 1)}</span>,
    },
    num("members_count", "成員數", 0),
    num("foreign_score", "外資分", 2),
    num("vol_surge_score", "爆量分", 2),
    {
      id: "rank_delta",
      accessorFn: (r) => r.rank_delta,
      header: "排名Δ",
      sortingFn: (a, b, id) => Number(a.getValue(id) ?? 0) - Number(b.getValue(id) ?? 0),
      cell: (i) => <RankDelta v={i.getValue()} />,
    },
  ];
}

// 次產業成員：本週候選股中、theme 標籤含此 sub_industry 者（reports/ 內推導，§5.3）
function buildMemberMap(candidates: Row[] | null): Map<string, Row[]> {
  const map = new Map<string, Row[]>();
  if (!candidates) return map;
  for (const c of candidates) {
    const tags = String(c.theme ?? "")
      .split("、")
      .map((t) => t.trim())
      .filter(Boolean);
    for (const tag of tags) {
      const arr = map.get(tag) ?? [];
      arr.push(c);
      map.set(tag, arr);
    }
  }
  return map;
}

const SECTOR_Z_COLS: { key: string; label: string }[] = [
  { key: "net_flow_5d_z", label: "5d資金z" },
  { key: "net_flow_20d_z", label: "20d資金z" },
  { key: "foreign_flow_20d_z", label: "外資20dz" },
  { key: "trust_flow_20d_z", label: "投信20dz" },
];

export function Sectors({ week }: Props) {
  const sectors = useTable(week, getSectors);
  const themes = useTable(week, getThemes);
  const candidates = useTable(week, getCandidates);

  const [narrative, setNarrative] = useState<"sector_rotation" | "group_analysis" | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // 換週時收合展開列
  useEffect(() => setExpanded(new Set()), [week]);

  const memberMap = useMemo(() => buildMemberMap(candidates.data), [candidates.data]);
  const themeCols = useMemo(() => themeColumns(), []);

  const orderedSectors = useMemo(
    () =>
      [...(sectors.data ?? [])].sort(
        (a, b) => Number(a.radar_rank ?? 1e9) - Number(b.radar_rank ?? 1e9),
      ),
    [sectors.data],
  );

  function toggle(sub: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(sub)) next.delete(sub);
      else next.add(sub);
      return next;
    });
  }

  return (
    <div className="cand-layout">
      <div className="cand-main">
        <div className="toolbar">
          <button
            className={`chip ${narrative === "sector_rotation" ? "on" : ""}`}
            onClick={() =>
              setNarrative((n) => (n === "sector_rotation" ? null : "sector_rotation"))
            }
          >
            輪動敘事
          </button>
          <button
            className={`chip ${narrative === "group_analysis" ? "on" : ""}`}
            onClick={() =>
              setNarrative((n) => (n === "group_analysis" ? null : "group_analysis"))
            }
          >
            族群分析
          </button>
        </div>

        <div className="chart-card chart-card--wide">
          <h3>族群資金熱力圖</h3>
          {sectors.loading ? (
            <div className="placeholder">載入中…</div>
          ) : sectors.status === 200 ? (
            <SectorHeatmap rows={sectors.data ?? []} />
          ) : (
            <div className="empty-state">本週無族群輪動資料（sector_rotation）</div>
          )}
        </div>

        {sectors.status === 200 && orderedSectors.length > 0 && (
          <div className="chart-card chart-card--wide">
            <h3>族群資金輪動（點次產業展開本週候選成員）</h3>
            <div className="chart-cap">
              依資金雷達<b>排名</b>列出；<b>資金轉向</b>＝近 5 日由賣轉買/買轉賣的前哨（多數族群無、顯
              「—」屬正常）；<b>排名Δ</b>＝對上週名次變化（持平＝沒變）；
              <b>成員（候選/總）</b>＝本週入選候選股數／族群成員總數（reports 只有總數、無逐檔清單）。
            </div>
            <div className="table-wrap">
              <table className="hud">
                <thead>
                  <tr>
                    <th>排名</th>
                    <th className="text">次產業</th>
                    <th className="text">象限</th>
                    <th className="text">資金轉向</th>
                    <th className="text">觸發</th>
                    {SECTOR_Z_COLS.map((c) => (
                      <th key={c.key}>{c.label}</th>
                    ))}
                    <th>籃報酬5d%</th>
                    <th>CP</th>
                    <th>排名Δ</th>
                    <th>成員(候選/總)</th>
                  </tr>
                </thead>
                <tbody>
                  {orderedSectors.map((r) => {
                    const sub = String(r.sub_industry ?? "");
                    const members = memberMap.get(sub) ?? [];
                    const open = expanded.has(sub);
                    return (
                      <SectorRows
                        key={sub || String(r.radar_rank)}
                        row={r}
                        sub={sub}
                        members={members}
                        open={open}
                        onToggle={() => toggle(sub)}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="chart-card chart-card--wide">
          <h3>主題 / 次產業強度排行</h3>
          <div className="chart-cap">
            預設依 <b>排名（radar_rank）</b>排序，排名＝<b>領先分 lead_score 由高到低</b>
            （綜合動能、外資分、爆量分的族群領先強度）；點任一表頭可改排序。
            點列展開本週入選之候選成員。
          </div>
          {themes.loading ? (
            <div className="placeholder">載入中…</div>
          ) : themes.status === 200 && (themes.data?.length ?? 0) > 0 ? (
            <DataTable
              columns={themeCols}
              data={themes.data ?? []}
              initialSorting={[{ id: "radar_rank", desc: false }]}
              renderExpanded={(r) => (
                <MemberChips
                  members={memberMap.get(String(r.theme ?? "")) ?? []}
                  universe={r.members_count}
                />
              )}
            />
          ) : (
            <div className="empty-state">本週無主題強度資料（theme_strength）</div>
          )}
        </div>
      </div>

      {narrative && (
        <NarrativePanel
          week={week}
          name={narrative}
          title={narrative === "sector_rotation" ? "族群輪動敘事" : "族群分析 group_analysis"}
          onClose={() => setNarrative(null)}
        />
      )}
    </div>
  );
}

// 一個次產業 ＝ 主列 ＋（展開時）成員列
function SectorRows({
  row,
  sub,
  members,
  open,
  onToggle,
}: {
  row: Row;
  sub: string;
  members: Row[];
  open: boolean;
  onToggle: () => void;
}) {
  const entry = row.entry_triggered ? <span className="flag amber">⚑進場</span> : null;
  const confirm = row.confirm_triggered ? <span className="flag teal">✓確認</span> : null;
  const turn = row.flow_turn ? (
    <span className={`flag ${String(row.flow_turn).includes("回流") ? "teal" : "alert"}`}>
      {String(row.flow_turn)}
    </span>
  ) : (
    <span className="num-flat">—</span>
  );
  const colSpan = 5 + SECTOR_Z_COLS.length + 4;

  return (
    <>
      <tr>
        <td>{fmtNum(row.radar_rank, 0)}</td>
        <td className="text">
          <button className="cell-link sector-toggle" onClick={onToggle}>
            {open ? "▾" : "▸"} {sub || "—"}
          </button>
        </td>
        <td className="text">
          <span className={`flag ${quadClass(row.quadrant)}`}>{String(row.quadrant ?? "—")}</span>
        </td>
        <td className="text">{turn}</td>
        <td className="text">
          {entry}
          {confirm}
          {!entry && !confirm && <span className="num-flat">—</span>}
        </td>
        {SECTOR_Z_COLS.map((c) => (
          <td key={c.key}>{fmtNum(row[c.key], 1)}</td>
        ))}
        <td>
          <span className={signClass(row.basket_ret_5d_pct)}>
            {fmtNum(row.basket_ret_5d_pct, 1)}
          </span>
        </td>
        <td>{fmtNum(row.cp_score, 1)}</td>
        <td>
          <RankDelta v={row.rank_delta} />
        </td>
        <td>
          <span className={members.length > 0 ? "cell-id" : "num-flat"}>
            {members.length}
          </span>
          <span className="muted"> / {fmtNum(row.members, 0)}</span>
        </td>
      </tr>
      {open && (
        <tr className="sector-members-row">
          <td className="text" colSpan={colSpan}>
            <MemberChips members={members} universe={row.members} />
          </td>
        </tr>
      )}
    </>
  );
}
