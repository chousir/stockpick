.PHONY: help init sync test test-unit lint typecheck fmt clean clean-cache deep-clean \
        fetch-twse fetch-stock fetch-tdcc fetch-candidates-history fetch-institutional-history build-themes screen screen-all screen-dry doctor \
        group report week weekend backtest-strategies diagnose pick-outcome rotation-calib rotation backfill-universe-history \
        audit-concepts cp-value-calib cp-value-candidates cp-value-valuation \
        dash-install dash-dev dash-build dash dash-test week-check

.DEFAULT_GOAL := help

# ─── 主要（每週真的在用的三個；其餘為進階，help 不列）────────────────────────

help:  ## 列主要指令（裸打 make 即顯示）
	@echo "主要指令："
	@echo "  make week GROUP=defg   完整週流程：抓資料→篩選→輪動→族群分析（主入口）"
	@echo "  make pick-outcome      pick 閉環：歷週分層命中率×α＋偽陰性帳（每季）"
	@echo "  make dash-dev          戰情室 dashboard（首次先 make dash-install）"
	@echo ""
	@echo "進階指令：見 Makefile 各進階區段或 README「指令總覽」"

week:  ## 完整週流程（GROUP=defg 主流程）：fetch-twse → fetch-institutional-history → fetch-tdcc → doctor → screen-all → fetch-candidates-history → rotation → cp-value-candidates → group → week-check
ifndef GROUP
	@echo "❌ 請指定 GROUP=defg（現行唯一主流程；abc/def 已退役）"
	@exit 1
endif
	$(MAKE) fetch-twse
	$(MAKE) fetch-institutional-history   # 回補近 20 日上市+上櫃法人；隔幾天沒跑也自動補齊
	-$(MAKE) fetch-tdcc   # 集保大戶持股比（規劃書 02 D3）；TDCC 異常不擋主流程，大戶欄退化為 null
	$(MAKE) doctor   # Goodinfo 健康檢查（規劃書 02 D1）：被擋/改版就早停，不讓 screen-all 白跑
	$(MAKE) screen-all GROUP=$(GROUP)
	$(MAKE) fetch-candidates-history
	-$(MAKE) rotation   # 先跑輪動（group 的 2.8 雷達要讀 sector_rotation.csv 並列；失敗不擋主流程）
	-$(MAKE) cp-value-candidates   # 個股 CP 補漲候選＋三重濾網（group 的 7. 分析請求要讀 cp_candidates.md；失敗不擋）
	$(MAKE) group
	$(MAKE) week-check   # 尾段產物完整性檢查（規劃書 05 F4）：上面容錯步驟若無聲失敗，這裡點名

pick-outcome:  ## pick 閉環：分層命中率×α（vs 大盤＋vs 族群）＋偽陰性帳（規劃書 05 F1，產 research/pick_outcome/）
	uv run tw-screener picks outcome

dash-dev:  ## 併發起 FastAPI(:8000) ＋ Vite(:5173, proxy /api)；Ctrl-C 同時關閉
	@trap 'kill 0' EXIT INT TERM; \
	  uv run tw-screener serve --reload & \
	  ( cd frontend && npm run dev ) & \
	  wait

# ─── 進階：week 內部相依（week 自動串起，通常不單獨跑）───────────────────────

fetch-twse:  ## 增量抓 TWSE 日線、法人、月營收
	uv run tw-screener data fetch-twse

fetch-institutional-history:  ## 回補近 N 個交易日三大法人（DAYS=20 可調，族群法人強度用）
	uv run tw-screener data fetch-institutional-history --days $(or $(DAYS),20)

fetch-tdcc:  ## 抓 TDCC 集保戶股權分散表（每週大戶持股比，規劃書 02 D3）
	uv run tw-screener data fetch-tdcc

doctor:  ## Goodinfo 健康檢查（被擋/改版→exit 1）；make week 在 screen-all 前先擋
	uv run tw-screener screen doctor

screen-all:  ## 跑指定組策略（GROUP=defg 現行唯一主流程；abc/def 已退役）
ifndef GROUP
	@echo "❌ 請指定 GROUP=defg（現行唯一主流程；abc/def 已退役）"
	@exit 1
endif
	uv run tw-screener screen run-all --group $(GROUP)

fetch-candidates-history:  ## 對本週篩選結果補抓 STOCK_DAY 歷史（MA20/60+斜率+動能，MONTHS=13 預設≈年線；首次 30-40 分鐘，過去月份永久快取）
	uv run tw-screener data fetch-candidates-history --months $(or $(MONTHS),13)

rotation:  ## 次產業資金流向輪動報表（產 reports/週次/sector_rotation.md+csv；docs/12）
	uv run tw-screener sector rotation

cp-value-candidates:  ## B3 個股 CP 候選清單（生產軌，產 reports/週次/cp_candidates.md+csv；docs/13）
	uv run tw-screener cp candidates

group:  ## 跑族群分析，產出 group_analysis.md
	uv run tw-screener analysis group

week-check:  ## 產物完整性檢查：本週機器產物＋歷週 pick 底帳，缺者 WARNING（規劃書 05 F4；不擋流程）
	uv run tw-screener report check

# ─── 進階：偶爾手動 ──────────────────────────────────────────────────────────

weekend:  ## 完整週流程並 commit 結果（GROUP=defg 主流程）
ifndef GROUP
	@echo "❌ 請指定 GROUP=defg（現行唯一主流程；abc/def 已退役）"
	@exit 1
endif
	$(MAKE) week GROUP=$(GROUP)
	git add reports/ watchlist/
	@if git diff --staged --quiet; then \
	  echo "無新檔可 commit，跳過 git commit/push"; \
	else \
	  git commit -m "Weekly analysis $$(date +%Y-W%V)" && git push; \
	fi

report:  ## 產單檔個股報告（STOCK_ID=2330，首次跑該檔會花 5-10 秒補 3 個月歷史 OHLCV）
	uv run tw-screener report stock $(STOCK_ID)

screen:  ## 跑單一策略（STRATEGY=d_quality_leader）
	uv run tw-screener screen run $(STRATEGY)

screen-dry:  ## 預演（不打網，只組 URL）（STRATEGY=d_quality_leader）
	uv run tw-screener screen run $(STRATEGY) --dry-run

fetch-stock:  ## 抓單檔個股完整資料（STOCK_ID=2330）
	uv run tw-screener data fetch-stock $(STOCK_ID)

build-themes:  ## 爬 Yahoo 概念股 merge 進 config/concepts.yaml（DRY=1 只產 candidate 不覆蓋）
	uv run tw-screener data build-themes $(if $(DRY),--dry-run,)

audit-concepts:  ## 清查 concepts.yaml 次產業無價成員（興櫃/下市/誤標；只報告不改檔）
	uv run tw-screener sector universe --audit

backfill-universe-history:  ## ⏳ 一次性回補全部次產業成員日線（上市+上櫃，~1500 檔×13 月，8-12 小時，可中斷續跑）
	uv run tw-screener data backfill-universe-history --months $(or $(MONTHS),13)

# ─── 進階：研究軌／季度校準 ──────────────────────────────────────────────────

backtest-strategies:  ## 回測 D/E/F/G 入選後勝率/報酬/回撤 vs 大盤（規劃書 03 V1，產 research/strategy_backtest/）
	uv run tw-screener backtest strategies

diagnose:  ## M-Diag1「抓太晚」診斷：進場延伸度曲線＋排序訊號 IC（研究軌，產 research/diagnostic/）
	uv run tw-screener backtest diagnose

rotation-calib:  ## R2 起漲點回測校準（研究軌，產 research/rotation/ 校準報告；docs/12）
	uv run tw-screener sector calibrate

cp-value-calib:  ## B2 個股版起漲事件回測（研究軌，產 research/cp_value/ 三 label 校準報告；docs/13）
	uv run tw-screener cp calibrate

cp-value-valuation:  ## C1 個股相對 PE 估值表（生產軌，產 reports/週次/cp_valuation.md+csv；docs/13 §C1）
	uv run tw-screener cp valuation

# ─── 進階：Dashboard（投資戰情室；docs/17）───────────────────────────────────

dash-install:  ## 安裝前後端依賴（uv sync ＋ npm install）
	uv sync
	cd frontend && npm install

dash-build:  ## 前端 build → frontend/dist
	cd frontend && npm run build

dash:  ## FastAPI 服務 frontend/dist ＋ /api（:8000）
	uv run tw-screener serve

dash-test:  ## 後端 webapp 路由 pytest（happy path）
	uv run pytest tests/webapp -v --tb=short

# ─── 進階：開發／環境 ────────────────────────────────────────────────────────

init:  ## 初始化資料夾結構（首次 clone 後跑）
	mkdir -p data/cache data/raw logs reports watchlist

sync:  ## uv sync 安裝/更新依賴
	uv sync

test:  ## 跑全部測試
	uv run pytest tests/ -v --tb=short

test-unit:  ## 只跑單元測試（離線、快）
	uv run pytest tests/ -v --tb=short -m "not integration"

lint:  ## ruff check
	uv run ruff check src/ tests/

typecheck:  ## mypy
	uv run mypy src/

fmt:  ## ruff format
	uv run ruff format src/ tests/

clean:  ## 清掉暫存（不刪 reports/）
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache

clean-cache:  ## 清掉 7 天以上的 cache
	find data/cache -type f -mtime +7 -delete 2>/dev/null || true
	find data/raw   -type f -mtime +7 -delete 2>/dev/null || true

deep-clean:  ## ⚠️ 清掉所有 data/ 和 cache（保留 reports/）
	rm -rf data/cache data/raw
