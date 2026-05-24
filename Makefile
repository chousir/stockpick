.PHONY: init sync test test-unit test-integration lint typecheck fmt clean clean-cache deep-clean \
        fetch-twse fetch-stock fetch-candidates-history fetch-institutional-history build-themes screen screen-all screen-dry \
        group leaders report report-batch week weekend backtest-strategies

# ─── 環境 ────────────────────────────────────────────────────────────────────

init:  ## 初始化資料夾結構（首次 clone 後跑）
	mkdir -p data/cache data/raw logs reports watchlist

sync:  ## uv sync 安裝/更新依賴
	uv sync

clean:  ## 清掉暫存（不刪 reports/）
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache

clean-cache:  ## 清掉 7 天以上的 cache
	find data/cache -type f -mtime +7 -delete 2>/dev/null || true
	find data/raw   -type f -mtime +7 -delete 2>/dev/null || true

deep-clean:  ## ⚠️ 清掉所有 data/ 和 cache（保留 reports/）
	rm -rf data/cache data/raw

# ─── 開發 ────────────────────────────────────────────────────────────────────

test:  ## 跑全部測試
	uv run pytest tests/ -v --tb=short

test-unit:  ## 只跑單元測試（離線、快）
	uv run pytest tests/ -v --tb=short -m "not integration"

test-integration:  ## 跑整合測試（會打網，慢）
	uv run pytest tests/ -v --tb=short -m integration

lint:  ## ruff check
	uv run ruff check src/ tests/

typecheck:  ## mypy
	uv run mypy src/

fmt:  ## ruff format
	uv run ruff format src/ tests/

# ─── 資料 ────────────────────────────────────────────────────────────────────

fetch-twse:  ## 增量抓 TWSE 日線、法人、月營收
	uv run tw-screener data fetch-twse

fetch-stock:  ## 抓單檔個股完整資料（STOCK_ID=2330）
	uv run tw-screener data fetch-stock $(STOCK_ID)

fetch-candidates-history:  ## 對本週篩選結果補抓 STOCK_DAY 歷史（MA20/60+斜率+動能，MONTHS=13 預設≈年線；首次 30-40 分鐘，過去月份永久快取）
	uv run tw-screener data fetch-candidates-history --months $(or $(MONTHS),13)

fetch-institutional-history:  ## 回補近 N 個交易日三大法人（DAYS=20 可調，族群法人強度用）
	uv run tw-screener data fetch-institutional-history --days $(or $(DAYS),20)

build-themes:  ## 爬 Yahoo 概念股 merge 進 config/concepts.yaml（DRY=1 只產 candidate 不覆蓋）
	uv run tw-screener data build-themes $(if $(DRY),--dry-run,)

# ─── 選股 ────────────────────────────────────────────────────────────────────

screen:  ## 跑單一策略（STRATEGY=a_breakout）
	uv run tw-screener screen run $(STRATEGY)

screen-all:  ## 跑指定組策略（GROUP=defg 主流程；亦可 def / abc legacy）
ifndef GROUP
	@echo "❌ 請指定 GROUP=defg（主流程）、def 或 abc（legacy）"
	@exit 1
endif
	uv run tw-screener screen run-all --group $(GROUP)

screen-dry:  ## 預演（不打網，只組 URL）（STRATEGY=a_breakout）
	uv run tw-screener screen run $(STRATEGY) --dry-run

# ─── 分析 ────────────────────────────────────────────────────────────────────

group:  ## 跑族群分析，產出 group_analysis.md
	uv run tw-screener analysis group

leaders:  ## 只跑領頭羊判斷
	uv run tw-screener analysis leaders

# ─── 報告 ────────────────────────────────────────────────────────────────────

report:  ## 產單檔個股報告（STOCK_ID=2330，首次跑該檔會花 5-10 秒補 3 個月歷史 OHLCV）
	uv run tw-screener report stock $(STOCK_ID)

report-batch:  ## 批次產本週推薦清單報告
	uv run tw-screener report batch

# ─── 完整流程 ─────────────────────────────────────────────────────────────────

week:  ## 完整週流程（GROUP=defg 主流程）：fetch-twse → screen-all → fetch-candidates-history → group
ifndef GROUP
	@echo "❌ 請指定 GROUP=defg（主流程）、def 或 abc（legacy）"
	@exit 1
endif
	$(MAKE) fetch-twse
	$(MAKE) screen-all GROUP=$(GROUP)
	$(MAKE) fetch-candidates-history
	$(MAKE) group

weekend:  ## 完整週流程並 commit 結果（GROUP=defg 主流程）
ifndef GROUP
	@echo "❌ 請指定 GROUP=defg（主流程）、def 或 abc（legacy）"
	@exit 1
endif
	$(MAKE) week GROUP=$(GROUP)
	git add reports/ watchlist/
	@if git diff --staged --quiet; then \
	  echo "無新檔可 commit，跳過 git commit/push"; \
	else \
	  git commit -m "Weekly analysis $$(date +%Y-W%V)" && git push; \
	fi

# ─── 回測（預留，三個月後實作）───────────────────────────────────────────────

backtest-strategies:  ## ⚠️ 回測三組策略勝率（需 3 個月以上歷史資料，功能尚未實作）
	@echo "backtest-strategies 尚未實作，需累積至少 3 個月的 reports/ 歷史資料（預計 2026-08 後）"
	@exit 1
