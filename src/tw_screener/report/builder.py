"""個股報告產生器：資料 → prompt → Claude API → Markdown。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from loguru import logger

from tw_screener.report.data_fetcher import fetch_stock_bundle

_PROMPT_DIR = Path(__file__).parent / "prompts"

_SYSTEM_PROMPT = """\
你是台股波段分析助理。根據使用者提供的資料，產出個股分析報告。

核心規則：
1. 所有數字來自提供的資料，沒有的資料直接標「未取得」，不要編造
2. 多方論點 3-5 點，空方論點 3-5 點，空方不可少於多方
3. 禁止使用：目標價、強烈建議、飆股、保證、絕對
4. 進場條件需給具體價位或技術訊號
5. 最後一段必須是「資料來源」列出各資料取得時間
6. 繁體中文，台股術語
"""


def _render_prompt(bundle: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)), autoescape=False)
    template = env.get_template("stock_report.md.j2")
    return template.render(**bundle)


def _call_claude(prompt: str, api_key: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return str(message.content[0].text)


def _build_data_draft(bundle: dict) -> str:
    """
    Fallback: produce data-only draft for manual review with Claude.
    使用者會把整份檔案貼到 Claude 對話視窗，再請它補上分析段落。
    """
    sid = bundle["stock_id"]
    name = bundle["name"] or sid
    goodinfo_url = bundle["goodinfo_url"]
    return "\n".join([
        f"# {sid} {name} 個股分析報告（資料草稿）",
        "",
        f"> 分析日期：{bundle['fetched_at']}",
        f"> 入選策略：{bundle['group_info'].get('strategy_str', '未知')}",
        f"> 產業：{bundle['industry_name'] or '未分類'}",
        f"> Goodinfo: {goodinfo_url}",
        "",
        "---",
        "",
        "## 給 Claude 的指示（請貼整份檔案到 Claude 後使用以下提示）",
        "",
        "請依照下方資料 + Goodinfo 連結補完此份報告。具體要求：",
        "",
        f"1. **打開 Goodinfo 連結**：{goodinfo_url}",
        "   - 經營績效頁：EPS、毛利率、營業利益率、ROE 近 4 季趨勢",
        "   - 本益比河流圖：當前 PE 相對歷史區間位置",
        "   - 股利政策：連續配息年數、現金股利歷史、近 5 年殖利率",
        "   - 籌碼/董監：董監持股比、大股東名單（千張大戶比已由下方 TDCC 資料提供，直接引用）",
        "2. **用下方數據** + Goodinfo 上看到的內容，補完所有 `<!-- TODO -->` 段落",
        "3. 多方論點 3-5 點，空方論點 3-5 點，**空方不可少於多方**",
        "4. 進場條件給具體價位（從技術面數據推算）",
        "5. 禁用「目標價」「強烈建議」「飆股」「保證」「絕對」字眼",
        "",
        "---",
        "",
        "## 技術面資料（近 60 日 OHLCV）",
        "",
        "```",
        bundle["price_summary"],
        "```",
        "",
        "## 基本面資料（近 12 月營收，其餘看 Goodinfo）",
        "",
        "```",
        bundle["revenue_summary"],
        "```",
        "",
        "## 籌碼面資料（近 20 日三大法人）",
        "",
        "```",
        bundle["institutional_summary"],
        "```",
        "",
        "## 集保大戶持股比（TDCC，每週）",
        "",
        "```",
        bundle["big_holder_summary"],
        "```",
        "",
        "## 融資融券（上市 MI_MARGN，每日）",
        "",
        "```",
        bundle["margin_summary"],
        "```",
        "",
        "---",
        "",
        "## 多方論點",
        "<!-- TODO: Claude 補寫，3-5 點 -->",
        "",
        "## 空方論點 / 風險",
        "<!-- TODO: Claude 補寫，3-5 點，不可少於多方 -->",
        "",
        "## 進場條件",
        "<!-- TODO: Claude 補寫 — 觸發訊號 / 參考價位 / 停損設定 -->",
        "",
        "## 不適合進場的情境",
        "<!-- TODO: Claude 補寫，2-3 點 -->",
        "",
        "## 族群與相對位置",
        "<!-- TODO: Claude 補寫，1-2 行 -->",
        "",
        "## 資料來源",
        f"- 證交所日線（{bundle['fetched_at']} 取，含近 60 日 OHLCV）",
        f"- 證交所月營收（{bundle['fetched_at']} 取）",
        f"- 三大法人 T86（{bundle['fetched_at']} 取）",
        "- 證交所融資融券 MI_MARGN（每日；上市，單位張）",
        "- TDCC 集保戶股權分散表（每週；大戶持股比，占集保庫存≈流通量）",
        f"- Goodinfo 個股頁（請填寫實際查看日期，連結：{goodinfo_url}）",
    ])


def build_stock_report(
    stock_id: str,
    settings_path: Path,
    week_tag: str | None = None,
    api_key: str | None = None,
) -> Path:
    """Fetch data, call Claude API (or produce draft), write report to file."""
    with open(settings_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    if week_tag is None:
        from tw_screener.screener.runner import derive_week_tag
        week_tag = derive_week_tag(settings_path)

    if api_key is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")

    bundle = fetch_stock_bundle(stock_id, settings_path)

    if api_key:
        logger.info("呼叫 Claude API 產出 {} 報告", stock_id)
        prompt = _render_prompt(bundle)
        content = _call_claude(prompt, api_key)
    else:
        logger.warning("未設定 ANTHROPIC_API_KEY，產出資料草稿（{}）", stock_id)
        content = _build_data_draft(bundle)

    report_dir = Path(cfg["paths"]["reports_dir"]) / week_tag / "stocks"
    report_dir.mkdir(parents=True, exist_ok=True)

    safe_name = (bundle["name"] or stock_id).replace("*", "").replace("/", "-").strip()
    filename = f"{stock_id}_{safe_name}.md"
    output_path = report_dir / filename
    output_path.write_text(content, encoding="utf-8")
    logger.info("報告輸出 → {}", output_path)

    return output_path
