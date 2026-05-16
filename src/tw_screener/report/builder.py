"""個股報告產生器：資料 → prompt → Claude API → Markdown。"""

from __future__ import annotations

import os
from datetime import date
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
    """Fallback: produce data-only draft without calling Claude API."""
    sid = bundle["stock_id"]
    name = bundle["name"] or sid
    return "\n".join([
        f"# {sid} {name} 個股分析報告",
        "",
        f"> 分析日期：{bundle['fetched_at']}",
        f"> 入選策略：{bundle['group_info'].get('strategy_str', '未知')}",
        f"> 產業：{bundle['industry_name'] or '未分類'}",
        f"> Goodinfo: {bundle['goodinfo_url']}",
        "",
        "<!-- ⚠️ 未設定 ANTHROPIC_API_KEY，以下為資料草稿，分析段落需人工補寫 -->",
        "",
        "## 技術面（近 60 日 OHLCV）",
        "",
        "```",
        bundle["price_summary"],
        "```",
        "",
        "## 基本面（近 12 月營收）",
        "",
        "```",
        bundle["revenue_summary"],
        "```",
        "",
        "## 籌碼面（近 20 日三大法人）",
        "",
        "```",
        bundle["institutional_summary"],
        "```",
        "",
        "## 多方論點",
        "<!-- TODO: Claude 補寫 -->",
        "",
        "## 空方論點 / 風險",
        "<!-- TODO: Claude 補寫 -->",
        "",
        "## 進場條件",
        "<!-- TODO: Claude 補寫 -->",
        "",
        "## 不適合進場的情境",
        "<!-- TODO: Claude 補寫 -->",
        "",
        "## 族群與相對位置",
        "<!-- TODO: Claude 補寫 -->",
        "",
        "## 資料來源",
        f"- 證交所日線快取（{bundle['fetched_at']} 取）",
        f"- 證交所月營收（{bundle['fetched_at']} 取）",
        f"- 三大法人（{bundle['fetched_at']} 取）",
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
        week_tag = date.today().strftime("%Y-W%V")

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
