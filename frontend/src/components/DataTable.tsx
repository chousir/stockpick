// 可排序資料表（TanStack Table）。篩選由各頁在傳入前處理。
// 選配：initialSorting 設預設排序；renderExpanded 開啟點列展開（不傳則行為不變）。

import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { Fragment, type ReactNode, useState } from "react";

interface Props<T> {
  columns: ColumnDef<T, unknown>[];
  data: T[];
  initialSorting?: SortingState;
  renderExpanded?: (row: T) => ReactNode;
}

export function DataTable<T>({ columns, data, initialSorting, renderExpanded }: Props<T>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting ?? []);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const expandable = Boolean(renderExpanded);
  const cols: ColumnDef<T, unknown>[] = expandable
    ? [
        {
          id: "__expand",
          header: "",
          enableSorting: false,
          cell: ({ row }) => (
            <span className="exp-caret">{expanded.has(row.id) ? "▾" : "▸"}</span>
          ),
        },
        ...columns,
      ]
    : columns;

  const table = useReactTable({
    data,
    columns: cols,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const isText = (meta: unknown) => Boolean((meta as { text?: boolean })?.text);
  const span = table.getVisibleLeafColumns().length;

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="table-wrap">
      <table className="hud">
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => {
                const sorted = h.column.getIsSorted();
                return (
                  <th
                    key={h.id}
                    className={isText(h.column.columnDef.meta) ? "text" : ""}
                    onClick={h.column.getToggleSortingHandler()}
                    title="點擊排序"
                  >
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    <span className="sort">
                      {sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : ""}
                    </span>
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((r) => (
            <Fragment key={r.id}>
              <tr
                className={expandable ? "row-expandable" : ""}
                onClick={expandable ? () => toggle(r.id) : undefined}
              >
                {r.getVisibleCells().map((c) => (
                  <td
                    key={c.id}
                    className={isText(c.column.columnDef.meta) ? "text" : ""}
                  >
                    {flexRender(c.column.columnDef.cell, c.getContext())}
                  </td>
                ))}
              </tr>
              {expandable && expanded.has(r.id) && (
                <tr className="sector-members-row">
                  <td className="text" colSpan={span}>
                    {renderExpanded!(r.original)}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
