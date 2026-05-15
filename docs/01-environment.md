# 01 — 環境設定

## 必要工具

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | ≥ 3.11 | 主要開發語言 |
| uv | latest | Python 套件 / 虛擬環境管理 |
| Node.js | ≥ 18 | Claude Code 安裝（或用原生安裝器免 Node） |
| Claude Code | latest | AI pair programmer |
| Git | ≥ 2.30 | 版控 |
| Make | any | 指令統一入口 |

## 主要環境：本地

理由：Goodinfo 對非台灣 IP 的爬蟲較敏感，本地用家用網路 IP 成功率高。

### macOS 安裝

```bash
# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Claude Code（兩種任選）
curl -fsSL https://claude.ai/install.sh | bash      # 原生，零依賴
# 或
npm install -g @anthropic-ai/claude-code            # 需 Node.js 18+

# 專案啟動
git clone <your-repo>
cd tw-stock-screener
uv sync                    # 裝 Python 依賴
make init                  # 初始化 data/、reports/ 等資料夾
```

### Linux / WSL2 安裝

同上。

### Windows 原生

不建議。請用 WSL2。Claude Code 在 Windows 原生需要 Git for Windows 才能跑 Bash。

## 備援環境：Codespaces

當你出差、換機、本機壞掉時用。

`.devcontainer/devcontainer.json` 會自動裝好 Python、uv、Claude Code CLI。

**Codespaces 限制（必須知道）**：
- 不要在 Codespaces 跑 Goodinfo 爬蟲，雲端 IP 容易被擋。
- 若要在 Codespaces 工作，先把 `data/cache/` 透過 git LFS 或 rsync 從本機同步過去。
- Codespaces 適合：寫程式、跑測試、產報告、用 Claude Code 改 code。

## API Keys / 環境變數

`.env`（gitignored）：

```bash
# Anthropic API（Claude Code 用）
ANTHROPIC_API_KEY=sk-ant-xxx

# 若之後用 FinMind 補資料
FINMIND_TOKEN=

# 證交所 OpenAPI 不需 token
```

## Makefile 指令（完整清單見 docs/07-cli-spec.md）

關鍵幾個：
```bash
make init              # 初始化資料夾
make sync              # uv sync 裝依賴
make test              # 跑測試
make screen            # 跑三組策略選股
make group             # 族群分析
make week              # 完整週流程（screen + group）
make clean-cache       # 清掉超過 7 天的快取
```

## Python 依賴清單（pyproject.toml）

```toml
[project]
name = "tw-stock-screener"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "polars>=1.0",
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "pyyaml>=6.0",
    "pydantic>=2.0",
    "typer>=0.12",
    "rich>=13.0",
    "loguru>=0.7",
    "jinja2>=3.1",
    "tenacity>=8.0",
    "pyarrow>=15.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "ruff>=0.5",
    "mypy>=1.10",
]
```

## devcontainer.json 骨架

```json
{
  "name": "tw-stock-screener",
  "image": "mcr.microsoft.com/devcontainers/python:3.11",
  "features": {
    "ghcr.io/devcontainers/features/node:1": { "version": "20" }
  },
  "postCreateCommand": "curl -LsSf https://astral.sh/uv/install.sh | sh && uv sync && npm install -g @anthropic-ai/claude-code",
  "customizations": {
    "vscode": {
      "extensions": [
        "ms-python.python",
        "charliermarsh.ruff",
        "redhat.vscode-yaml"
      ]
    }
  }
}
```

## 為什麼不用 Docker（production）

評估過：對本專案是過度工程。
- 不部署服務、不需要環境隔離（uv venv 就夠）
- 增加複雜度卻沒對應收益
- devcontainer 已能提供「一鍵環境」

如果未來要加 Web Dashboard 或多人用，再評估。
