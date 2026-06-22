// 可排序資料表（TanStack Table）。篩選由各頁在傳入前處理。

import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useState } from "react";

interface Props<T> {
  columns: ColumnDef<T, unknown>[];
  data: T[];
}

export function DataTable<T>({ columns, data }: Props<T>) {
  const [sorting, setSorting] = useState<SortingState>([]);

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const isText = (meta: unknown) => Boolean((meta as { text?: boolean })?.text);

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
            <tr key={r.id}>
              {r.getVisibleCells().map((c) => (
                <td
                  key={c.id}
                  className={isText(c.column.columnDef.meta) ? "text" : ""}
                >
                  {flexRender(c.column.columnDef.cell, c.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
