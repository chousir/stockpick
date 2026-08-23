.PHONY: help init sync test test-unit lint typecheck fmt clean clean-cache deep-clean \
        fetch-twse fetch-stock fetch-tdcc fetch-candidates-history fetch-institutional-history fetch-margin-history build-themes screen screen-all screen-dry doctor \
        group report week weekend backtest-strategies diagnose pick-outcome rotation-calib rotation backfill-universe-history \
        l6-g4-watch g1-g2-g5-watch \
        backfill-daily-history backfill-institutional-history \
        build-panel regime-history factor-lab pick-outcome-brief rotation-efficacy laggard-grid contrarian-efficacy flow-inflection margin-factors \
        audit-concepts cp-value-calib cp-value-candidates cp-value-valuation \
        dash-install dash-dev dash-build dash dash-test week-check snapshot-week

.DEFAULT_GOAL := help

# ─── 主要（每週真的在用的三個；其餘為進階，help 不列）────────────────────────

help:  ## 列主要指令（裸打 make 即顯示）
	@echo "主要指令："
	@echo "  make week GROUP=defg   完整週流程：抓資料→篩選→輪動→族群分析（主入口）"
	@echo "  make pick-outcome      pick 閉環：歷週分層命中率×α＋偽陰性帳（每季）"
	@echo "  make dash-dev          戰情室 dashboard（首次先 make dash-install）"
	@echo ""
	@echo "進階指令：見 Makefile 各進階區段或 README「指令總覽」"

week:  ## 完整週流程（GROUP=defg 主流程）：fetch-twse → fetch-institutional-history → fetch-tdcc → doctor → screen-all → fetch-candidates-history → rotation → macro → cp-value-candidates → group → l6-g4-watch → g1-g2-g5-watch → snapshot-week → week-check → pick-outcome-brief
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
	-$(MAKE) macro   # docs/25 v2 總經燈號（BAA10Y 主訊號＋揭露面板）；FRED 掛了不擋主流程
	-$(MAKE) cp-value-candidates   # 個股 CP 補漲候選＋三重濾網（group 的 7. 分析請求要讀 cp_candidates.md；失敗不擋）
	$(MAKE) group   # 官方族群前5前瞻累積軌（docs/31 §13）已內含在這步，不必另跑
	-$(MAKE) l6-g4-watch   # docs/31 §9 前瞻累積軌：記錄本週快照，不做裁決（失敗不擋主流程）
	-$(MAKE) g1-g2-g5-watch   # docs/31 §11 前瞻累積軌：記錄本週快照，不做裁決（失敗不擋主流程）
	-$(MAKE) snapshot-week   # WS-J.1 point-in-time 快照：凍結本週 concepts/watchlist/holdings/宇宙成員（失敗不擋主流程）
	$(MAKE) week-check   # 尾段產物完整性檢查（規劃書 05 F4）：上面容錯步驟若無聲失敗，這裡點名
	-$(MAKE) pick-outcome-brief   # WS-A3 上週 picks r+5 回饋一頁（輸入包；失敗不擋主流程）

pick-outcome-brief:  ## WS-A3 上週 picks r+5/α/勝率＋偽陰性一頁（寫進最新週報目錄；week 末段自動跑）
	uv run tw-screener picks outcome --brief

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

macro:  ## docs/25 v2 總經燈號：抓 FRED（快取24h）→算 BAA10Y 主訊號+揭露面板→append history.parquet
	uv run tw-screener market macro

cp-value-candidates:  ## B3 個股 CP 候選清單（生產軌，產 reports/週次/cp_candidates.md+csv；docs/13）
	uv run tw-screener cp candidates

group:  ## 跑族群分析，產出 group_analysis.md（docs/31 §13 官方族群前5前瞻累積軌已內含）
	uv run tw-screener analysis group

l6-g4-watch:  ## docs/31 §9 L6/G4 前瞻累積軌：記錄本週快照（PE/月營收YoY/投信買超），不做裁決
	uv run tw-screener backtest l6-g4-watch

g1-g2-g5-watch:  ## docs/31 §11 G1/G2/G5 前瞻累積軌：記錄本週快照，不做裁決
	uv run tw-screener backtest g1-g2-g5-watch

week-check:  ## 產物完整性檢查：本週機器產物＋歷週 pick 底帳，缺者 WARNING（規劃書 05 F4；不擋流程）
	uv run tw-screener report check

snapshot-week:  ## WS-J.1 point-in-time 快照：凍結本週 concepts/watchlist/holdings/次產業宇宙成員到 data/snapshots/<週次>/
	uv run tw-screener report snapshot

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

backfill-universe-history:  ## ⏳ 一次性回補全部次產業成員日線（上市+上櫃，~1500 檔×13 月，8-12 小時，可中斷續跑；START=2022-01-01 可指定起始月覆蓋 MONTHS）
	uv run tw-screener data backfill-universe-history $(if $(START),--start $(START),--months $(or $(MONTHS),13))

backfill-daily-history:  ## ⏳ 一次性逐日回補全市場歷史日線（MI_INDEX bulk，一天一請求、含下市股；START=2022-01-01 END=2026-07-11 必填）
	uv run tw-screener data backfill-daily-history --start $(START) --end $(END)

backfill-institutional-history:  ## ⏳ 一次性逐日回補上市法人歷史（T86，一天一請求；顯式 START/END 不依賴錨點，供面板法人冷啟動；START=2022-01-01 END=2025-06-05 必填）
	uv run tw-screener data backfill-institutional-history --start $(START) --end $(END)

fetch-margin-history:  ## 回補近 N 個交易日上市融資融券（DAYS=20 可調，舊版 MI_MARGN 可回查歷史）
	uv run tw-screener data fetch-margin-history --days $(or $(DAYS),20)

# ─── 進階：研究軌／季度校準 ──────────────────────────────────────────────────

build-panel:  ## WS-A2 ground-truth 面板：date×stock_id 前瞻報酬/位階/法人/量比 parquet（產 research/panel/）
	uv run tw-screener backtest build-panel

regime-history:  ## WS-H.3 regime 標籤歷史化：V2 引擎逐日 as-of 重算（產 research/panel/regime_labels.parquet）
	uv run tw-screener backtest regime-history

factor-lab:  ## WS-B 因子實驗台驗收：機器等價＋docs/19 基準對表＋面板首驗（產 research/factor_lab/）
	uv run tw-screener backtest factor-lab

rotation-efficacy:  ## WS-C 輪動欄效度：歷史重建→生產對表→forward basket IC/lift（產 research/rotation_efficacy/）
	uv run tw-screener backtest rotation-efficacy

laggard-grid:  ## WS-D 族群內強弱：2×2×位階 forward 報酬格（產 research/laggard_grid/）
	uv run tw-screener backtest laggard-grid

contrarian-efficacy:  ## M-BR1 Phase 2 底部左側聯合桶（轉買×貼近低）forward alpha 檢驗＋§1 硬門檻裁決（產 research/contrarian_efficacy/）
	uv run tw-screener backtest contrarian-efficacy

macro-regime-validate:  ## M-Macro2 as-of 回放驗證＋門檻敏感度＋DEXJPUS tail-event 重測（需先跑過三輪研究，產 research/macro_regime_screening/round4）
	uv run tw-screener backtest macro-regime-validate

macro-regime-resonance:  ## M-Macro3 燈號vs V2 regime共振/背離讀法驗證（需先跑過 make regime-history，產 research/macro_regime_screening/round5）
	uv run tw-screener backtest macro-regime-resonance

flow-inflection:  ## WS-E 資金流 inflection 因子族首驗（產 research/flow_inflection/）
	uv run tw-screener backtest flow-inflection

margin-factors:  ## WS-K 籌碼因子首驗：融資減肥/大戶WoW/融資量比（docs/23 §3 預註冊，產 research/margin_factors/）
	uv run tw-screener backtest margin-factors

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
