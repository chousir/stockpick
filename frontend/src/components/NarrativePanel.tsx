// 敘事側欄：react-markdown ＋ GFM 渲染 reports 的 md（docs/17 §5.2）。
// 缺檔（如 W21/W24 無 pick.md）→ 空狀態，不報錯。

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, getNarrative } from "../lib/api";

interface Props {
  week: string;
  name: string;
  title: string;
  onClose: () => void;
}

export function NarrativePanel({ week, name, title, onClose }: Props) {
  const [md, setMd] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    setMd(null);
    getNarrative(week, name)
      .then((r) => {
        if (!cancelled) setMd(r.markdown);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setErr(
          e instanceof ApiError && e.status === 404
            ? `本週無${title}`
            : `載入失敗：${String(e)}`,
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [week, name, title]);

  return (
    <aside className="narrative-panel hud-card">
      <div className="narrative-head">
        <h3>{title}</h3>
        <button className="chip" onClick={onClose} title="收合">
          ✕
        </button>
      </div>
      {loading ? (
        <div className="placeholder">載入中…</div>
      ) : err ? (
        <div className="empty-state">
          <span className="warn">⚠ {err}</span>
        </div>
      ) : (
        <div className="md">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{md ?? ""}</ReactMarkdown>
        </div>
      )}
    </aside>
  );
}
