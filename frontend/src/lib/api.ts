// fetch wrapper ＋ 型別。dev 走 Vite proxy /api → :8000；prod 同源。

export interface WeeksResponse {
  weeks: string[];
  latest: string | null;
}

// enriched 列：欄位跨週演進，值可能為 null（缺欄）。
export type Row = Record<string, string | number | null>;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // 非 JSON 回應，沿用 statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export interface NarrativeResponse {
  name: string;
  markdown: string;
}

// 個股彙整（docs/17 §4/§5.4）。sectors：null＝本週無 sector_rotation；[]＝有檔但無對應次產業。
export interface StockDetailResponse {
  stock_id: string;
  week: string;
  sources: string[];
  screens: string[];
  row: Row;
  sectors: Row[] | null;
  goodinfo_url: string | null;
}

export const getWeeks = () => getJSON<WeeksResponse>("/api/weeks");
export const getCandidates = (week: string) =>
  getJSON<Row[]>(`/api/weeks/${week}/candidates`);
export const getHoldings = (week: string) =>
  getJSON<Row[]>(`/api/weeks/${week}/holdings`);
export const getSectors = (week: string) =>
  getJSON<Row[]>(`/api/weeks/${week}/sectors`);
export const getThemes = (week: string) =>
  getJSON<Row[]>(`/api/weeks/${week}/themes`);
export const getNarrative = (week: string, name: string) =>
  getJSON<NarrativeResponse>(`/api/weeks/${week}/narrative/${name}`);
export const getStock = (week: string, stockId: string) =>
  getJSON<StockDetailResponse>(`/api/weeks/${week}/stock/${stockId}`);
